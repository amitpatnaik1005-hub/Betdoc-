"""The manual parlay workbench (Group 77): the board of match cards, the rating, the placement and the shield.

* ``board``: every fixture the live market prices (pre-match, and in play within ``LIVE_LOOKBACK`` of kickoff), as
  KPI cards per sport (``MANUAL_PARLAY_SPORTS``): each market's selections with the models' and the ensemble's
  probability, every book's price, the best price at the account books, the EV there, the sharp steam on it, the
  market's measured overround per book, and the fixture's reported maximum stake when the evidence store has one.
  The feeds carry no live score or clock: an in-play card says how long since kickoff, nothing more.
* ``inspect``: the parlay through the twin's fortress, priced at the chosen account's book (the BetDoc skin: the
  best of the retail books), then rated (``cognitive_parlay_rater``) with the measured margins and a stop-loss.
* ``submit``: the inspected slip into Ashoka's ledger at the user's real price and stake, with the in-play
  stop-loss shield armed (straight multiples; a system's lines settle separately and are not watched).
* ``emergency_cashout``: issue the bookmaker's cashout ticket now, or record the cashout the user took.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.bookmakers.base_cashout_adapter import adapter_for
from app.core.config import Settings
from app.domain.bookmakers.adapters import EXCHANGE_BOOKMAKERS
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.builder.recipe import choose_offender, scoreline_link
from app.domain.manual_parlay import cognitive_parlay_rater as rater
from app.domain.oracle.match_model import likeliest_score
from app.domain.oracle.parlay_engine import LegCandidate
from app.models.digital_twin import PulloutReason, TwinInPlayMonitor, TwinVettingAudit
from app.models.omni_vault import VaultBookmakerAccount
from app.models.sentinel import Severity
from app.models.user_bets_ledger import PlacedBookmaker, PlacedStatus, UserPlacedBet
from app.schemas.twin import LedgerFromAudit
from app.services import ashoka_market
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin import inplay, vetting
from app.services.twin.intel import read_intel

# skin -> (the canonical book it prices at, the ledger's bookmaker); BetDoc: the best of the retail books
SKINS: dict[str, tuple[str | None, PlacedBookmaker | None]] = {
    "parimatch": ("parimatch", PlacedBookmaker.PARIMATCH), "one_xbet": ("1xbet", PlacedBookmaker.ONEXBET),
    "stake": ("stake", PlacedBookmaker.STAKE), "betdoc": (None, None),
}
ACCOUNT_BOOKS = ("parimatch", "1xbet", "stake")


class WorkbenchRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


def sport_of(sport_key: str | None, settings: Settings) -> str | None:
    key = (sport_key or "").casefold()
    return next((label for label, prefixes in settings.MANUAL_PARLAY_SPORTS.items() if any(key.startswith(p) for p in prefixes)), None)


# ================================================================ the board
def _market_rows(legs: Sequence[LegCandidate], steam: set[str] | None, now: datetime, settings: Settings) -> dict[str, Any]:
    first = legs[0]
    selections = list(first.market.selections)
    quotes = {leg.selection: {b: q.odds for b, q in leg.quotes.items() if b not in EXCHANGE_BOOKMAKERS} for leg in legs}
    rows = []
    for leg in sorted(legs, key=lambda x: selections.index(x.selection) if x.selection in selections else 99):
        best = leg.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, ACCOUNT_BOOKS)
        payload = ashoka_market.leg_payload(leg, best, now)
        rows.append({**{k: payload[k] for k in ("leg_id", "selection", "label", "fair_probability", "models", "ev", "prices")},
                     "best": None if best is None else {"book": best.bookmaker, "odds": best.odds}, "steam": None if steam is None else leg.selection in steam})
    return {"market": first.market.key, "kind": first.market.kind.value, "selections": rows, "margins": rater.market_margins(selections, quotes)}


async def board(redis: Redis, settings: Settings, now: datetime, *, sport: str | None = None) -> dict[str, Any]:
    candidates, stats = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK)
    steam = await vetting.steam_by_market(redis, settings, now)
    by_fixture: dict[str, list[LegCandidate]] = defaultdict(list)
    for leg in candidates:
        by_fixture[leg.fixture_id].append(leg)
    try:
        intel = await read_intel(redis, settings, set(by_fixture))
    except (RedisError, OSError):
        intel = {}
    counts: dict[str, int] = {label: 0 for label in settings.MANUAL_PARLAY_SPORTS}
    cards = []
    for fixture_id, legs in by_fixture.items():
        head = legs[0]
        label = sport_of(head.sport_key, settings)
        if label is not None:
            counts[label] += 1
        if sport is not None and label != sport:
            continue
        markets: dict[str, list[LegCandidate]] = defaultdict(list)
        for leg in legs:
            markets[leg.market.key].append(leg)
        kickoff = head.kickoff
        started = kickoff is not None and kickoff <= now
        liquidity = intel.get(fixture_id).liquidity if intel.get(fixture_id) is not None else None  # type: ignore[union-attr]
        # Group 102/103: the scoreline the market implies, from the Dixon-Coles matrix Ashoka fitted to this fixture's
        # de-vigged 1X2 (and totals); None where no three-way market was fitted (two-way sports)
        fitted = next((leg.models.matrices["dixon_coles"] for leg in legs if leg.models is not None and "dixon_coles" in leg.models.matrices), None)
        cards.append({
            "fixture_id": fixture_id, "home": head.home, "away": head.away, "sport": label, "sport_key": head.sport_key, "league": head.league,
            "kickoff": None if kickoff is None else kickoff.isoformat(), "in_play": started,
            "minutes_since_kickoff": round((now - kickoff).total_seconds() / 60) if started and kickoff is not None else None,
            "max_stake_inr": None if liquidity is None else {k: str(v) for k, v in liquidity.max_stake_inr.items()},
            "projection": None if fitted is None else likeliest_score(fitted),
            "markets": [_market_rows(rows, None if steam is None else steam.get((fixture_id, key), set()), now, settings)
                        for key, rows in sorted(markets.items(), key=lambda kv: (kv[0] != "Match Odds", kv[0]))],
        })
    cards.sort(key=lambda c: (not c["in_play"], c["kickoff"] or ""))
    return {"sports": counts, "sport": sport, "fixtures": cards, "steam_readable": steam is not None, "stats": stats, "as_of": now.isoformat()}


# ================================================================ inspect
@dataclass(frozen=True, slots=True)
class Inspection:
    audit: TwinVettingAudit
    payload: dict[str, Any]


async def _leg_context(redis: Redis, settings: Settings, leg_ids: Sequence[str], book: str | None, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Each leg's margins (every book's overround on its market, and the account's) and its scoreline sync (Group 104):
    its result at its fixture's likeliest score under the Dixon-Coles matrix Ashoka fitted to the market, or None."""
    fixtures = {leg_id.split("|", 1)[0] for leg_id in leg_ids}
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK, fixtures=fixtures)
    by_market: dict[tuple[str, str], list[LegCandidate]] = defaultdict(list)
    by_id = {leg.leg_id: leg for leg in candidates}
    for leg in candidates:
        by_market[(leg.fixture_id, leg.market.key)].append(leg)
    sync: dict[str, Any] = {}
    for leg_id in leg_ids:
        leg = by_id.get(leg_id)
        matrix = leg.models.matrices.get("dixon_coles") if leg is not None and leg.models is not None else None
        sync[leg_id] = None if leg is None else scoreline_link(matrix, leg.market, leg.selection)
    rows, factor, complete = [], 1.0, True
    for leg_id in leg_ids:
        fixture, market, selection = leg_id.split("|", 2)
        legs = by_market.get((fixture, market), [])
        if not legs:
            complete = False
            continue
        quotes = {leg.selection: {b: q.odds for b, q in leg.quotes.items() if b not in EXCHANGE_BOOKMAKERS} for leg in legs}
        margins = rater.market_margins(list(legs[0].market.selections), quotes)
        mine = margins["best_price"] if book is None else margins["by_book"].get(book)
        if mine is None:
            complete = False
        else:
            factor *= 1.0 + mine
        rows.append({"leg_id": leg_id, "selection": selection, **margins, "account_margin": mine})
    margins_view = {"legs": rows, "account_book": book or "best of the retail books", "parlay_overround": round(factor - 1.0, 6) if complete and rows else None}
    return margins_view, sync


