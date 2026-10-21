"""SHIVAJI's backup schedule (Group 87). Developer Credit: Amit Ashok Kumar Patnaik.

    backup.vault_snapshot   every BACKUP_SCHEDULE_HOURS: seal a SCHEDULED snapshot, then apply BACKUP_RETENTION_COUNT
                            (pinned snapshots are never pruned). Returns at once while BACKUP_SCHEDULE_ENABLED is off.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _snapshot() -> dict[str, Any]:
    from app.models.backup_vault import BackupTriggerMode  # noqa: PLC0415
    from app.schemas.archive import BackupCreateRequest  # noqa: PLC0415
    from app.services.backup_vault_service import SCHEDULER, BackupVaultService  # noqa: PLC0415

    async with _resources() as (redis, sessions, settings):
        if not settings.BACKUP_SCHEDULE_ENABLED:
            return {"enabled": False}
        vault = BackupVaultService()
        async with sessions() as db:
            manifest = await vault.create_snapshot(db, redis, settings, BackupCreateRequest(snapshot_label="Scheduled encrypted snapshot"), by=SCHEDULER,
                                                   trigger=BackupTriggerMode.SCHEDULED)
            pruned = await vault.prune(db, settings.BACKUP_RETENTION_COUNT, by=SCHEDULER)
        return {"filename": manifest.filename, "tables": manifest.total_tables, "rows": manifest.total_rows, "complete": manifest.is_complete, "pruned": pruned}


@celery_app.task(name="backup.vault_snapshot", ignore_result=True)
def vault_snapshot() -> dict[str, Any]:
    return asyncio.run(_snapshot())
