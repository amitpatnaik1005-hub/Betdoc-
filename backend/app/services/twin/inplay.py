"""The twin's in-play watch: every placed bet it watches, re-priced every ``TWIN_INPLAY_POLL_SECONDS`` (Group 72).

One tick (``tick``), from Celery beat or by hand:

1. A Redis lock (``SET NX EX``) lets one tick run at a time across workers; a tick that finds it held
   returns at once. In PostgreSQL the active monitors are also locked ``ORDER BY id FOR UPDATE SKIP
   LOCKED``: a deterministic order, and a monitor busy elsewhere is skipped, never waited on.
2. A monitor whose bet has settled (or was cashed out) is closed.
3. Every open leg is priced from the live market (the board keeps started fixtures; the scoreline
   models are refitted to the in-play 1X2 consensus), settled legs pay their factor, and Ashoka's cashout
   advisor values the slip: fair value, HOLD / CASH OUT / HEDGE LEG against the offer the user last read.
4. The pullout call, first match wins: STOP_LOSS (Group 77: the cashout value, the offer the user read else
   fair value, at or under ``(1 - stop_loss_pct) x stake``), PROBABILITY_COLLAPSE (the live win probability
   under ``TWIN_PULLOUT_PROB_RATIO`` of the entry one, or down ``TWIN_PULLOUT_PROB_DROP`` points), CASHOUT_ADVISED (the offer is worth taking), HEDGE_LOCK (a hedge locks more
   than the offer), TARGET_PROFIT_REACHED (the offer, else fair value, ``TWIN_PULLOUT_TARGET_PROFIT_PCT``
   over the stake). The monitor fires once, closes, and pages the user's phone through the Sentinel.

A stop-loss or a collapse also issues the bookmaker's cashout ticket (``app.adapters.bookmakers``): the steps
to take the cashout at the book, sent to the phone. Every priced tick is published to the user's live channel
(``<TWIN_PREFIX>:inplay:live:<user>``, the ``/ws/inplay-shield`` socket).

The twin recommends; the cashout is the user's to take at the bookmaker and record on the bet.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.bookmakers.base_cashout_adapter import adapter_for
from app.core.config import Settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.oracle.cashout_advisor import Advice, CashoutAdvice, OpenLeg, SettledLeg, advise
from app.domain.oracle.markets import LegResult, parse_market
from app.domain.oracle.parlay_engine import LegCandidate
from app.models.digital_twin import PulloutReason, TwinInPlayMonitor
from app.models.sentinel import Severity
from app.models.user_bets_ledger import PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.services import ashoka_market
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.twin.vetting import LIVE_LOOKBACK

logger = logging.getLogger("betdoc.twin.inplay")

STRAIGHT = ("SINGLE", "DOUBLE", "TREBLE", "ACCUMULATOR")
HEDGE_BOOKS = ("pinnacle", "betfair", "1xbet", "parimatch", "stake")


class NoLivePrice(LookupError):
    def __init__(self, positions: Sequence[int]) -> None:
        super().__init__(f"no live price for leg(s) {', '.join(str(p) for p in positions)}")
        self.positions = list(positions)


def lock_key(settings: Settings) -> str:
    return f"{settings.TWIN_PREFIX}:inplay:lock"


def live_channel(settings: Settings, user_id: uuid.UUID | str) -> str:
    return f"{settings.TWIN_PREFIX}:inplay:live:{user_id}"


def last_frames_key(settings: Settings, user_id: uuid.UUID | str) -> str:
    """monitor id -> its latest frame, for a socket that connects between ticks."""
    return f"{settings.TWIN_PREFIX}:inplay:last:{user_id}"


@dataclass(slots=True)
class Valuation:
    probability: float
    advice: CashoutAdvice
    legs: list[dict[str, Any]] = field(default_factory=list)


async def live_legs(redis: Redis | None, settings: Settings, fixtures: set[str], now: datetime) -> dict[str, LegCandidate]:
    if redis is None or not fixtures:
        return {}
    try:
        candidates, _ = await ashoka_market.load_candidates(redis, settings, now - LIVE_LOOKBACK, fixtures=fixtures)
    except (RedisError, OSError, TimeoutError):
        return {}
    return {c.leg_id: c for c in candidates}


def value_bet(bet: UserPlacedBet, legs: Sequence[UserPlacedLeg], live: dict[str, LegCandidate], settings: Settings, now: datetime, *,
              offer: Decimal | None = None) -> Valuation:
    """The slip's worth right now. Raises NoLivePrice for an open leg nothing prices, ValueError when it cannot be advised."""
    open_rows = [leg for leg in legs if leg.result == PlacedStatus.PENDING.value]
    unpriced = [leg.position for leg in open_rows if ashoka_market.fair_value_inputs(live.get(f"{leg.fixture_id}|{leg.market}|{leg.selection}")) is None]
    if unpriced:
        raise NoLivePrice(unpriced)
    open_legs: list[OpenLeg] = []
    rows: list[dict[str, Any]] = []
    for leg in open_rows:
        candidate = live[f"{leg.fixture_id}|{leg.market}|{leg.selection}"]
        p = float(ashoka_market.fair_value_inputs(candidate))  # type: ignore[arg-type]
        hedge_back: list[tuple[str, float]] = []
        hedge_book = None
        ref = parse_market(leg.market)
        if ref is not None:
            others = [live.get(f"{leg.fixture_id}|{ref.key}|{s}") for s in ref.selections if s != leg.selection]
            quotes = [(c, c.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, HEDGE_BOOKS)) for c in others if c is not None]
            if quotes and len(quotes) == len(ref.selections) - 1 and all(q is not None for _, q in quotes):
                hedge_back = [(c.selection, q.net_odds) for c, q in quotes]  # type: ignore[union-attr]
                hedge_book = ", ".join(sorted({q.bookmaker for _, q in quotes}))  # type: ignore[union-attr]
        open_legs.append(OpenLeg(f"{leg.home} vs {leg.away}: {leg.selection}", float(leg.odds), p, tuple(hedge_back), hedge_book))
        rows.append({"position": leg.position, "fixture": f"{leg.home} vs {leg.away}", "market": leg.market, "selection": leg.selection, "probability": round(p, 4)})
    settled = [SettledLeg(f"{leg.home} vs {leg.away}", float(leg.odds), LegResult(leg.result)) for leg in legs if leg.result != PlacedStatus.PENDING.value]
    advice = advise(
        float(bet.stake_inr), settled, open_legs, offer=None if offer is None else float(offer), hold_ratio=settings.ASHOKA_CASHOUT_HOLD_RATIO,
        total_odds=None if bet.placed_odds is None else float(bet.placed_odds),
    )
    return Valuation(advice.win_probability, advice, rows)