def slip_rating(audit: TwinVettingAudit, margins: dict[str, Any], sync: dict[str, Any], settings: Settings) -> rater.Rating:
    """The BetDoc Slip Score (Group 104): the fortress's 15-pillar score with the badges the inspection earned."""
    policy = rater.RaterPolicy.from_settings(settings)
    by_leg = {row["leg_id"]: row for row in margins["legs"]}
    evidence = rater.SlipEvidence(
        fixtures=[leg_id.split("|", 1)[0] for leg_id in audit.leg_ids],
        margins=[by_leg.get(leg_id, {}).get("account_margin") for leg_id in audit.leg_ids],
        score_sync=[None if sync.get(leg_id) is None else bool(sync[leg_id]["synced"]) for leg_id in audit.leg_ids],
        joint_ev=audit.joint_ev,
    )
    return rater.slip_score(rater.rate(audit.pillars, policy), rater.badges(audit.pillars, evidence, rater.BadgePolicy.from_settings(settings)), policy)


@dataclass(frozen=True, slots=True)
class Trial:
    """A fortress run that is not recorded (vetting.assess) and its Slip Score: the auto-fix's and the Chef's Table's."""

    audit: TwinVettingAudit
    rating: rater.Rating
    margins: dict[str, Any]
    sync: dict[str, Any]


