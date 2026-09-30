"""Control Panel router. Mount with prefix="/control-panel"."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.control_panel.errors import ControlPanelDomainError
from app.domain.control_panel.manager import ControlPanelManager
from app.schemas.control_panel import REDACTED, SECRET_FIELDS, SettingsRead, SettingsUpdate

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
async def update_settings(payload: SettingsUpdate, db: DbSession, manager: Manager) -> SettingsRead:
    try:
        settings = await manager.update_settings(db, updates=payload.model_dump(exclude_unset=True))
    except ControlPanelDomainError as exc:
        logger.warning("CONTROL: settings update rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return _redact(SettingsRead.model_validate(settings))


@router.post("/emergency-stop", response_model=SettingsRead)
async def emergency_stop(db: DbSession, manager: Manager) -> SettingsRead:
    try:
        settings = await manager.emergency_stop(db)
    except ControlPanelDomainError as exc:
        logger.error("CONTROL: emergency stop failed: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message) from exc
    return _redact(SettingsRead.model_validate(settings))
