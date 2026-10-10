"""KUMBHA's capital growth engine (Group 76): the drawdown regime and its latch, forecasts, rebalancing, advisories.

* ``drawdown_state``: the user's bankroll (the CFO main account) and rolling drawdown over
  ``CFO_DRAWDOWN_WINDOW_DAYS``, the same figures pillar 13 sizes with (one definition, ``vetting.drawdown_for``).
* ``observe_regime``: the damper's regime for that drawdown, written as an advisory when it changes (and paged
  through the Sentinel). Reaching the halt line writes a CAPITAL_PRESERVATION_HALT advisory, and while it is
  unacknowledged the halt is latched: pillar 13 stakes nothing until an administrator signs it off. The vetting
  run and the periodic scan both call it; the scan also re-confirms a steady regime every
  ``CFO_ADVISORY_CONFIRM_HOURS`` with the ledger's own figures.
* ``history`` / ``forecast`` / ``compare``: the Monte Carlo over the user's settled bets (``growth_math.simulate``),
  never over assumed win rates. Fewer than ``CFO_MIN_HISTORY_BETS`` bets: no forecast (``NotEnoughHistory``).
* ``rebalance``: the venues' balances from the Vault (INR, at the FX snapshot), each venue's EV flow from the
  bets placed there, and the water-filling plan. Recording a plan supersedes the pending one; executing a
  transfer is the operator's job at the bookmakers, the plan only records it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.cfo import growth_math as gm
from app.models.cfo_growth import REGIME_CODES, AdvisorySeverity, CFOAdvisoryLog, CFOGrowthSimulation, CFORebalanceRecommendation, InsightCode, RebalanceStatus
from app.models.cfo_vault import BankrollAccount
from app.models.omni_vault import VaultBookmakerAccount
from app.models.sentinel import Severity
from app.models.user_bets_ledger import PlacedStatus, PlacedStructure, UserPlacedBet, UserPlacedLeg
from app.services.fx_rates import FxRates, FxUnavailableError
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin import model_calibrator
from app.services.twin.vetting import _PLACED_BOOK, bankroll_for, developer_credit, drawdown_for

logger = logging.getLogger("betdoc.kumbha.growth")

STRAIGHT = frozenset({PlacedStructure.SINGLE.value, PlacedStructure.DOUBLE.value, PlacedStructure.TREBLE.value, PlacedStructure.ACCUMULATOR.value})
CANONICAL_BOOK = {v.value: k for k, v in _PLACED_BOOK.items()}  # PlacedBookmaker value -> canonical bookmaker id
PAISA = Decimal("0.01")


class NotEnoughHistory(Exception):
    def __init__(self, found: int, needed: int) -> None:
        super().__init__(f"{found} settled bets with a model probability in the window; a forecast needs {needed}")
        self.found, self.needed = found, needed


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# ================================================================ the regime
async def drawdown_state(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime, bankroll: Decimal | None = None) -> tuple[Decimal | None, float]:
    wallet = await bankroll_for(session, user_id, bankroll)
    return wallet, await drawdown_for(session, user_id, wallet, now, settings.CFO_DRAWDOWN_WINDOW_DAYS)


async def fleet_skill(session: AsyncSession, weights: Mapping[str, float]) -> float | None:
    """The models' Brier skill against the sharp close, weighted as pillar 1 weighs them (Group 74's latest audits);
    None while no model has a skill score that counts."""
    rows = [(a.brier_skill_score, float(weights.get(name, 1.0))) for name, a in (await model_calibrator.latest_audits(session)).items() if a.brier_skill_score is not None]
    total = sum(w for _, w in rows if w > 0)
    return None if total <= 0 else sum(b * w for b, w in rows if w > 0) / total  # type: ignore[operator]


@dataclass(frozen=True, slots=True)
class RegimeState:
    regime: gm.Regime  # what sizing runs under now (LATCHED_HALT while a halt awaits its sign-off)
    drawdown: float
    latched: bool
    advisory: CFOAdvisoryLog | None  # the advisory this observation wrote, if any


def _advisory(user_id: uuid.UUID, regime: gm.Regime, drawdown: float, bankroll: Decimal | None, policy: gm.SizingPolicy, settings: Settings, credit: str, now: datetime,
              *, confirm: dict[str, Any] | None = None) -> CFOAdvisoryLog:
    ceiling = policy.max_fraction * regime.multiplier
    limit = None if bankroll is None else (bankroll * Decimal(str(ceiling))).quantize(PAISA)
    metrics = {"drawdown": round(drawdown, 6), "regime": regime.name, "damper": regime.multiplier, "kelly_fraction": policy.kelly_fraction,
               "max_fraction": policy.max_fraction, "effective_ceiling": round(ceiling, 6), "bankroll_inr": None if bankroll is None else str(bankroll),
               "stake_ceiling_inr": None if limit is None else str(limit), "window_days": settings.CFO_DRAWDOWN_WINDOW_DAYS, **(confirm or {})}
    dd = f"rolling {settings.CFO_DRAWDOWN_WINDOW_DAYS:g}-day drawdown {drawdown:.1%}"
    if regime.halted:
        code, severity = InsightCode.CAPITAL_PRESERVATION_HALT, AdvisorySeverity.CRITICAL
        title = f"KUMBHA circuit breaker: {dd} reached the {policy.halt_at:.0%} halt line"
        message = (f"Every stake is zero. The halt holds after the drawdown recovers, until an administrator signs it off with a reason. "
                   f"Bankroll {'unknown' if bankroll is None else f'₹{bankroll:,}'}.")
        action = "Stop staking; review the losses (the Never-Forget journal holds their post-mortems), then sign the halt off."
    elif regime.multiplier < 1.0:
        code = InsightCode.VARIANCE_THROTTLE
        severity = AdvisorySeverity.WARNING if regime.name == gm.DEFENSIVE else AdvisorySeverity.RECOMMENDATION
        title = f"Variance throttle: {regime.name.replace('_', ' ').lower()}, sizing x{regime.multiplier:g}"
        message = (f"The {dd} is past {regime.floor:.0%}: every stake is cut to {regime.multiplier:.0%} of the policy "
                   f"({policy.kelly_fraction:g}x Kelly, ceiling {policy.max_fraction:.2%} -> {ceiling:.3%} of bankroll"
                   + ("" if limit is None else f", at most ₹{limit:,} a bet") + ").")
        action = f"Stakes are cut automatically; at {policy.halt_at:.0%} drawdown everything stops."
    else:
        code, severity = InsightCode.OPTIMAL_GROWTH_TRAJECTORY, AdvisorySeverity.INFO
        title = "Steady: full policy sizing" if confirm is None else "Steady growth, re-confirmed"
        stats = "" if not confirm else (f" Last {settings.CFO_DRAWDOWN_WINDOW_DAYS:g} days: {confirm['settled_bets']} bets settled, P&L ₹{Decimal(confirm['pnl_inr']):,}"
                                        + ("" if confirm.get("roi") is None else f", ROI {confirm['roi']:+.2%}") + ".")
        message = f"The {dd} is under {policy.regimes[1].floor:.0%}: stakes run at the full policy ({policy.kelly_fraction:g}x Kelly, at most {policy.max_fraction:.2%} of bankroll).{stats}"
        action = None
    return CFOAdvisoryLog(id=uuid.uuid4(), user_id=user_id, insight_code=code.value, severity=severity.value, regime=regime.name, title=title[:255], message=message,
                          action_directive=None if action is None else action[:255], metrics_snapshot=metrics, is_acknowledged=False, developer_credit=credit, created_at=now)


async def _ledger_stats(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime) -> dict[str, Any]:
    rows = (await session.execute(
        select(UserPlacedBet.stake_inr, UserPlacedBet.pnl_inr).where(
            UserPlacedBet.user_id == user_id, UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.pnl_inr.is_not(None),
            UserPlacedBet.settled_at >= now - timedelta(days=settings.CFO_DRAWDOWN_WINDOW_DAYS),
        )
    )).all()
    staked = sum((s for s, _ in rows), Decimal(0))
    pnl = sum((p for _, p in rows), Decimal(0))
    return {"settled_bets": len(rows), "pnl_inr": str(pnl.quantize(PAISA)), "roi": None if staked <= 0 else round(float(pnl / staked), 6)}


def regime_alert(advisory: CFOAdvisoryLog) -> SentinelAlert:
    severity = {AdvisorySeverity.CRITICAL.value: Severity.CRITICAL, AdvisorySeverity.WARNING.value: Severity.WARNING}.get(advisory.severity, Severity.INFO)
    return SentinelAlert(kind=AlertKind.CFO_REGIME_CHANGE, severity=severity, source="kumbha", title=advisory.title[:200],
                         body=f"{advisory.message}\n{advisory.action_directive or ''}\nDeveloper: {advisory.developer_credit}"[:4000],
                         dedupe_key=f"kumbha:regime:{advisory.user_id}:{advisory.id}", detail={"advisory_id": str(advisory.id), "user_id": str(advisory.user_id), **advisory.metrics_snapshot})


async def observe_regime(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, drawdown: float,
                         bankroll: Decimal | None, now: datetime, *, confirm: bool = False) -> RegimeState:
    policy = gm.SizingPolicy.from_settings(settings)
    regime = gm.damper(drawdown, policy)
    written: CFOAdvisoryLog | None = None
    async with sessions() as session:
        open_halt = (await session.execute(
            select(CFOAdvisoryLog).where(CFOAdvisoryLog.user_id == user_id, CFOAdvisoryLog.insight_code == InsightCode.CAPITAL_PRESERVATION_HALT.value,
                                         CFOAdvisoryLog.is_acknowledged.is_(False)).order_by(CFOAdvisoryLog.created_at.desc()).limit(1).with_for_update()
        )).scalars().first()
        last = (await session.execute(
            select(CFOAdvisoryLog).where(CFOAdvisoryLog.user_id == user_id, CFOAdvisoryLog.insight_code.in_(REGIME_CODES))
            .order_by(CFOAdvisoryLog.created_at.desc(), CFOAdvisoryLog.id).limit(1)
        )).scalars().first()
        credit = await developer_credit(session)
        if regime.halted:
            if open_halt is None:
                written = _advisory(user_id, regime, drawdown, bankroll, policy, settings, credit, now)
        elif open_halt is None:
            changed = last is None or last.regime != regime.name
            stale = last is not None and now - _aware(last.created_at) >= timedelta(hours=settings.CFO_ADVISORY_CONFIRM_HOURS)
            if changed or (confirm and stale):
                stats = None if changed else await _ledger_stats(session, user_id, settings, now)
                written = _advisory(user_id, regime, drawdown, bankroll, policy, settings, credit, now, confirm=stats)
        if written is not None:
            session.add(written)
        await session.commit()
    latched = open_halt is not None or (written is not None and regime.halted)
    effective = regime if regime.halted or not latched else gm.damper(drawdown, policy, latched=True)
    if written is not None and (written.insight_code != InsightCode.OPTIMAL_GROWTH_TRAJECTORY.value or last is not None and last.regime != regime.name):
        await emit_alert(redis, settings, regime_alert(written))  # a change of regime pages; a re-confirmation does not
    return RegimeState(effective, drawdown, latched, written)


async def scan(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, int]:
    """Every user with a CFO main account or a bet settled in the window: observe the regime, re-confirming a steady one."""
    async with sessions() as session:
        users = set((await session.execute(select(BankrollAccount.user_id).where(BankrollAccount.bot_id.is_(None)))).scalars())
        users |= set((await session.execute(select(UserPlacedBet.user_id).where(UserPlacedBet.settled_at >= now - timedelta(days=settings.CFO_DRAWDOWN_WINDOW_DAYS)))).scalars())
    out = {"users": 0, "advisories": 0, "halted": 0}
    for user_id in sorted(users, key=str):
        async with sessions() as session:
            wallet, drawdown = await drawdown_state(session, user_id, settings, now)
        state = await observe_regime(sessions, redis, settings, user_id, drawdown, wallet, now, confirm=True)
        out["users"] += 1
        out["advisories"] += state.advisory is not None
        out["halted"] += state.regime.halted
    return out


async def acknowledge(session: AsyncSession, advisory: CFOAdvisoryLog, by: uuid.UUID, note: str | None, now: datetime) -> CFOAdvisoryLog:
    advisory.is_acknowledged, advisory.acknowledged_by, advisory.acknowledged_at, advisory.acknowledgement_note = True, by, now, note
    await session.flush()
    return advisory


# ================================================================ forecasts
async def history(session: AsyncSession, user_id: uuid.UUID, settings: Settings, now: datetime) -> gm.History:
    """The user's settled straight bets of the window that carry a model probability on every leg."""
    since = now - timedelta(days=settings.CFO_HISTORY_DAYS)
    bets = list((await session.execute(
        select(UserPlacedBet).where(
            UserPlacedBet.user_id == user_id, UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.pnl_inr.is_not(None),
            UserPlacedBet.structure.in_(STRAIGHT), UserPlacedBet.placed_at >= since,
        ).order_by(UserPlacedBet.placed_at)
    )).scalars())
    legs: dict[uuid.UUID, list[UserPlacedLeg]] = defaultdict(list)
    if bets:
        for leg in (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id.in_([b.id for b in bets])))).scalars():
            legs[leg.bet_id].append(leg)
    odds, probs, returns, first = [], [], [], None
    for bet in bets:
        rows = legs.get(bet.id, [])
        if not rows or any(leg.fair_probability is None for leg in rows) or bet.stake_inr <= 0:
            continue
        price = float(bet.placed_odds) if bet.placed_odds is not None else math.prod(float(leg.odds) for leg in rows)
        odds.append(price)
        probs.append(math.prod(float(leg.fair_probability) for leg in rows))  # type: ignore[arg-type]
        returns.append(float(bet.pnl_inr / bet.stake_inr))  # type: ignore[operator]
        first = first or _aware(bet.placed_at)
    span = 0.0 if first is None else max(1.0, min(settings.CFO_HISTORY_DAYS, (now - first).total_seconds() / 86400.0))
    return gm.History(np.array(odds), np.array(probs), np.array(returns), len(odds) / span if span else 0.0)