async def trial(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID | None, *, leg_ids: Sequence[str],
                kind: str | None, book: str | None, bankroll: Decimal | None, now: datetime) -> Trial:
    audit = await vetting.assess(sessions, redis, settings, user_id=user_id, leg_ids=leg_ids, kind=kind, bankroll=bankroll, now=now, books=(book,) if book else None)
    margins, sync = await _leg_context(redis, settings, list(audit.leg_ids), book, now)
    return Trial(audit, slip_rating(audit, margins, sync, settings), margins, sync)


async def inspect(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, *, leg_ids: Sequence[str], kind: str | None, skin: str,
                  bankroll: Decimal | None, now: datetime) -> Inspection:
    if skin not in SKINS:
        raise WorkbenchRefusal("UNKNOWN_SKIN", f"skin is one of {', '.join(SKINS)}", 422)
    book = SKINS[skin][0]
    audit = await vetting.vet(sessions, redis, settings, user_id=user_id, leg_ids=leg_ids, kind=kind, bankroll=bankroll, now=now, books=(book,) if book else None)
    margins, sync = await _leg_context(redis, settings, list(audit.leg_ids), book, now)
    rating = slip_rating(audit, margins, sync, settings)
    policy = slm.StopLossPolicy.from_settings(settings)
    async with sessions() as session:
        credit = await vetting.developer_credit(session)
    stop = slm.recommended_pct(audit.joint_probability or 0.0, policy)
    payload = {
        "audit": vetting.audit_view(audit, credit), "rating": rating.as_dict(), "margins": margins, "score_sync": sync,
        "skin": skin, "book": audit.bookmaker,
        "stop_loss": {"recommended_pct": stop, "min_pct": policy.min_pct, "max_pct": policy.max_pct, "floor_inr_at_twin_stake": str(slm.floor(audit.stake_inr, stop)),
                      "basis": "wider for a long shot, tighter for a near-certainty: linear in the slip's win probability"},
        "developer_credit": credit,
    }
    return Inspection(audit, payload)


