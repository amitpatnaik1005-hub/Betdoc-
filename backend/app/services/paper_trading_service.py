"""
Paper Trading Sandbox Service (Group 80).
Executes mock bets with virtual INR capital against real-time odds, and settles them with the Group 79 sport adapters.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

* **Placing**: the stake leaves the virtual balance at once (``Balance_t = Balance_{t-1} - Stake``), the slip is
  executed by ASHVATTHAMA on the target book (Parimatch by default) at the live price it can get, and the bet records
  the requested and executed odds, FA-4's sport Kelly stake, pillar 15's confidence when supplied and FA-2's
  discipline read (a disciplined paper bet earns ``DISCIPLINED_BET`` XP, once). Placing is idempotent per
  ``client_ref``; CHANAKYA's kill switch halts paper trading too.
* **Settling**: each leg through its sport adapter's ``settle_market`` (WON, LOST, VOID/PUSH, HALF_WON, HALF_LOST);
  a parlay multiplies its legs' multipliers (WON: odds, HALF_WON: (odds + 1) / 2, VOID: 1, HALF_LOST: 1/2, LOST: 0).
  The payout returns to the balance (``+ Payout``); a winning bet writes its Section 115BBJ entry (30% TDS on the
  net winnings) and carries the post-tax payout beside the gross one. The beat settles pending bets from the
  recorded final scores (``fixture_scores``); markets a final score cannot settle are settled by hand.
* **Ledger**: every movement writes the balance and the equity (balance + open stakes at cost) after it, the curve
  the drawdown and the daily Sharpe ratio are measured on. A reset starts a new epoch: counters zero, pending bets
  of the old epoch are voided without credit, and statistics read the current epoch only.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.domain.cfo import growth_math as gm
from app.domain.paper_trading import metrics as pm
from app.domain.paper_trading.legs import LegTranslationError, translate
from app.domain.paper_trading.tax import financial_year, section_115bbj
from app.domain.sports import discipline as dsc
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.errors import SportNotFoundError
from app.domain.sports.registry import SportAdapterRegistry
from app.domain.sports.settlement import SettlementInputError
from app.domain.sports.structures import leg_multiplier
from app.domain.sports.twin_bridge import sport_kelly_sizing
from app.models.never_forget import XPActionType
from app.models.paper_trading import (
    LedgerKind,
    PaperAccount,
    PaperBet,
    PaperBetSource,
    PaperBetStatus,
    PaperLedgerEntry,
    PaperStructure,
    TaxLedgerEntry,
)
from app.models.user_bets_ledger import FixtureScore, ScoreStatus
from app.schemas.bet_types import LegStatus
from app.services import ashvatthama
from app.services.twin.vetting import kill_switch_state
from app.services.twin.xp_engine import Award, award

logger = logging.getLogger("betdoc.paper_trading")

PAISA = Decimal("0.01")
ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
WIN_STATUSES = frozenset({PaperBetStatus.WON.value, PaperBetStatus.HALF_WON.value})
LOSS_STATUSES = frozenset({PaperBetStatus.LOST.value, PaperBetStatus.HALF_LOST.value})
NEUTRAL_STATUSES = frozenset({PaperBetStatus.PUSH.value, PaperBetStatus.VOID.value})
SCORE_VOID = frozenset({ScoreStatus.ABANDONED.value})


class PaperTradingError(ValueError):
    def __init__(self, code: str, message: str, *, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.receipt = receipt


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def _insert(session: AsyncSession, model: Any) -> Any:
    return (postgresql.insert(model) if session.get_bind().dialect.name == "postgresql" else sqlite.insert(model)).on_conflict_do_nothing()


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


# ================================================================ requests
@dataclass
class LegRequest:
    sport_key: str
    match_id: str
    market_type: str
    selection: str
    odds: Decimal
    line: float | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    event_name: str = ""
    quote_market: str | None = None
    quote_selection: str | None = None


@dataclass
class PlaceRequest:
    legs: list[LegRequest]
    stake: Decimal
    target_bookmaker: str = "parimatch"
    source: str = PaperBetSource.MANUAL.value
    strategy: str | None = None
    fair_probability: float | None = None
    confidence_score: float | None = None
    client_ref: str | None = None
    event_name: str | None = None


def leg_from_oracle(sport_key: str | None, match_id: str, market: str, selection: str, odds: Decimal, event_name: str = "") -> LegRequest:
    """An Ashoka slip leg (platform market grammar) as a settleable paper leg that still snaps the book's price."""
    t = translate(sport_key, market, selection)
    return LegRequest(t.sport_key, match_id, t.market_type, t.selection, odds, t.line, {}, event_name, t.quote_market, t.quote_selection)


