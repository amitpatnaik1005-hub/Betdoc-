import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class ResearchStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExperimentStatus(StrEnum):
    RUNNING = "RUNNING"
    CONCLUDED = "CONCLUDED"


def _status_check(enum: type[StrEnum]) -> str:
    return "status IN ({})".format(", ".join(f"'{member.value}'" for member in enum))


class ResearchReportModel(Base):
    __tablename__ = "lab_research_reports"
    __table_args__ = (
        CheckConstraint(_status_check(ResearchStatus), name="ck_lab_research_reports_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(String(50), index=True)
    topic: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(16), index=True, default=ResearchStatus.PENDING.value
    )
    markdown_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ResearchReport id={self.id} category={self.category} status={self.status}>"


class ExperimentModel(Base):
    __tablename__ = "lab_experiments"
    __table_args__ = (
        CheckConstraint(_status_check(ExperimentStatus), name="ck_lab_experiments_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    model_a_name: Mapped[str] = mapped_column(String(100))
    model_b_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(16), index=True, default=ExperimentStatus.RUNNING.value
    )
    winner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Experiment id={self.id} name={self.name!r} status={self.status}>"
