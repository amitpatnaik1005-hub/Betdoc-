"""Automated result settlement (Group 113, points b.6 and b.12, FA-6). Developed for Amit Ashok Kumar Patnaik.

One results table, every ledger settles from it. ``fixture_scores`` already settles the Oracle's placed bets
(``user_pnl_tracker.settle_pending``) and is what the real-money desk's tickets settle from; Group 113 fills it from the
free results feeds and settles every ledger from it:

1. ``waiting_fixtures``: the fixtures bets are waiting on (an Oracle leg past its sport's length, a placed real-money
   ticket, an accepted exchange order) that have no score yet, each as its own ledger names it. An exchange order keeps
   an Odds API event id, resolved to its teams and kickoff through the price history (``odds_snapshots``).
2. ``finished_matches`` + ``record_results``: Football-Data.org and Sportmonks (neither metered against the Odds API)
   asked only for the days those fixtures were played, and each fixture paired with exactly one finished match by both
   sides' names and the kickoff (``app.domain.result_settlement.match_result``). A finished match gives the score after
   90 minutes (one decided in extra time or on penalties without its regular-time score is left to an admin); a cancelled
   match is ABANDONED (void); a postponed one is POSTPONED (void after the books' 48 hours). An admin's score is never
   overwritten; a final score is never replaced.
3. ``settle_real_tickets``: each placed ticket with a score and a market the grader can read settles through the desk's
   own ``settle_ticket`` (the bankroll, the cash ledger, the 30% TDS and its tax-ledger row, the stop-loss and cool-down).
   Nothing it cannot read is voided: it stays placed and is reported.
4. ``settle_exchange_orders``: each accepted order graded the same way, its payout by the Arena's own rule (P&L = payout
   - stake). TDS is recorded only for an order a real exchange accepted (an adapter registered in
   ``app.exchanges.factory``); today every exchange account routes to the mock exchange, so no order there moved money
   and none is taxed or credited anywhere. The real-money vault is never touched from this ledger: its stakes were never
   locked there.
5. The Oracle's bets settle through ``settle_pending``; then one ``settlement`` event tells every open browser to
   refetch the bankrolls, the open positions and the P&L.

Every amount is Decimal: Gross Payout = stake x odds; Net Winnings = max(0, Gross Payout - stake); TDS = 30% of Net
Winnings; Net Post-Tax Payout = Gross Payout - TDS (``app.domain.wagering.tax.section_115bbj``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.sports_data import SportsDataError
from app.adapters.sports_data.football_data import PROVIDER as FOOTBALL_DATA, FootballDataClient
from app.adapters.sports_data.sportsmonks import PROVIDER as SPORTMONKS, SportmonksClient
from app.core.config import Settings
from app.core.events import publish_event
from app.domain.arena.tracker import ArenaEngine
from app.domain.live_context import ProviderFixture
from app.domain.oracle.markets import LegResult
from app.domain.result_settlement import Waiting, canonical_leg, family, grade, match_result
from app.domain.wagering.tax import financial_year, money, section_115bbj
from app.exchanges.factory import _ADAPTERS as REAL_EXCHANGES
from app.models import BetLedger, ExchangeAccount
from app.models.odds import OddsSnapshot
from app.models.real_money import RealBetTicket, RealTicketStatus
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, ScoreStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.arena import SettlementRequest
from app.schemas.ashoka import ScoreIn
from app.services import real_money_service as rms
from app.services import user_pnl_tracker
from app.services.oracle_scores import finished_after

logger = logging.getLogger("betdoc.settlement")
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
ADMIN_SOURCE = "admin"
MAX_RANGE_DAYS = 9  # Football-Data.org's free tier answers at most ten days a call
_STATUS_FROM_FEED = {"CANCELLED": ScoreStatus.ABANDONED, "POSTPONED": ScoreStatus.POSTPONED}


class SettlementError(ValueError):
    """An admin result that cannot be recorded (unknown fixture without names, a final score without goals)."""


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _split_event(name: str) -> tuple[str, str] | None:
    for sep in (" vs ", " v ", " VS ", " Vs ", " - "):
        if sep in name:
            home, _, away = name.partition(sep)
            if home.strip() and away.strip():
                return home.strip(), away.strip()
    return None


# ================================================================ 1. what bets are waiting on
async def _scored(session: AsyncSession, ids: set[str]) -> set[str]:
    if not ids:
        return set()
    rows = await session.execute(select(FixtureScore.fixture_id, FixtureScore.status).where(FixtureScore.fixture_id.in_(ids)))
    return {fid for fid, status in rows.all() if status != ScoreStatus.POSTPONED.value}  # a postponed match may still get a result


async def waiting_fixtures(session: AsyncSession, settings: Settings, now: datetime) -> list[Waiting]:
    """Every fixture a pending bet waits on whose match should be over and that has no final (or abandoned) score yet."""
    since = now - timedelta(days=settings.SETTLEMENT_LOOKBACK_DAYS)
    out: dict[str, Waiting] = {}

    legs = await session.execute(
        select(UserPlacedLeg.fixture_id, UserPlacedLeg.sport_key, UserPlacedLeg.home, UserPlacedLeg.away, UserPlacedLeg.kickoff)
        .join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id)
        .where(UserPlacedBet.status == PlacedStatus.PENDING.value, UserPlacedLeg.result == PlacedStatus.PENDING.value,
               UserPlacedLeg.kickoff.is_not(None), UserPlacedLeg.kickoff >= since)
    )
    for fid, sport_key, home, away, kickoff in legs.all():
        kickoff = _aware(kickoff)
        if kickoff is not None and kickoff + finished_after(sport_key or "") <= now:
            out.setdefault(fid, Waiting(fid, family(sport_key), home, away, kickoff))

    tickets = await session.execute(
        select(RealBetTicket.match_id, RealBetTicket.sport, RealBetTicket.event_name, RealBetTicket.placed_at, RealBetTicket.created_at)
        .where(RealBetTicket.status == RealTicketStatus.PLACED.value)
    )
    window = timedelta(days=settings.SETTLEMENT_TICKET_WINDOW_DAYS)
    for match_id, sport, event_name, placed_at, created_at in tickets.all():
        teams = _split_event(event_name or "")
        placed = _aware(placed_at) or _aware(created_at)
        if teams is None or placed is None or placed < since:
            continue
        # A ticket does not keep its kickoff: the match is the one these two sides played soon after it was placed (or
        # in play as it was placed)
        if placed + finished_after(sport or "") <= now:
            out.setdefault(match_id, Waiting(match_id, family(sport), teams[0], teams[1], None, placed - timedelta(hours=6), placed + window))

    orders = list((await session.execute(
        select(BetLedger.match_id).where(BetLedger.status == "ACCEPTED", BetLedger.placed_at >= since).distinct()
    )).scalars())
    if orders:
        history = await session.execute(
            select(OddsSnapshot.match_id, OddsSnapshot.sport_key, OddsSnapshot.home_team, OddsSnapshot.away_team, func.max(OddsSnapshot.commence_time))
            .where(OddsSnapshot.match_id.in_(orders))
            .group_by(OddsSnapshot.match_id, OddsSnapshot.sport_key, OddsSnapshot.home_team, OddsSnapshot.away_team)
        )
        for match_id, sport_key, home, away, kickoff in history.all():
            kickoff = _aware(kickoff)
            if kickoff is not None and kickoff + finished_after(sport_key or "") <= now:
                out.setdefault(match_id, Waiting(match_id, family(sport_key), home, away, kickoff))

    scored = await _scored(session, set(out))
    return [w for fid, w in out.items() if fid not in scored]


# ================================================================ 2. the results feeds
def _ranges(waiting: Sequence[Waiting], now: datetime) -> list[tuple[datetime, datetime]]:
    """The days the waiting fixtures were played, merged into ranges of at most MAX_RANGE_DAYS + 1 days."""
    days = sorted({d.date() for w in waiting for d in ([w.kickoff] if w.kickoff else [w.earliest, min(w.latest or now, now)]) if d is not None})
    out: list[tuple[datetime, datetime]] = []
    for day in days:
        moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
        if out and (moment - out[-1][0]).days <= MAX_RANGE_DAYS:
            out[-1] = (out[-1][0], max(out[-1][1], moment))
        else:
            out.append((moment, moment))
    return out


async def finished_matches(http: httpx.AsyncClient, settings: Settings, waiting: Sequence[Waiting], now: datetime) -> tuple[list[ProviderFixture], dict[str, Any]]:
    """Finished, cancelled and postponed matches from the feeds, for the days the waiting football fixtures were played."""
    report: dict[str, Any] = {}
    football = [w for w in waiting if w.family == "soccer"]
    if not football:
        return [], report
    ranges = _ranges(football, now)
    timeout = settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS
    found: list[ProviderFixture] = []
    key = settings.FOOTBALL_DATA_ORG_API_KEY.get_secret_value() if settings.FOOTBALL_DATA_ORG_API_KEY else ""
    if key:
        client = FootballDataClient(http, key, settings.FOOTBALL_DATA_BASE_URL, timeout)
        try:
            for start, end in ranges:
                found.extend(await client.fixtures(settings.FOOTBALL_DATA_COMPETITIONS, (start - timedelta(days=1)).date(), (end + timedelta(days=1)).date()))
            report[FOOTBALL_DATA] = {"calls": len(ranges)}
        except SportsDataError as exc:
            report[FOOTBALL_DATA] = {"error": str(exc)}
    else:
        report[FOOTBALL_DATA] = {"error": "no key (FOOTBALL_DATA_ORG_API_KEY)"}
    key = settings.SPORTSMONKS_API_KEY.get_secret_value() if settings.SPORTSMONKS_API_KEY else ""
    if key and settings.SPORTSMONKS_LEAGUE_SPORT_KEYS:
        client_sm = SportmonksClient(http, key, settings.SPORTSMONKS_BASE_URL, timeout)
        try:
            for start, end in ranges:
                found.extend(await client_sm.fixtures(settings.SPORTSMONKS_LEAGUE_SPORT_KEYS, (start - timedelta(days=1)).date(), (end + timedelta(days=1)).date()))
            report[SPORTMONKS] = {"calls": len(ranges)}
        except SportsDataError as exc:
            report[SPORTMONKS] = {"error": str(exc)}
    finished = [p for p in found if p.status in ("FINISHED", "CANCELLED", "POSTPONED")]
    report["finished_matches"] = len(finished)
    return finished, report


@dataclass(slots=True)
class Recorded:
    fixture_id: str
    provider: str
    provider_id: str
    status: str
    score: tuple[int, int] | None


async def record_results(session: AsyncSession, waiting: Sequence[Waiting], finished: Sequence[ProviderFixture], settings: Settings,
                         now: datetime) -> tuple[list[Recorded], int]:
    """Each waiting fixture's one finished match as its fixture_scores row, in the orientation the ledger names it.
    ``(recorded, unpaired)``. The caller commits."""
    recorded: list[Recorded] = []
    unpaired = 0
    window = timedelta(hours=settings.SETTLEMENT_PAIR_WINDOW_HOURS)
    for w in waiting:
        found = match_result(w, finished, cutoff=settings.SETTLEMENT_NAME_CUTOFF, window=window)
        if found is None:
            unpaired += 1
            continue
        match, swapped = found
        if match.status == "FINISHED":
            if match.regulation is None:
                unpaired += 1  # decided past 90 minutes without a regular-time score: an admin's call
                continue
            home_goals, away_goals = (match.regulation[1], match.regulation[0]) if swapped else match.regulation
            status = ScoreStatus.FINAL
        else:
            status, home_goals, away_goals = _STATUS_FROM_FEED[match.status], None, None
        existing = await session.get(FixtureScore, w.fixture_id)
        if existing is not None and (existing.source == ADMIN_SOURCE or existing.status != ScoreStatus.POSTPONED.value):
            continue  # an admin's score stands; a final or abandoned score is not replaced
        await user_pnl_tracker.record_score(
            session, ScoreIn(fixture_id=w.fixture_id, home=w.home[:128], away=w.away[:128], sport_key=match.sport_key, kickoff=match.kickoff,
                             home_goals=home_goals, away_goals=away_goals, status=status),
            source=match.provider[:32], by=None, now=now,
        )
        recorded.append(Recorded(w.fixture_id, match.provider, match.provider_id, status.value,
                                 None if home_goals is None or away_goals is None else (home_goals, away_goals)))
    return recorded, unpaired


# ================================================================ 3-4. the ledgers
@dataclass(slots=True)
class LedgerReport:
    settled: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    pending_ungradeable: int = 0  # a score is there but the market or selection cannot be read: left for a person
    gross_payout: Decimal = Decimal("0.00")
    tds_withheld: Decimal = Decimal("0.00")
    net_post_tax: Decimal = Decimal("0.00")
    tax_entries: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def count(self, result: str) -> None:
        self.settled += 1
        if result in ("WON", "HALF_WON"):
            self.won += 1
        elif result in ("LOST", "HALF_LOST"):
            self.lost += 1
        else:
            self.void += 1

    def as_dict(self) -> dict[str, Any]:
        return {"settled": self.settled, "won": self.won, "lost": self.lost, "void": self.void, "pending_ungradeable": self.pending_ungradeable,
                "gross_payout": str(self.gross_payout), "tds_withheld": str(self.tds_withheld), "net_post_tax": str(self.net_post_tax),
                "tax_entries": self.tax_entries, "rows": self.rows}


def _leg_result(market: str, selection: str, score: FixtureScore, now: datetime, cutoff: float) -> LegResult | None | str:
    """The grade, None (not yet), or "UNGRADEABLE" (a score is there but the bet cannot be read)."""
    if score.status == ScoreStatus.FINAL.value:
        leg = canonical_leg(market, selection, score.home, score.away, cutoff=cutoff)
        if leg is None:
            return "UNGRADEABLE"
        return grade(leg, status=score.status, home_goals=score.home_goals, away_goals=score.away_goals, sport_key=score.sport_key,
                     kickoff=_aware(score.kickoff), now=now)
    # ABANDONED / POSTPONED void every market alike (POSTPONED once the grace period has passed)
    any_leg = canonical_leg("Match Odds", "HOME", score.home, score.away)
    return None if any_leg is None else grade(any_leg, status=score.status, home_goals=None, away_goals=None, sport_key=score.sport_key,
                                              kickoff=_aware(score.kickoff), now=now)


async def settle_real_tickets(session: AsyncSession, settings: Settings, now: datetime) -> LedgerReport:
    """Every placed ticket a score now decides, through the desk's own ``settle_ticket``. The caller commits."""
    report = LedgerReport()
    tickets = list((await session.execute(
        select(RealBetTicket).where(RealBetTicket.status == RealTicketStatus.PLACED.value).order_by(RealBetTicket.id).with_for_update(skip_locked=True)
    )).scalars())
    scores = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(FixtureScore.fixture_id.in_({t.match_id for t in tickets})))).scalars()} if tickets else {}
    for ticket in tickets:
        score = scores.get(ticket.match_id)
        if score is None or not ticket.settleable_from_score:
            continue
        result = _leg_result(ticket.quote_market or ticket.market_type, ticket.quote_selection or ticket.selection, score, now, settings.SETTLEMENT_NAME_CUTOFF)
        if result == "UNGRADEABLE":
            report.pending_ungradeable += 1
            report.rows.append({"ticket": ticket.ticket_reference, "left_pending": f"{ticket.market_type} / {ticket.selection} cannot be read"})
            continue
        if result is None:
            continue
        notes = (f"Auto-settled from the {score.source} final score {score.home_goals}-{score.away_goals}" if score.status == ScoreStatus.FINAL.value
                 else f"Auto-settled: the match is {score.status.lower()} ({score.source})")
        try:
            async with session.begin_nested():  # one ticket's refusal never undoes another's settlement
                settled = await rms.settle_ticket(db=session, settings=settings, user_id=ticket.user_id, ticket_ref=ticket.ticket_reference,
                                                  outcome=result.value, notes=notes)
        except rms.RealMoneyError as exc:
            report.rows.append({"ticket": ticket.ticket_reference, "refused": f"{exc.code}: {exc}"})
            continue
        report.count(settled.status)
        report.gross_payout += settled.gross_return
        report.tds_withheld += settled.tds_deducted
        report.net_post_tax += settled.settlement_payout
        report.tax_entries += 1 if settled.tds_deducted > 0 else 0
        report.rows.append({"ticket": ticket.ticket_reference, "result": settled.status, "gross": str(settled.gross_return), "tds": str(settled.tds_deducted),
                            "payout": str(settled.settlement_payout), "source": score.source})
    return report