# ================================================================ accounts
async def get_or_create_account(session: AsyncSession, settings: Settings, user_id: uuid.UUID, now: datetime | None = None, *,
                                lock: bool = False) -> PaperAccount:
    """The user's sandbox, created with ₹1,00,000 (``PAPER_DEFAULT_BALANCE_INR``) on first use."""
    moment = now or datetime.now(UTC)
    initial = money(settings.PAPER_DEFAULT_BALANCE_INR)
    account_id = uuid.uuid4()
    created = (await session.execute(_insert(session, PaperAccount).values(
        id=account_id, user_id=user_id, currency="INR", virtual_balance=initial, initial_balance=initial, total_paper_bets=0, won_bets=0, lost_bets=0,
        push_bets=0, cashed_out_bets=0, total_staked=ZERO, total_returned=ZERO, total_pnl=ZERO, roi_pct=0.0, total_tds=ZERO, peak_equity=initial,
        max_drawdown_pct=0.0, resets=0, is_active=True, created_at=moment, updated_at=moment,
    ))).rowcount
    if created:
        session.add(PaperLedgerEntry(account_id=account_id, kind=LedgerKind.OPEN.value, amount=initial, balance_after=initial, equity_after=initial,
                                     epoch=0, created_at=moment))
        await session.flush()
    query = select(PaperAccount).where(PaperAccount.user_id == user_id).execution_options(populate_existing=True)
    return (await session.execute(query.with_for_update() if lock else query)).scalar_one()


async def open_stakes(session: AsyncSession, account: PaperAccount) -> Decimal:
    total = await session.scalar(select(func.coalesce(func.sum(PaperBet.stake_amount), 0)).where(
        PaperBet.account_id == account.id, PaperBet.status == PaperBetStatus.PENDING.value))
    return money(Decimal(total or 0))


async def _ledger(session: AsyncSession, account: PaperAccount, kind: LedgerKind, amount: Decimal, bet: PaperBet | None, now: datetime) -> None:
    await session.flush()
    equity = money(account.virtual_balance + await open_stakes(session, account))
    session.add(PaperLedgerEntry(account_id=account.id, bet_id=None if bet is None else bet.id, kind=kind.value, amount=money(amount),
                                 balance_after=money(account.virtual_balance), equity_after=equity, epoch=account.resets, created_at=now))
    account.peak_equity = max(account.peak_equity, equity)
    if account.peak_equity > 0:
        drawdown = float((account.peak_equity - equity) / account.peak_equity * 100)
        account.max_drawdown_pct = round(max(account.max_drawdown_pct, drawdown), 4)


# ================================================================ placing
def _validate_leg(leg: LegRequest) -> BaseSportAdapter:
    try:
        adapter = SportAdapterRegistry.get_adapter(leg.sport_key)
        adapter.canonical_market(leg.market_type)
    except SportNotFoundError as exc:
        raise PaperTradingError("UNKNOWN_SPORT", exc.message) from exc
    except SettlementInputError as exc:
        raise PaperTradingError("UNKNOWN_MARKET", str(exc)) from exc
    if leg.odds <= 1:
        raise PaperTradingError("INVALID_ODDS", "decimal odds must be above 1")
    return adapter


async def _history(session: AsyncSession, account: PaperAccount) -> list[dsc.BetRecord]:
    rows = (await session.execute(select(PaperBet).where(PaperBet.account_id == account.id).order_by(PaperBet.placed_at.desc()).limit(200))).scalars()
    status_map = {PaperBetStatus.PUSH.value: "VOID", PaperBetStatus.CASHED_OUT.value: "CASH_OUT"}
    return [dsc.BetRecord(placed_at=_aware(b.placed_at), sport_key=b.sport_key, stake=float(b.stake_amount), odds=float(b.executed_odds),
                          result=status_map.get(b.status, b.status), pnl=None if b.status == PaperBetStatus.PENDING.value else float(b.pnl))
            for b in rows]


