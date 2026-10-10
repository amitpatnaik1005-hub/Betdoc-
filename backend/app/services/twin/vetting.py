"""The twin's fortress run: gather the evidence, run the 15 pillars, keep the audit (Groups 72, 75).

``vet`` takes a slip as Ashoka's slips carry it (leg ids and a structure), re-reads every leg from the live
market, keeps only the retail books' prices (``TWIN_RETAIL_BOOKS``), lets the parlay engine price and
simulate it at the best retail book quoting every leg, then gathers what the context pillars need:

* the sharp books' whole market per leg (pillar 7), Aryabhata's steam flags (6), the fixture evidence
  store (2-6, 9-11), BetDoc's own measured public share per selection (6), live public-trap parlays (8),
  the calibration store's model weights (1);
* the user's bankroll (the caller's figure, else the CFO main account), rolling drawdown from their settled
  bets, the models' Brier skill and KUMBHA's halt latch (13, sized by the CFO's policy, Group 76); the kill
  switch, from Redis and the Control Panel's emergency stop (14);
* the Never-Forget vault, read from the database on every run (15). Its vetoes are kept as prevention
  rows, and a vetted slip (and a run of disciplined days) earns the user XP (Group 75).

Every run is an audit row. A vetted slip pages the user's phone through the Sentinel; a change of drawdown
regime pages through KUMBHA's advisory (CRITICAL at the halt line). ``confirm`` re-runs pillar 14 against freshly read prices just before the slip
is placed or routed; ``record_placed`` puts a placed slip in Ashoka's ledger (one P&L) and ``route``
hands a vetted single to the Smart Order Router (Pathway B).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.venue import VenueStakeRules
from app.core.config import Settings
from app.domain.bookmakers.adapters import canonical_bookmaker
from app.domain.oracle import fortress
from app.domain.oracle import never_forget as nf
from app.domain.oracle.parlay_engine import LegCandidate, ParlayEngine, SlipCandidate, SlipKind
from app.models.cfo_vault import BankrollAccount
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.digital_twin import TwinVettingAudit
from app.models.popular_picks import PopularParlayModel, TrendCategory
from app.models.sentinel import Severity
from app.models.user_bets_ledger import PlacedBookmaker, PlacedStatus, PlacedStructure, UserPlacedBet
from app.schemas.ashoka import PlaceBetRequest, PlacedLegIn
from app.schemas.twin import LedgerFromAudit
from app.services import ashoka_market
from app.services import user_pnl_tracker as tracker
from app.services.risk_guard import kill_switch_engaged
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin.intel import model_weights, read_intel

logger = logging.getLogger("betdoc.twin")

ODDS_Q = Decimal("0.0001")
LIVE_LOOKBACK = timedelta(hours=3)  # started fixtures stay readable (the board keeps them while they play)


class TwinRefusal(Exception):
    status_code = 409
    reason = "TWIN_REFUSED"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class LegsNotQuoted(TwinRefusal):
    status_code = 410
    reason = "NO_LONGER_QUOTED"


class NoRetailBook(TwinRefusal):
    reason = "NO_RETAIL_BOOK"


def books(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(b for b in (canonical_bookmaker(x.strip()) for x in raw.split(",") if x.strip()) if b))


def retail_books(settings: Settings) -> tuple[str, ...]:
    return books(settings.TWIN_RETAIL_BOOKS)


def sharp_books(settings: Settings) -> tuple[str, ...]:
    return books(settings.TWIN_SHARP_BOOKS)


def _engine(settings: Settings, retail: Sequence[str] | None = None) -> ParlayEngine:
    """Ashoka's engine, priced over the retail books only (Kelly unscaled: the fortress sizes)."""
    return ParlayEngine(
        ashoka_market.thresholds(settings), paths=settings.ASHOKA_MC_PATHS, kelly_fraction=1.0, max_stake_pct=1.0, value_max_stake_pct=1.0,
        max_legs=settings.ASHOKA_MAX_CANDIDATE_LEGS, max_slips=settings.ASHOKA_MAX_SLIPS, priority=tuple(retail) if retail else retail_books(settings),
    )


# ================================================================ evidence
async def bankroll_for(session: AsyncSession, user_id: uuid.UUID, given: Decimal | None) -> Decimal | None:
    if given is not None:
        return given
    account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id, BankrollAccount.bot_id.is_(None)))
    return None if account is None else account.available_balance + account.exposure_balance