async def _inputs(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, now: datetime,
                  bankroll: Decimal | None) -> tuple[gm.History, Decimal | None, float, float | None, str]:
    from app.services.twin.intel import model_weights  # noqa: PLC0415

    weights = await model_weights(redis, settings) if redis is not None else {}
    async with sessions() as session:
        hist = await history(session, user_id, settings, now)
        wallet, drawdown = await drawdown_state(session, user_id, settings, now, bankroll)
        skill = await fleet_skill(session, weights)
        credit = await developer_credit(session)
    if hist.odds.size < settings.CFO_MIN_HISTORY_BETS:
        raise NotEnoughHistory(int(hist.odds.size), settings.CFO_MIN_HISTORY_BETS)
    return hist, wallet, drawdown, skill, credit


def strategy_for(code: str, policy: gm.SizingPolicy) -> gm.Strategy:
    found = next((s for s in gm.strategies(policy) if s.code == code), None)
    if found is None:
        raise ValueError(f"unknown strategy {code!r}: one of {', '.join(s.code for s in gm.strategies(policy))}")
    return found


async def forecast(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, now: datetime, *, strategy: str,
                   horizon_days: int, paths: int, bankroll: Decimal | None = None, seed: int | None = None) -> CFOGrowthSimulation:
    policy = gm.SizingPolicy.from_settings(settings)
    chosen = strategy_for(strategy, policy)
    if horizon_days not in settings.CFO_SIMULATION_HORIZONS:
        raise ValueError(f"horizon_days: one of {', '.join(str(h) for h in settings.CFO_SIMULATION_HORIZONS)}")
    hist, wallet, drawdown, skill, credit = await _inputs(sessions, redis, settings, user_id, now, bankroll)
    if wallet is None or wallet <= 0:
        raise ValueError("no bankroll to forecast: fund the CFO main account or give one")
    run_seed = secrets.randbits(63) if seed is None else seed
    result = await asyncio.to_thread(gm.simulate, hist, chosen, policy, start=float(wallet), horizon_days=horizon_days, paths=paths, seed=run_seed, bss=skill,
                                     rf_annual=settings.CFO_RISK_FREE_RATE_ANNUAL, ruin_level=settings.CFO_RUIN_LEVEL, curve_points=settings.CFO_CURVE_POINTS)
    row = CFOGrowthSimulation(
        id=uuid.uuid4(), user_id=user_id, strategy=chosen.code, horizon_days=horizon_days, simulated_paths=paths, trades=result.trades, seed=run_seed,
        starting_bankroll_inr=wallet, median_ending_bankroll_inr=Decimal(str(round(result.median_end, 2))), mean_ending_bankroll_inr=Decimal(str(round(result.mean_end, 2))),
        expected_cagr_pct=round(result.cagr * 100.0, 4), sharpe_ratio=result.sharpe, sortino_ratio=result.sortino, prob_circuit_breaker=result.prob_halt,
        prob_ruin=result.prob_ruin, median_max_drawdown=result.median_max_drawdown, p95_max_drawdown=result.p95_max_drawdown, percentile_curves={"points": result.curve},
        parameters={"history": hist.as_dict(), "policy": policy.as_dict(), "strategy": {"code": chosen.code, "kelly": chosen.kelly, "fixed": chosen.fixed},
                    "skill_bss": skill, "current_drawdown": round(drawdown, 6), "ruin_level": settings.CFO_RUIN_LEVEL, "risk_free_annual": settings.CFO_RISK_FREE_RATE_ANNUAL},
        developer_credit=credit, created_at=now,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
    return row


async def compare(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, now: datetime, *,
                  bankroll: Decimal | None = None, seed: int | None = None) -> dict[str, Any]:
    """Every strategy over the same history and seed: its forecast, the analytic ruin bound and its stake on a typical bet now."""
    policy = gm.SizingPolicy.from_settings(settings)
    hist, wallet, drawdown, skill, credit = await _inputs(sessions, redis, settings, user_id, now, bankroll)
    regime = gm.damper(drawdown, policy)
    async with sessions() as session:
        latched = await halt_latched(session, user_id)
    if latched and not regime.halted:
        regime = gm.damper(drawdown, policy, latched=True)
    run_seed = secrets.randbits(63) if seed is None else seed
    typical = float(np.median(hist.full_kelly))
    typical_odds = float(np.median(hist.odds))
    rows = []
    for s in gm.strategies(policy):
        result = await asyncio.to_thread(gm.simulate, hist, s, policy, start=float(wallet or 0) or 1.0, horizon_days=settings.CFO_COMPARISON_HORIZON_DAYS,
                                         paths=settings.CFO_COMPARISON_PATHS, seed=run_seed, bss=skill, rf_annual=settings.CFO_RISK_FREE_RATE_ANNUAL,
                                         ruin_level=settings.CFO_RUIN_LEVEL, curve_points=settings.CFO_CURVE_POINTS)
        raw = float(s.fractions(np.array([typical]), np.array([typical_odds]), policy, skill)[0])
        fraction = raw * regime.multiplier
        rows.append({
            "strategy_code": s.code, "strategy_name": s.label, "kelly_multiplier": s.kelly, "fixed_fraction": s.fixed,
            "base_fraction": round(raw, 6), "effective_fraction": round(fraction, 6), "drawdown_damper_multiplier": regime.multiplier,
            "recommended_stake_on_next_bet_inr": None if wallet is None else str(gm.round_stake(wallet, fraction, settings.TWIN_STAKE_STEP_INR)),
            "simulated_cagr_pct": round(result.cagr * 100.0, 4), "median_end_multiple": round(result.median_end / result.start, 6),
            "sharpe_ratio": None if result.sharpe is None else round(result.sharpe, 4), "sortino_ratio": None if result.sortino is None else round(result.sortino, 4),
            "prob_circuit_breaker": round(result.prob_halt, 6), "prob_ruin": round(result.prob_ruin, 6), "median_max_drawdown_pct": round(result.median_max_drawdown * 100, 4),
            "p95_max_drawdown_pct": round(result.p95_max_drawdown * 100, 4),
            "ruin_bound_halving": None if s.kelly is None else round(gm.ruin_probability(s.kelly, 0.5), 6),
        })
    return {
        "current_bankroll_inr": None if wallet is None else str(wallet), "current_drawdown_pct": round(drawdown * 100, 4), "regime": regime.name,
        "damper": regime.multiplier, "halt_latched": latched, "skill_bss": None if skill is None else round(skill, 6), "skill_multiplier": round(gm.skill_multiplier(skill, policy), 4),
        "active_strategy": gm.active_code(policy, gm.strategies(policy)), "history": hist.as_dict(), "typical_bet": {"full_kelly": round(typical, 6), "odds": round(typical_odds, 4)},
        "horizon_days": settings.CFO_COMPARISON_HORIZON_DAYS, "paths": settings.CFO_COMPARISON_PATHS, "seed": run_seed, "policy": policy.as_dict(),
        "strategies": rows, "developer_credit": credit,
    }


async def halt_latched(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return (await session.execute(
        select(CFOAdvisoryLog.id).where(CFOAdvisoryLog.user_id == user_id, CFOAdvisoryLog.insight_code == InsightCode.CAPITAL_PRESERVATION_HALT.value,
                                        CFOAdvisoryLog.is_acknowledged.is_(False)).limit(1)
    )).first() is not None


# ================================================================ rebalancing
@dataclass(frozen=True, slots=True)
class VenueBook:
    balances: dict[str, Decimal]  # canonical bookmaker -> INR free to move (balance less reservations, every active account)
    unpriced: dict[str, str]  # bookmaker -> why its balance could not be counted (no FX rate, no balance recorded)
    accounts: dict[str, int]


async def venue_balances(session: AsyncSession, redis: Redis | None, settings: Settings) -> VenueBook:
    fx = FxRates(redis, settings)
    rates = await fx.snapshot()
    balances: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    unpriced: dict[str, str] = {}
    accounts: dict[str, int] = defaultdict(int)
    for acc in (await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.is_active.is_(True)))).scalars():
        book = acc.bookmaker_id.strip().casefold()
        if acc.balance is None:
            unpriced.setdefault(book, "no balance recorded in the Vault")
            continue
        free = max(acc.balance - (acc.reserved or Decimal(0)), Decimal(0))
        try:
            quote = fx.quote(acc.currency, rates)
        except FxUnavailableError as exc:
            unpriced[book] = str(exc)
            continue
        inr = free if quote is None else free * quote.inr_per_unit * (Decimal(1) - quote.haircut)
        balances[book] += inr.quantize(PAISA)
        accounts[book] += 1
    return VenueBook(dict(balances), {k: v for k, v in unpriced.items() if k not in balances}, dict(accounts))