async def place_paper_bet(session: AsyncSession, redis: Redis | None, settings: Settings, user_id: uuid.UUID, request: PlaceRequest,
                          now: datetime | None = None) -> tuple[PaperBet, bool]:
    """Execute a paper slip. Returns (bet, created); a repeated ``client_ref`` returns the first execution."""
    moment = now or datetime.now(UTC)
    if not request.legs:
        raise PaperTradingError("NO_LEGS", "a paper bet needs at least one leg")
    if len(request.legs) > 12:
        raise PaperTradingError("TOO_MANY_LEGS", "a paper parlay takes at most 12 legs")
    if len({leg.match_id for leg in request.legs}) != len(request.legs):
        raise PaperTradingError("RELATED_LEGS", "a parlay cannot hold two legs on one match")
    for leg in request.legs:
        _validate_leg(leg)
    stake = money(request.stake)
    if stake <= 0:
        raise PaperTradingError("INVALID_STAKE", "the stake must be positive")
    if await kill_switch_state(session, redis, settings) is True:
        raise PaperTradingError("TRADING_HALTED", "CHANAKYA's kill switch is engaged: paper and real trading are halted")

    account = await get_or_create_account(session, settings, user_id, moment, lock=True)
    client_ref = (request.client_ref or uuid.uuid4().hex)[:64]
    existing = (await session.execute(select(PaperBet).where(PaperBet.account_id == account.id, PaperBet.client_ref == client_ref))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if not account.is_active:
        raise PaperTradingError("ACCOUNT_INACTIVE", "the paper account is deactivated")
    if stake > account.virtual_balance:
        raise PaperTradingError("INSUFFICIENT_FUNDS", f"Insufficient virtual balance (₹{account.virtual_balance:,.2f}) for the stake (₹{stake:,.2f})")
    cap = money(account.virtual_balance * Decimal(str(settings.PAPER_MAX_STAKE_PCT)))
    if stake > cap:
        raise PaperTradingError("STAKE_OVER_CAP", f"one paper stake is at most {settings.PAPER_MAX_STAKE_PCT:.0%} of the balance (₹{cap:,.2f})")

    multiple = len(request.legs) > 1
    try:
        receipt = await ashvatthama.execute_paper(redis, settings, target=request.target_bookmaker, stake_inr=stake, now=moment, multiple=multiple,
                                                  legs=[ashvatthama.ExecLeg(leg.match_id, leg.odds, leg.quote_market, leg.quote_selection)
                                                        for leg in request.legs])
    except ashvatthama.ExecutionRefused as exc:
        raise PaperTradingError(exc.receipt.reason.split(":")[0] if exc.receipt.reason else "REFUSED", exc.receipt.reason or "refused",
                                receipt=exc.receipt.as_dict()) from exc
    except ValueError as exc:
        raise PaperTradingError("UNKNOWN_VENUE", str(exc)) from exc
    executed = receipt.executed_odds or receipt.requested_odds

    first = request.legs[0]
    legs_json = []
    for leg, snapped in zip(request.legs, receipt.legs, strict=True):
        leg_odds = Decimal(snapped["live_odds"]) if snapped["live_odds"] is not None else leg.odds
        legs_json.append({"sport_key": SportAdapterRegistry.get_adapter(leg.sport_key).sport_key, "match_id": leg.match_id,
                          "event_name": leg.event_name, "market_type": leg.market_type, "selection": leg.selection, "line": leg.line,
                          "params": dict(leg.params), "odds": str(leg.odds), "executed_odds": str(leg_odds), "status": LegStatus.PENDING.value,
                          "quote_market": leg.quote_market, "quote_selection": leg.quote_selection})
    sport = legs_json[0]["sport_key"]
    event = request.event_name or (first.event_name if not multiple else f"{len(request.legs)}-leg parlay: " + " / ".join(
        leg.event_name or leg.match_id for leg in request.legs))[:255]

    kelly = None
    if request.fair_probability is not None and 0 < request.fair_probability < 1:
        equity = account.virtual_balance + await open_stakes(session, account)
        drawdown = float((account.peak_equity - equity) / account.peak_equity) if account.peak_equity > 0 else 0.0
        sizing = sport_kelly_sizing(sport, request.fair_probability, float(executed), account.virtual_balance, max(0.0, drawdown),
                                    gm.SizingPolicy.from_settings(settings), PAISA, overrides=settings.SPORTS_KELLY_MULTIPLIERS)
        kelly = money(sizing.stake)
    report = dsc.assess(dsc.CandidateBet(moment, sport, float(stake), float(executed), request.fair_probability,
                                         None if kelly is None else float(kelly)), await _history(session, account))

    bet = PaperBet(account_id=account.id, client_ref=client_ref, structure=(PaperStructure.PARLAY if multiple else PaperStructure.SINGLE).value,
                   sport_key=sport, match_id=first.match_id if not multiple else ",".join(leg.match_id for leg in request.legs)[:128],
                   event_name=event[:255] or first.match_id, market_type=first.market_type if not multiple else "parlay",
                   selection=first.selection if not multiple else f"{len(request.legs)} legs", line=first.line if not multiple else None,
                   params=dict(first.params) if not multiple else {}, legs=legs_json, odds=receipt.requested_odds, executed_odds=executed,
                   stake_amount=stake, potential_payout=money(stake * executed), status=PaperBetStatus.PENDING.value, pnl=ZERO,
                   tds_amount=ZERO, target_bookmaker=receipt.venue, source=request.source, strategy=request.strategy,
                   fair_probability=request.fair_probability, confidence_score=request.confidence_score, kelly_stake=kelly,
                   execution=receipt.as_dict(), discipline=report.as_dict(), placed_at=moment)
    session.add(bet)
    account.virtual_balance = money(account.virtual_balance - stake)
    account.total_paper_bets += 1
    account.updated_at = moment
    await session.flush()
    await _ledger(session, account, LedgerKind.STAKE, -stake, bet, moment)
    if report.disciplined:
        paid = await award(session, settings, Award(user_id, XPActionType.DISCIPLINED_BET, f"paper-bet:{bet.id}",
                                                    f"Disciplined paper bet on {sport}: edge {report.edge:.1%} within its Kelly stake",
                                                    {"paper_bet_id": str(bet.id), "sport_key": sport, "edge": report.edge}), moment)
        bet.discipline = {**report.as_dict(), "xp_awarded": paid}
    await session.commit()
    await session.refresh(bet)
    logger.info("PAPER: %s placed %s @ %s for ₹%s on %s", user_id, bet.event_name, executed, stake, receipt.venue)
    return bet, True


# ================================================================ settling
def _payout(odds: Decimal, stake: Decimal, status: LegStatus) -> Decimal:
    return money(stake * leg_multiplier(float(odds), status))


def _bet_status(status: LegStatus, *, abandoned: bool) -> str:
    if status is LegStatus.VOID:
        return PaperBetStatus.VOID.value if abandoned else PaperBetStatus.PUSH.value
    return status.value


async def _finalise(session: AsyncSession, settings: Settings, account: PaperAccount, bet: PaperBet, status: str, payout: Decimal, now: datetime,
                    notes: str | None) -> PaperBet:
    payout = money(payout)
    bet.status = status
    bet.payout = payout
    bet.pnl = money(payout - bet.stake_amount)
    bet.settled_at = now
    bet.settlement_notes = (notes or "Settled through the Group 79 sport adapters")[:2000]
    account.virtual_balance = money(account.virtual_balance + payout)
    account.total_staked = money(account.total_staked + bet.stake_amount)
    account.total_returned = money(account.total_returned + payout)
    account.total_pnl = money(account.total_pnl + bet.pnl)
    if status in WIN_STATUSES:
        account.won_bets += 1
    elif status in LOSS_STATUSES:
        account.lost_bets += 1
    elif status in NEUTRAL_STATUSES:
        account.push_bets += 1
    else:
        account.cashed_out_bets += 1
    account.roi_pct = pm.roi_pct(float(account.total_pnl), float(account.initial_balance))
    bet.net_post_tax_payout = payout
    if bet.pnl > 0:
        rate = Decimal(str(settings.ORACLE_TDS_RATE))
        tax = section_115bbj(payout, bet.stake_amount, rate)
        bet.tds_amount = tax.tds
        bet.net_post_tax_payout = tax.net_post_tax_payout
        account.total_tds = money(account.total_tds + tax.tds)
        await session.execute(_insert(session, TaxLedgerEntry).values(
            id=uuid.uuid4(), user_id=account.user_id, paper_bet_id=bet.id, mode="PAPER", gross_payout=tax.gross_payout, stake=tax.stake,
            net_winnings=tax.net_winnings, tds_rate=float(rate), tds_amount=tax.tds, net_post_tax_payout=tax.net_post_tax_payout, currency="INR",
            jurisdiction="IN", section="115BBJ", financial_year=financial_year(now), created_at=now,
        ))
    account.updated_at = now
    kind = LedgerKind.CASHOUT if status == PaperBetStatus.CASHED_OUT.value else LedgerKind.PAYOUT
    await _ledger(session, account, kind, payout, bet, now)
    return bet


async def _load(session: AsyncSession, bet_id: uuid.UUID, user_id: uuid.UUID | None) -> tuple[PaperBet, PaperAccount]:
    bet = (await session.execute(select(PaperBet).where(PaperBet.id == bet_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if bet is None:
        raise PaperTradingError("NOT_FOUND", "no such paper bet")
    account = (await session.execute(select(PaperAccount).where(PaperAccount.id == bet.account_id).with_for_update()
                                     .execution_options(populate_existing=True))).scalar_one()
    if user_id is not None and account.user_id != user_id:
        raise PaperTradingError("NOT_FOUND", "no such paper bet")
    return bet, account


async def get_bet(session: AsyncSession, bet_id: uuid.UUID, user_id: uuid.UUID) -> PaperBet:
    """The user's paper bet (no lock: a read)."""
    bet = (await session.execute(select(PaperBet).join(PaperAccount, PaperAccount.id == PaperBet.account_id)
                                 .where(PaperBet.id == bet_id, PaperAccount.user_id == user_id))).scalar_one_or_none()
    if bet is None:
        raise PaperTradingError("NOT_FOUND", "no such paper bet")
    return bet


def _settle_legs(bet: PaperBet, results: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Settle every pending leg whose match has a result. Returns the legs and whether any match was abandoned."""
    legs = [dict(leg) for leg in bet.legs]
    abandoned = False
    for leg in legs:
        if leg["status"] != LegStatus.PENDING.value or leg["match_id"] not in results:
            continue
        result = results[leg["match_id"]]
        adapter = SportAdapterRegistry.get_adapter(leg["sport_key"])
        status = adapter.settle_market(leg["market_type"], result, leg["selection"], leg.get("line"), leg.get("params") or {})
        leg["status"] = status.value
        if str(result.get("status", "")).strip().upper() in SCORE_VOID:
            abandoned = True
    return legs, abandoned


async def settle_with_results(session: AsyncSession, settings: Settings, bet_id: uuid.UUID, results: Mapping[str, Mapping[str, Any]], *,
                              user_id: uuid.UUID | None = None, notes: str | None = None, now: datetime | None = None) -> PaperBet:
    """Settle a paper bet from match results keyed by match id (a single takes its own match's)."""
    moment = now or datetime.now(UTC)
    bet, account = await _load(session, bet_id, user_id)
    if bet.status != PaperBetStatus.PENDING.value:
        return bet
    try:
        legs, abandoned = _settle_legs(bet, results)
    except SettlementInputError as exc:
        raise PaperTradingError("RESULT_INSUFFICIENT", str(exc)) from exc
    bet.legs = legs
    statuses = [LegStatus(leg["status"]) for leg in legs]
    if bet.structure == PaperStructure.SINGLE.value:
        if statuses[0] is LegStatus.PENDING:
            raise PaperTradingError("RESULT_MISSING", f"no result for match {legs[0]['match_id']}")
        await _finalise(session, settings, account, bet, _bet_status(statuses[0], abandoned=abandoned),
                        _payout(bet.executed_odds, bet.stake_amount, statuses[0]), moment, notes)
    elif LegStatus.LOST in statuses:
        await _finalise(session, settings, account, bet, PaperBetStatus.LOST.value, ZERO, moment, notes or "A parlay leg lost")
    elif LegStatus.PENDING not in statuses:
        product = ONE
        for leg, status in zip(legs, statuses, strict=True):
            product *= leg_multiplier(float(leg["executed_odds"]), status)
        payout = money(bet.stake_amount * product)
        if all(s is LegStatus.VOID for s in statuses):
            status_value = PaperBetStatus.VOID.value if abandoned else PaperBetStatus.PUSH.value
        elif payout > bet.stake_amount:
            status_value = PaperBetStatus.WON.value if all(s in (LegStatus.WON, LegStatus.VOID) for s in statuses) else PaperBetStatus.HALF_WON.value
        elif payout == bet.stake_amount:
            status_value = PaperBetStatus.PUSH.value
        else:
            status_value = PaperBetStatus.HALF_LOST.value
        await _finalise(session, settings, account, bet, status_value, payout, moment, notes)
    await session.commit()
    await session.refresh(bet)
    return bet


async def settle_paper_bet(session: AsyncSession, settings: Settings, bet_id: uuid.UUID, match_result: Mapping[str, Any], *,
                           user_id: uuid.UUID | None = None, notes: str | None = None, now: datetime | None = None) -> PaperBet:
    """A single's settlement from its match's result (the brief's interface)."""
    bet, _ = await _load(session, bet_id, user_id)
    results = {leg["match_id"]: match_result for leg in bet.legs}
    return await settle_with_results(session, settings, bet_id, results, user_id=user_id, notes=notes, now=now)


def score_result(score: FixtureScore) -> dict[str, Any]:
    """A recorded final score as the result dict every adapter's ``normalize_result`` reads."""
    return {"home_score": score.home_goals, "away_score": score.away_goals, "home_goals": score.home_goals, "away_goals": score.away_goals,
            "home_runs": score.home_goals, "away_runs": score.away_goals, "home_name": score.home, "away_name": score.away,
            "status": "abandoned" if score.status in SCORE_VOID else score.status.lower()}


async def auto_settle(sessions: async_sessionmaker[AsyncSession], settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """Settle every pending paper bet whose matches have a recorded final (or abandoned) score."""
    moment = now or datetime.now(UTC)
    report: dict[str, Any] = {"pending": 0, "settled": 0, "needs_manual": [], "waiting": 0}
    async with sessions() as session:
        pending = (await session.execute(select(PaperBet.id, PaperBet.legs).where(PaperBet.status == PaperBetStatus.PENDING.value))).all()
        report["pending"] = len(pending)
        ids = {leg["match_id"] for _, legs in pending for leg in legs}
        scores = {s.fixture_id: s for s in (await session.execute(select(FixtureScore).where(
            FixtureScore.fixture_id.in_(ids), FixtureScore.status.in_([ScoreStatus.FINAL.value, ScoreStatus.ABANDONED.value])))).scalars()} if ids else {}
    for bet_id, legs in pending:
        results = {leg["match_id"]: score_result(scores[leg["match_id"]]) for leg in legs if leg["match_id"] in scores
                   and (scores[leg["match_id"]].status in SCORE_VOID or scores[leg["match_id"]].home_goals is not None)}
        if not results:
            report["waiting"] += 1
            continue
        async with sessions() as session:
            try:
                bet = await settle_with_results(session, settings, bet_id, results, notes="Auto-settled from the recorded final score", now=moment)
            except PaperTradingError as exc:
                await session.rollback()
                if exc.code == "RESULT_INSUFFICIENT":
                    report["needs_manual"].append({"bet_id": str(bet_id), "reason": exc.message})
                else:
                    report["waiting"] += 1
                continue
            if bet.status != PaperBetStatus.PENDING.value:
                report["settled"] += 1
            else:
                report["waiting"] += 1
    return report


# ================================================================ FA-1 / FA-5: live check and cash-out
async def cash_out(session: AsyncSession, settings: Settings, user_id: uuid.UUID, bet_id: uuid.UUID, amount: Decimal,
                   now: datetime | None = None) -> PaperBet:
    """Take a virtual cash-out (a book's offer, or the twin's fair value) on an open paper bet."""
    moment = now or datetime.now(UTC)
    bet, account = await _load(session, bet_id, user_id)
    if bet.status != PaperBetStatus.PENDING.value:
        raise PaperTradingError("ALREADY_SETTLED", "the paper bet is already settled")
    value = money(amount)
    if value < 0 or value > bet.potential_payout:
        raise PaperTradingError("INVALID_CASHOUT", f"a cash-out lies between ₹0 and the potential payout ₹{bet.potential_payout:,.2f}")
    await _finalise(session, settings, account, bet, PaperBetStatus.CASHED_OUT.value, value, moment, f"Cashed out at ₹{value:,.2f}")
    await session.commit()
    await session.refresh(bet)
    return bet


# ================================================================ reset
async def reset_account(session: AsyncSession, settings: Settings, user_id: uuid.UUID, initial_balance: Decimal | None = None,
                        now: datetime | None = None) -> PaperAccount:
    """A fresh slate: a new epoch at ``initial_balance`` (default ₹1,00,000); open bets of the old epoch are voided unpaid."""
    moment = now or datetime.now(UTC)
    initial = money(initial_balance if initial_balance is not None else settings.PAPER_DEFAULT_BALANCE_INR)
    if initial <= 0:
        raise PaperTradingError("INVALID_BALANCE", "the starting balance must be positive")
    account = await get_or_create_account(session, settings, user_id, moment, lock=True)
    for bet in (await session.execute(select(PaperBet).where(PaperBet.account_id == account.id, PaperBet.status == PaperBetStatus.PENDING.value))).scalars():
        bet.status = PaperBetStatus.VOID.value
        bet.payout = ZERO
        bet.settled_at = moment
        bet.settlement_notes = "Cancelled by an account reset (the stake left with the old epoch)"
    account.resets += 1
    account.virtual_balance = initial
    account.initial_balance = initial
    account.total_paper_bets = account.won_bets = account.lost_bets = account.push_bets = account.cashed_out_bets = 0
    account.total_staked = account.total_returned = account.total_pnl = account.total_tds = ZERO
    account.roi_pct = 0.0
    account.peak_equity = initial
    account.max_drawdown_pct = 0.0
    account.last_reset_at = moment
    account.updated_at = moment
    await _ledger(session, account, LedgerKind.RESET, initial, None, moment)
    await session.commit()
    await session.refresh(account)
    logger.info("PAPER: %s reset to ₹%s (epoch %d)", user_id, initial, account.resets)
    return account


# ================================================================ reading
def _epoch_filter(account: PaperAccount) -> Any:
    return PaperBet.placed_at >= account.last_reset_at if account.last_reset_at is not None else True


async def list_bets(session: AsyncSession, account: PaperAccount, *, status: str | None = None, limit: int = 100) -> list[PaperBet]:
    query = select(PaperBet).where(PaperBet.account_id == account.id, _epoch_filter(account))
    if status:
        query = query.where(PaperBet.status == status)
    return list((await session.execute(query.order_by(PaperBet.placed_at.desc()).limit(limit))).scalars())


async def performance(session: AsyncSession, settings: Settings, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """Win rate, net P&L, ROI, drawdown, profit factor, Sharpe, significance, per sport and per strategy (current epoch)."""
    moment = now or datetime.now(UTC)
    account = await get_or_create_account(session, settings, user_id, moment)
    await session.commit()
    bets = list((await session.execute(select(PaperBet).where(PaperBet.account_id == account.id, _epoch_filter(account))
                                       .order_by(PaperBet.placed_at))).scalars())
    settled = [b for b in bets if b.status != PaperBetStatus.PENDING.value and b.settlement_notes != "Cancelled by an account reset (the stake left with the old epoch)"]
    decided = [b for b in settled if b.status in WIN_STATUSES | LOSS_STATUSES]
    gross_profits = float(sum((b.pnl for b in settled if b.pnl > 0), ZERO))
    gross_losses = float(-sum((b.pnl for b in settled if b.pnl < 0), ZERO))
    ledger = (await session.execute(select(PaperLedgerEntry.created_at, PaperLedgerEntry.equity_after).where(
        PaperLedgerEntry.account_id == account.id, PaperLedgerEntry.epoch == account.resets).order_by(PaperLedgerEntry.created_at))).all()
    equity_points: list[tuple[date, float]] = [(_aware(at).date(), float(eq)) for at, eq in ledger]
    sharpe = pm.sharpe_ratio(pm.daily_closes(equity_points), settings.PAPER_RISK_FREE_RATE)
    significance = pm.forward_test_significance([float(b.pnl / b.stake_amount) for b in decided], min_sample=settings.PAPER_MIN_SAMPLE,
                                                alpha=settings.PAPER_SIGNIFICANCE_ALPHA)
    by_sport: dict[str, dict[str, Any]] = defaultdict(lambda: {"bets": 0, "won": 0, "lost": 0, "staked": 0.0, "pnl": 0.0})
    by_strategy: dict[str, dict[str, Any]] = defaultdict(lambda: {"bets": 0, "won": 0, "lost": 0, "staked": 0.0, "pnl": 0.0})
    for b in settled:
        for bucket in (by_sport[b.sport_key], by_strategy[b.strategy or b.source.lower()]):
            bucket["bets"] += 1
            bucket["won"] += int(b.status in WIN_STATUSES)
            bucket["lost"] += int(b.status in LOSS_STATUSES)
            bucket["staked"] += float(b.stake_amount)
            bucket["pnl"] += float(b.pnl)
    for bucket in (*by_sport.values(), *by_strategy.values()):
        bucket["win_rate_pct"] = pm.win_rate_pct(bucket["won"], bucket["lost"])
        bucket["yield_pct"] = round(bucket["pnl"] / bucket["staked"] * 100, 4) if bucket["staked"] else 0.0
        bucket["pnl"] = round(bucket["pnl"], 2)
        bucket["staked"] = round(bucket["staked"], 2)
    brier_pairs = [(b.fair_probability, 1.0 if b.status in WIN_STATUSES else 0.0) for b in decided
                   if b.fair_probability is not None and b.structure == PaperStructure.SINGLE.value]
    pending = [b for b in bets if b.status == PaperBetStatus.PENDING.value]
    won, lost = account.won_bets, account.lost_bets
    return {
        "virtual_balance": float(account.virtual_balance), "initial_balance": float(account.initial_balance),
        "equity": float(money(account.virtual_balance + sum((b.stake_amount for b in pending), ZERO))),
        "total_pnl": float(account.total_pnl), "net_pnl_after_tds": float(money(account.total_pnl - account.total_tds)),
        "total_tds": float(account.total_tds), "roi_pct": account.roi_pct,
        "yield_pct": round(float(account.total_pnl / account.total_staked * 100), 4) if account.total_staked > 0 else 0.0,
        "win_rate_pct": pm.win_rate_pct(won, lost), "total_bets": account.total_paper_bets, "won_bets": won, "lost_bets": lost,
        "push_bets": account.push_bets, "cashed_out_bets": account.cashed_out_bets, "pending_bets": len(pending),
        "open_exposure": float(sum((b.stake_amount for b in pending), ZERO)),
        "max_drawdown_pct": account.max_drawdown_pct, "profit_factor": pm.profit_factor(gross_profits, gross_losses), "sharpe_ratio": sharpe,
        "brier_score": pm.brier_score(brier_pairs), "significance": significance.as_dict(),
        "status_summary": pm.status_summary(float(account.total_pnl), len(decided), significance, min_sample=settings.PAPER_MIN_SAMPLE),
        "live_ready": significance.proven, "by_sport": dict(by_sport), "by_strategy": dict(by_strategy),
        "equity_curve": [{"at": _aware(at).isoformat(), "equity": float(eq)} for at, eq in ledger[-500:]],
        "epoch": account.resets, "developer_credit": DEVELOPER_CREDIT,
    }


async def tax_ledger(session: AsyncSession, user_id: uuid.UUID, fy: str | None = None) -> dict[str, Any]:
    query = select(TaxLedgerEntry).where(TaxLedgerEntry.user_id == user_id)
    if fy:
        query = query.where(TaxLedgerEntry.financial_year == fy)
    rows = list((await session.execute(query.order_by(TaxLedgerEntry.created_at.desc()).limit(1000))).scalars())
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"entries": 0, "net_winnings": 0.0, "tds": 0.0, "gross_payout": 0.0})
    for r in rows:
        t = totals[r.financial_year]
        t["entries"] += 1
        t["net_winnings"] = round(t["net_winnings"] + float(r.net_winnings), 2)
        t["tds"] = round(t["tds"] + float(r.tds_amount), 2)
        t["gross_payout"] = round(t["gross_payout"] + float(r.gross_payout), 2)
    return {"entries": rows, "by_financial_year": dict(totals), "developer_credit": DEVELOPER_CREDIT}


def adapter_for(sport_key: str) -> BaseSportAdapter:
    return SportAdapterRegistry.get_adapter(sport_key)


def oracle_legs(raw_legs: Sequence[Mapping[str, Any]]) -> list[LegRequest]:
    try:
        return [leg_from_oracle(leg.get("sport_key"), str(leg["match_id"]), str(leg["market"]), str(leg["selection"]), Decimal(str(leg["odds"])),
                                str(leg.get("event_name") or "")) for leg in raw_legs]
    except LegTranslationError as exc:
        raise PaperTradingError("UNKNOWN_MARKET", str(exc)) from exc


# ================================================================ the brief's service interface
class PaperTradingService:
    """
    Paper Trading Sandbox Service: the brief's object interface over the functions above.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    def __init__(self, settings: Settings | None = None, redis: Redis | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis = redis

    async def get_or_create_account(self, db: AsyncSession, user_id: uuid.UUID) -> PaperAccount:
        account = await get_or_create_account(db, self.settings, user_id)
        await db.commit()
        return account

    async def place_paper_bet(self, db: AsyncSession, user_id: uuid.UUID, sport_key: str, match_id: str, event_name: str, market_type: str,
                              selection: str, odds: float | Decimal, stake_amount: float | Decimal, target_bookmaker: str = "parimatch",
                              *, line: float | None = None, fair_probability: float | None = None, client_ref: str | None = None) -> PaperBet:
        leg = LegRequest(sport_key, match_id, market_type, selection, Decimal(str(odds)), line, {}, event_name)
        bet, _ = await place_paper_bet(db, self.redis, self.settings, user_id, PlaceRequest(
            [leg], Decimal(str(stake_amount)), target_bookmaker, fair_probability=fair_probability, client_ref=client_ref))
        return bet

    async def settle_paper_bet(self, db: AsyncSession, bet_id: uuid.UUID, match_result: Mapping[str, Any], settlement_notes: str | None = None) -> PaperBet:
        return await settle_paper_bet(db, self.settings, bet_id, match_result, notes=settlement_notes)

    async def reset_account(self, db: AsyncSession, user_id: uuid.UUID, initial_balance: float | Decimal | None = None) -> PaperAccount:
        return await reset_account(db, self.settings, user_id, None if initial_balance is None else Decimal(str(initial_balance)))

    async def get_performance_stats(self, db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
        return await performance(db, self.settings, user_id)

