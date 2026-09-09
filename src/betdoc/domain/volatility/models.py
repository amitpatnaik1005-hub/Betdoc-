from __future__ import annotations
from datetime import UTC, datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

class OddsTick(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_key: str = Field(min_length=1, max_length=256)
    offered_odds: float = Field(gt=1.0, lt=10_000.0)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

class VolatilityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_key: str
    sample_count: int = Field(ge=0)
    realized_volatility: float = Field(ge=0.0)
    volatility_per_minute: float = Field(ge=0.0)
    drift_bps: int
    latest_odds: float = Field(gt=0.0)
    window_span_seconds: float = Field(ge=0.0)
    is_stale: bool
    assessed_at: datetime

    def breaches(self, tolerance: float) -> bool:
        return self.is_stale or self.volatility_per_minute > tolerance
