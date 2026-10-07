"""Pydantic V2 contracts for The Archive."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArchiveSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ArchiveOverviewResponse(ArchiveSchema):
    encryption_status: str
    algorithm: str
    last_backup_at: datetime | None  # None: no backup archive visible to the API
    total_tables: int = Field(ge=0)
    database_status: Literal["ONLINE", "DEGRADED"]
    probe_latency_ms: float | None
    total_records: int = Field(ge=0)


class TableSummaryRead(ArchiveSchema):
    table_name: str
    row_count: int = Field(ge=0)


class TableDataResponse(ArchiveSchema):
    table_name: str
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    data: list[dict[str, Any]]


class ArchiveLogRead(ArchiveSchema):
    id: UUID
    user_id: UUID | None
    action: Literal["VIEW_TABLE", "INTEGRITY_CHECK"]
    target_resource: str
    created_at: datetime