async def ev_flow(session: AsyncSession, settings: Settings, now: datetime) -> dict[str, float]:
    """bookmaker -> the EV its bets captured over the lookback: sum of stake x (p x odds - 1), on bets with a model probability."""
    bets = list((await session.execute(
        select(UserPlacedBet).where(UserPlacedBet.placed_at >= now - timedelta(days=settings.CFO_REBALANCE_LOOKBACK_DAYS), UserPlacedBet.structure.in_(STRAIGHT))
    )).scalars())
    legs: dict[uuid.UUID, list[UserPlacedLeg]] = defaultdict(list)
    if bets:
        for leg in (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id.in_([b.id for b in bets])))).scalars():
            legs[leg.bet_id].append(leg)
    out: dict[str, float] = defaultdict(float)
    for bet in bets:
        book = CANONICAL_BOOK.get(bet.bookmaker)
        rows = legs.get(bet.id, [])
        if book is None or not rows or any(leg.fair_probability is None for leg in rows):
            continue
        price = float(bet.placed_odds) if bet.placed_odds is not None else math.prod(float(leg.odds) for leg in rows)
        out[book] += float(bet.stake_inr) * (math.prod(float(leg.fair_probability) for leg in rows) * price - 1.0)  # type: ignore[arg-type]
    return dict(out)