def pullout(monitor: TwinInPlayMonitor, bet: UserPlacedBet, valuation: Valuation, settings: Settings) -> tuple[PulloutReason, str] | None:
    """The pullout call for this tick, if any (first match wins)."""
    advice = valuation.advice
    policy = slm.StopLossPolicy.from_settings(settings)
    stop = slm.evaluate(bet.stake_inr, policy.clamp(monitor.stop_loss_pct), offer=advice.offer, fair_value=advice.fair_value,
                        entry_probability=monitor.initial_win_prob, live_probability=valuation.probability, policy=policy)
    if stop is not None:
        return (PulloutReason.STOP_LOSS if stop.rule == "STOP_LOSS_FLOOR" else PulloutReason.PROBABILITY_COLLAPSE), stop.reason
    drop = monitor.initial_win_prob - valuation.probability
    if drop >= settings.TWIN_PULLOUT_PROB_DROP:
        return PulloutReason.PROBABILITY_COLLAPSE, f"win probability {monitor.initial_win_prob:.1%} -> {valuation.probability:.1%}: cash out or hedge now"
    if advice.offer is not None and advice.advice is Advice.CASH_OUT:
        return PulloutReason.CASHOUT_ADVISED, f"take the ₹{advice.offer:,} cashout: {advice.reasons[-1]}"
    if advice.advice is Advice.HEDGE_LEG and advice.hedge is not None:
        return PulloutReason.HEDGE_LOCK, f"{advice.hedge.instruction} (locks ₹{advice.hedge.locked_profit:,})"
    worth = advice.offer if advice.offer is not None else advice.fair_value
    target = bet.stake_inr * Decimal(str(monitor.target_profit_pct))
    if worth - bet.stake_inr >= target:
        what = f"the ₹{worth:,} offer" if advice.offer is not None else f"fair value ₹{worth:,}"
        return PulloutReason.TARGET_PROFIT_REACHED, f"{what} is {float((worth - bet.stake_inr) / bet.stake_inr):.0%} over the stake: bank it (check the book's cashout)"
    return None


