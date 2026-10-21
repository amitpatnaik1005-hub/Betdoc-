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


# ---------------------------------------------------------------- Group 87: the encrypted backup vault (SHIVAJI & TODAR MAL)
BackupTriggerLiteral = Literal["MANUAL", "SCHEDULED", "PRE_MIGRATION"]
BackupStatusLiteral = Literal["COMPLETED", "VERIFIED", "CORRUPTED", "PRUNED"]
BackupCheckLiteral = Literal["INTEGRITY_VERIFY", "DRY_RUN_RESTORE"]


class BackupSnapshotRead(ArchiveSchema):
    id: UUID
    snapshot_label: str
    trigger_mode: BackupTriggerLiteral
    status: BackupStatusLiteral
    filename: str
    encryption_algorithm: str
    key_fingerprint: str
    file_bytes: int
    uncompressed_bytes: int
    total_tables: int
    total_rows: int
    is_complete: bool
    truncated_tables: list[str]
    protected_bankroll_inr: float | None
    protected_bankroll_usd: float | None
    plaintext_sha256: str
    ciphertext_sha256: str
    prev_hash: str
    entry_hash: str
    is_pinned: bool
    triggered_by: str
    last_verified_at: datetime | None
    pruned_at: datetime | None
    created_at: datetime


class BackupRestoreAuditRead(ArchiveSchema):
    id: UUID
    snapshot_id: UUID | None
    operation: Literal["INTEGRITY_VERIFY", "DRY_RUN_RESTORE", "PRUNE_RETENTION"]
    verdict: Literal["PASS", "FAIL"]
    tables_checked: int
    rows_verified: int
    duration_ms: float
    detail: str
    triggered_by: str
    created_at: datetime


class BackupVaultDashboardResponse(ArchiveSchema):
    encryption_algorithm: str
    backup_dir: str
    schedule_enabled: bool
    schedule_hours: int
    retention_count: int
    max_rows_per_table: int
    current_key_fingerprint: str
    total_snapshots: int  # live (not pruned)
    total_storage_bytes: int
    latest_backup_at: datetime | None
    chain_intact: bool
    snapshots: list[BackupSnapshotRead]
    audits: list[BackupRestoreAuditRead]
    architect: str


class BackupCreateRequest(ArchiveSchema):
    """The signed-in administrator is recorded as the trigger; the scheduler records itself."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    snapshot_label: str = Field(default="Manual encrypted snapshot", min_length=3, max_length=128)
    trigger_mode: Literal["MANUAL", "PRE_MIGRATION"] = "MANUAL"
    is_pinned: bool = False


class BackupVerifyRequest(ArchiveSchema):
    model_config = ConfigDict(extra="forbid")

    operation: BackupCheckLiteral = "DRY_RUN_RESTORE"


class BackupVerifyResponse(ArchiveSchema):
    valid: bool
    verdict: Literal["PASS", "FAIL"]
    operation: BackupCheckLiteral
    tables_checked: int
    rows_verified: int
    duration_ms: float
    plaintext_sha256: str | None
    ciphertext_sha256: str
    detail: str
    missing_tables: list[str]  # dry run: in the snapshot, not in the live database
    missing_columns: dict[str, list[str]]  # dry run: table -> snapshot columns the live table lacks
    snapshot: BackupSnapshotRead
    audit: BackupRestoreAuditRead


class BackupPinRequest(ArchiveSchema):
    model_config = ConfigDict(extra="forbid")

    is_pinned: bool


def _retention_default() -> int:
    from app.core.config import get_settings  # noqa: PLC0415

    return get_settings().BACKUP_RETENTION_COUNT


class BackupPruneRequest(ArchiveSchema):
    model_config = ConfigDict(extra="forbid")

    retention_count: int = Field(default_factory=_retention_default, ge=1, le=365)
