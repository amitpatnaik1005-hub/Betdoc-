"""THE VAULT - CFO Advisory (TODAR MAL): alerts, tax records, stress tests, advisories."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import false

from app.models import Base


class CfoAlertModel(Base):
    __tablename__ = "cfo_alerts"
    __table_args__ = (
        CheckConstraint("level IN ('INFO', 'WARNING', 'CRITICAL')", name="ck_cfo_alert_level"),
        CheckConstraint("length(message) > 0", name="ck_cfo_alert_message_not_empty"),
        Index("ix_cfo_alerts_user_unread_created", "user_id", "is_read", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(String(255), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaxRecordModel(Base):
    __tablename__ = "tax_records"
    __table_args__ = (
        UniqueConstraint("user_id", "year", name="uq_tax_records_user_year"),
        # NULLs are distinct in unique constraints, so anonymous rows need their own partial unique index.
        Index(
            "uq_tax_records_anonymous_year",
            "year",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
        CheckConstraint("year >= 1900 AND year <= 9999", name="ck_tax_records_year_range"),
        CheckConstraint("taxable_amount >= 0", name="ck_tax_records_taxable_non_negative"),
        CheckConstraint("estimated_tax >= 0", name="ck_tax_records_tax_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    total_profit: Mapped[float] = mapped_column(Float, nullable=False)
    taxable_amount: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_tax: Mapped[float] = mapped_column(Float, nullable=False)
    last_calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StressTestResultModel(Base):
    __tablename__ = "cfo_stress_tests"
    __table_args__ = (
        CheckConstraint("portfolio_value_before > 0", name="ck_cfo_stress_portfolio_positive"),
        CheckConstraint("simulated_pnl <= 0", name="ck_cfo_stress_pnl_non_positive"),
        CheckConstraint(
            "simulated_drawdown_pct >= 0 AND simulated_drawdown_pct <= 100", name="ck_cfo_stress_drawdown_range"
        ),
        CheckConstraint("length(scenario_name) > 0", name="ck_cfo_stress_scenario_not_empty"),
        Index("ix_cfo_stress_tests_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scenario_name: Mapped[str] = mapped_column(String(64), nullable=False)
    portfolio_value_before: Mapped[float] = mapped_column(Float, nullable=False)
    simulated_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    simulated_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    survived: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CfoAdvisoryModel(Base):
    __tablename__ = "cfo_advisories"
    __table_args__ = (
        CheckConstraint(
            "capital_health_score >= 0 AND capital_health_score <= 100", name="ck_cfo_advisory_health_range"
        ),
        CheckConstraint("variance_status IN ('HIGH', 'STABLE')", name="ck_cfo_advisory_variance_status"),
        Index("ix_cfo_advisories_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    capital_health_score: Mapped[float] = mapped_column(Float, nullable=False)
    variance_status: Mapped[str] = mapped_column(String(32), nullable=False)
    suggestions_json: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