async def start(session: AsyncSession, redis: Redis | None, settings: Settings, bet: UserPlacedBet, now: datetime, *,
                audit_id: uuid.UUID | None = None, target_profit_pct: float | None = None, stop_loss_pct: float | None = None) -> TwinInPlayMonitor:
    """Watch a pending straight bet (re-arms an existing monitor). Its starting probability is the live one,
    else the fair probabilities Ashoka recorded on the legs."""
    if bet.status != PlacedStatus.PENDING.value:
        raise ValueError("only a pending bet can be watched")
    if bet.structure not in STRAIGHT:
        raise ValueError("the watch covers straight multiples; a system's lines settle separately")
    legs = list((await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet.id).order_by(UserPlacedLeg.position))).scalars())
    live = await live_legs(redis, settings, {leg.fixture_id for leg in legs if leg.result == PlacedStatus.PENDING.value}, now)
    try:
        valuation = value_bet(bet, legs, live, settings, now)
        probability, fair = valuation.probability, valuation.advice.fair_value
    except NoLivePrice:
        recorded = [leg.fair_probability for leg in legs if leg.result == PlacedStatus.PENDING.value]
        if any(p is None for p in recorded):
            raise
        probability, fair = math.prod(recorded), None  # type: ignore[arg-type]
    target = target_profit_pct if target_profit_pct is not None else settings.TWIN_PULLOUT_TARGET_PROFIT_PCT
    monitor = (await session.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id == bet.id))).scalars().first()
    if monitor is None:
        monitor = TwinInPlayMonitor(id=uuid.uuid4(), bet_id=bet.id, user_id=bet.user_id, ticks=0, detail={}, created_at=now)
        session.add(monitor)
    monitor.vetting_audit_id = audit_id or monitor.vetting_audit_id or bet.vetting_audit_id
    monitor.is_active, monitor.target_profit_pct = True, target
    if stop_loss_pct is not None or monitor.stop_loss_pct is None:
        monitor.stop_loss_pct = slm.StopLossPolicy.from_settings(settings).clamp(stop_loss_pct)
    monitor.initial_win_prob = monitor.current_win_prob = min(max(probability, 0.0), 1.0)
    monitor.fair_value_inr = monitor.peak_fair_value_inr = fair
    monitor.pullout_triggered, monitor.pullout_reason, monitor.pullout_at = False, None, None
    monitor.updated_at = now
    await session.flush()
    return monitor


@dataclass(slots=True)
class TickReport:
    ran: bool = True
    watched: int = 0
    priced: int = 0
    closed: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)


async def tick(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *, user_id: uuid.UUID | None = None) -> TickReport:
    if redis is None:
        return TickReport(ran=False)
    token = uuid.uuid4().hex
    try:
        acquired = await redis.set(lock_key(settings), token, nx=True, ex=max(int(settings.TWIN_INPLAY_POLL_SECONDS * 6), 5))
    except (RedisError, OSError):
        return TickReport(ran=False)
    if not acquired:
        return TickReport(ran=False)
    try:
        return await _tick(sessions, redis, settings, now, user_id)
    finally:
        try:  # release only our own lock (it may have expired and been taken)
            if await redis.get(lock_key(settings)) == token:
                await redis.delete(lock_key(settings))
        except (RedisError, OSError):
            pass


def cashout_ticket(monitor: TwinInPlayMonitor, bet: UserPlacedBet, valuation: Valuation, reason: str, settings: Settings, now: datetime) -> dict[str, Any]:
    """The bookmaker's cashout ticket for a stop-loss or a collapse: what the shield saw and the steps to take."""
    policy = slm.StopLossPolicy.from_settings(settings)
    pct = policy.clamp(monitor.stop_loss_pct)
    offer = valuation.advice.offer
    value, source = (offer, "offer") if offer is not None else (valuation.advice.fair_value, "fair_value")
    return adapter_for(bet.bookmaker).ticket(bet, floor=slm.floor(bet.stake_inr, pct), value=value, value_source=source, reason=reason, now=now).as_dict()


