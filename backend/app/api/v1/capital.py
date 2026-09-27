import logging

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.domain.risk.stop_loss import StopLossEngine
from app.models.risk import StopLossEventModel
from app.schemas.risk import (
    RiskMetrics,
    SessionResetOut,
    StopLossConfig,
    StopLossEventOut,
    StopLossStatus,
)
from app.services.risk_service import RiskService

kumbha = logging.getLogger("betdoc.kumbha")

router = APIRouter(tags=["capital"])

_risk_service = RiskService()
_stop_loss = StopLossEngine()


@router.get("/risk", response_model=RiskMetrics)
async def get_risk_metrics(
    db: DbSession,
    current_user: CurrentUser,
    bankroll: float = Query(default=10000.0, gt=0),
) -> RiskMetrics:
    return await _risk_service.compute_metrics(db, current_user.id, bankroll)


@router.get("/stop-loss/status", response_model=StopLossStatus)
async def get_stop_loss_status(
    db: DbSession,
    current_user: CurrentUser,
    bankroll: float | None = Query(default=None, gt=0),
) -> StopLossStatus:
    status = await _stop_loss.check(db, current_user.id, bankroll)
    await db.commit()  # persist any newly fired events
    return status


@router.get("/stop-loss/config", response_model=StopLossConfig)
async def get_stop_loss_config(db: DbSession, current_user: CurrentUser) -> StopLossConfig:
    return await _stop_loss.get_config(db, current_user.id)


@router.post("/stop-loss/configure", response_model=StopLossConfig)
async def configure_stop_loss(
    config: StopLossConfig,
    db: DbSession,
    current_user: CurrentUser,
) -> StopLossConfig:
    model = await _stop_loss.update_config(db, current_user.id, config)
    out = StopLossConfig.model_validate(model)  # serialize BEFORE commit expires attributes
    await db.commit()
    kumbha.info("User %s configured stop-loss: %s", current_user.id, out.model_dump())
    return out


@router.post("/stop-loss/reset-session", response_model=SessionResetOut)
async def reset_stop_loss_session(db: DbSession, current_user: CurrentUser) -> SessionResetOut:
    model = await _stop_loss.reset_session(db, current_user.id)
    out = SessionResetOut(session_started_at=model.session_started_at)
    await db.commit()
    return out


@router.get("/stop-loss/history", response_model=list[StopLossEventOut])
async def get_stop_loss_history(db: DbSession, current_user: CurrentUser) -> list[StopLossEventOut]:
    rows = await db.scalars(
        select(StopLossEventModel)
        .where(StopLossEventModel.user_id == current_user.id)
        .order_by(StopLossEventModel.triggered_at.desc())
        .limit(100)
    )
    return [StopLossEventOut.model_validate(r) for r in rows.all()]
