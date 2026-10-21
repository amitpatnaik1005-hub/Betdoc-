"""Group 87: the encrypted backup vault's records (SHIVAJI & TODAR MAL). Developer Credit: Amit Ashok Kumar Patnaik.

* ``backup_snapshot_manifests``: one row per sealed snapshot file, chained by SHA-256. ``prev_hash`` is unique, so two
  snapshots taken at once cannot both claim the same predecessor: the chain stays a line. ``is_complete`` is false when
  a table hit ``BACKUP_MAX_ROWS_PER_TABLE`` (``truncated_tables`` names them). ``key_fingerprint`` names the key that
  sealed it (never the key). A pruned snapshot keeps its manifest (the chain stays verifiable); its file is gone.
* ``backup_restore_audits``: append-only record of every verification, dry-run restore and retention pass.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.expression import false, true
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class BackupTriggerMode(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    PRE_MIGRATION = "PRE_MIGRATION"


class BackupSnapshotStatus(StrEnum):
    COMPLETED = "COMPLETED"
    VERIFIED = "VERIFIED"
    CORRUPTED = "CORRUPTED"
    PRUNED = "PRUNED"


class BackupAuditOperation(StrEnum):
    INTEGRITY_VERIFY = "INTEGRITY_VERIFY"
    DRY_RUN_RESTORE = "DRY_RUN_RESTORE"
    PRUNE_RETENTION = "PRUNE_RETENTION"


class BackupAuditVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


def _in(column: str, enum_cls: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum_cls)})"


class BackupSnapshotManifestModel(Base):
    """One sealed snapshot. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "backup_snapshot_manifests"
    __table_args__ = (
        CheckConstraint(_in("trigger_mode", BackupTriggerMode), name="trigger_mode_known"),
        CheckConstraint(_in("status", BackupSnapshotStatus), name="status_known"),
        CheckConstraint("file_bytes >= 0 AND uncompressed_bytes >= 0", name="bytes_non_negative"),
        CheckConstraint("total_tables >= 0 AND total_rows >= 0", name="counts_non_negative"),
        Index("ix_backup_manifest_created_at", "created_at"),
        Index("ix_backup_manifest_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_label: Mapped[str] = mapped_column(String(128))
    trigger_mode: Mapped[str] = mapped_column(String(24), default=BackupTriggerMode.MANUAL.value)
    status: Mapped[str] = mapped_column(String(24), default=BackupSnapshotStatus.COMPLETED.value)
    filename: Mapped[str] = mapped_column(String(255), unique=True)
    storage_path: Mapped[str] = mapped_column(String(512))
    encryption_algorithm: Mapped[str] = mapped_column(String(64))
    key_fingerprint: Mapped[str] = mapped_column(String(16))
    file_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uncompressed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tables: Mapped[int] = mapped_column(Integer, default=0)
    total_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    truncated_tables: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)
    protected_bankroll_inr: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    protected_bankroll_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    plaintext_sha256: Mapped[str] = mapped_column(String(64))
    ciphertext_sha256: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64), unique=True)
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    triggered_by: Mapped[str] = mapped_column(String(128))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BackupRestoreAuditModel(Base):
    """One verification, dry-run restore or retention pass. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "backup_restore_audits"
    __table_args__ = (
        CheckConstraint(_in("operation", BackupAuditOperation), name="operation_known"),
        CheckConstraint(_in("verdict", BackupAuditVerdict), name="verdict_known"),
        CheckConstraint("tables_checked >= 0 AND rows_verified >= 0", name="counts_non_negative"),
        Index("ix_backup_audit_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("backup_snapshot_manifests.id", ondelete="SET NULL"), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str] = mapped_column(String(16))
    tables_checked: Mapped[int] = mapped_column(Integer, default=0)
    rows_verified: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    detail: Mapped[str] = mapped_column(String(1024))
    triggered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