def frame(monitor: TwinInPlayMonitor, bet: UserPlacedBet) -> dict[str, Any]:
    """What the live socket sends per monitor per tick."""
    pct = monitor.stop_loss_pct
    return {"type": "shield", "monitor_id": str(monitor.id), "bet_id": str(bet.id), "bookmaker": bet.bookmaker, "booking_code": bet.booking_code,
            "stake_inr": str(bet.stake_inr), "stop_loss_pct": pct, "floor_inr": None if pct is None else str(slm.floor(bet.stake_inr, pct)),
            "initial_win_prob": round(monitor.initial_win_prob, 4), "current_win_prob": round(monitor.current_win_prob, 4),
            "fair_value_inr": None if monitor.fair_value_inr is None else str(monitor.fair_value_inr),
            "cashout_offer_inr": None if monitor.cashout_offer_inr is None else str(monitor.cashout_offer_inr), "last_advice": monitor.last_advice,
            "is_active": monitor.is_active, "pullout_reason": monitor.pullout_reason, "at": None if monitor.last_tick_at is None else monitor.last_tick_at.isoformat(),
            "ticket": (monitor.detail or {}).get("cashout_ticket")}


async def publish(redis: Redis, settings: Settings, frames: Sequence[tuple[uuid.UUID, dict[str, Any]]]) -> None:
    """Each frame to its user's live channel, and kept as the user's latest for a socket that connects later."""
    import json  # noqa: PLC0415

    if not frames:
        return
    try:
        pipe = redis.pipeline(transaction=False)
        for user, body in frames:
            raw = json.dumps(body, separators=(",", ":"))
            pipe.publish(live_channel(settings, user), raw)
            pipe.hset(last_frames_key(settings, user), body["monitor_id"], raw)
            pipe.expire(last_frames_key(settings, user), max(int(settings.TWIN_INPLAY_POLL_SECONDS * 60), 60))
        await pipe.execute()
    except (RedisError, OSError):
        logger.warning("in-play frames not published (Redis)")