def exchange_order_payout(stake: Decimal, odds: Decimal, result: LegResult) -> Decimal:
    """The order's payout by the Arena's rule (P&L = payout - stake), so an automated and a manual settlement agree."""
    return ArenaEngine._resolve_payout(stake, odds, SettlementRequest(status=result.value))  # noqa: SLF001 - the Arena's single payout rule


async def settle_exchange_orders(session: AsyncSession, settings: Settings, now: datetime) -> LedgerReport:
    """Every accepted exchange order a score now decides. TDS (and its tax-ledger row) only for an order a real exchange
    accepted; never the real-money vault. The caller commits."""
    report = LedgerReport()
    orders = list((await session.execute(
        select(BetLedger, ExchangeAccount.user_id, ExchangeAccount.exchange_name).join(ExchangeAccount, ExchangeAccount.id == BetLedger.exchange_account_id)
        .where(BetLedger.status == "ACCEPTED").order_by(BetLedger.id).with_for_update(skip_locked=True, of=BetLedger)
    )).all())
    scores = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(FixtureScore.fixture_id.in_({o.match_id for o, _, _ in orders})))).scalars()} if orders else {}
    for order, user_id, exchange in orders:
        score = scores.get(order.match_id)
        if score is None:
            continue
        result = _leg_result(order.market_type, order.selection, score, now, settings.SETTLEMENT_NAME_CUTOFF)
        if result == "UNGRADEABLE":
            report.pending_ungradeable += 1
            report.rows.append({"order": str(order.id), "left_pending": f"{order.market_type} / {order.selection} cannot be read"})
            continue
        if result is None:
            continue
        stake, odds = Decimal(order.stake), Decimal(order.odds)
        payout = exchange_order_payout(stake, odds, result)
        order.status, order.payout, order.resolved_at = result.value, payout, now
        report.count(result.value)
        report.gross_payout += money(payout)
        real = (exchange or "").strip().casefold() in REAL_EXCHANGES
        tds = Decimal("0.00")
        if real and result in (LegResult.WON, LegResult.HALF_WON):
            # the winning stake and what it returned: all of it, or the winning half of a half-won order
            winning_stake = stake if result is LegResult.WON else stake / 2
            breakdown = section_115bbj(money(winning_stake * odds), money(winning_stake))
            tds = breakdown.tds
            if breakdown.net_winnings > 0:
                session.add(TaxLedgerEntry(
                    user_id=user_id, paper_bet_id=None, mode="LIVE", gross_payout=breakdown.gross_payout, stake=breakdown.stake,
                    net_winnings=breakdown.net_winnings, tds_rate=float(breakdown.tds_rate), tds_amount=tds, net_post_tax_payout=breakdown.net_post_tax_payout,
                    currency=order.currency or "INR", jurisdiction="IN", section="115BBJ", financial_year=financial_year(now), created_at=now,
                ))
                report.tax_entries += 1
        report.tds_withheld += tds
        report.net_post_tax += money(payout) - tds
        report.rows.append({"order": str(order.id), "match_id": order.match_id, "result": result.value, "payout": str(money(payout)),
                            "tds": str(tds), "exchange": exchange, "real_exchange": real, "source": score.source})
    return report


