"""The Core: Engine Room persistence models (PRATAP's factory floor)."""

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum_values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


JSONPayload = JSON().with_variant(JSONB(), "postgresql")


class SmallcaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    STANDBY = "STANDBY"
    DISABLED = "DISABLED"


class EngineTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _status_type(enum_cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=_enum_values,
        validate_strings=True,
    )


class CoreEngineMetricsModel(Base):
    __tablename__ = "core_engine_metrics"
    __table_args__ = (
        CheckConstraint("cpu_usage_pct >= 0 AND cpu_usage_pct <= 100", name="ck_core_metrics_cpu_range"),
        CheckConstraint("memory_usage_mb >= 0", name="ck_core_metrics_memory_non_negative"),
        CheckConstraint("queue_depth >= 0", name="ck_core_metrics_queue_non_negative"),
        CheckConstraint("active_models_count >= 0", name="ck_core_metrics_models_non_negative"),
        Index("ix_core_engine_metrics_recorded_at", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cpu_usage_pct: Mapped[float] = mapped_column(Float, nullable=False)
    memory_usage_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_models_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class SmallcaseRegistryModel(Base):
    __tablename__ = "core_smallcase_registry"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="ck_core_smallcase_name_not_empty"),
        CheckConstraint(
            "current_accuracy >= 0 AND current_accuracy <= 1", name="ck_core_smallcase_accuracy_range"
        ),
        CheckConstraint(
            "cross_val_score >= 0 AND cross_val_score <= 1", name="ck_core_smallcase_cross_val_range"
        ),
        CheckConstraint("total_backtests_run >= 0", name="ck_core_smallcase_backtests_non_negative"),
        CheckConstraint(
            "jsonb_typeof(pipeline_config) = 'array'", name="ck_core_smallcase_pipeline_is_array"
        ).ddl_if(dialect="postgresql"),
        Index("ix_core_smallcase_status", "status"),
        Index(
            "ix_core_smallcase_pipeline_config_gin", "pipeline_config", postgresql_using="gin"
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pipeline_config: Mapped[list[str]] = mapped_column(JSONPayload, nullable=False, default=list)
    status: Mapped[SmallcaseStatus] = mapped_column(
        _status_type(SmallcaseStatus, "core_smallcase_status"),
        nullable=False,
        default=SmallcaseStatus.STANDBY,
    )
    current_accuracy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cross_val_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_backtests_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class TestBenchRunModel(Base):
    __tablename__ = "core_test_bench_runs"
    __test__ = False  # stop pytest from trying to collect this class
    __table_args__ = (
        CheckConstraint(
            "execution_time_ms IS NULL OR execution_time_ms >= 0", name="ck_core_test_bench_exec_time"
        ),
        CheckConstraint(
            "jsonb_typeof(match_context) = 'object'", name="ck_core_test_bench_context_is_object"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(pipeline_execution_steps) = 'array'", name="ck_core_test_bench_steps_is_array"
        ).ddl_if(dialect="postgresql"),
        Index("ix_core_test_bench_runs_smallcase_created", "smallcase_id", "created_at"),
        Index("ix_core_test_bench_runs_status", "status"),
        Index(
            "ix_core_test_bench_runs_match_context_gin", "match_context", postgresql_using="gin"
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_core_test_bench_runs_predicted_outcome_gin", "predicted_outcome", postgresql_using="gin"
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    smallcase_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("core_smallcase_registry.id", ondelete="CASCADE"), nullable=False
    )
    match_context: Mapped[dict[str, Any]] = mapped_column(JSONPayload, nullable=False)
    pipeline_execution_steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONPayload, nullable=False, default=list
    )
    predicted_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONPayload, nullable=True)
    status: Mapped[EngineTaskStatus] = mapped_column(
        _status_type(EngineTaskStatus, "core_test_bench_status"),
        nullable=False,
        default=EngineTaskStatus.QUEUED,
    )
    is_stress_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestJobModel(Base):
    __tablename__ = "core_backtest_jobs"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_core_backtest_date_order"),
        CheckConstraint("total_matches_simulated >= 0", name="ck_core_backtest_matches_non_negative"),
        CheckConstraint(
            "accuracy_pct IS NULL OR (accuracy_pct >= 0 AND accuracy_pct <= 100)",
            name="ck_core_backtest_accuracy_range",
        ),
        CheckConstraint(
            "max_drawdown_pct IS NULL OR (max_drawdown_pct >= 0 AND max_drawdown_pct <= 100)",
            name="ck_core_backtest_drawdown_range",
        ),
        CheckConstraint("roi_pct IS NULL OR roi_pct >= -100", name="ck_core_backtest_roi_floor"),
        Index("ix_core_backtest_jobs_smallcase_created", "smallcase_id", "created_at"),
        Index("ix_core_backtest_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    smallcase_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("core_smallcase_registry.id", ondelete="CASCADE"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_matches_simulated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roi_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[EngineTaskStatus] = mapped_column(
        _status_type(EngineTaskStatus, "core_backtest_status"),
        nullable=False,
        default=EngineTaskStatus.QUEUED,
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
