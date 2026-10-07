"""The Archive router. Mount with prefix="/archive"."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.domain.archive.errors import ArchiveDomainError, TableNotFoundError
from app.domain.archive.manager import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, ArchiveManager
from app.schemas.archive import ArchiveOverviewResponse, TableDataResponse, TableSummaryRead

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
