"""The Archive: audit trail for database browsing and security checks."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base


class ArchiveAction(StrEnum):
    VIEW_TABLE = "VIEW_TABLE"
    INTEGRITY_CHECK = "INTEGRITY_CHECK"


def _in_clause(column: str, enum_cls: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class ArchiveAccessLogModel(Base):
    __tablename__ = "archive_access_logs"
    __table_args__ = (
        CheckConstraint(_in_clause("action", ArchiveAction), name="ck_archive_access_logs_action"),
        CheckConstraint("length(target_resource) > 0", name="ck_archive_access_logs_target_not_empty"),
        Index("ix_archive_access_logs_target_created", "target_resource", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_resource: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