async def rebalance_plan(session: AsyncSession, redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:
    book = await venue_balances(session, redis, settings)
    flow = await ev_flow(session, settings, now)
    found = gm.plan(book.balances, flow, settings.CFO_REBALANCE_GAMMA, settings.CFO_REBALANCE_MIN_TRANSFER_INR) if book.balances else None
    no_account = sorted(b for b, e in flow.items() if e > 0 and b not in book.balances)
    return {
        "total_bankroll_inr": str(sum(book.balances.values(), Decimal(0))), "venue_balances": {k: str(v) for k, v in sorted(book.balances.items())},
        "ev_flow_inr": {k: round(v, 2) for k, v in sorted(flow.items())}, "unpriced": book.unpriced, "ev_without_account": no_account,
        "target_allocations": {} if found is None else {k: str(v) for k, v in sorted(found.targets.items())},
        "target_weights": {} if found is None else {k: round(v, 6) for k, v in sorted(found.weights.items())},
        "transfers": [] if found is None else [{"source_venue": t.source, "destination_venue": t.destination, "amount_inr": str(t.amount)} for t in found.transfers],
        "reason": None if found is not None else ("no venue balance is recorded in the Vault" if not book.balances else
                                                   f"no bet placed at these venues in {settings.CFO_REBALANCE_LOOKBACK_DAYS:g} days carries a model probability: no EV to allocate by"),
        "parameters": {"gamma": settings.CFO_REBALANCE_GAMMA, "min_transfer_inr": str(settings.CFO_REBALANCE_MIN_TRANSFER_INR), "lookback_days": settings.CFO_REBALANCE_LOOKBACK_DAYS},
    }


async def record_plan(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, admin_id: uuid.UUID, now: datetime) -> tuple[dict[str, Any], list[CFORebalanceRecommendation]]:
    """Persist the current plan's transfers (superseding the pending ones) and write a VENUE_REBALANCE advisory."""
    async with sessions() as session:
        current = await rebalance_plan(session, redis, settings, now)
        credit = await developer_credit(session)
        await session.execute(update(CFORebalanceRecommendation).where(CFORebalanceRecommendation.status == RebalanceStatus.PENDING.value).values(
            status=RebalanceStatus.SUPERSEDED.value, status_changed_at=now, status_changed_by=admin_id, status_note="superseded by a newer plan"))
        plan_id = uuid.uuid4()
        balances, target = current["venue_balances"], current["target_allocations"]
        rows = [CFORebalanceRecommendation(
            id=uuid.uuid4(), plan_id=plan_id, created_by=admin_id, source_venue=t["source_venue"], destination_venue=t["destination_venue"], amount_inr=Decimal(t["amount_inr"]),
            reason=(f"Move ₹{Decimal(t['amount_inr']):,} from {t['source_venue']} (holds ₹{Decimal(balances[t['source_venue']]):,}, its EV share ₹{Decimal(target[t['source_venue']]):,}) "
                    f"to {t['destination_venue']} (holds ₹{Decimal(balances[t['destination_venue']]):,}, its EV share ₹{Decimal(target[t['destination_venue']]):,}): withdraw, then deposit."),
            status=RebalanceStatus.PENDING.value, source_balance_before=Decimal(balances[t["source_venue"]]), dest_balance_before=Decimal(balances[t["destination_venue"]]),
            source_target=Decimal(target[t["source_venue"]]), dest_target=Decimal(target[t["destination_venue"]]), developer_credit=credit, created_at=now,
        ) for t in current["transfers"]]
        session.add_all(rows)
        advisory = None
        if rows:
            advisory = CFOAdvisoryLog(
                id=uuid.uuid4(), user_id=admin_id, insight_code=InsightCode.VENUE_REBALANCE.value, severity=AdvisorySeverity.RECOMMENDATION.value, regime=None,
                title=f"Venue rebalance: {len(rows)} transfer(s), ₹{sum((r.amount_inr for r in rows), Decimal(0)):,} in all",
                message="; ".join(f"{r.source_venue} -> {r.destination_venue} ₹{r.amount_inr:,}" for r in rows),
                action_directive="Withdraw at each source, deposit at each destination, then mark each transfer executed.",
                metrics_snapshot={"plan_id": str(plan_id), "target_weights": current["target_weights"], "ev_flow_inr": current["ev_flow_inr"]},
                is_acknowledged=False, developer_credit=credit, created_at=now,
            )
            session.add(advisory)
        await session.commit()
    if advisory is not None:
        await emit_alert(redis, settings, SentinelAlert(kind=AlertKind.CFO_REBALANCE, severity=Severity.INFO, source="kumbha", title=advisory.title[:200],
                                                        body=f"{advisory.message}\nDeveloper: {credit}"[:4000], dedupe_key=f"kumbha:rebalance:{plan_id}",
                                                        detail={"plan_id": str(plan_id), "transfers": len(rows)}))
    return {**current, "plan_id": str(plan_id)}, rows


async def set_transfer_status(session: AsyncSession, row: CFORebalanceRecommendation, status: RebalanceStatus, by: uuid.UUID, note: str | None, now: datetime) -> CFORebalanceRecommendation:
    allowed = {RebalanceStatus.PENDING.value: {RebalanceStatus.APPROVED, RebalanceStatus.EXECUTED, RebalanceStatus.DISMISSED},
               RebalanceStatus.APPROVED.value: {RebalanceStatus.EXECUTED, RebalanceStatus.DISMISSED}}
    if status not in allowed.get(row.status, set()):
        raise ValueError(f"a {row.status} transfer cannot become {status.value}")
    row.status, row.status_changed_by, row.status_note, row.status_changed_at = status.value, by, note, now
    if status is RebalanceStatus.EXECUTED:
        row.executed_at = now
    await session.flush()
    return row


async def recent_transfers(session: AsyncSession, limit: int) -> list[CFORebalanceRecommendation]:
    return list((await session.execute(
        select(CFORebalanceRecommendation).where(or_(CFORebalanceRecommendation.status != RebalanceStatus.SUPERSEDED.value, CFORebalanceRecommendation.executed_at.is_not(None)))
        .order_by(CFORebalanceRecommendation.created_at.desc(), CFORebalanceRecommendation.id).limit(limit)
    )).scalars())


# ================================================================ views
def simulation_view(row: CFOGrowthSimulation) -> dict[str, Any]:
    return {"id": str(row.id), "strategy": row.strategy, "horizon_days": row.horizon_days, "paths": row.simulated_paths, "trades": row.trades, "seed": str(row.seed),
            "starting_bankroll_inr": str(row.starting_bankroll_inr), "median_ending_bankroll_inr": str(row.median_ending_bankroll_inr),
            "mean_ending_bankroll_inr": str(row.mean_ending_bankroll_inr), "expected_cagr_pct": row.expected_cagr_pct, "sharpe_ratio": row.sharpe_ratio,
            "sortino_ratio": row.sortino_ratio, "prob_circuit_breaker": row.prob_circuit_breaker, "prob_ruin": row.prob_ruin,
            "median_max_drawdown": row.median_max_drawdown, "p95_max_drawdown": row.p95_max_drawdown, "percentile_curve": (row.percentile_curves or {}).get("points", []),
            "parameters": row.parameters, "developer_credit": row.developer_credit, "created_at": row.created_at.isoformat()}


def advisory_view(row: CFOAdvisoryLog) -> dict[str, Any]:
    return {"id": str(row.id), "insight_code": row.insight_code, "severity": row.severity, "regime": row.regime, "title": row.title, "message": row.message,
            "action_directive": row.action_directive, "metrics_snapshot": row.metrics_snapshot, "is_acknowledged": row.is_acknowledged,
            "acknowledged_at": None if row.acknowledged_at is None else row.acknowledged_at.isoformat(), "acknowledgement_note": row.acknowledgement_note,
            "developer_credit": row.developer_credit, "created_at": row.created_at.isoformat()}


def transfer_view(row: CFORebalanceRecommendation) -> dict[str, Any]:
    return {"id": str(row.id), "plan_id": str(row.plan_id), "source_venue": row.source_venue, "destination_venue": row.destination_venue, "amount_inr": str(row.amount_inr),
            "reason": row.reason, "status": row.status, "source_balance_before": str(row.source_balance_before), "dest_balance_before": str(row.dest_balance_before),
            "source_target": str(row.source_target), "dest_target": str(row.dest_target), "status_note": row.status_note,
            "status_changed_at": None if row.status_changed_at is None else row.status_changed_at.isoformat(),
            "executed_at": None if row.executed_at is None else row.executed_at.isoformat(), "developer_credit": row.developer_credit, "created_at": row.created_at.isoformat()}
