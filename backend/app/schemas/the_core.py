"""Pydantic V2 contracts for The Core (Engine Room)."""

import json
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.the_core import EngineTaskStatus, SmallcaseStatus

MAX_MATCH_CONTEXT_BYTES = 64 * 1024
MAX_COMPARE_SMALLCASES = 16
MAX_BACKTEST_RANGE_DAYS = 3650

EngineState = Literal["ONLINE", "DEGRADED", "SATURATED"]
StageKind = Literal["probability", "value_filter", "blender", "staking"]
StageExecutionMode = Literal["NATIVE", "SIMULATED"]


class CoreSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


def _validate_match_context(value: dict[str, Any]) -> dict[str, Any]:
    if not value:
        raise ValueError("match_context must not be empty.")
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"match_context must be JSON-serialisable: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_MATCH_CONTEXT_BYTES:
        raise ValueError(f"match_context exceeds {MAX_MATCH_CONTEXT_BYTES} bytes.")
    return value


class EngineTelemetryRead(CoreSchema):
    id: UUID
    cpu_usage_pct: float = Field(ge=0, le=100)
    memory_usage_mb: int = Field(ge=0)
    queue_depth: int = Field(ge=0)
    active_models_count: int = Field(ge=0)
    recorded_at: datetime
    engine_state: EngineState
    master_bot: str = "PRATAP"
    math_engine: str = "PANINI"
    total_smallcases: int = Field(ge=0)
    active_smallcases: int = Field(ge=0)


class SmallcaseRead(CoreSchema):
    id: UUID
    name: str
    description: str
    status: SmallcaseStatus
    current_accuracy: float
    cross_val_score: float
    total_backtests_run: int
    last_tested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PipelineStageRead(CoreSchema):
    model_config = ConfigDict(from_attributes=True, extra="forbid", protected_namespaces=())

    index: int = Field(ge=0)
    model_name: str
    stage_kind: StageKind
    execution_mode: StageExecutionMode


class SmallcaseDetailRead(SmallcaseRead):
    pipeline_config: list[str]
    pipeline_stages: list[PipelineStageRead] = Field(default_factory=list)


class SmallcaseToggleRequest(CoreSchema):
    expected_status: SmallcaseStatus
    target_status: SmallcaseStatus

    @model_validator(mode="after")
    def _must_change(self) -> "SmallcaseToggleRequest":
        if self.expected_status == self.target_status:
            raise ValueError("target_status must differ from expected_status.")
        return self


class TestBenchRequest(CoreSchema):
    __test__ = False

    smallcase_id: UUID
    match_context: dict[str, Any]

    @field_validator("match_context")
    @classmethod
    def _check_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_match_context(value)


class TestCompareRequest(CoreSchema):
    __test__ = False

    smallcase_ids: list[UUID] = Field(min_length=1, max_length=MAX_COMPARE_SMALLCASES)
    match_context: dict[str, Any]

    @field_validator("smallcase_ids")
    @classmethod
    def _unique_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("smallcase_ids must be unique.")
        return value

    @field_validator("match_context")
    @classmethod
    def _check_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_match_context(value)


class BacktestJobRequest(CoreSchema):
    smallcase_id: UUID
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_range(self) -> "BacktestJobRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date.")
        if (self.end_date - self.start_date).days > MAX_BACKTEST_RANGE_DAYS:
            raise ValueError(f"Backtest range cannot exceed {MAX_BACKTEST_RANGE_DAYS} days.")
        return self


class TestBenchRunRead(CoreSchema):
    __test__ = False

    id: UUID
    smallcase_id: UUID
    match_context: dict[str, Any]
    pipeline_execution_steps: list[dict[str, Any]]
    predicted_outcome: dict[str, Any] | None
    status: EngineTaskStatus
    is_stress_test: bool
    execution_time_ms: float | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class BacktestJobRead(CoreSchema):
    id: UUID
    smallcase_id: UUID
    start_date: date
    end_date: date
    total_matches_simulated: int
    roi_pct: float | None
    accuracy_pct: float | None
    max_drawdown_pct: float | None
    status: EngineTaskStatus
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None