async def _oracle_preview(session: AsyncSession, now: datetime) -> int:
    """How many pending Oracle legs a recorded score would settle now (the dry run's count; nothing is written)."""
    legs = list((await session.execute(
        select(UserPlacedLeg).join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id)
        .where(UserPlacedBet.status == PlacedStatus.PENDING.value, UserPlacedLeg.result == PlacedStatus.PENDING.value)
    )).scalars())
    scores = await user_pnl_tracker._scores_for(session, legs) if legs else {}  # noqa: SLF001 - the settler's own matching
    return sum(1 for leg in legs if leg.id in scores and user_pnl_tracker.leg_result_from_score(leg, scores[leg.id], now) is not None)


# ================================================================ the sweep
async def sweep(session_factory: async_sessionmaker[AsyncSession], http: httpx.AsyncClient | None, redis: Redis | None, settings: Settings, *,
                now: datetime | None = None, fetch: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Results in, every ledger settled, one event out. ``dry_run``: everything computed, nothing written or sent."""
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"dry_run": dry_run, "at": now.isoformat(), "developer_credit": DEVELOPER_CREDIT}
    oracle_users: set[uuid.UUID] = set()
    async with session_factory() as session:
        waiting = await waiting_fixtures(session, settings, now)
        report["waiting_fixtures"] = len(waiting)
        finished: list[ProviderFixture] = []
        if fetch and waiting and http is not None:
            finished, report["feeds"] = await finished_matches(http, settings, waiting, now)
        recorded, unpaired = await record_results(session, waiting, finished, settings, now)
        report["results"] = {"recorded": [asdict(r) for r in recorded], "unpaired": unpaired}
        await session.flush()
        if dry_run:
            # one session, rolled back: the scores just recorded grade what they would grade, and nothing is kept
            report["oracle"] = {"legs_settled": await _oracle_preview(session, now), "bets_settled": None}
            tickets = await settle_real_tickets(session, settings, now)
            orders = await settle_exchange_orders(session, settings, now)
            await session.rollback()
        else:
            await session.commit()
    if not dry_run:
        settled = await user_pnl_tracker.settle_pending(session_factory, now)
        report["oracle"] = {"legs_settled": settled.legs, "bets_settled": settled.bets}
        oracle_users = set(settled.users)
        async with session_factory() as session:
            tickets = await settle_real_tickets(session, settings, now)
            orders = await settle_exchange_orders(session, settings, now)
            await session.commit()
    report["real_money_tickets"] = tickets.as_dict()
    report["exchange_orders"] = orders.as_dict()
    total = (report["oracle"]["bets_settled"] or 0) + tickets.settled + orders.settled
    report["settled_total"] = total
    report["tds_withheld"] = str(tickets.tds_withheld + orders.tds_withheld)
    if not dry_run and (total or recorded):
        await user_pnl_tracker.bump(redis, oracle_users)
        await publish_event(redis, {"type": "settlement", "section": "settlement", "settled": total, "results_recorded": len(recorded),
                                    "tickets": tickets.settled, "orders": orders.settled, "oracle_bets": report["oracle"]["bets_settled"],
                                    "tds_withheld": report["tds_withheld"]})
    logger.info("settlement sweep: %d waiting, %d results, %d settled (dry run %s)", len(waiting), len(recorded), total, dry_run)
    return report


# ================================================================ an admin's result
async def known_names(session: AsyncSession, fixture_id: str) -> tuple[str, str, str | None, datetime | None] | None:
    """The teams a ledger names for this fixture id (an exchange order's history, a ticket, an Oracle leg)."""
    row = (await session.execute(
        select(OddsSnapshot.home_team, OddsSnapshot.away_team, OddsSnapshot.sport_key, func.max(OddsSnapshot.commence_time))
        .where(OddsSnapshot.match_id == fixture_id).group_by(OddsSnapshot.home_team, OddsSnapshot.away_team, OddsSnapshot.sport_key)
    )).first()
    if row is not None:
        return row[0], row[1], row[2], _aware(row[3])
    leg = (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.fixture_id == fixture_id).limit(1))).scalar_one_or_none()
    if leg is not None:
        return leg.home, leg.away, leg.sport_key, _aware(leg.kickoff)
    ticket = (await session.execute(select(RealBetTicket).where(RealBetTicket.match_id == fixture_id).limit(1))).scalar_one_or_none()
    teams = None if ticket is None else _split_event(ticket.event_name or "")
    return None if teams is None else (teams[0], teams[1], None, None)


