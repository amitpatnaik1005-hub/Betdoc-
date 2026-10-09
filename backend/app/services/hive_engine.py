"""The Hive engine (Group 65): from one live signal to at most one order per owner and market.

For each signal (an Aryabhata ``EdgeSignal`` off the ``<HIVE_PREFIX>:signals`` stream):

1. The global halt flag (the master kill switch, or a flash crash) stops everything. If Redis
   cannot say whether it is set, nothing fires.
2. Every ACTIVE bot whose cooldown on this selection has expired runs its pipeline
   (``hive_pipeline.evaluate``): a stake proposal, or a reason to pass.
3. Thundering herd: an owner's matching bots (of one execution mode) never fire one bet each.
   The proposal with the highest conviction sizing wins; the others are recorded as MERGED into it. The winner fires
   under a Redis lock on (owner, fixture, market), so two workers, or two signals on the same
   market, cannot both get through the checks below.
4. Wash trades: under that lock, any open bet of the same owner (any bot, or the main account) on
   another outcome of this market blocks the order, so the owner never pays the vig on both sides.
   Legs the Group 64 engines tagged ``arbitrage`` or ``hedge`` are the deliberate exception.
5. Breakers: more than ``max_bets_per_minute`` orders in the velocity window, or a sub-bankroll
   down more than ``drawdown_limit_pct`` over 24h, suspend the bot (its owner resumes it).
6. Execution by mode. SHADOW_MODE records a hypothetical position. PAPER_TRADE fills through the
   paper bookmaker against its virtually funded sub-account. LIVE_EXECUTION goes through the
   Omni-Sniper against its transfer-funded sub-account, and only when the platform runs live.
   A stake above ``slice_size_inr`` with slicing on becomes a TWAP plan of randomised slices,
   queued in Celery 60-120s apart; every slice re-checks the live edge, the halt, the wash-trade
   rule and the breakers before it fires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.cfo_vault import OPEN_STATUSES, PhantomLedger
from app.models.hive_bots import (
    BotExecutionMode,
    BotStatus,
    HiveBotEvent,
    HiveEventKind,
    HiveOrderPlan,
    HiveShadowPosition,
    PlanStatus,
    ShadowStatus,
    TradingBot,
)
from app.schemas.aryabhata import EdgeSignal
from app.services.aryabhata_pipeline import AryabhataKeys
from app.services.bookmaker_gateway import BookmakerGateway
from app.services.cfo_execution import TradeExecutor, slippage_floor
from app.services.cfo_ledger import CfoError, OrderTicket
from app.services.hive_pipeline import MarketReadings, Proposal, Rejection, RiskContext, evaluate, load_market, load_risk_context
from app.services.risk_guard import load_limits, realized_pnl_24h

logger = logging.getLogger("betdoc.hive")

ZERO, ONE, PAISA, HUNDRED = Decimal(0), Decimal(1), Decimal("0.01"), Decimal(100)
COORDINATED_STRATEGIES = ("arbitrage", "hedge")  # Group 64 legs: deliberately on several outcomes
_SLICE_NAMESPACE = uuid.UUID("2f6d0a52-65e0-4b8c-9d77-1a65c0b26565")
_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HiveKeys:
    def __init__(self, settings: Settings) -> None:
        p = settings.HIVE_PREFIX
        self.prefix = p
        self.signals = f"{p}:signals"
        self.group = "hive"
        self.halt = f"{p}:halt"
        self.leader = f"{p}:leader"

    def lock(self, user_id: uuid.UUID, fixture_id: str, market: str) -> str:
        return f"{self.prefix}:lock:{user_id}:{fixture_id}|{market}"

    def fires(self, bot_id: uuid.UUID) -> str:
        return f"{self.prefix}:fires:{bot_id}"

    def seen(self, bot_id: uuid.UUID, fixture_id: str, selection: str) -> str:
        return f"{self.prefix}:seen:{bot_id}:{fixture_id}|{selection}"

    def dedupe(self, bot_id: uuid.UUID, reason: str, key: str) -> str:
        return f"{self.prefix}:evt:{bot_id}:{reason}:{key}"


# ---------------------------------------------------------------- the global halt
class HiveUnavailable(RuntimeError):
    """Redis cannot answer: the Hive treats that as halted."""


async def read_halt(redis: Redis | None, settings: Settings) -> dict[str, Any] | None:
    if redis is None:
        raise HiveUnavailable("no Redis")
    try:
        raw = await redis.get(HiveKeys(settings).halt)
    except (RedisError, OSError) as exc:
        raise HiveUnavailable(type(exc).__name__) from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"reason": "HALTED"}
    except (json.JSONDecodeError, TypeError):
        return {"reason": "HALTED"}


async def set_halt(redis: Redis, settings: Settings, reason: str, *, by: str | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    flag = {"reason": reason, "by": by, "at": _utcnow().isoformat(), "detail": detail or {}}
    await redis.set(HiveKeys(settings).halt, json.dumps(flag, default=str))
    return flag


async def clear_halt(redis: Redis, settings: Settings) -> None:
    await redis.delete(HiveKeys(settings).halt)


# ---------------------------------------------------------------- order slicing (TWAP)
def slice_stake(total: Decimal, slice_size: Decimal, jitter_pct: float, rng: random.Random) -> list[Decimal]:
    """``ceil(total / slice_size)`` slices, each within ``jitter_pct`` of an equal split, summing to
    the total exactly (to the paisa)."""
    if total <= ZERO or slice_size <= ZERO:
        raise ValueError("stake and slice size must be positive")
    n = max(1, math.ceil(total / slice_size))
    if n == 1:
        return [total]
    jitter = jitter_pct / 100
    weights = [1 + rng.uniform(-jitter, jitter) for _ in range(n)]
    scale = total / Decimal(str(sum(weights)))
    slices = [(Decimal(str(w)) * scale).quantize(PAISA, rounding=ROUND_DOWN) for w in weights[:-1]]
    slices.append(total - sum(slices, ZERO))
    return slices


def slice_countdowns(n: int, low: int, high: int, rng: random.Random) -> list[int]:
    """The first slice goes now (the edge is fresh); each next one a random 60-120s after the last."""
    out, at = [0], 0
    for _ in range(n - 1):
        at += rng.randint(min(low, high), max(low, high))
        out.append(at)
    return out


class SliceScheduler(Protocol):
    def schedule(self, plan_id: uuid.UUID, index: int, countdown: int) -> None: ...


# ---------------------------------------------------------------- decisions
@dataclass(slots=True)
class Decision:
    bot_id: uuid.UUID
    event: HiveEventKind
    reason: str
    stake: Decimal | None = None
    ledger_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None


GatewayFor = Callable[[BotExecutionMode], BookmakerGateway | None]


class HiveEngine:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        gateway_for: GatewayFor,
        scheduler: SliceScheduler | None = None,
        clock: Callable[[], datetime] = _utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings
        self.keys = HiveKeys(settings)
        self.gateway_for = gateway_for
        self.scheduler = scheduler
        self.clock = clock
        self.rng = rng or random.SystemRandom()
        self._release = redis.register_script(_RELEASE_LOCK)

    # -------------------------------------------------------------- one signal
    async def process(self, edge: EdgeSignal, bots: Sequence[TradingBot]) -> list[Decision]:
        now = self.clock()
        if edge.expires_at <= now or not bots:
            return []
        try:
            if await read_halt(self.redis, self.settings) is not None:
                return []
        except HiveUnavailable:
            logger.warning("Hive: halt flag unreadable; nothing fires")
            return []
        market = await load_market(self.redis, self.settings, edge, now)
        readings = MarketReadings(market)
        decisions: list[Decision] = []
        # Merge within one owner's bots of one execution mode: shadow bots keep their own
        # hypothetical book, and paper (virtual) and live (real) money never pool into one order
        by_owner: dict[tuple[uuid.UUID, BotExecutionMode], list[TradingBot]] = defaultdict(list)
        for bot in bots:
            if bot.status is BotStatus.ACTIVE:
                by_owner[(bot.user_id, bot.execution_mode)].append(bot)
        for (owner, _), owned in by_owner.items():
            decisions += await self._for_owner(owner, owned, edge, readings, now)
        return decisions

    async def _for_owner(self, owner: uuid.UUID, bots: list[TradingBot], edge: EdgeSignal, readings: MarketReadings, now: datetime) -> list[Decision]:
        ready = [bot for bot in bots if not await self.redis.exists(self.keys.seen(bot.id, edge.fixture_id, edge.selection))]
        if not ready:
            return []
        proposals: list[tuple[TradingBot, Proposal, RiskContext]] = []
        decisions: list[Decision] = []
        async with self.session_factory() as session:
            contexts = {bot.id: await load_risk_context(session, bot, now) for bot in ready}
        for bot in ready:
            verdict = await asyncio.to_thread(evaluate, bot, readings, contexts[bot.id])
            if isinstance(verdict, Rejection):
                await self._event(bot, HiveEventKind.SKIPPED, verdict.reason, edge, detail=verdict.detail, dedupe=True)
                decisions.append(Decision(bot.id, HiveEventKind.SKIPPED, verdict.reason))
            else:
                proposals.append((bot, verdict, contexts[bot.id]))
        if not proposals:
            return decisions

        # Thundering herd: one order, sized by the strongest conviction; the rest merge into it
        proposals.sort(key=lambda item: (item[1].stake, item[1].ev, str(item[0].id)), reverse=True)
        winner, proposal, ctx = proposals[0]
        merged = [bot for bot, _, _ in proposals[1:]]
        token = uuid.uuid4().hex
        lock = self.keys.lock(owner, edge.fixture_id, edge.market_type)
        if not await self.redis.set(lock, token, nx=True, px=self.settings.HIVE_MERGE_LOCK_MS):
            await self._event(winner, HiveEventKind.SKIPPED, "MARKET_BUSY", edge, dedupe=True)
            return decisions + [Decision(winner.id, HiveEventKind.SKIPPED, "MARKET_BUSY")]
        try:
            for bot, other, _ in proposals[1:]:
                await self._event(bot, HiveEventKind.MERGED, "MERGED_INTO_STRONGER_BOT", edge, stake=other.stake, conviction=other.conviction, detail={"into_bot": str(winner.id), "into_stake": str(proposal.stake)})
                decisions.append(Decision(bot.id, HiveEventKind.MERGED, "MERGED_INTO_STRONGER_BOT", other.stake))
            decision = await self._fire(winner, proposal, ctx, edge)
            decisions.append(decision)
            if decision.event is not HiveEventKind.SKIPPED:
                for bot in [winner, *merged]:  # one entry per selection per cooldown, for every bot that matched
                    if bot.cooldown_seconds > 0:
                        await self.redis.set(self.keys.seen(bot.id, edge.fixture_id, edge.selection), "1", ex=bot.cooldown_seconds)
        finally:
            await self._release(keys=[lock], args=[token])
        return decisions

    async def _fire(self, bot: TradingBot, proposal: Proposal, ctx: RiskContext, edge: EdgeSignal) -> Decision:
        blocked = await self.wash_trade(bot, edge.fixture_id, edge.market_type, edge.selection)
        if blocked is not None:
            await self._event(bot, HiveEventKind.BLOCKED, "WASH_TRADE", edge, stake=proposal.stake, conviction=proposal.conviction, detail=blocked)
            return Decision(bot.id, HiveEventKind.BLOCKED, "WASH_TRADE", proposal.stake)
        breaker = await self.breakers(bot, ctx)
        if breaker is not None:
            await self._event(bot, HiveEventKind.BLOCKED, breaker, edge, stake=proposal.stake)
            return Decision(bot.id, HiveEventKind.BLOCKED, breaker, proposal.stake)

        if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
            async with self.session_factory() as session:
                session.add(
                    HiveShadowPosition(
                        user_id=bot.user_id, bot_id=bot.id, signal_id=edge.signal_id, fixture_id=edge.fixture_id, market=edge.market_type,
                        selection=edge.selection, bookmaker_id=edge.bookmaker_id, odds=Decimal(edge.odds), commission_rate=Decimal(edge.commission),
                        stake_inr=proposal.stake, true_prob=proposal.conviction, created_at=self.clock(),
                    )
                )
                await session.commit()
            await self._record_fire(bot)
            await self._event(bot, HiveEventKind.SHADOW_FILLED, "SHADOW_POSITION", edge, stake=proposal.stake, conviction=proposal.conviction, detail=proposal.detail())
            return Decision(bot.id, HiveEventKind.SHADOW_FILLED, "SHADOW_POSITION", proposal.stake)

        gateway = self.gateway_for(bot.execution_mode)
        if gateway is None:
            await self._event(bot, HiveEventKind.BLOCKED, "LIVE_EXECUTION_UNAVAILABLE", edge, stake=proposal.stake)
            return Decision(bot.id, HiveEventKind.BLOCKED, "LIVE_EXECUTION_UNAVAILABLE", proposal.stake)
        if bot.enable_order_slicing and proposal.stake > Decimal(bot.slice_size_inr):
            return await self._plan(bot, proposal, edge)

        ticket = self._ticket(bot, edge, proposal.stake, proposal.conviction, uuid.uuid5(_SLICE_NAMESPACE, f"{bot.id}|{edge.signal_id}"), None)
        return await self._execute(bot, gateway, ticket, edge, proposal.conviction, HiveEventKind.FIRED, detail=proposal.detail())

    # -------------------------------------------------------------- execution
    def _ticket(self, bot: TradingBot, edge: EdgeSignal, stake: Decimal, conviction: Decimal, key: uuid.UUID, group: uuid.UUID | None) -> OrderTicket:
        return OrderTicket(
            user_id=bot.user_id, idempotency_key=key, fixture_id=edge.fixture_id, market=edge.market_type, selection=edge.selection,
            bookmaker_id=edge.bookmaker_id, stake_inr=stake, odds=Decimal(edge.odds), true_prob=conviction, signal_id=edge.signal_id,
            commence_time=edge.commence_time, strategy="hive", group_id=group, bot_id=bot.id, commission=Decimal(edge.commission),
        )

    async def _execute(
        self, bot: TradingBot, gateway: BookmakerGateway, ticket: OrderTicket, edge: EdgeSignal, conviction: Decimal, event: HiveEventKind, *, detail: dict[str, Any] | None = None
    ) -> Decision:
        async with self.session_factory() as session:
            limits = await load_limits(session, bot.user_id)
        floor = slippage_floor(ticket.odds, conviction, limits.max_slippage_pct, ticket.commission)
        executor = TradeExecutor(self.session_factory, self.redis, self.settings, gateway, self.clock)
        try:
            receipt = await executor.execute_leg(ticket, min_odds=floor)
        except CfoError as exc:
            await self._event(bot, HiveEventKind.BLOCKED, exc.reason, edge, stake=ticket.stake_inr, conviction=conviction, detail={"message": exc.message, **(detail or {})})
            return Decision(bot.id, HiveEventKind.BLOCKED, exc.reason, ticket.stake_inr)
        await self._record_fire(bot)
        await self._event(
            bot, event, receipt.status, edge, stake=receipt.stake_inr, conviction=conviction,
            detail={"ledger_id": str(receipt.ledger_id), "remote_bet_id": receipt.remote_bet_id, "odds": str(receipt.odds), **(detail or {})},
        )
        return Decision(bot.id, event, receipt.status, receipt.stake_inr, receipt.ledger_id)

    async def _plan(self, bot: TradingBot, proposal: Proposal, edge: EdgeSignal) -> Decision:
        stakes = slice_stake(proposal.stake, Decimal(bot.slice_size_inr), self.settings.HIVE_SLICE_JITTER_PCT, self.rng)
        countdowns = slice_countdowns(len(stakes), self.settings.HIVE_SLICE_DELAY_MIN_SECONDS, self.settings.HIVE_SLICE_DELAY_MAX_SECONDS, self.rng)
        plan = HiveOrderPlan(
            id=uuid.uuid4(), user_id=bot.user_id, bot_id=bot.id, signal_id=edge.signal_id, fixture_id=edge.fixture_id, market=edge.market_type,
            selection=edge.selection, bookmaker_id=edge.bookmaker_id, odds=Decimal(edge.odds), true_prob=proposal.conviction,
            commence_time=edge.commence_time, total_stake_inr=proposal.stake, status=PlanStatus.SCHEDULED,
            slices=[{"index": i, "stake_inr": str(s), "countdown_s": c, "status": "SCHEDULED"} for i, (s, c) in enumerate(zip(stakes, countdowns, strict=True))],
        )
        async with self.session_factory() as session:
            session.add(plan)
            await session.commit()
        if self.scheduler is None:
            raise RuntimeError("order slicing needs a slice scheduler")
        for i, countdown in enumerate(countdowns):
            self.scheduler.schedule(plan.id, i, countdown)
        await self._event(
            bot, HiveEventKind.SLICED, "TWAP_SCHEDULED", edge, stake=proposal.stake, conviction=proposal.conviction,
            detail={"plan_id": str(plan.id), "slices": [str(s) for s in stakes], "countdowns": countdowns, **proposal.detail()},
        )
        return Decision(bot.id, HiveEventKind.SLICED, "TWAP_SCHEDULED", proposal.stake, plan_id=plan.id)

    async def run_slice(self, plan_id: uuid.UUID, index: int, active_edge: Callable[[str, str], Any] | None = None) -> Decision | None:
        """One TWAP slice (the Celery task calls this). Fires only while the edge is still live at no
        worse than the plan's price floor, nothing is halted, and no wash trade or breaker objects."""
        async with self.session_factory() as session:
            plan = await session.get(HiveOrderPlan, plan_id)
            bot = await session.get(TradingBot, plan.bot_id) if plan is not None else None
        if plan is None or bot is None or plan.status is PlanStatus.CANCELLED:
            return None
        stake = Decimal(plan.slices[index]["stake_inr"])

        async def cancel(reason: str) -> Decision:
            await self._update_plan(plan_id, index, "CANCELLED", reason, cancel_rest=True)
            await self._event(bot, HiveEventKind.SLICE_CANCELLED, reason, None, stake=stake, detail={"plan_id": str(plan_id), "slice": index}, market=(plan.fixture_id, plan.market, plan.selection))
            return Decision(bot.id, HiveEventKind.SLICE_CANCELLED, reason, stake, plan_id=plan_id)

        try:
            if await read_halt(self.redis, self.settings) is not None:
                return await cancel("HALTED")
        except HiveUnavailable:
            return await cancel("HALT_UNREADABLE")
        if bot.status is not BotStatus.ACTIVE:
            return await cancel(f"BOT_{bot.status}")
        edge = await (active_edge or self._active_edge)(plan.fixture_id, plan.selection)
        async with self.session_factory() as session:
            limits = await load_limits(session, bot.user_id)
        if edge is None or edge.expires_at <= self.clock():
            return await cancel("EDGE_GONE")
        floor = slippage_floor(Decimal(plan.odds), Decimal(plan.true_prob), limits.max_slippage_pct, Decimal(edge.commission))
        if Decimal(edge.odds) < floor:
            return await cancel("EDGE_GONE")
        gateway = self.gateway_for(bot.execution_mode)
        if gateway is None:
            return await cancel("LIVE_EXECUTION_UNAVAILABLE")
        token, lock = uuid.uuid4().hex, self.keys.lock(bot.user_id, plan.fixture_id, plan.market)
        if not await self.redis.set(lock, token, nx=True, px=self.settings.HIVE_MERGE_LOCK_MS):
            return await cancel("MARKET_BUSY")
        try:
            if await self.wash_trade(bot, plan.fixture_id, plan.market, plan.selection) is not None:
                return await cancel("WASH_TRADE")
            async with self.session_factory() as session:
                ctx = await load_risk_context(session, bot, self.clock())
            breaker = await self.breakers(bot, ctx)
            if breaker is not None:
                return await cancel(breaker)
            ticket = self._ticket(bot, edge, min(stake, ctx.available), Decimal(plan.true_prob), uuid.uuid5(_SLICE_NAMESPACE, f"{plan_id}|{index}"), plan_id)
            decision = await self._execute(bot, gateway, ticket, edge, Decimal(plan.true_prob), HiveEventKind.SLICE_FIRED, detail={"plan_id": str(plan_id), "slice": index})
        finally:
            await self._release(keys=[lock], args=[token])
        done = decision.event is HiveEventKind.SLICE_FIRED
        await self._update_plan(plan_id, index, "FIRED" if done else "FAILED", decision.reason, ledger_id=decision.ledger_id, cancel_rest=not done)
        return decision

    async def _active_edge(self, fixture_id: str, selection: str) -> EdgeSignal | None:
        try:
            raw = await self.redis.hget(AryabhataKeys(self.settings.ARYABHATA_PREFIX).active, f"{fixture_id}|{selection}")
            return EdgeSignal.model_validate_json(raw) if raw else None
        except (RedisError, OSError, ValidationError):
            return None

    async def _update_plan(self, plan_id: uuid.UUID, index: int, status: str, reason: str, *, ledger_id: uuid.UUID | None = None, cancel_rest: bool = False) -> None:
        async with self.session_factory() as session:
            plan = (await session.execute(select(HiveOrderPlan).where(HiveOrderPlan.id == plan_id).with_for_update())).scalar_one()
            slices = [dict(s) for s in plan.slices]
            slices[index].update(status=status, reason=reason, ledger_id=str(ledger_id) if ledger_id else None, at=self.clock().isoformat())
            if cancel_rest:
                for later in slices[index + 1 :]:
                    if later["status"] == "SCHEDULED":
                        later.update(status="CANCELLED", reason=reason)
            plan.slices = slices
            final = all(s["status"] != "SCHEDULED" for s in slices)
            plan.status = (PlanStatus.CANCELLED if cancel_rest and status != "FIRED" else PlanStatus.COMPLETE) if final else PlanStatus.RUNNING
            await session.commit()

    # -------------------------------------------------------------- the loophole checks
    async def wash_trade(self, bot: TradingBot, fixture_id: str, market: str, selection: str) -> dict[str, Any] | None:
        """An open position of the same owner on another outcome of this market, or None. Any
        account counts (every bot, and the main account); Group 64's arbitrage and hedge legs are
        the deliberate exception. A shadow bot is also checked against its owner's shadow book."""
        async with self.session_factory() as session:
            opposing = (
                await session.execute(
                    select(PhantomLedger.id, PhantomLedger.bot_id, PhantomLedger.selection, PhantomLedger.stake_inr, PhantomLedger.strategy).where(
                        PhantomLedger.user_id == bot.user_id,
                        PhantomLedger.fixture_id == fixture_id,
                        PhantomLedger.market == market,
                        PhantomLedger.status.in_(OPEN_STATUSES),
                        PhantomLedger.selection != selection,
                        (PhantomLedger.strategy.is_(None)) | (PhantomLedger.strategy.not_in(COORDINATED_STRATEGIES)),
                    )
                )
            ).first()
            if opposing is not None:
                return {"opposing_ledger_id": str(opposing.id), "opposing_bot_id": str(opposing.bot_id) if opposing.bot_id else None, "opposing_selection": opposing.selection}
            if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
                shadow = (
                    await session.execute(
                        select(HiveShadowPosition.id, HiveShadowPosition.bot_id, HiveShadowPosition.selection).where(
                            HiveShadowPosition.user_id == bot.user_id,
                            HiveShadowPosition.fixture_id == fixture_id,
                            HiveShadowPosition.market == market,
                            HiveShadowPosition.status == ShadowStatus.OPEN,
                            HiveShadowPosition.selection != selection,
                        )
                    )
                ).first()
                if shadow is not None:
                    return {"opposing_shadow_id": str(shadow.id), "opposing_bot_id": str(shadow.bot_id), "opposing_selection": shadow.selection}
        return None

    async def breakers(self, bot: TradingBot, ctx: RiskContext) -> str | None:
        """Trip a breaker (and suspend the bot) or return None."""
        now = time.time()
        key = self.keys.fires(bot.id)
        window = self.settings.HIVE_VELOCITY_WINDOW_SECONDS
        await self.redis.zremrangebyscore(key, "-inf", now - window)
        recent = await self.redis.zcard(key)
        if recent >= bot.max_bets_per_minute:
            await self.suspend(bot, "VELOCITY_BREAKER", {"orders_in_window": recent, "window_seconds": window})
            return "VELOCITY_BREAKER"
        drop = await self.drawdown_24h(bot, ctx)
        if drop is not None and drop * HUNDRED > Decimal(bot.drawdown_limit_pct):
            await self.suspend(bot, "DRAWDOWN_BREAKER", {"drop_pct": str((drop * HUNDRED).quantize(PAISA)), "limit_pct": str(bot.drawdown_limit_pct)})
            return "DRAWDOWN_BREAKER"
        return None

    async def drawdown_24h(self, bot: TradingBot, ctx: RiskContext) -> Decimal | None:
        """How far the sub-bankroll fell over 24h of realised P&L, as a fraction of where it started."""
        since = self.clock() - timedelta(hours=24)
        async with self.session_factory() as session:
            if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
                rows = (
                    await session.execute(select(HiveShadowPosition.pnl_inr).where(HiveShadowPosition.bot_id == bot.id, HiveShadowPosition.settled_at >= since))
                ).scalars()
                pnl = sum((Decimal(p) for p in rows if p is not None), ZERO)
            else:
                pnl = await realized_pnl_24h(session, bot.user_id, self.clock(), bot.id)
        if pnl >= ZERO:
            return None
        start = ctx.equity - pnl
        return -pnl / start if start > ZERO else ONE

    async def _record_fire(self, bot: TradingBot) -> None:
        now = time.time()
        key = self.keys.fires(bot.id)
        await self.redis.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex[:6]}": now})
        await self.redis.expire(key, self.settings.HIVE_VELOCITY_WINDOW_SECONDS * 2)

    async def suspend(self, bot: TradingBot, reason: str, detail: dict[str, Any] | None = None) -> None:
        now = self.clock()
        async with self.session_factory() as session:
            await session.execute(
                update(TradingBot).where(TradingBot.id == bot.id, TradingBot.status == BotStatus.ACTIVE).values(status=BotStatus.SUSPENDED, suspended_reason=reason, suspended_at=now, updated_at=now)
            )
            await session.commit()
        bot.status, bot.suspended_reason, bot.suspended_at = BotStatus.SUSPENDED, reason, now
        await self._event(bot, HiveEventKind.SUSPENDED, reason, None, detail=detail)

    # -------------------------------------------------------------- maintenance (one leader at a time)
    async def flash_crash_scan(self) -> dict[str, Any] | None:
        """A market shock halts every bot: a selection whose consensus probability (the fair price
        across all fresh books, not one book's quote) swung more than ``HIVE_FLASH_THRESHOLD_PCT``
        within ``HIVE_FLASH_WINDOW_SECONDS``. The halt stays until a person lifts it."""
        now = time.time()
        window = self.settings.HIVE_FLASH_WINDOW_SECONDS
        try:
            cells: list[str] = await self.redis.zrangebyscore(f"{self.settings.LIVE_ODDS_CHANNEL}:board:ts", now - window, "+inf")
            if not cells:
                return None
            pipe = self.redis.pipeline(transaction=False)
            for cell in cells:
                pipe.zrangebyscore(f"{self.settings.LIVE_ODDS_CHANNEL}:hist:{cell}", now - window, "+inf")
            histories = await pipe.execute()
        except (RedisError, OSError):
            return None
        worst: tuple[str, float, float, float, int] | None = None
        for cell, members in zip(cells, histories, strict=True):
            probs = []
            for member in members or []:
                try:
                    p = float(str(member).split("|")[2])
                except (IndexError, ValueError):
                    continue
                if math.isfinite(p) and 0.0 < p < 1.0:
                    probs.append(p)
            if len(probs) < self.settings.HIVE_FLASH_MIN_POINTS:
                continue
            low, high = min(probs), max(probs)
            swing = (high - low) / low * 100
            if swing > self.settings.HIVE_FLASH_THRESHOLD_PCT and (worst is None or swing > worst[1]):
                worst = (cell, swing, low, high, len(probs))
        if worst is None:
            return None
        try:
            if await read_halt(self.redis, self.settings) is not None:
                return None  # already halted
        except HiveUnavailable:
            return None
        cell, swing, low, high, points = worst
        flag = await set_halt(
            self.redis, self.settings, "FLASH_CRASH", by="hive",
            detail={"market": cell, "swing_pct": round(swing, 2), "low": low, "high": high, "points": points, "window_seconds": window},
        )
        logger.warning("Hive: flash crash on %s (%.1f%% in %ss): every bot halted", cell, swing, window)
        async with self.session_factory() as session:
            owners = (await session.execute(select(TradingBot.user_id).where(TradingBot.status == BotStatus.ACTIVE).distinct())).scalars().all()
            for owner in owners:
                session.add(HiveBotEvent(user_id=owner, bot_id=None, event=HiveEventKind.HALTED, reason="FLASH_CRASH", fixture_id=cell.split("|")[0], detail=flag["detail"], created_at=self.clock()))
            await session.commit()
        return flag

    async def grade_shadow(self) -> int:
        """Shadow positions on graded markets: WON / LOST / VOID with their hypothetical P&L."""
        from app.models.cfo_vault import MarketResult  # noqa: PLC0415 - local: keeps the import graph flat

        graded = 0
        now = self.clock()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(HiveShadowPosition, MarketResult)
                    .join(MarketResult, (MarketResult.fixture_id == HiveShadowPosition.fixture_id) & (MarketResult.market == HiveShadowPosition.market))
                    .where(HiveShadowPosition.status == ShadowStatus.OPEN)
                    .with_for_update(of=HiveShadowPosition)
                )
            ).all()
            for position, result in rows:
                stake, odds = Decimal(position.stake_inr), Decimal(position.odds)
                if result.is_void:
                    position.status, position.pnl_inr = ShadowStatus.VOID, ZERO
                elif result.winning_selection == position.selection:
                    net_win = stake * (odds - ONE) * (ONE - Decimal(position.commission_rate or ZERO))
                    position.status, position.pnl_inr = ShadowStatus.WON, net_win.quantize(PAISA, rounding=ROUND_DOWN)
                else:
                    position.status, position.pnl_inr = ShadowStatus.LOST, -stake
                position.settled_at = now
                graded += 1
            await session.commit()
        return graded

    async def sweep_breakers(self) -> list[uuid.UUID]:
        """The drawdown breaker between signals too: a bot can be losing while nothing is firing."""
        tripped: list[uuid.UUID] = []
        async with self.session_factory() as session:
            bots = (await session.execute(select(TradingBot).where(TradingBot.status == BotStatus.ACTIVE))).scalars().all()
            contexts = {bot.id: await load_risk_context(session, bot, self.clock()) for bot in bots}
        for bot in bots:
            drop = await self.drawdown_24h(bot, contexts[bot.id])
            if drop is not None and drop * HUNDRED > Decimal(bot.drawdown_limit_pct):
                await self.suspend(bot, "DRAWDOWN_BREAKER", {"drop_pct": str((drop * HUNDRED).quantize(PAISA)), "limit_pct": str(bot.drawdown_limit_pct)})
                tripped.append(bot.id)
        return tripped

    # -------------------------------------------------------------- the decision log
    async def _event(
        self,
        bot: TradingBot,
        event: HiveEventKind,
        reason: str,
        edge: EdgeSignal | None,
        *,
        stake: Decimal | None = None,
        conviction: Decimal | None = None,
        detail: dict[str, Any] | None = None,
        dedupe: bool = False,
        market: tuple[str, str, str] | None = None,
    ) -> None:
        if dedupe and edge is not None:
            key = self.keys.dedupe(bot.id, reason, f"{edge.fixture_id}|{edge.selection}")
            if not await self.redis.set(key, "1", nx=True, ex=self.settings.HIVE_EVENT_DEDUPE_SECONDS):
                return
        fixture, mkt, selection = (edge.fixture_id, edge.market_type, edge.selection) if edge is not None else (market or (None, None, None))
        async with self.session_factory() as session:
            session.add(
                HiveBotEvent(
                    user_id=bot.user_id, bot_id=bot.id, event=event, reason=reason[:64], signal_id=edge.signal_id if edge else None,
                    fixture_id=fixture, market=mkt, selection=selection, stake_inr=stake, odds=Decimal(edge.odds) if edge else None,
                    conviction=conviction.quantize(Decimal("0.0000000001")) if conviction is not None else None,
                    detail=json.loads(json.dumps(detail or {}, default=str)), created_at=self.clock(),
                )
            )
            await session.commit()
