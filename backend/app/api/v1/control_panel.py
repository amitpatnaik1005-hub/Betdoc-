"""Control Panel router. Mount with prefix="/control-panel"."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_db
from app.core.config import Settings, get_settings
from app.core.config import settings as app_settings
from app.domain.control_panel.errors import ControlPanelDomainError
from app.domain.control_panel.manager import ControlPanelManager
from app.schemas.control_panel import (
    REDACTED,
    SECRET_FIELDS,
    AutoDrawdownCheckRequest,
    AutoDrawdownCheckResponse,
    BookmakerLockRequest,
    EmergencyDashboardResponse,
    EmergencyResumeRequest,
    EmergencyTriggerRequest,
    ProviderBucketResetRequest,
    QuotaGuardDashboardResponse,
    QuotaShieldModeUpdateRequest,
    SettingsRead,
    SettingsUpdate,
)
from app.services.aryabhata_pipeline import publish_risk_limits
from app.services.emergency_kill_switch_service import EmergencyKillSwitchService
from app.services.quota_guard_service import QuotaGuardError, QuotaGuardService
from app.services.risk_guard import kill_switch_engaged, set_kill_switch
from app.services.sentinel_bus import emit_alert
from app.services.sentinel_watch import kill_switch_alert

logger = logging.getLogger("betdoc.control_panel")

router = APIRouter(tags=["Control Panel"])

_manager = ControlPanelManager()


def get_control_panel_manager() -> ControlPanelManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[ControlPanelManager, Depends(get_control_panel_manager)]


def _redact(schema: SettingsRead) -> SettingsRead:
    """Redact secrets on a Pydantic copy. The SQLAlchemy model is never touched."""
    masked = {field: REDACTED for field in SECRET_FIELDS if getattr(schema, field)}
    return schema.model_copy(update=masked) if masked else schema


@router.get("", response_model=SettingsRead)
async def read_settings(db: DbSession, manager: Manager) -> SettingsRead:
    try:
        settings = await manager.get_settings(db)
    except ControlPanelDomainError as exc:
        logger.error("CONTROL: settings unavailable: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message) from exc
    return _redact(SettingsRead.model_validate(settings))


@router.patch("", response_model=SettingsRead)
async def update_settings(payload: SettingsUpdate, request: Request, db: DbSession, manager: Manager) -> SettingsRead:
    try:
        settings = await manager.update_settings(db, updates=payload.model_dump(exclude_unset=True))
    except ControlPanelDomainError as exc:
        logger.warning("CONTROL: settings update rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    # Live stake sizing reads the Redis mirror: refresh it now, not when its cache expires
    redis = getattr(request.app.state, "redis", None)
    await publish_risk_limits(redis, settings, app_settings)
    if "max_daily_exposure" in payload.model_fields_set and settings.max_daily_exposure > 0:
        engaged = await kill_switch_engaged(redis, app_settings)
        await set_kill_switch(redis, app_settings, engaged=False)  # trading resumed
        if engaged:
            await emit_alert(redis, app_settings, kill_switch_alert(engaged=False, by="control-panel"))
    return _redact(SettingsRead.model_validate(settings))


@router.post("/emergency-stop", response_model=SettingsRead)
async def emergency_stop(request: Request, db: DbSession, manager: Manager) -> SettingsRead:
    try:
        settings = await manager.emergency_stop(db)
    except ControlPanelDomainError as exc:
        logger.error("CONTROL: emergency stop failed: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message) from exc
    redis = getattr(request.app.state, "redis", None)
    await set_kill_switch(redis, app_settings, engaged=True)  # every Omni execution checks this first
    await publish_risk_limits(redis, settings, app_settings)  # stakes drop to 0
    await emit_alert(redis, app_settings, kill_switch_alert(engaged=True, by="control-panel"))  # the Sentinel
    return _redact(SettingsRead.model_validate(settings))


# ---------------------------------------------------------------- Group 86: the emergency fortress (KAUTILYA, CHANAKYA & SHIVAJI)
_emergency = EmergencyKillSwitchService()


def get_emergency_kill_switch_service() -> EmergencyKillSwitchService:
    return _emergency


Emergency = Annotated[EmergencyKillSwitchService, Depends(get_emergency_kill_switch_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.get("/emergency-dashboard", response_model=EmergencyDashboardResponse)
async def get_emergency_dashboard(request: Request, db: DbSession, service: Emergency, settings: AppSettings) -> EmergencyDashboardResponse:
    """The fortress: lockout state, book locks, measured drawdown and streak, and the audit chain (re-verified)."""
    return await service.dashboard(db, getattr(request.app.state, "redis", None), settings)


@router.post("/emergency-kill", response_model=EmergencyDashboardResponse)
async def trigger_emergency_kill(payload: EmergencyTriggerRequest, request: Request, db: DbSession, service: Emergency, settings: AppSettings) -> EmergencyDashboardResponse:
    return await service.engage_global_kill(db, getattr(request.app.state, "redis", None), settings, payload)


@router.post("/emergency-resume", response_model=EmergencyDashboardResponse)
async def resume_emergency_kill(payload: EmergencyResumeRequest, request: Request, db: DbSession, service: Emergency, settings: AppSettings) -> EmergencyDashboardResponse:
    return await service.resume_global_operations(db, getattr(request.app.state, "redis", None), settings, payload)


@router.post("/bookmaker-lock", response_model=EmergencyDashboardResponse)
async def toggle_bookmaker_lock(payload: BookmakerLockRequest, request: Request, db: DbSession, service: Emergency, settings: AppSettings) -> EmergencyDashboardResponse:
    return await service.set_bookmaker_lock(db, getattr(request.app.state, "redis", None), settings, payload)


@router.post("/auto-drawdown-check", response_model=AutoDrawdownCheckResponse)
async def evaluate_auto_drawdown(payload: AutoDrawdownCheckRequest, request: Request, db: DbSession, service: Emergency, settings: AppSettings) -> AutoDrawdownCheckResponse:
    return await service.evaluate_and_enforce_auto_drawdown(db, getattr(request.app.state, "redis", None), settings, payload)


# ---------------------------------------------------------------- Group 92: API rate-limit intelligence & quota guard (BHEESHMA)
_quota_guard = QuotaGuardService()


def get_quota_guard_service() -> QuotaGuardService:
    return _quota_guard


QuotaGuard = Annotated[QuotaGuardService, Depends(get_quota_guard_service)]


@router.get("/quota-guard", response_model=QuotaGuardDashboardResponse)
async def get_quota_guard_dashboard(request: Request, db: DbSession, service: QuotaGuard, settings: AppSettings) -> QuotaGuardDashboardResponse:
    """Token buckets, measured provider quota, breakers and the cache shield's effect on every source's cadence."""
    return await service.dashboard(db, getattr(request.app.state, "redis", None), settings)


@router.post("/quota-guard/mode", response_model=QuotaGuardDashboardResponse)
async def update_quota_guard_mode(
    payload: QuotaShieldModeUpdateRequest, request: Request, db: DbSession, service: QuotaGuard, settings: AppSettings, admin: CurrentAdmin
) -> QuotaGuardDashboardResponse:
    """Set the cache shield mode the fleet beat applies (admin: it changes how often metered providers are polled)."""
    try:
        return await service.set_shield_mode(db, getattr(request.app.state, "redis", None), settings, payload, actor=admin.username)
    except QuotaGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/quota-guard/reset-bucket", response_model=QuotaGuardDashboardResponse)
async def reset_provider_quota_bucket(
    payload: ProviderBucketResetRequest, request: Request, db: DbSession, service: QuotaGuard, settings: AppSettings, admin: CurrentAdmin
) -> QuotaGuardDashboardResponse:
    """Close a fleet source's breaker, zero its failure streak and refill its token bucket (admin)."""
    try:
        return await service.reset_provider_bucket(db, getattr(request.app.state, "redis", None), settings, payload, actor=admin.username)
    except QuotaGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
