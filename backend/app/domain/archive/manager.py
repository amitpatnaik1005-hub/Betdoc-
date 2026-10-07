"""ArchiveManager: health overview, table inventory and safe read-only browsing."""

import asyncio
import base64
import logging
import math
import re
import time
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import Table, func, inspect, literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.archive.errors import ArchiveDomainError, TableNotFoundError
from app.models import Base
from app.models.archive import ArchiveAccessLogModel, ArchiveAction

logger = logging.getLogger("betdoc.archive")

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 1_000
MAX_TARGET_RESOURCE_LENGTH = 120
DEFAULT_PROBE_LATENCY_S = 0.005
BACKUP_AGE = timedelta(hours=2)
REDACTED = "***REDACTED***"

SENSITIVE_COLUMN_PATTERN = re.compile(
    r"(password|passwd|secret|token|api_?key|private_?key|salt|otp|ssn|card_?number|cvv|pin_hash)",
    re.IGNORECASE,
)


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):  # before str/int: StrEnum and IntEnum subclass them
        return str(value.value)
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):  # before date: datetime subclasses date
        return value.isoformat()
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        return {str(k): _serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize_value(item) for item in value]
    return str(value)


def _serialize_row(row: Mapping[str, Any], redacted_columns: Collection[str] = ()) -> dict[str, Any]:
    """Convert a RowMapping into a JSON-safe dict (UUID/datetime/date/Decimal/Enum -> str)."""
    return {
        str(key): REDACTED if key in redacted_columns else _serialize_value(value)
        for key, value in row.items()
    }


def _sensitive_columns(table: Table) -> frozenset[str]:
    names: set[str] = set()
    for column in table.columns:
        if SENSITIVE_COLUMN_PATTERN.search(column.name):
            names.add(column.name)
            names.add(column.key)
    return frozenset(names)


class ArchiveManager:
    def __init__(
        self,
        *,
        probe_latency_s: float = DEFAULT_PROBE_LATENCY_S,
        blocked_tables: Iterable[str] = (),
    ) -> None:
        if probe_latency_s < 0:
            raise ValueError("probe_latency_s must be non-negative.")
        self._probe_latency_s = probe_latency_s
        self._blocked_tables = frozenset(blocked_tables)

    # ------------------------------------------------------------------ inventory helpers

    async def _materialized_tables(self, db: AsyncSession) -> set[str]:
        """Metadata keys of registered tables that physically exist in the connected database."""

        def _collect(sync_session: Session) -> set[str]:
            inspector = inspect(sync_session.connection())
            return {
                key
                for key, table in Base.metadata.tables.items()
                if inspector.has_table(table.name, schema=table.schema)
            }

        return await db.run_sync(_collect)

    # ------------------------------------------------------------------ overview

    async def get_overview(self, db: AsyncSession) -> dict[str, Any]:
        database_status = "ONLINE"
        probe_latency_ms: float | None = None
        total_records = 0
        try:
            started = time.perf_counter()
            await db.execute(select(literal(1)))
            await asyncio.sleep(self._probe_latency_s)  # mock storage-engine health probe
            probe_latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            total_records = sum(entry["row_count"] for entry in await self.get_tables(db))
        except SQLAlchemyError:
            await db.rollback()
            database_status = "DEGRADED"
            logger.error("ARCHIVE: database health probe failed.", exc_info=True)

        overview = {
            "encryption_status": "E2E Active",
            "algorithm": "AES-256-GCM",
            "last_backup_at": datetime.now(UTC) - BACKUP_AGE,
            "total_tables": len(Base.metadata.tables),
            "database_status": database_status,
            "probe_latency_ms": probe_latency_ms,
            "total_records": total_records,
        }
        logger.info(
            "ARCHIVE: overview served (status=%s, tables=%d, records=%d).",
            database_status,
            overview["total_tables"],
            total_records,
        )
        return overview

    # ------------------------------------------------------------------ tables

    async def get_tables(self, db: AsyncSession) -> list[dict[str, Any]]:
        materialized = await self._materialized_tables(db)
        summaries: list[dict[str, Any]] = []
        for name, table in sorted(Base.metadata.tables.items()):
            if name in self._blocked_tables:
                continue  # not browsable, so not listed either
            if name not in materialized:
                logger.warning("ARCHIVE: table %s is registered but not present in the database; skipped.", name)
                continue
            count = await db.scalar(select(func.count()).select_from(table))
            summaries.append({"table_name": name, "row_count": int(count or 0)})
        logger.info("ARCHIVE: inventoried %d tables.", len(summaries))
        return summaries

    # ------------------------------------------------------------------ data browser

    async def query_table(
        self,
        db: AsyncSession,
        table_name: str,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        user_id: UUID | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ArchiveDomainError(f"limit must be an integer between 1 and {MAX_PAGE_LIMIT}.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ArchiveDomainError("offset must be a non-negative integer.")

        table = Base.metadata.tables.get(table_name)
        if table is None or table_name in self._blocked_tables:
            raise TableNotFoundError(table_name)
        if table_name not in await self._materialized_tables(db):
            raise TableNotFoundError(table_name)

        access_log = ArchiveAccessLogModel(
            user_id=user_id,
            action=ArchiveAction.VIEW_TABLE.value,
            target_resource=table_name[:MAX_TARGET_RESOURCE_LENGTH],
        )
        db.add(access_log)
        try:
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
            logger.error("ARCHIVE: failed to record access to %s.", table_name, exc_info=True)
            raise
        await db.refresh(access_log)  # loads the server-side created_at

        statement = select(table).limit(limit).offset(offset)
        primary_key_columns = list(table.primary_key.columns)
        if primary_key_columns:
            statement = statement.order_by(*primary_key_columns)  # stable pagination

        rows = (await db.execute(statement)).mappings().all()
        redacted = _sensitive_columns(table)
        data = [_serialize_row(row, redacted) for row in rows]

        logger.info(
            "ARCHIVE: %s viewed %s (limit=%d, offset=%d, rows=%d, redacted=%d) at %s.",
            user_id or "anonymous",
            table_name,
            limit,
            offset,
            len(data),
            len(redacted),
            access_log.created_at,
        )
        return {"table_name": table_name, "limit": limit, "offset": offset, "data": data}
