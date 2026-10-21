"""The Archive router and the Group 87 encrypted backup vault (SHIVAJI & TODAR MAL). Mount with prefix="/archive".
Developed for Amit Ashok Kumar Patnaik.

    GET  /overview, /tables, /tables/{table}/data      browse (sensitive tables never browsable)
    GET  /backups                                       the vault: snapshots, chain check, audits, policy
    POST /backups/create                                seal a snapshot now (admin)
    POST /backups/{id}/verify                           integrity check or dry-run restore (admin)
    POST /backups/{id}/pin                              pin or unpin: a pinned snapshot is never pruned (admin)
    POST /backups/prune                                 apply retention: delete old unpinned files (admin)

A snapshot holds every table, sensitive ones included (a restore needs them): it is only ever written sealed, and no
endpoint returns its contents.
"""

import logging
from typing import Annotated

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_db
from app.core.config import Settings, get_settings, settings
from app.domain.archive.errors import ArchiveDomainError, TableNotFoundError
from app.domain.archive.manager import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, ArchiveManager
from app.schemas.archive import (
    ArchiveOverviewResponse,
    BackupCreateRequest,
    BackupPinRequest,
    BackupPruneRequest,
    BackupVaultDashboardResponse,
    BackupVerifyRequest,
    BackupVerifyResponse,
    TableDataResponse,
    TableSummaryRead,
)
from app.services.backup_vault_service import BackupVaultService

logger = logging.getLogger("betdoc.archive")

router = APIRouter(tags=["The Archive"])

# Tables holding credentials or password hashes are never browsable, encrypted or not.
SENSITIVE_TABLES: frozenset[str] = frozenset({
    "users",
    "exchange_accounts",
    "api_credentials",
    "bookmaker_configs",
    "omni_provider_configs",
    "system_settings",
})

_manager = ArchiveManager(blocked_tables=SENSITIVE_TABLES, backup_dir=settings.ARCHIVE_BACKUP_DIR)


def get_archive_manager() -> ArchiveManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[ArchiveManager, Depends(get_archive_manager)]


@router.get("/overview", response_model=ArchiveOverviewResponse)
async def archive_overview(db: DbSession, manager: Manager) -> ArchiveOverviewResponse:
    overview = await manager.get_overview(db)
    return ArchiveOverviewResponse.model_validate(overview)


@router.get("/tables", response_model=list[TableSummaryRead])
async def archive_tables(db: DbSession, manager: Manager) -> list[TableSummaryRead]:
    tables = await manager.get_tables(db)
    return [TableSummaryRead.model_validate(entry) for entry in tables]


@router.get("/tables/{table_name}/data", response_model=TableDataResponse)
async def archive_table_data(
    table_name: str,
    db: DbSession,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TableDataResponse:
    try:
        payload = await manager.query_table(db, table_name, limit=limit, offset=offset, user_id=None)
    except TableNotFoundError as exc:
        logger.warning("ARCHIVE: browse denied for unknown table %r.", table_name)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ArchiveDomainError as exc:
        logger.warning("ARCHIVE: browse rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return TableDataResponse.model_validate(payload)


# ---------------------------------------------------------------- Group 87: the encrypted backup vault
_vault = BackupVaultService()


def get_backup_vault_service() -> BackupVaultService:
    return _vault


Vault = Annotated[BackupVaultService, Depends(get_backup_vault_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _missing(exc: ArchiveDomainError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)


@router.get("/backups", response_model=BackupVaultDashboardResponse)
async def backup_vault(request: Request, db: DbSession, vault: Vault, cfg: AppSettings) -> BackupVaultDashboardResponse:
    return await vault.dashboard(db, getattr(request.app.state, "redis", None), cfg)


@router.post("/backups/create", response_model=BackupVaultDashboardResponse)
async def create_backup(payload: BackupCreateRequest, request: Request, db: DbSession, vault: Vault, cfg: AppSettings, admin: CurrentAdmin) -> BackupVaultDashboardResponse:
    redis = getattr(request.app.state, "redis", None)
    await vault.create_snapshot(db, redis, cfg, payload, by=admin.username)
    return await vault.dashboard(db, redis, cfg)


@router.post("/backups/{snapshot_id}/verify", response_model=BackupVerifyResponse)
async def verify_backup(snapshot_id: UUID, payload: BackupVerifyRequest, db: DbSession, vault: Vault, cfg: AppSettings, admin: CurrentAdmin) -> BackupVerifyResponse:
    try:
        return await vault.verify(db, cfg, snapshot_id, payload.operation, by=admin.username)
    except ArchiveDomainError as exc:
        raise _missing(exc) from exc


@router.post("/backups/{snapshot_id}/pin", response_model=BackupVaultDashboardResponse)
async def pin_backup(snapshot_id: UUID, payload: BackupPinRequest, request: Request, db: DbSession, vault: Vault, cfg: AppSettings, admin: CurrentAdmin) -> BackupVaultDashboardResponse:  # noqa: ARG001
    try:
        await vault.set_pin(db, snapshot_id, payload.is_pinned)
    except ArchiveDomainError as exc:
        raise _missing(exc) from exc
    return await vault.dashboard(db, getattr(request.app.state, "redis", None), cfg)


@router.post("/backups/prune", response_model=BackupVaultDashboardResponse)
async def prune_backups(payload: BackupPruneRequest, request: Request, db: DbSession, vault: Vault, cfg: AppSettings, admin: CurrentAdmin) -> BackupVaultDashboardResponse:
    await vault.prune(db, payload.retention_count, by=admin.username)
    return await vault.dashboard(db, getattr(request.app.state, "redis", None), cfg)
