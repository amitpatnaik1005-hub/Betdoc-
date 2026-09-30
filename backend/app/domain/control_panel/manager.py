"""ControlPanelManager: settings singleton, dynamic partial updates, emergency stop."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.control_panel.errors import ControlPanelDomainError
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.schemas.control_panel import REDACTED, SECRET_FIELDS, SettingsUpdate

logger = logging.getLogger("betdoc.control_panel")

# Derived from the schema, so adding a field to SettingsUpdate makes it editable with no other change.
EDITABLE_FIELDS: frozenset[str] = frozenset(SettingsUpdate.model_fields)


class ControlPanelManager:
    async def _find_settings(self, db: AsyncSession) -> SystemSettingsModel | None:
        result = await db.execute(
            select(SystemSettingsModel)
            .where(SystemSettingsModel.id == SETTINGS_SINGLETON_ID)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_settings(self, db: AsyncSession) -> SystemSettingsModel:
        settings = await self._find_settings(db)
        if settings is None:
            candidate = SystemSettingsModel(id=SETTINGS_SINGLETON_ID)
            db.add(candidate)
            try:
                await db.commit()
                settings = candidate
                logger.info("CONTROL: settings singleton created with defaults.")
            except IntegrityError:
                await db.rollback()
                logger.warning("CONTROL: concurrent singleton creation detected; loading the existing row.")
                settings = await self._find_settings(db)
                if settings is None:
                    raise ControlPanelDomainError("Settings singleton could not be created or loaded.") from None
        await db.refresh(settings)  # loads server-side created_at / updated_at
        return settings

    async def get_settings(self, db: AsyncSession) -> SystemSettingsModel:
        return await self._get_or_create_settings(db)

    async def update_settings(self, db: AsyncSession, updates: Mapping[str, Any]) -> SystemSettingsModel:
        if not isinstance(updates, Mapping):
            raise ControlPanelDomainError("updates must be a mapping of setting names to values.")

        unknown = sorted(set(updates) - EDITABLE_FIELDS)
        if unknown:
            raise ControlPanelDomainError(f"Unknown or read-only settings: {', '.join(unknown)}.")

        effective = {
            key: value
            for key, value in updates.items()
            if not (key in SECRET_FIELDS and value == REDACTED)
        }
        ignored = sorted(set(updates) - set(effective))

        try:
            changes = SettingsUpdate.model_validate(effective).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise ControlPanelDomainError(f"Invalid settings ({exc.error_count()} error(s)).") from exc

        settings = await self._get_or_create_settings(db)
        if not changes:
            logger.info("CONTROL: update contained no effective changes (ignored redacted: %s).", ignored or "none")
            return settings

        for key, value in changes.items():
            setattr(settings, key, value)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            logger.warning("CONTROL: settings update rejected by storage constraints.")
            raise ControlPanelDomainError("Settings update rejected by storage constraints.") from exc
        await db.refresh(settings)

        logger.info(
            "CONTROL: settings updated (%s); ignored redacted placeholders: %s.",
            ", ".join(sorted(changes)),
            ", ".join(ignored) or "none",
        )
        return settings

    async def emergency_stop(self, db: AsyncSession) -> SystemSettingsModel:
        settings = await self._get_or_create_settings(db)
        settings.bots_enabled = False
        settings.max_daily_exposure = 0.0
        settings.last_emergency_stop_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(settings)
        logger.critical("CONTROL: EMERGENCY STOP INITIATED. Bots disabled, exposure zeroed.")
        return settings