async def _tick(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, now: datetime, user_id: uuid.UUID | None) -> TickReport:
    report = TickReport()
    alerts: list[SentinelAlert] = []
    frames: list[tuple[uuid.UUID, dict[str, Any]]] = []
    async with sessions() as session:
        query = select(TwinInPlayMonitor).where(TwinInPlayMonitor.is_active.is_(True)).order_by(TwinInPlayMonitor.id).with_for_update(skip_locked=True)
        if user_id is not None:
            query = query.where(TwinInPlayMonitor.user_id == user_id)
        monitors = list((await session.execute(query)).scalars())
        report.watched = len(monitors)
        if not monitors:
            await session.commit()
            return report
        bets = {b.id: b for b in (await session.execute(select(UserPlacedBet).where(UserPlacedBet.id.in_([m.bet_id for m in monitors])))).scalars()}
        legs: dict[uuid.UUID, list[UserPlacedLeg]] = {}
        for leg in (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id.in_(list(bets))).order_by(UserPlacedLeg.position))).scalars():
            legs.setdefault(leg.bet_id, []).append(leg)
        fixtures = {leg.fixture_id for rows in legs.values() for leg in rows if leg.result == PlacedStatus.PENDING.value}
        live = await live_legs(redis, settings, fixtures, now)
        for monitor in monitors:
            bet = bets.get(monitor.bet_id)
            monitor.last_tick_at, monitor.ticks = now, monitor.ticks + 1
            if bet is None or bet.status != PlacedStatus.PENDING.value:
                monitor.is_active = False
                monitor.detail = {**(monitor.detail or {}), "closed": f"bet {bet.status if bet else 'removed'}"}
                report.closed += 1
                continue
            try:
                valuation = value_bet(bet, legs.get(bet.id, []), live, settings, now, offer=monitor.cashout_offer_inr)
            except NoLivePrice as exc:
                monitor.detail = {**(monitor.detail or {}), "status": "NO_LIVE_PRICE", "positions": exc.positions, "at": now.isoformat()}
                continue
            except ValueError as exc:  # a leg lost before settlement ran: nothing to cash out
                monitor.detail = {**(monitor.detail or {}), "status": "NOT_ADVISABLE", "reason": str(exc), "at": now.isoformat()}
                continue
            report.priced += 1
            advice = valuation.advice
            monitor.current_win_prob = min(max(valuation.probability, 0.0), 1.0)
            monitor.fair_value_inr = advice.fair_value
            monitor.peak_fair_value_inr = max(advice.fair_value, monitor.peak_fair_value_inr or advice.fair_value)
            monitor.last_advice = advice.advice.value
            monitor.detail = {
                "status": "PRICED", "at": now.isoformat(), "legs": valuation.legs, "reasons": list(advice.reasons),
                "potential_payout_inr": str(advice.potential_payout), "offer_ratio": None if advice.offer_ratio is None else round(advice.offer_ratio, 4),
                "hedge": None if advice.hedge is None else {"kind": advice.hedge.kind, "book": advice.hedge.book, "instruction": advice.hedge.instruction,
                                                            "locked_profit_inr": str(advice.hedge.locked_profit)},
            }
            call = pullout(monitor, bet, valuation, settings)
            if call is None:
                frames.append((monitor.user_id, frame(monitor, bet)))
                continue
            reason, action = call
            monitor.pullout_triggered, monitor.pullout_reason, monitor.pullout_at, monitor.is_active = True, reason.value, now, False
            ticket = cashout_ticket(monitor, bet, valuation, action, settings, now) if reason in (PulloutReason.STOP_LOSS, PulloutReason.PROBABILITY_COLLAPSE) else None
            monitor.detail = {**monitor.detail, "action": action, **({"cashout_ticket": ticket} if ticket else {})}
            item = {"monitor_id": str(monitor.id), "bet_id": str(bet.id), "reason": reason.value, "action": action, "fair_value_inr": str(advice.fair_value),
                    "win_probability": round(valuation.probability, 4), "booking_code": bet.booking_code, **({"ticket": ticket} if ticket else {})}
            report.alerts.append(item)
            frames.append((monitor.user_id, frame(monitor, bet)))
            body = action if ticket is None else "\n".join([action, *[f"{i}. {s}" for i, s in enumerate(ticket["instructions"], 1)], *ticket["caveats"]])
            alerts.append(SentinelAlert(
                kind=AlertKind.TWIN_PULLOUT, severity=Severity.WARNING if reason is PulloutReason.STOP_LOSS else Severity.INFO, source="digital_twin",
                title=f"ASHOKA {'STOP-LOSS' if reason is PulloutReason.STOP_LOSS else 'pullout: ' + reason.value.replace('_', ' ').lower()} · stake ₹{bet.stake_inr:,}"
                      + (f" · {bet.booking_code}" if bet.booking_code else ""),
                body=body[:4000], dedupe_key=f"twin:pullout:{bet.id}", detail=item,
            ))
        await session.commit()
    await publish(redis, settings, frames)
    for alert in alerts:
        await emit_alert(redis, settings, alert)
    return report


def monitor_view(monitor: TwinInPlayMonitor, bet: UserPlacedBet | None = None) -> dict[str, Any]:
    return {
        "id": str(monitor.id), "bet_id": str(monitor.bet_id), "vetting_audit_id": None if monitor.vetting_audit_id is None else str(monitor.vetting_audit_id),
        "is_active": monitor.is_active, "target_profit_pct": monitor.target_profit_pct, "stop_loss_pct": monitor.stop_loss_pct, "initial_win_prob": round(monitor.initial_win_prob, 4),
        "current_win_prob": round(monitor.current_win_prob, 4), "fair_value_inr": None if monitor.fair_value_inr is None else str(monitor.fair_value_inr),
        "peak_fair_value_inr": None if monitor.peak_fair_value_inr is None else str(monitor.peak_fair_value_inr),
        "cashout_offer_inr": None if monitor.cashout_offer_inr is None else str(monitor.cashout_offer_inr), "last_advice": monitor.last_advice,
        "last_tick_at": None if monitor.last_tick_at is None else monitor.last_tick_at.isoformat(), "ticks": monitor.ticks,
        "pullout_triggered": monitor.pullout_triggered, "pullout_reason": monitor.pullout_reason,
        "pullout_at": None if monitor.pullout_at is None else monitor.pullout_at.isoformat(), "detail": monitor.detail or {},
        **({"bet": {"stake_inr": str(bet.stake_inr), "status": bet.status, "bookmaker": bet.bookmaker, "booking_code": bet.booking_code,
                    "placed_odds": None if bet.placed_odds is None else str(bet.placed_odds)}} if bet is not None else {}),
    }