async def record_admin_result(session_factory: async_sessionmaker[AsyncSession], fixture_id: str, *, status: ScoreStatus, home_goals: int | None,
                              away_goals: int | None, home: str | None = None, away: str | None = None, sport_key: str | None = None,
                              now: datetime | None = None) -> dict[str, Any]:
    """An explicit administrative result for one fixture id: it replaces any score the feeds gave (bets already settled
    on the earlier score are not regraded here: the Arena's force_regrade and the desk's history are where a person does
    that). Raises SettlementError for an unknown fixture without names, or a final score without both goals."""
    now = now or datetime.now(UTC)
    if status is ScoreStatus.FINAL and (home_goals is None or away_goals is None or home_goals < 0 or away_goals < 0):
        raise SettlementError("a final score needs both sides' goals (0 or more)")
    async with session_factory() as session:
        names = await known_names(session, fixture_id)
        if home is None or away is None:
            if names is None:
                raise SettlementError(f"no ledger names fixture {fixture_id}: give --home and --away")
            home, away = names[0], names[1]
        previous = await session.get(FixtureScore, fixture_id)
        before = None if previous is None else {"status": previous.status, "home_goals": previous.home_goals, "away_goals": previous.away_goals, "source": previous.source}
        row = await user_pnl_tracker.record_score(
            session, ScoreIn(fixture_id=fixture_id, home=home, away=away, sport_key=sport_key or (names[2] if names else None),
                             kickoff=names[3] if names else None, home_goals=home_goals, away_goals=away_goals, status=status),
            source=ADMIN_SOURCE, by=None, now=now,
        )
        await session.commit()
        return {"fixture_id": row.fixture_id, "home": row.home, "away": row.away, "status": row.status, "home_goals": row.home_goals,
                "away_goals": row.away_goals, "replaced": before}


__all__ = ["SettlementError", "finished_matches", "known_names", "record_admin_result", "record_results", "settle_exchange_orders",
           "settle_real_tickets", "sweep", "waiting_fixtures"]
