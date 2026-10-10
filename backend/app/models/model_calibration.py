"""The model recalibration engine's record (Group 74): every run, and every model's verdict in it.

* ``model_recalibration_runs``: one row per run (scheduled, on demand, triggered by a run of model-blamed
  losses, an administrator's override, an emergency reset): the benchmark it measured against, how many
  models moved up or down the lifecycle, the weights it published to ``<TWIN_PREFIX>:model_weights``
  (the hash pillar 1 reads) and the thresholds it used.
* ``model_weight_audits``: one row per model per run: its sample counts, Brier scores (30 and 90 days,
  and half-life decayed), its Brier skill against the benchmark on the legs both priced, the Murphy
  decomposition, its CLV, the weight it had and the weight it got, its lifecycle state and why.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class ModelLifecycleStatus(StrEnum):
    ALPHA_BOOSTED = "ALPHA_BOOSTED"  # beats the benchmark and the closing line: the most say in pillar 1
    ACTIVE = "ACTIVE"  # within bounds of the benchmark (or still short of history)
    PROBATION = "PROBATION"  # under the benchmark or the closing line: attenuated, the Sentinel warned
    BENCHED = "BENCHED"  # worse than a coin flip, or bleeding CLV: weight 0, and no veto in pillar 1


LIFECYCLE_RANK = {ModelLifecycleStatus.BENCHED: 0, ModelLifecycleStatus.PROBATION: 1, ModelLifecycleStatus.ACTIVE: 2, ModelLifecycleStatus.ALPHA_BOOSTED: 3}


class RecalibrationTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"  # Celery beat
    ON_DEMAND_ADMIN = "ON_DEMAND_ADMIN"
    LOSS_THRESHOLD_TRIGGER = "LOSS_THRESHOLD_TRIGGER"  # a run of losses blamed on the models (Group 73's root causes)
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"  # an administrator set one model's weight
    EMERGENCY_RESET = "EMERGENCY_RESET"  # every weight cleared: pillar 1 back to equal weights


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class ModelRecalibrationRun(Base):
    __tablename__ = "model_recalibration_runs"
    __table_args__ = (
        CheckConstraint(f"trigger_type IN ({_values(RecalibrationTrigger)})", name="trigger_known"),
        CheckConstraint("models_evaluated >= 0 AND models_promoted >= 0 AND models_demoted >= 0", name="counts_not_negative"),
        Index("ix_model_recalibration_runs_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger_type: Mapped[str] = mapped_column(String(32))
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # the administrator, when one did
    models_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    models_promoted: Mapped[int] = mapped_column(Integer, default=0)  # moved up the lifecycle since the previous run
    models_demoted: Mapped[int] = mapped_column(Integer, default=0)
    benchmark_model: Mapped[str] = mapped_column(String(32))
    benchmark_brier: Mapped[float | None] = mapped_column(Float, nullable=True)  # its 90-day Brier score, when it has one
    published: Mapped[bool] = mapped_column(Boolean, default=False)  # the weights reached Redis
    published_weights: Mapped[dict[str, float]] = mapped_column(JsonColumn, default=dict)
    parameters: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # the thresholds the run used
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # an override's reason, a skip's cause
    developer_credit: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ModelWeightAudit(Base):
    __tablename__ = "model_weight_audits"
    __table_args__ = (
        CheckConstraint("new_weight >= 0", name="new_weight_not_negative"),
        CheckConstraint("previous_weight IS NULL OR previous_weight >= 0", name="previous_weight_not_negative"),
        CheckConstraint(f"status IN ({_values(ModelLifecycleStatus)})", name="status_known"),
        Index("ix_model_weight_audits_model_created", "model_name", "created_at"),
        Index("ix_model_weight_audits_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_recalibration_runs.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(32))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)  # settled predictions in the long window
    sample_count_30d: Mapped[int] = mapped_column(Integer, default=0)
    paired_count: Mapped[int] = mapped_column(Integer, default=0)  # of them, legs the benchmark also priced
    brier_score_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    brier_score_90d: Mapped[float | None] = mapped_column(Float, nullable=True)
    brier_decayed: Mapped[float | None] = mapped_column(Float, nullable=True)  # half-life weighted
    brier_skill_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 1 - BS_model / BS_benchmark on the paired legs
    avg_clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    reliability: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_weight: Mapped[float | None] = mapped_column(Float, nullable=True)  # None: it had none (pillar 1 used 1)
    new_weight: Mapped[float] = mapped_column(Float)
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    status_reason: Mapped[str] = mapped_column(Text)
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
