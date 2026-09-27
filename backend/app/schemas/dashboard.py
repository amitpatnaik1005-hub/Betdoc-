from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardSummary(_StrictModel):
    total_bankroll: float
    daily_pnl: float
    active_bets_count: int = Field(ge=0)
    win_rate_pct: float = Field(ge=0.0, le=100.0)
    current_exposure: float = Field(ge=0.0)
    stop_loss_status: str  # OK | WARNING | BREACHED | NO_MANDATE


class ActivityEvent(_StrictModel):
    event_type: str
    timestamp: datetime
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TipRecommendation(_StrictModel):
    tip_id: str = Field(min_length=64, max_length=64)
    match_ids: list[str] = Field(min_length=1)
    market_types: list[str] = Field(min_length=1)
    selections: list[str] = Field(min_length=1)
    recommended_structure: Literal["SINGLE", "PARLAY"]
    is_parlay: bool
    confidence_score_pct: float = Field(ge=0.0, le=100.0)
    rationale: str
    kelly_stake_pct: float = Field(gt=0.0, le=100.0)