# ================================================================ the 1-click auto-fix (Group 104)
async def auto_fix(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, *, leg_ids: Sequence[str], kind: str | None,
                   skin: str, bankroll: Decimal | None, now: datetime) -> dict[str, Any]:
    """Replace the slip's offending leg with the alternative the fortress rates highest.

    The offender (``choose_offender``): a second leg on one fixture, a negative-EV leg, the heaviest-margin leg, or the
    weakest leg of a vetoed slip. Its alternatives: legs on fixtures the slip does not touch, quoted at the account's
    book with positive EV there, the offender's sport first, best EV first; the top ``MANUAL_PARLAY_AUTOFIX_CANDIDATES``
    each run through the fortress without being recorded. The best is kept only if it scores higher, and is then
    inspected for real (its audit is what the slip records). Nothing is invented: every alternative is a quoted leg."""
    if skin not in SKINS:
        raise WorkbenchRefusal("UNKNOWN_SKIN", f"skin is one of {', '.join(SKINS)}", 422)
    book = SKINS[skin][0]
    leg_ids = list(dict.fromkeys(leg_ids))
    current = await trial(sessions, redis, settings, user_id, leg_ids=leg_ids, kind=kind, book=book, bankroll=bankroll, now=now)
    slip_legs = (current.audit.slip or {}).get("legs", [])
    evs = {leg["leg_id"]: leg.get("ev") for leg in slip_legs}
    margins = {row["leg_id"]: row.get("account_margin") for row in current.margins["legs"]}
    vetoed = any(row["status"] == "FAIL" for row in current.rating.breakdown)
    before = {"score": current.rating.score, "tier": current.rating.tier}
    offender = choose_offender(leg_ids, evs, margins, {b.code for b in current.rating.badges}, vetoed)
    if offender is None:
        return {"improved": False, "reason": "NOTHING_TO_FIX", "message": "No leg shares a fixture, is priced at a loss, or carries a heavy margin: nothing to fix.",
                "before": before, "tried": []}
    offender_id, why = offender
    head = next((leg for leg in slip_legs if leg["leg_id"] == offender_id), {})
    sport = str(head.get("sport_key") or "").split("_", 1)[0]
    taken = {leg_id.split("|", 1)[0] for leg_id in leg_ids}
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK)
    pool: list[tuple[bool, float, LegCandidate, Any]] = []
    for leg in candidates:
        if leg.fixture_id in taken:
            continue
        quote = leg.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, (book,) if book else ACCOUNT_BOOKS)
        if quote is None:
            continue
        try:
            ev = leg.ev(quote.net_odds)
        except ValueError:
            continue
        if ev > 0:
            pool.append((str(leg.sport_key or "").split("_", 1)[0] == sport, ev, leg, quote))
    pool.sort(key=lambda t: (not t[0], -t[1]))
    tried: list[dict[str, Any]] = []
    best: tuple[float, list[str], dict[str, Any]] | None = None
    for _, _, leg, quote in pool[: settings.MANUAL_PARLAY_AUTOFIX_CANDIDATES]:
        swapped = [leg.leg_id if leg_id == offender_id else leg_id for leg_id in leg_ids]
        try:
            run = await trial(sessions, redis, settings, user_id, leg_ids=swapped, kind=kind, book=book, bankroll=bankroll, now=now)
        except (WorkbenchRefusal, vetting.TwinRefusal, ValueError):
            continue
        view = ashoka_market.leg_payload(leg, quote, now)
        tried.append({"leg_id": leg.leg_id, "label": view["label"], "fixture": view["fixture"], "ev": view["ev"], "score": run.rating.score, "tier": run.rating.tier})
        if best is None or run.rating.score > best[0]:
            best = (run.rating.score, swapped, view)
    replaced = {"leg_id": offender_id, "label": head.get("label", offender_id), "fixture": head.get("fixture", offender_id.split("|", 1)[0]), "reason": why}
    if best is None or best[0] <= current.rating.score:
        message = "No quoted alternative rates higher than the slip as it is." if tried else "No positive-EV leg on another fixture is quoted at this book."
        return {"improved": False, "reason": "NO_BETTER_LEG", "replaced": replaced, "before": before, "tried": tried, "message": message}
    inspection = await inspect(sessions, redis, settings, user_id, leg_ids=best[1], kind=kind, skin=skin, bankroll=bankroll, now=now)
    return {"improved": True, "replaced": replaced, "replacement": best[2], "before": before,
            "after": {"score": inspection.payload["rating"]["score"], "tier": inspection.payload["rating"]["tier"]}, "tried": tried,
            "inspection": inspection.payload}


