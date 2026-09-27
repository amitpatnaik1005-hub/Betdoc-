import uuid
from typing import Literal

from pydantic import BaseModel, Field

Selection = Literal["HOME", "DRAW", "AWAY"]


class MatchContext(BaseModel):
    home_team: str = Field(..., min_length=1, max_length=120)
    away_team: str = Field(..., min_length=1, max_length=120)
    home_xg: float | None = Field(default=None, ge=0.0, le=10.0)
    away_xg: float | None = Field(default=None, ge=0.0, le=10.0)
    home_elo: float | None = Field(default=None, ge=0.0, le=4000.0)
    away_elo: float | None = Field(default=None, ge=0.0, le=4000.0)
    bookmaker_odds: dict[str, float] | None = None
    match_id: str | None = Field(default=None, max_length=128)


class PredictionResult(BaseModel):
    home_win_prob: float
    draw_prob: float
    away_win_prob: float
    most_likely_scoreline: str
    confidence_score: float


class ValueBetFlag(BaseModel):
    selection: Selection
    true_prob: float
    bookmaker_odds: float
    expected_value: float
    kelly_stake_fraction: float
    leg_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    match_id: str = ""
    market_type: str = "MATCH_WINNER_1X2"


class EnginePredictionResponse(BaseModel):
    prediction: PredictionResult
    value_bets: list[ValueBetFlag]
