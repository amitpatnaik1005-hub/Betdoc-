"""CFO ledger API under ``/api/v1/omni``: two-phase execution, bankroll, risk settings, settlement.

Every refusal carries ``{"reason", "message", ...}`` in ``detail``; ``reason`` is the same code the
audit log records (``BLOCKED_BY_DRAWDOWN``, ``BANKROLL_LOCKED``, ``BOOKMAKER_HTTP_500``, ...).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models.cfo_vault import AuditLog, LedgerStatus, MarketResult, PhantomLedger, RiskGuardSettings
from app.schemas.cfo_vault import (
    AuditRead,
    BankrollRead,
    ExecuteTradeRequest,
    ExecutionReceipt,
    MarketResultCreate,
    PositionRead,
    ReconcileRequest,
    RiskSettingsRead,
    RiskSettingsUpdate,
)
from app.services.bookmaker_gateway import BookmakerGateway
from app.services.cfo_execution import TradeExecutor
from app.services.cfo_ledger import CfoError, ZERO, opening_balance, read_account, reconcile
from app.services.risk_guard import (
    RiskGuardViolation,
    drawdown_limit,
    kill_switch_engaged,
    load_limits,
    read_loss_streak,
    realized_pnl_24h,
)

logger = logging.getLogger("betdoc.cfo")

router = APIRouter(prefix="/omni", tags=["cfo"])


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _http(exc: CfoError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.message, **exc.detail})


# ---------------------------------------------------------------- execution
@router.post(
    "/execute-trade",
    response_model=ExecutionReceipt,
    responses={202: {"model": ExecutionReceipt, "description": "Sent, but the bookmaker never confirmed: the stake stays in exposure"}},
)
async def execute_trade(
    payload: ExecuteTradeRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    sessions: SessionFactory,
    settings: AppSettings,
) -> ExecutionReceipt:
    gateway: BookmakerGateway | None = getattr(request.app.state, "bookmaker", None)
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "BOOKMAKER_UNCONFIGURED", "message": "No bookmaker gateway is configured"})
    executor = TradeExecutor(sessions, _redis(request), settings, gateway)
    try:
        receipt = await executor.execute(user.id, payload)
    except CfoError as exc:
        raise _http(exc) from exc
    if receipt.status == "UNKNOWN":
        response.status_code = status.HTTP_202_ACCEPTED
    return receipt


# ---------------------------------------------------------------- bankroll and positions
@router.get("/bankroll", response_model=BankrollRead)
async def bankroll(request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BankrollRead:
    redis = _redis(request)
    async with sessions() as session:
        account = await read_account(session, user.id)
        limits = await load_limits(session, user.id)
        pnl = await realized_pnl_24h(session, user.id, datetime.now(UTC))
        streak: int | None
        try:
            streak = await read_loss_streak(redis, settings, session, user.id)
        except RiskGuardViolation:
            streak = None  # Redis down: shown as unknown, and executions are refused meanwhile
        positions = (
            await session.execute(
                select(PhantomLedger)
                .where(PhantomLedger.user_id == user.id, PhantomLedger.bot_id.is_(None), PhantomLedger.status == LedgerStatus.PENDING)
                .order_by(PhantomLedger.created_at.desc())
            )
        ).scalars().all()
        if account is None:
            start = await opening_balance(session, user.id, settings)
            available, exposure, peak = start, ZERO, start
        else:
            available, exposure, peak = account.available_balance, account.exposure_balance, account.peak_balance
    return BankrollRead(
        opened=account is not None,
        currency="INR",
        available_balance=available,
        exposure_balance=exposure,
        equity=available + exposure,
        peak_balance=peak,
        pnl_24h=pnl,
        drawdown_limit=drawdown_limit(peak, limits) if peak > 0 else None,
        loss_streak=streak,
        kill_switch=bool(await kill_switch_engaged(redis, settings)),
        execution_mode=settings.CFO_EXECUTION_MODE,
        limits=RiskSettingsRead(
            daily_drawdown_pct=limits.daily_drawdown_pct,
            max_market_exposure_pct=limits.max_market_exposure_pct,
            max_loss_streak=limits.max_loss_streak,
            velocity_max_cv_pct=limits.velocity_max_cv_pct,
            max_slippage_pct=limits.max_slippage_pct,
        ),
        open_positions=[PositionRead.model_validate(p) for p in positions],
    )


@router.get("/positions", response_model=list[PositionRead])
async def positions(
    user: CurrentUser,
    sessions: SessionFactory,
    status_filter: Annotated[LedgerStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PositionRead]:
    stmt = select(PhantomLedger).where(PhantomLedger.user_id == user.id, PhantomLedger.bot_id.is_(None))  # bots: /hive/trading
    if status_filter is not None:
        stmt = stmt.where(PhantomLedger.status == status_filter)
    async with sessions() as session:
        rows = (await session.execute(stmt.order_by(PhantomLedger.created_at.desc()).limit(limit))).scalars().all()
    return [PositionRead.model_validate(r) for r in rows]


@router.get("/audit", response_model=list[AuditRead])
async def audit_trail(user: CurrentUser, sessions: SessionFactory, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[AuditRead]:
    async with sessions() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
    return [AuditRead.model_validate(r) for r in rows]


# ---------------------------------------------------------------- risk settings (Control Panel, Risk management)
@router.get("/risk-settings", response_model=RiskSettingsRead)
async def read_risk_settings(user: CurrentUser, sessions: SessionFactory) -> RiskSettingsRead:
    async with sessions() as session:
        limits = await load_limits(session, user.id)
    return RiskSettingsRead(
        daily_drawdown_pct=limits.daily_drawdown_pct,
        max_market_exposure_pct=limits.max_market_exposure_pct,
        max_loss_streak=limits.max_loss_streak,
        velocity_max_cv_pct=limits.velocity_max_cv_pct,
        max_slippage_pct=limits.max_slippage_pct,
    )


@router.put("/risk-settings", response_model=RiskSettingsRead)
async def update_risk_settings(payload: RiskSettingsUpdate, user: CurrentUser, sessions: SessionFactory) -> RiskSettingsRead:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with sessions() as session:
        try:
            row = await session.get(RiskGuardSettings, user.id, with_for_update=True)
            if row is None:
                row = RiskGuardSettings(user_id=user.id)
                session.add(row)
            for name, value in changes.items():
                setattr(row, name, value)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "SETTINGS_CONFLICT", "message": "Settings changed concurrently; try again"}) from exc
        except Exception:
            await session.rollback()
            raise
        await session.refresh(row)
        return RiskSettingsRead.model_validate(row)


# ---------------------------------------------------------------- settlement inputs (admin)
@router.post("/results", status_code=status.HTTP_201_CREATED)
async def record_result(payload: MarketResultCreate, admin: CurrentAdmin, sessions: SessionFactory) -> dict[str, str]:
    """Grade a market. ``cfo.settle_markets`` pays out every pending bet on it within a minute."""
    async with sessions() as session:
        existing = await session.scalar(select(MarketResult).where(MarketResult.fixture_id == payload.fixture_id, MarketResult.market == payload.market))
        if existing is not None:
            same = existing.is_void == payload.is_void and existing.winning_selection == payload.winning_selection
            if not same:
                raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "RESULT_CONFLICT", "message": "This market already has a different result"})
            return {"status": "unchanged", "id": str(existing.id)}
        row = MarketResult(
            fixture_id=payload.fixture_id,
            market=payload.market,
            winning_selection=payload.winning_selection,
            is_void=payload.is_void,
            source=payload.source,
            recorded_by=admin.id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "RESULT_CONFLICT", "message": "This market was graded concurrently"}) from exc
        return {"status": "recorded", "id": str(row.id)}


@router.post("/positions/{ledger_id}/reconcile", response_model=PositionRead)
async def reconcile_position(
    ledger_id: uuid.UUID, payload: ReconcileRequest, admin: CurrentAdmin, sessions: SessionFactory, settings: AppSettings
) -> PositionRead:
    async with sessions() as session:
        try:
            entry = await reconcile(session, settings, ledger_id, placed=payload.placed, remote_bet_id=payload.remote_bet_id, actor=admin.id)
            await session.commit()
        except CfoError as exc:
            await session.rollback()
            raise _http(exc) from exc
        except Exception:
            await session.rollback()
            raise
        return PositionRead.model_validate(entry)
