"""The Lab: Competitive Intelligence (FA-7) persistence models, run by SPY-BOT."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import false

from app.models import Base


class BotStatus(StrEnum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    ERROR = "ERROR"


class GapSeverity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


def _in_clause(column: str, enum_cls: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class CompetitorBotModel(Base):
    __tablename__ = "competitor_bots"
    __table_args__ = (
        CheckConstraint(_in_clause("status", BotStatus), name="ck_competitor_bots_status"),
        CheckConstraint("length(name) > 0", name="ck_competitor_bots_name_not_empty"),
        CheckConstraint("length(target_site) > 0", name="ck_competitor_bots_target_not_empty"),
        Index("ix_competitor_bots_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    target_site: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=BotStatus.IDLE.value)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeatureGapAlertModel(Base):
    __tablename__ = "feature_gap_alerts"
    __table_args__ = (
        CheckConstraint(_in_clause("severity", GapSeverity), name="ck_feature_gap_alerts_severity"),
        CheckConstraint("length(missing_feature) > 0", name="ck_feature_gap_alerts_feature_not_empty"),
        Index("ix_feature_gap_alerts_resolved_created", "is_resolved", "created_at"),
        Index("ix_feature_gap_alerts_site_feature", "site_name", "missing_feature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_name: Mapped[str] = mapped_column(String(120), nullable=False)
    missing_feature: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    suggestions: Mapped[list["DevSuggestionModel"]] = relationship(
        "DevSuggestionModel",
        back_populates="gap",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        order_by="DevSuggestionModel.priority",
    )


class DevSuggestionModel(Base):
    __tablename__ = "dev_suggestions"
    __table_args__ = (
        CheckConstraint("priority >= 1 AND priority <= 5", name="ck_dev_suggestions_priority_range"),
        CheckConstraint("length(suggestion_text) > 0", name="ck_dev_suggestions_text_not_empty"),
        Index("ix_dev_suggestions_gap_priority", "gap_id", "priority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gap_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("feature_gap_alerts.id", ondelete="CASCADE"), nullable=False
    )
    suggestion_text: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    gap: Mapped["FeatureGapAlertModel"] = relationship(
        "FeatureGapAlertModel", back_populates="suggestions", lazy="raise"
    )