async def drawdown_for(session: AsyncSession, user_id: uuid.UUID, bankroll: Decimal | None, now: datetime, days: float) -> float:
    """The rolling drawdown of the user's own settled bets over the window."""
    if bankroll is None or bankroll <= 0:
        return 0.0
    rows = (await session.execute(
        select(UserPlacedBet.pnl_inr).where(
            UserPlacedBet.user_id == user_id, UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.pnl_inr.is_not(None),
            UserPlacedBet.settled_at >= now - timedelta(days=days),
        ).order_by(UserPlacedBet.settled_at, UserPlacedBet.id)
    )).scalars().all()
    return fortress.rolling_drawdown(bankroll, [Decimal(r) for r in rows])


async def kill_switch_state(session: AsyncSession, redis: Redis | None, settings: Settings) -> bool | None:
    """True when trading is halted (Redis switch or the emergency stop); None when Redis cannot say."""
    flag = await kill_switch_engaged(redis, settings)
    if flag is None:
        return None
    if flag:
        return True
    controls = await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
    return controls is not None and controls.max_daily_exposure <= 0


async def developer_credit(session: AsyncSession) -> str:
    controls = await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
    if controls is not None and controls.developer_name:
        return controls.developer_name
    return str(SystemSettingsModel.__table__.c.developer_name.default.arg)


async def public_traps(session: AsyncSession, legs: Sequence[LegCandidate], now: datetime) -> dict[str, list[str]]:
    """leg id -> titles of live PUBLIC_TRAP parlays that hold it."""
    wanted = {(leg.fixture_id, leg.market.key, leg.selection): leg.leg_id for leg in legs}
    rows = (await session.execute(
        select(PopularParlayModel).where(
            PopularParlayModel.is_active.is_(True), PopularParlayModel.category == TrendCategory.PUBLIC_TRAP.value, PopularParlayModel.expires_at > now,
        )
    )).scalars().all()
    out: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for leg in row.legs or []:
            leg_id = wanted.get((str(leg.get("match_id")), str(leg.get("market")), str(leg.get("selection"))))
            if leg_id is not None:
                out[leg_id].append(row.title)
    return dict(out)


async def steam_by_market(redis: Redis, settings: Settings, now: datetime) -> dict[tuple[str, str], set[str]] | None:
    """(fixture, market key) -> selections with sharp steam; None when the edges cannot be read."""
    from app.domain.oracle.markets import parse_market  # noqa: PLC0415
    from app.services.aryabhata_pipeline import read_active_edges  # noqa: PLC0415 - heavy module

    try:
        edges = await read_active_edges(redis, settings, now)
    except (RedisError, OSError, TimeoutError):
        return None
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for edge in edges:
        ref = parse_market(edge.market_type)
        if edge.is_steam_move and ref is not None:
            out[(edge.fixture_id, ref.key)].add(edge.selection)
    return dict(out)


# ================================================================ the run
async def _live_legs(redis: Redis, settings: Settings, leg_ids: Sequence[str], now: datetime) -> tuple[list[LegCandidate], dict[str, LegCandidate]]:
    fixtures = {leg_id.split("|", 1)[0] for leg_id in leg_ids}
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - LIVE_LOOKBACK, fixtures=fixtures)
    by_id = {c.leg_id: c for c in candidates}
    missing = [leg_id for leg_id in leg_ids if leg_id not in by_id]
    if missing:
        raise LegsNotQuoted("A leg of this slip is no longer quoted by the feeds", legs=missing)
    return [by_id[leg_id] for leg_id in leg_ids], by_id


def _retail_only(leg: LegCandidate, retail: Sequence[str]) -> LegCandidate:
    return dataclasses.replace(leg, quotes={b: q for b, q in leg.quotes.items() if b in retail})