# ================================================================ submit
async def submit(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, audit_id: uuid.UUID, *,
                 skin: str, stake_inr: Decimal, placed_odds: Decimal | None, booking_code: str | None, stop_loss_pct: float | None, now: datetime,
                 placed_at: datetime | None = None) -> dict[str, Any]:
    if skin not in SKINS:
        raise WorkbenchRefusal("UNKNOWN_SKIN", f"skin is one of {', '.join(SKINS)}", 422)
    async with sessions() as session:
        audit = await session.get(TwinVettingAudit, audit_id)
        if audit is None or audit.user_id != user_id:
            raise WorkbenchRefusal("AUDIT_NOT_FOUND", "no such inspection of yours", 404)
        ledger_book = SKINS[skin][1] or vetting.placed_bookmaker(audit.bookmaker)
        body = LedgerFromAudit(bookmaker=ledger_book, stake_inr=stake_inr, placed_odds=placed_odds, booking_code=booking_code, watch=True, stop_loss_pct=stop_loss_pct,
                               placed_at=placed_at)
        bet = await vetting.record_placed(session, user_id, audit, body, now)
        watch: dict[str, Any]
        try:
            monitor = await inplay.start(session, redis, settings, bet, now, audit_id=audit.id, stop_loss_pct=stop_loss_pct)
            watch = {"armed": True, "shield": inplay.monitor_view(monitor)}
        except inplay.NoLivePrice as exc:
            watch = {"armed": False, "reason": "NO_LIVE_PRICE", "message": str(exc)}
        except ValueError as exc:
            watch = {"armed": False, "reason": "NOT_WATCHABLE", "message": str(exc)}
        await session.commit()
        credit = await vetting.developer_credit(session)
    return {"bet_id": str(bet.id), "status": bet.status, "bookmaker": bet.bookmaker, "stake_inr": str(bet.stake_inr), "booking_code": bet.booking_code,
            "structure": bet.structure, "audit_id": str(audit.id), "shield": watch, "developer_credit": credit}


# ================================================================ the shields
async def shields(session: AsyncSession, user_id: uuid.UUID, *, active_only: bool) -> list[dict[str, Any]]:
    query = select(TwinInPlayMonitor, UserPlacedBet).join(UserPlacedBet, UserPlacedBet.id == TwinInPlayMonitor.bet_id).where(TwinInPlayMonitor.user_id == user_id)
    if active_only:
        query = query.where(TwinInPlayMonitor.is_active.is_(True))
    rows = (await session.execute(query.order_by(TwinInPlayMonitor.created_at.desc()).limit(50))).all()
    return [{**inplay.monitor_view(m, b), "frame": inplay.frame(m, b)} for m, b in rows]


