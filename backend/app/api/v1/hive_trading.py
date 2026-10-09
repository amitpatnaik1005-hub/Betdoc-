"""The Hive's autonomous trading bots under ``/api/v1/hive/trading`` (Group 65).

    GET    /hive/trading/registry              the seeded model registry (math, risk, bet types)
    POST   /hive/trading/registry/seed         re-seed it (admin)
    GET    /hive/trading/bots                  your bots, each with its sub-account
    POST   /hive/trading/bots                  create one (paused, paper by default)
    PATCH  /hive/trading/bots/{id}             change its pipeline or parameters
    POST   /hive/trading/bots/{id}/status      ACTIVE or PAUSED (also resumes a suspended bot)
    PUT    /hive/trading/bots/{id}/capital     set its allocation (the capital slider)
    DELETE /hive/trading/bots/{id}             archive it (no positions, no capital left)
    GET    /hive/trading/halt                  the master kill switch
    PUT    /hive/trading/halt                  halt every bot (anyone) / resume (admin)
    GET    /hive/trading/topology              who holds what, where bots collide
    GET    /hive/trading/events                the decision log
    GET    /hive/trading/plans                 TWAP order plans

Refusals carry ``{"reason", "message", ...}`` in ``detail``.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.cfo_vault import OPEN_STATUSES, AccountFunding, LedgerStatus, PhantomLedger
from app.models.hive_bots import BotExecutionMode, BotStatus, HiveBotEvent, HiveEventKind, HiveOrderPlan, HiveShadowPosition, ShadowStatus, TradingBot
from app.models.the_core import ComponentKind
from app.schemas.hive_trading import (
    BotCreate,
    BotRead,
    BotStatusChange,
    BotUpdate,
    CapitalChange,
    ComponentRead,
    EventRead,
    HaltChange,
    HaltRead,
    PlanRead,
    RegistryRead,
    SubAccountRead,
    TopologyLink,
    TopologyMarket,
    TopologyRead,
)
from app.services.cfo_ledger import CfoError, allocate, deallocate, read_account
from app.services.hive_engine import HiveKeys, HiveUnavailable, clear_halt, read_halt, set_halt
from app.services.hive_pipeline import validate_pipeline
from app.services.hive_registry import CATALOGUE, EXPECTED, registry_components, seed_registry
from app.services.portfolio_manager import board_markets

router = APIRouter(prefix="/hive/trading", tags=["The Hive · trading bots"])

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]
ZERO = Decimal(0)


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _refuse(code: int, reason: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(code, {"reason": reason, "message": message, **extra})


def _http(exc: CfoError) -> HTTPException:
    return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, **exc.detail})


# ---------------------------------------------------------------- registry
@router.get("/registry", response_model=RegistryRead)
async def registry(user: CurrentUser, sessions: SessionFactory) -> RegistryRead:  # noqa: ARG001
    async with sessions() as session:
        rows = await registry_components(session)
    components = sorted(
        (
            ComponentRead(
                key=key, kind=str(row.component_kind), name=row.name.split(" · ", 1)[-1], description=row.description, category=row.category,
                implementation=row.implementation, live_capable=row.live_capable, source=row.catalogue_source,
            )
            for key, row in rows.items()
        ),
        key=lambda c: (c.kind, not c.live_capable, c.category or "", c.name),
    )
    counts: dict[str, int] = {}
    live: dict[str, int] = {}
    for c in components:
        counts[c.kind] = counts.get(c.kind, 0) + 1
        live[c.kind] = live.get(c.kind, 0) + int(c.live_capable)
    return RegistryRead(components=components, counts=counts, live_counts=live, expected={k.value: v for k, v in EXPECTED.items()})


@router.post("/registry/seed")
async def reseed(admin: CurrentAdmin, sessions: SessionFactory) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        report = await seed_registry(session, CATALOGUE)
        await session.commit()
    return report.as_dict()


# ---------------------------------------------------------------- bots
async def _owned(session: AsyncSession, user_id: uuid.UUID, bot_id: uuid.UUID, *, lock: bool = False) -> TradingBot:
    stmt = select(TradingBot).where(TradingBot.id == bot_id, TradingBot.user_id == user_id, TradingBot.status != BotStatus.ARCHIVED)
    bot = (await session.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if bot is None:
        raise _refuse(404, "NOT_FOUND", "No such bot")
    return bot


async def _account_view(session: AsyncSession, bot: TradingBot) -> SubAccountRead:
    if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
        rows = (await session.execute(select(HiveShadowPosition).where(HiveShadowPosition.bot_id == bot.id))).scalars().all()
        open_rows = [r for r in rows if r.status is ShadowStatus.OPEN]
        realized = sum((Decimal(r.pnl_inr) for r in rows if r.pnl_inr is not None), ZERO)
        exposure = sum((Decimal(r.stake_inr) for r in open_rows), ZERO)
        equity = Decimal(bot.allocated_capital) + realized
        return SubAccountRead(funding="SHADOW", available=max(equity - exposure, ZERO), exposure=exposure, equity=equity, realized_pnl=realized, open_positions=len(open_rows))
    account = await read_account(session, bot.user_id, bot.id)
    open_count = await session.scalar(select(func.count()).select_from(PhantomLedger).where(PhantomLedger.bot_id == bot.id, PhantomLedger.status.in_(OPEN_STATUSES)))
    realized = await session.scalar(
        select(func.coalesce(func.sum(PhantomLedger.realized_pnl), 0)).where(PhantomLedger.bot_id == bot.id, PhantomLedger.status.in_((LedgerStatus.WON, LedgerStatus.LOST)))
    )
    if account is None:
        return SubAccountRead(funding=None, available=ZERO, exposure=ZERO, equity=ZERO, realized_pnl=Decimal(str(realized or 0)), open_positions=int(open_count or 0))
    return SubAccountRead(
        funding=account.funding, available=account.available_balance, exposure=account.exposure_balance, equity=account.equity,
        realized_pnl=Decimal(str(realized or 0)), open_positions=int(open_count or 0),
    )


async def _read(session: AsyncSession, redis: Redis | None, settings: Settings, bot: TradingBot, registry_rows: dict[str, Any]) -> BotRead:
    recent = 0
    if redis is not None:
        try:
            recent = int(await redis.zcount(HiveKeys(settings).fires(bot.id), time.time() - settings.HIVE_VELOCITY_WINDOW_SECONDS, "+inf"))
        except (RedisError, OSError):
            recent = 0
    return BotRead(
        id=bot.id, name=bot.name, description=bot.description, execution_mode=bot.execution_mode, status=bot.status, math_models=list(bot.math_models or []),
        risk_models=list(bot.risk_models or []), target_bet_types=list(bot.target_bet_types or []), risk_params=dict(bot.risk_params or {}),
        allocated_capital=bot.allocated_capital, kelly_multiplier=bot.kelly_multiplier, max_stake_pct=bot.max_stake_pct, min_edge_pct=bot.min_edge_pct,
        min_quoting_books=bot.min_quoting_books, min_market_liquidity=bot.min_market_liquidity, enable_order_slicing=bot.enable_order_slicing,
        slice_size_inr=bot.slice_size_inr, max_bets_per_minute=bot.max_bets_per_minute, drawdown_limit_pct=bot.drawdown_limit_pct,
        cooldown_seconds=bot.cooldown_seconds, suspended_reason=bot.suspended_reason, suspended_at=bot.suspended_at, created_at=bot.created_at,
        updated_at=bot.updated_at, account=await _account_view(session, bot), orders_last_minute=recent,
        pipeline_problems=validate_pipeline(bot.math_models or [], bot.risk_models or [], bot.target_bet_types or [], registry_rows),
    )


@router.get("/bots", response_model=list[BotRead])
async def bots(request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> list[BotRead]:
    async with sessions() as session:
        rows = (await session.execute(select(TradingBot).where(TradingBot.user_id == user.id, TradingBot.status != BotStatus.ARCHIVED).order_by(TradingBot.created_at))).scalars().all()
        registry_rows = await registry_components(session)
        return [await _read(session, _redis(request), settings, bot, registry_rows) for bot in rows]


def _apply(bot: TradingBot, payload: BotCreate | BotUpdate) -> bool:
    """Copy the given fields onto the bot. Returns whether its pipeline changed."""
    data = payload.model_dump(exclude_unset=isinstance(payload, BotUpdate), exclude_none=isinstance(payload, BotUpdate))
    pipeline_changed = False
    for field_name, value in data.items():
        if field_name in ("math_models", "risk_models", "target_bet_types", "risk_params") and value != getattr(bot, field_name, None):
            pipeline_changed = True
        if field_name != "execution_mode":
            setattr(bot, field_name, value if value is not None else getattr(bot, field_name))
    return pipeline_changed


@router.post("/bots", response_model=BotRead, status_code=status.HTTP_201_CREATED)
async def create_bot(payload: BotCreate, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BotRead:
    async with sessions() as session:
        registry_rows = await registry_components(session)
        problems = validate_pipeline(payload.math_models, payload.risk_models, payload.target_bet_types, registry_rows)
        if problems:
            raise _refuse(422, "INVALID_PIPELINE", "This pipeline cannot run on live signals", problems=problems)
        now = datetime.now(UTC)
        bot = TradingBot(
            id=uuid.uuid4(), user_id=user.id, name=payload.name, description=payload.description or "", execution_mode=payload.execution_mode,
            status=BotStatus.PAUSED, allocated_capital=ZERO, pipeline_updated_at=now, created_at=now, updated_at=now,
        )
        _apply(bot, payload)
        session.add(bot)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _refuse(409, "NAME_TAKEN", "You already have a bot with that name") from exc
        return await _read(session, _redis(request), settings, bot, registry_rows)


async def _require_empty(session: AsyncSession, bot: TradingBot) -> None:
    view = await _account_view(session, bot)
    if view.open_positions:
        raise _refuse(409, "OPEN_POSITIONS", "This bot still holds open positions")
    if view.equity != ZERO or (bot.execution_mode is BotExecutionMode.SHADOW_MODE and Decimal(bot.allocated_capital) != ZERO):
        raise _refuse(409, "RELEASE_CAPITAL_FIRST", "Release this bot's capital first (capital slider to zero)")


@router.patch("/bots/{bot_id}", response_model=BotRead)
async def update_bot(bot_id: uuid.UUID, payload: BotUpdate, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BotRead:
    async with sessions() as session:
        bot = await _owned(session, user.id, bot_id, lock=True)
        registry_rows = await registry_components(session)
        if payload.execution_mode is not None and payload.execution_mode is not bot.execution_mode:
            if bot.status is BotStatus.ACTIVE:
                raise _refuse(409, "PAUSE_FIRST", "Pause the bot before changing how it executes")
            await _require_empty(session, bot)  # paper capital never turns into live capital, nor back
            bot.execution_mode = payload.execution_mode
        changed = _apply(bot, payload)
        problems = validate_pipeline(bot.math_models or [], bot.risk_models or [], bot.target_bet_types or [], registry_rows)
        if problems:
            raise _refuse(422, "INVALID_PIPELINE", "This pipeline cannot run on live signals", problems=problems)
        if changed:
            bot.pipeline_updated_at = datetime.now(UTC)  # the model-decay clock restarts
        bot.updated_at = datetime.now(UTC)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _refuse(409, "NAME_TAKEN", "You already have a bot with that name") from exc
        return await _read(session, _redis(request), settings, bot, registry_rows)


@router.post("/bots/{bot_id}/status", response_model=BotRead)
async def change_status(bot_id: uuid.UUID, payload: BotStatusChange, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BotRead:
    async with sessions() as session:
        bot = await _owned(session, user.id, bot_id, lock=True)
        registry_rows = await registry_components(session)
        if payload.status == "ACTIVE":
            problems = validate_pipeline(bot.math_models or [], bot.risk_models or [], bot.target_bet_types or [], registry_rows)
            if problems:
                raise _refuse(422, "INVALID_PIPELINE", "This pipeline cannot run on live signals", problems=problems)
            if bot.execution_mode is BotExecutionMode.LIVE_EXECUTION and settings.CFO_EXECUTION_MODE != "live":
                raise _refuse(409, "PLATFORM_NOT_LIVE", "Live bots need the platform in live execution mode (CFO_EXECUTION_MODE=live)")
            view = await _account_view(session, bot)
            if view.equity <= ZERO:
                raise _refuse(409, "NO_CAPITAL", "Allocate capital to this bot before activating it")
        bot.status = BotStatus(payload.status)
        bot.suspended_reason, bot.suspended_at = None, None
        bot.updated_at = datetime.now(UTC)
        await session.commit()
        return await _read(session, _redis(request), settings, bot, registry_rows)


@router.put("/bots/{bot_id}/capital", response_model=BotRead)
async def set_capital(bot_id: uuid.UUID, payload: CapitalChange, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BotRead:
    """The capital slider. A live bot's capital is moved out of (and back into) the main account; a
    paper bot's is virtual and never touches it; a shadow bot's is only the base its stakes size on."""
    target = payload.allocated_capital
    async with sessions() as session:
        bot = await _owned(session, user.id, bot_id, lock=True)
        delta = target - Decimal(bot.allocated_capital)
        try:
            if bot.execution_mode is BotExecutionMode.SHADOW_MODE:
                pass
            elif delta > ZERO:
                funding = AccountFunding.TRANSFER if bot.execution_mode is BotExecutionMode.LIVE_EXECUTION else AccountFunding.VIRTUAL
                await allocate(session, settings, user_id=user.id, bot_id=bot.id, amount=delta, funding=funding)
            elif delta < ZERO:
                await deallocate(session, settings, user_id=user.id, bot_id=bot.id, amount=-delta)
        except CfoError as exc:
            await session.rollback()
            raise _http(exc) from exc
        bot.allocated_capital = target
        bot.updated_at = datetime.now(UTC)
        session.add(HiveBotEvent(user_id=user.id, bot_id=bot.id, event=HiveEventKind.ALLOCATED, reason="CAPITAL_SET", stake_inr=target, detail={"delta": str(delta)}))
        await session.commit()
        return await _read(session, _redis(request), settings, bot, await registry_components(session))


@router.delete("/bots/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_bot(bot_id: uuid.UUID, user: CurrentUser, sessions: SessionFactory) -> None:
    async with sessions() as session:
        bot = await _owned(session, user.id, bot_id, lock=True)
        await _require_empty(session, bot)
        bot.status = BotStatus.ARCHIVED
        bot.updated_at = datetime.now(UTC)
        await session.commit()


# ---------------------------------------------------------------- the master kill switch
@router.get("/halt", response_model=HaltRead)
async def halt_state(request: Request, user: CurrentUser, settings: AppSettings) -> HaltRead:  # noqa: ARG001
    try:
        flag = await read_halt(_redis(request), settings)
    except HiveUnavailable:
        return HaltRead(halted=True, reason="HALT_UNREADABLE", detail={"message": "Redis is unavailable: no bot can fire"})
    return HaltRead(halted=False) if flag is None else HaltRead(halted=True, **{k: flag.get(k) for k in ("reason", "by", "at")}, detail=flag.get("detail") or {})


@router.put("/halt", response_model=HaltRead)
async def set_halt_state(payload: HaltChange, request: Request, user: CurrentUser, settings: AppSettings) -> HaltRead:
    """Anyone can halt every bot; only an administrator can resume them."""
    redis = _redis(request)
    if redis is None:
        raise _refuse(503, "HIVE_UNAVAILABLE", "Redis is unavailable")
    if payload.halted:
        flag = await set_halt(redis, settings, payload.reason, by=user.username)
        return HaltRead(halted=True, reason=flag["reason"], by=flag["by"], at=flag["at"], detail=flag["detail"])
    if user.role != "ADMIN":
        raise _refuse(403, "ADMIN_ONLY", "Only an administrator can resume autonomous trading")
    await clear_halt(redis, settings)
    return HaltRead(halted=False)


# ---------------------------------------------------------------- topology, events, plans
@router.get("/topology", response_model=TopologyRead)
async def topology(request: Request, user: CurrentUser, sessions: SessionFactory) -> TopologyRead:
    async with sessions() as session:
        owned = (await session.execute(select(TradingBot).where(TradingBot.user_id == user.id, TradingBot.status != BotStatus.ARCHIVED))).scalars().all()
        ledger = (await session.execute(select(PhantomLedger).where(PhantomLedger.user_id == user.id, PhantomLedger.status.in_(OPEN_STATUSES)))).scalars().all()
        shadow = (await session.execute(select(HiveShadowPosition).where(HiveShadowPosition.user_id == user.id, HiveShadowPosition.status == ShadowStatus.OPEN))).scalars().all()
    links = [
        TopologyLink(
            holder=str(row.bot_id) if row.bot_id else "main", market_key=f"{row.fixture_id}|{row.market}", fixture_id=row.fixture_id, market=row.market,
            selection=row.selection, stake_inr=row.stake_inr, source="ledger", strategy=row.strategy,
        )
        for row in ledger
    ] + [
        TopologyLink(holder=str(row.bot_id), market_key=f"{row.fixture_id}|{row.market}", fixture_id=row.fixture_id, market=row.market, selection=row.selection, stake_inr=row.stake_inr, source="shadow")
        for row in shadow
    ]
    metas = await board_markets(_redis(request))
    markets: dict[str, TopologyMarket] = {}
    for link in links:
        meta = metas.get(link.market_key)
        entry = markets.setdefault(
            link.market_key,
            TopologyMarket(
                market_key=link.market_key, fixture_id=link.fixture_id, market=link.market, home=meta.home if meta else "", away=meta.away if meta else "",
                commence_time=meta.commence_time if meta else None, holders=[], selections=[], collision=False, opposing=False,
            ),
        )
        if link.holder not in entry.holders:
            entry.holders.append(link.holder)
        if link.selection not in entry.selections:
            entry.selections.append(link.selection)
    for entry in markets.values():
        real = {l.selection for l in links if l.market_key == entry.market_key and l.source == "ledger" and l.strategy not in ("arbitrage", "hedge")}
        every = {l.selection for l in links if l.market_key == entry.market_key}
        entry.collision = len(entry.holders) > 1
        entry.opposing = len(real) > 1
        entry.shadow_overlap = not entry.opposing and len(every) > 1
    return TopologyRead(
        bots=[{"id": str(b.id), "name": b.name, "status": str(b.status), "execution_mode": str(b.execution_mode)} for b in owned],
        markets=sorted(markets.values(), key=lambda m: (m.commence_time or datetime.max.replace(tzinfo=UTC), m.market_key)),
        links=links,
    )


@router.get("/events", response_model=list[EventRead])
async def events(
    user: CurrentUser, sessions: SessionFactory, bot_id: uuid.UUID | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[HiveBotEvent]:
    async with sessions() as session:
        stmt = select(HiveBotEvent).where(HiveBotEvent.user_id == user.id)
        if bot_id is not None:
            stmt = stmt.where(HiveBotEvent.bot_id == bot_id)
        return list((await session.execute(stmt.order_by(HiveBotEvent.created_at.desc()).limit(limit))).scalars().all())


@router.get("/plans", response_model=list[PlanRead])
async def plans(user: CurrentUser, sessions: SessionFactory, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[HiveOrderPlan]:
    async with sessions() as session:
        stmt = select(HiveOrderPlan).where(HiveOrderPlan.user_id == user.id).order_by(HiveOrderPlan.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


__all__ = ["router", "ComponentKind"]
