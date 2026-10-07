"""SQLAlchemy model for dynamically configured bookmaker integrations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Uuid, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BookmakerConfigModel(Base):
    __tablename__ = "bookmaker_configs"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="ck_bookmaker_configs_name_not_empty"),
        CheckConstraint("priority_rank >= 1", name="ck_bookmaker_configs_priority_rank_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    api_key_encrypted: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    priority_rank: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    @property
    def has_api_key(self) -> bool:
        """Expose only whether a credential exists, never the credential itself."""
        return bool(self.api_key_encrypted)

    def __repr__(self) -> str:
        return (
            f"BookmakerConfigModel(id={self.id!r}, name={self.name!r}, "
            f"is_active={self.is_active!r}, priority_rank={self.priority_rank!r})"
        )