def _sharp_market(leg: LegCandidate, by_id: dict[str, LegCandidate], sharp: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for selection in leg.market.selections:
        other = by_id.get(f"{leg.fixture_id}|{leg.market.key}|{selection}")
        out[selection] = {} if other is None else {b: q for b, q in other.quotes.items() if b in sharp}
    return out


def _slip_payload(slip: SlipCandidate, verdict: fortress.FortressVerdict, now: datetime) -> dict[str, Any]:
    """Ashoka's slip serialisation (legs, bookmaker views, Parimatch vs 1xBet, quick copy) at the twin's stake."""
    core = ashoka_market.slip_core(slip, now)
    core["badge"] = f"Twin fortress {verdict.passed}/{len(verdict.pillars)}" + (" · vetted" if verdict.is_vetted else "")
    stake = verdict.sizing.stake if verdict.sizing is not None else Decimal("0.00")
    # personalise() stakes floor(bankroll x fraction): bankroll = the twin's stake and fraction 1 reproduce it exactly
    out = ashoka_market.personalise({**core, "stake_fraction": 1.0}, stake if stake > 0 else None)
    out["stake_fraction"] = 0.0 if verdict.sizing is None else round(verdict.sizing.fraction, 6)
    out["stake_inr"] = str(stake)
    # Group 73: each model's whole outcome distribution per leg, so settlement can score it (ranked probability score)
    for payload, leg in zip(out["legs"], slip.legs, strict=True):
        payload["distributions"] = {name: {r.value: round(p, 6) for r, p in dist.items()} for name, dist in leg.distributions().items()}
    return out


async def vet(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, *, user_id: uuid.UUID | None, leg_ids: Sequence[str],
    kind: str | None, bankroll: Decimal | None, now: datetime, books: Sequence[str] | None = None,
) -> TwinVettingAudit:
    """``books``: price the slip at these books only (the manual workbench's account, Group 77); default the retail books."""
    leg_ids = list(dict.fromkeys(leg_ids))
    live, by_id = await _live_legs(redis, settings, leg_ids, now)
    retail, sharp = (tuple(books) if books else retail_books(settings)), sharp_books(settings)
    legs = [_retail_only(leg, retail) for leg in live]
    slip_kind = SlipKind(kind) if kind in SlipKind.__members__ else None
    slip = _engine(settings, retail).evaluate(legs, slip_kind, now)
    if slip is None:
        raise NoRetailBook(f"No retail book ({', '.join(retail)}) quotes every leg of this slip", books=list(retail))
    try:
        intel = await read_intel(redis, settings, {leg.fixture_id for leg in legs})
    except (RedisError, OSError):
        intel = {}
    steam = await steam_by_market(redis, settings, now)
    weights = await model_weights(redis, settings)
    async with sessions() as session:
        from app.domain.popular_picks.trends import crowd_shares  # noqa: PLC0415 - pulls in the trend scan

        shares = await crowd_shares(session, legs, now)
        traps = await public_traps(session, legs, now)
        wallet = await bankroll_for(session, user_id, bankroll) if user_id is not None else bankroll
        drawdown = await drawdown_for(session, user_id, wallet, now, settings.CFO_DRAWDOWN_WINDOW_DAYS) if user_id is not None else 0.0
        halted = await kill_switch_state(session, redis, settings)
    vault = await _vault(sessions, settings)
    skill, latched = await _capital_state(sessions, redis, settings, user_id, wallet, drawdown, weights, now)
    evidence = [
        fortress.LegEvidence(
            leg=leg, quote=quote, sharp_market=_sharp_market(leg, by_id, sharp), intel=intel.get(leg.fixture_id),
            steam_selections=None if steam is None else frozenset(steam.get((leg.fixture_id, leg.market.key), set())),
            public_share=shares.get(leg.leg_id), public_traps=tuple(traps.get(leg.leg_id, ())),
        )
        for leg, quote in zip(slip.legs, slip.quotes, strict=True)
    ]
    inputs = fortress.FortressInputs(
        slip=slip, legs=evidence, bankroll=wallet, drawdown=drawdown, kill_switch=halted, model_weights=weights,
        book_max_stake=VenueStakeRules.for_venue(slip.book, settings).max_stake, now=now, lessons=vault, skill=skill, halt_latched=latched,
    )
    policy = fortress.FortressPolicy.from_settings(settings)
    verdict = fortress.run(inputs, policy, sharp, situations=nf.NeverForgetPolicy.from_settings(settings))
    sizing = verdict.sizing
    audit = TwinVettingAudit(
        id=uuid.uuid4(), user_id=user_id, slip_id=slip.slip_id, kind=slip.kind.value, leg_ids=[leg.leg_id for leg in slip.legs], bookmaker=slip.book,
        total_odds=Decimal(str(slip.odds)).quantize(ODDS_Q, rounding=ROUND_DOWN), stake_inr=Decimal("0.00") if sizing is None else sizing.stake,
        bankroll_inr=wallet, kelly_fraction=0.0 if sizing is None else sizing.fraction, joint_ev=slip.sim.joint_ev, joint_probability=slip.sim.joint_probability,
        consensus_ev=verdict.consensus_ev, sharp_edge=verdict.sharp_edge, pillars_passed=verdict.passed, conviction_score=verdict.conviction,
        is_vetted=verdict.is_vetted, pillars=[p.as_dict() for p in verdict.pillars], rejection_reasons=list(verdict.reasons),
        slip=_slip_payload(slip, verdict, now), created_at=now,
    )
    async with sessions() as session:
        session.add(audit)
        await session.commit()
    await _shield_and_xp(sessions, settings, audit, now)
    if verdict.is_vetted:
        await emit_alert(redis, settings, vetted_alert(audit))
    logger.info("twin audit %s slip=%s book=%s passed=%d vetted=%s", audit.id, audit.slip_id, audit.bookmaker, audit.pillars_passed, audit.is_vetted)
    return audit


async def _capital_state(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID | None, wallet: Decimal | None,
                         drawdown: float, weights: dict[str, float], now: datetime) -> tuple[float | None, bool]:
    """KUMBHA's inputs to pillar 13 (Group 76): the models' Brier skill, and whether a drawdown halt is latched. Observing
    the regime records (and pages) a change of it; a halt reached here latches at once."""
    from app.services.cfo import growth_optimizer as growth  # noqa: PLC0415 - it imports this module

    async with sessions() as session:
        skill = await growth.fleet_skill(session, weights)
    if user_id is None:
        return skill, False
    state = await growth.observe_regime(sessions, redis, settings, user_id, drawdown, wallet, now)
    return skill, state.latched


async def _vault(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> list[nf.Lesson] | None:
    """Pillar 15's lessons; None when the database cannot give them (the pillar is then unverified)."""
    if not settings.NEVER_FORGET_ENABLED:
        return []
    from app.services.twin import never_forget  # noqa: PLC0415 - it imports this module

    try:
        async with sessions() as session:
            return await never_forget.lessons(session)
    except SQLAlchemyError:
        logger.exception("never-forget vault unreadable: pillar 15 unverified")
        return None


async def _shield_and_xp(sessions: async_sessionmaker[AsyncSession], settings: Settings, audit: TwinVettingAudit, now: datetime) -> None:
    """Keep pillar 15's vetoes, and pay the user's XP for a vetted slip and a disciplined streak. The audit is
    already committed: a failure here is logged, never turned into a failed run."""
    from app.models.never_forget import XPActionType  # noqa: PLC0415
    from app.services.twin import never_forget, xp_engine  # noqa: PLC0415 - they import this module

    try:
        await never_forget.record_vetoes(sessions, audit, now)
    except SQLAlchemyError:
        logger.exception("never-forget: the vetoes of audit %s were not recorded", audit.id)
    if audit.user_id is None:
        return
    try:
        async with sessions() as session:
            if audit.is_vetted:
                await xp_engine.award(session, settings, xp_engine.Award(
                    audit.user_id, XPActionType.SLIP_VETTED, f"slip:{audit.slip_id}", f"Slip {audit.slip_id} cleared all {audit.pillars_passed} pillars at {audit.bookmaker}",
                    {"audit_id": str(audit.id)},
                ), now)
            streak = await xp_engine.streak_award(session, audit.user_id, settings, now)
            if streak is not None:
                await xp_engine.award(session, settings, streak, now)
            await session.commit()
    except SQLAlchemyError:
        logger.exception("xp: audit %s earned nothing (database error)", audit.id)


def vetted_alert(audit: TwinVettingAudit) -> SentinelAlert:
    slip = audit.slip or {}
    return SentinelAlert(
        kind=AlertKind.TWIN_SLIP_VETTED, severity=Severity.INFO, source="digital_twin",
        title=f"ASHOKA twin: {audit.pillars_passed}/{len(audit.pillars or []) or fortress.PILLAR_COUNT} pillars · {slip.get('title', audit.kind)} @ {audit.total_odds} · stake ₹{audit.stake_inr:,}",
        body=str(slip.get("quick_copy", ""))[:4000], dedupe_key=f"twin:vetted:{audit.slip_id}",
        detail={"audit_id": str(audit.id), "bookmaker": audit.bookmaker, "stake_inr": str(audit.stake_inr), "joint_ev": audit.joint_ev},
    )


def audit_view(audit: TwinVettingAudit, credit: str | None = None) -> dict[str, Any]:
    return {
        "id": str(audit.id), "slip_id": audit.slip_id, "kind": audit.kind, "leg_ids": list(audit.leg_ids), "bookmaker": audit.bookmaker,
        "total_odds": None if audit.total_odds is None else str(audit.total_odds), "stake_inr": str(audit.stake_inr),
        "bankroll_inr": None if audit.bankroll_inr is None else str(audit.bankroll_inr), "kelly_fraction": round(audit.kelly_fraction, 6),
        "joint_ev": audit.joint_ev, "joint_probability": audit.joint_probability, "consensus_ev": audit.consensus_ev, "sharp_edge": audit.sharp_edge,
        "pillars_passed": audit.pillars_passed, "conviction_score": audit.conviction_score, "is_vetted": audit.is_vetted, "pillars": list(audit.pillars),
        "rejection_reasons": list(audit.rejection_reasons), "slip": audit.slip, "created_at": audit.created_at.isoformat(),
        **({"developer_credit": credit} if credit is not None else {}),
    }


# ================================================================ before placing: pillar 14 again
async def confirm(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, audit: TwinVettingAudit, now: datetime) -> dict[str, Any]:
    """Re-read every leg's price at the audited book: the kill switch must be off, every price fresh and no
    more than ``TWIN_MAX_ODDS_DRIFT_PCT`` under the audited one."""
    policy = fortress.FortressPolicy.from_settings(settings)
    legs = (audit.slip or {}).get("legs", [])
    audited = [float(leg["prices"][audit.bookmaker]) for leg in legs if audit.bookmaker in (leg.get("prices") or {})]
    if len(audited) != len(audit.leg_ids):
        raise TwinRefusal("The audit has no price at its bookmaker for every leg")
    try:
        live, _ = await _live_legs(redis, settings, list(audit.leg_ids), now)
    except LegsNotQuoted as exc:
        return {"status": fortress.Status.FAIL.value, "reason": exc.message, "legs": exc.detail.get("legs", [])}
    quotes = [leg.quotes.get(audit.bookmaker or "") for leg in live]
    if any(q is None for q in quotes):
        gone = [leg.leg_id for leg, q in zip(live, quotes, strict=True) if q is None]
        return {"status": fortress.Status.FAIL.value, "reason": f"{audit.bookmaker} no longer quotes every leg", "legs": gone}
    async with sessions() as session:
        halted = await kill_switch_state(session, redis, settings)
    floors = fortress.drift_floors(audited, policy)
    result = fortress.pillar_14(halted, quotes, now, policy, floors=floors)  # type: ignore[arg-type]
    return {
        **result.as_dict(), "bookmaker": audit.bookmaker, "checked_at": now.isoformat(),
        "legs": [{"leg_id": leg.leg_id, "audited_odds": a, "floor": round(f, 4), "current_odds": q.odds, "age_seconds": round(q.age(now), 1)}  # type: ignore[union-attr]
                 for leg, a, f, q in zip(live, audited, floors, quotes, strict=True)],
    }


# ================================================================ Pathway A: the user placed it
_PLACED_BOOK = {"parimatch": PlacedBookmaker.PARIMATCH, "1xbet": PlacedBookmaker.ONEXBET, "stake": PlacedBookmaker.STAKE, "pinnacle": PlacedBookmaker.PINNACLE, "betfair": PlacedBookmaker.BETFAIR}


def placed_bookmaker(book: str | None) -> PlacedBookmaker:
    return _PLACED_BOOK.get(book or "", PlacedBookmaker.OTHER)


def place_request(audit: TwinVettingAudit, body: LedgerFromAudit) -> PlaceBetRequest:
    """The audited slip as Ashoka's ledger records it, at the prices the chosen book showed."""
    book = next((k for k, v in _PLACED_BOOK.items() if v is body.bookmaker), None)
    legs = []
    for leg in (audit.slip or {}).get("legs", []):
        prices = leg.get("prices") or {}
        price = prices.get(book) if book else None
        if price is None:
            price = prices.get(audit.bookmaker or "")
        if price is None:
            raise TwinRefusal(f"No recorded price for {leg.get('fixture')} at {body.bookmaker.value}")
        legs.append(PlacedLegIn(
            fixture_id=leg["fixture_id"], home=leg["home"], away=leg["away"], sport_key=leg.get("sport_key"), league=leg.get("league"),
            kickoff=leg.get("kickoff"), market=leg["market"], selection=leg["selection"], odds=Decimal(str(price)).quantize(Decimal("0.001")),
            fair_probability=leg.get("fair_probability"),
        ))
    return PlaceBetRequest(
        slip_id=audit.slip_id, source="ASHOKA", bookmaker=body.bookmaker, bookmaker_name="Ashoka twin" if body.bookmaker is PlacedBookmaker.OTHER else None,
        structure=PlacedStructure(audit.kind), stake_inr=body.stake_inr, placed_odds=body.placed_odds, placed_at=body.placed_at, legs=legs,
        notes=f"Twin audit {audit.id} · {audit.pillars_passed}/{len(audit.pillars or []) or fortress.PILLAR_COUNT} pillars",
    )


async def record_placed(session: AsyncSession, user_id: uuid.UUID, audit: TwinVettingAudit, body: LedgerFromAudit, now: datetime) -> UserPlacedBet:
    bet = await tracker.record_bet(session, user_id, place_request(audit, body), now)
    bet.vetting_audit_id = audit.id
    bet.booking_code = body.booking_code.upper() if body.booking_code else None
    await session.flush()
    return bet