async def emergency_cashout(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, shield_id: uuid.UUID, *,
                            amount_inr: Decimal | None, now: datetime) -> dict[str, Any]:
    """Without an amount: issue the cashout ticket now and close the shield. With one: the cashout the user took, recorded on the bet."""
    alert = None
    async with sessions() as session:
        monitor = await session.get(TwinInPlayMonitor, shield_id, with_for_update=True)
        if monitor is None or monitor.user_id != user_id:
            raise WorkbenchRefusal("SHIELD_NOT_FOUND", "no such shield of yours", 404)
        bet = await session.get(UserPlacedBet, monitor.bet_id, with_for_update=True)
        if bet is None:
            raise WorkbenchRefusal("SHIELD_NOT_FOUND", "the shield's bet is gone", 404)
        adapter = adapter_for(bet.bookmaker)
        if amount_inr is not None:
            if bet.status != PlacedStatus.PENDING.value:
                raise WorkbenchRefusal("ALREADY_SETTLED", f"the bet is {bet.status.lower()}")
            await adapter.record(session, bet, amount_inr, now)
            monitor.is_active = False
            if not monitor.pullout_triggered:
                monitor.pullout_triggered, monitor.pullout_reason, monitor.pullout_at = True, PulloutReason.MANUAL_USER_REQUEST.value, now
            monitor.detail = {**(monitor.detail or {}), "salvaged_inr": str(amount_inr.quantize(Decimal("0.01"))), "salvaged_at": now.isoformat(),
                              "salvaged_pct_of_stake": round(float(amount_inr / bet.stake_inr), 4)}
            outcome = {"recorded": True, "status": bet.status, "salvaged_inr": str(bet.return_inr), "pnl_inr": str(bet.pnl_inr)}
        else:
            if bet.status != PlacedStatus.PENDING.value:
                raise WorkbenchRefusal("ALREADY_SETTLED", f"the bet is {bet.status.lower()}")
            policy = slm.StopLossPolicy.from_settings(settings)
            pct = policy.clamp(monitor.stop_loss_pct)
            offer = monitor.cashout_offer_inr
            value, source = (offer, "offer") if offer is not None else (monitor.fair_value_inr or bet.stake_inr, "fair_value")
            ticket = adapter.ticket(bet, floor=slm.floor(bet.stake_inr, pct), value=value, value_source=source, reason="Emergency cashout requested by you: cash out now.", now=now)
            monitor.is_active, monitor.pullout_triggered, monitor.pullout_reason, monitor.pullout_at = False, True, PulloutReason.MANUAL_USER_REQUEST.value, now
            monitor.detail = {**(monitor.detail or {}), "cashout_ticket": ticket.as_dict()}
            outcome = {"recorded": False, "ticket": ticket.as_dict()}
            alert = SentinelAlert(kind=AlertKind.TWIN_PULLOUT, severity=Severity.WARNING, source="manual_parlay", title=f"Emergency cashout · stake ₹{bet.stake_inr:,}"
                                  + (f" · {bet.booking_code}" if bet.booking_code else ""), body=ticket.text()[:4000], dedupe_key=f"twin:emergency:{bet.id}",
                                  detail={"bet_id": str(bet.id), "monitor_id": str(monitor.id), "ticket": ticket.as_dict()})
        await session.commit()
        credit = await vetting.developer_credit(session)
        view = inplay.monitor_view(monitor, bet)
    if alert is not None:
        await emit_alert(redis, settings, alert)
    return {**outcome, "shield": view, "developer_credit": credit}


# ================================================================ the accounts (skins)
async def accounts(session: AsyncSession) -> dict[str, Any]:
    """Each account book's Vault accounts: the balance the user last recorded, by currency (the skin's balance mirror)."""
    out: dict[str, Any] = {}
    for acc in (await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.is_active.is_(True)).order_by(VaultBookmakerAccount.priority))).scalars():
        book = acc.bookmaker_id.strip().casefold()
        if book not in ACCOUNT_BOOKS:
            continue
        entry = out.setdefault(book, {"accounts": 0, "balances": {}, "reserved": {}, "last_updated": None})
        entry["accounts"] += 1
        if acc.balance is not None:
            entry["balances"][acc.currency] = str(Decimal(entry["balances"].get(acc.currency, "0")) + acc.balance)
        entry["reserved"][acc.currency] = str(Decimal(entry["reserved"].get(acc.currency, "0")) + (acc.reserved or Decimal(0)))
        stamp = acc.updated_at.isoformat() if acc.updated_at else None
        entry["last_updated"] = max(filter(None, [entry["last_updated"], stamp]), default=None)
    return out
