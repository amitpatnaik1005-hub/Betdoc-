"""Pydantic V2 contracts for Human Touch Mode (FA-8)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.human_touch.manager import (
    FACTOR_IMPACT_CEILING,
    FACTOR_IMPACT_FLOOR,
    FACTOR_VALUE_CEILING,
    FACTOR_VALUE_FLOOR,
    SENTIMENT_CEILING,
    SENTIMENT_FLOOR,
)
from app.models.human_touch import (
    MAX_ADJUSTMENT_LIMIT_CEILING_PCT,
    MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT,
    PROBABILITY_CEILING,
    PROBABILITY_FLOOR,
    VALID_OUTCOMES,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
)

MAX_NARRATIVE_FACTORS = 32

ConfidenceTierLiteral = Literal["HIGH_CONFIDENCE", "NEUTRAL", "CONTRARIAN"]


class HumanTouchSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


# --------------------------------------------------------------------------- configuration


class HumanTouchConfigRead(HumanTouchSchema):
    id: UUID
    is_blended_mode_active: bool
    max_adjustment_limit_pct: float
    sentiment_weight: float
    momentum_weight: float
    min_adjustment_threshold_pct: float
    created_at: datetime
    updated_at: datetime


class HumanTouchConfigUpdate(HumanTouchSchema):
    is_blended_mode_active: bool
    max_adjustment_limit_pct: float = Field(ge=0.0, le=MAX_ADJUSTMENT_LIMIT_CEILING_PCT)
    sentiment_weight: float = Field(ge=WEIGHT_FLOOR, le=WEIGHT_CEILING)
    momentum_weight: float = Field(ge=WEIGHT_FLOOR, le=WEIGHT_CEILING)
    min_adjustment_threshold_pct: float = Field(ge=0.0, le=MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT)

    @model_validator(mode="after")
    def _threshold_within_limit(self) -> "HumanTouchConfigUpdate":
        if self.min_adjustment_threshold_pct > self.max_adjustment_limit_pct:
            raise ValueError("min_adjustment_threshold_pct cannot exceed max_adjustment_limit_pct.")
        return self


# --------------------------------------------------------------------------- blending


class NarrativeFactor(HumanTouchSchema):
    name: str = Field(min_length=1, max_length=64)
    value: float = Field(ge=FACTOR_VALUE_FLOOR, le=FACTOR_VALUE_CEILING)
    impact: float = Field(ge=FACTOR_IMPACT_FLOOR, le=FACTOR_IMPACT_CEILING)


class BlendRequest(HumanTouchSchema):
    pure_math_prob: float = Field(ge=PROBABILITY_FLOOR, le=PROBABILITY_CEILING)
    sentiment_score: float = Field(ge=SENTIMENT_FLOOR, le=SENTIMENT_CEILING)
    factors: list[NarrativeFactor] = Field(default_factory=list, max_length=MAX_NARRATIVE_FACTORS)

    @model_validator(mode="after")
    def _unique_factor_names(self) -> "BlendRequest":
        names = [factor.name.lower() for factor in self.factors]
        if len(names) != len(set(names)):
            raise ValueError("Narrative factor names must be unique.")
        return self


class BlendExecuteRequest(BlendRequest):
    match_id: str = Field(min_length=1, max_length=128)


class BlendResponse(HumanTouchSchema):
    pure_math_prob: float
    adjusted_prob: float
    adjustment_delta: float
    confidence_tier: ConfidenceTierLiteral
    narrative_modifier: float
    raw_blended_prob: float
    bypassed: bool
    below_threshold: bool
    clamped: bool


# --------------------------------------------------------------------------- override logs


class OverrideLogRead(HumanTouchSchema):
    id: UUID
    match_id: str
    pure_math_prob: float
    blended_prob: float
    actual_outcome: float | None
    math_brier_score: float | None
    blended_brier_score: float | None
    human_touch_improved: bool | None
    created_at: datetime
    resolved_at: datetime | None


class BlendExecuteResponse(BlendResponse):
    log: OverrideLogRead


class ResolveOverrideRequest(HumanTouchSchema):
    actual_outcome: float

    @model_validator(mode="after")
    def _valid_outcome(self) -> "ResolveOverrideRequest":
        if self.actual_outcome not in VALID_OUTCOMES:
            raise ValueError(f"actual_outcome must be one of {VALID_OUTCOMES} (loss, draw, win).")
        return self


class ResolveOverrideResponse(HumanTouchSchema):
    log: OverrideLogRead
    human_touch_improved: bool
    brier_improvement: float
