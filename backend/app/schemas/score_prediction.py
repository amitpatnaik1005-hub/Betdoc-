"""Contracts of BETDOC SCORE PREDICTION (Group 102, point b.16). Developed for Amit Ashok Kumar Patnaik.

Stats and context dicts are the Group 79 adapters' own inputs (``/sports/calculate-odds``), bounded the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.sports_config import _bounded

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


class PredictionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SportStatsRequest(PredictionSchema):
    home_stats: dict[str, Any] = Field(default_factory=dict)
    away_stats: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] | None = None
    fixture_id: str | None = Field(default=None, max_length=128)  # read the twin's intel (The Wire's weather, injuries, officials)

    @field_validator("home_stats", "away_stats", "context")
    @classmethod
    def _stats_bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _bounded(value, "stats")


# ---------------------------------------------------------------- football
class MarketInput(PredictionSchema):
    """De-vigged probabilities (they are re-normalised to sum to one) and optional P(over) per goal line."""

    home: float = Field(gt=0, lt=1)
    draw: float = Field(gt=0, lt=1)
    away: float = Field(gt=0, lt=1)
    totals_over: dict[float, float] = Field(default_factory=dict, max_length=24)

    @field_validator("totals_over")
    @classmethod
    def _lines(cls, value: dict[float, float]) -> dict[float, float]:
        for line, p in value.items():
            if not (0 < line <= 10 and abs(line * 4 - round(line * 4)) < 1e-9) or not 0 < p < 1:
                raise ValueError("totals_over maps a goal line (0-10, quarter ball) to a probability strictly between 0 and 1")
        return value


class XgInput(PredictionSchema):
    home: float = Field(gt=0, le=8)
    away: float = Field(gt=0, le=8)


class FootballPredictionRequest(SportStatsRequest):
    """Exactly one source: ``market`` (the scorelines the prices imply), ``xg`` (your own expected goals) or ``stats``
    (the soccer adapter on ``home_stats`` / ``away_stats``)."""

    source: Literal["market", "xg", "stats"]
    market: MarketInput | None = None
    xg: XgInput | None = None
    rho: float | None = Field(default=None, ge=-0.3, le=0.1)  # Dixon-Coles low-score dependence (market / xg); default -0.13

    @model_validator(mode="after")
    def _one_source(self) -> FootballPredictionRequest:
        if self.source == "market" and self.market is None:
            raise ValueError("source 'market' needs market")
        if self.source == "xg" and self.xg is None:
            raise ValueError("source 'xg' needs xg")
        if (self.source != "market" and self.market is not None) or (self.source != "xg" and self.xg is not None):
            raise ValueError("send only the input of the chosen source")
        if self.source == "stats" and self.rho is not None:
            raise ValueError("rho applies to the market and xg sources; the soccer adapter reads its own from context")
        return self


class ScorelineOut(PredictionSchema):
    score: str
    home: int
    away: int
    probability: float


class PricedSelectionOut(PredictionSchema):
    market: str  # the Oracle board's market key
    kind: str
    line: float | None
    selection: str
    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float
    win_probability: float
    fair_odds: float | None


class FixtureRef(PredictionSchema):
    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    last_polled_at: datetime
    books: int


class FootballPredictionResponse(PredictionSchema):
    source: Literal["market", "xg", "stats"]
    expected_goals: list[float]
    heatmap: list[list[float]]
    heatmap_goals: int
    heatmap_mass: float
    top_scorelines: list[ScorelineOut]
    home_win: float
    draw: float
    away_win: float
    btts_yes: float
    btts_no: float
    over: dict[str, float]
    under: dict[str, float]
    markets: list[PricedSelectionOut]
    rho: float | None
    fit_error: float | None
    detail: dict[str, Any]
    fixture: FixtureRef | None = None
    developer_credit: str = DEVELOPER_CREDIT


# ---------------------------------------------------------------- cricket
class CricketPredictionRequest(SportStatsRequest):
    chase_target: int | None = Field(default=None, ge=1, le=1000)


class InningsOut(PredictionSchema):
    side: Literal["home", "away"]
    median: int
    interval: list[int]
    mean: float
    powerplay_median: int | None
    powerplay_interval: list[int] | None
    phase_expected_runs: dict[str, float]
    expected_sixes: float


class ChaseOut(PredictionSchema):
    target: int | None
    source: Literal["stated", "model"]
    win: float
    tie: float
    lose: float
    win_with_super_over: float | None


class CricketPredictionResponse(PredictionSchema):
    format: str
    overs: int
    batting_first: Literal["home", "away"]
    first_innings: InningsOut
    second_innings: InningsOut
    chase: ChaseOut
    match_winner: dict[str, float]
    coverage: float
    conditions: dict[str, Any]
    intel_sections: list[str] = Field(default_factory=list)
    developer_credit: str = DEVELOPER_CREDIT


# ---------------------------------------------------------------- tennis, basketball, MMA
class BasketballPredictionRequest(SportStatsRequest):
    spread_line: float | None = Field(default=None, ge=-60, le=60)  # the home side's handicap (-5.5: home gives 5.5)
    total_line: float | None = Field(default=None, gt=0, le=400)


class SportPredictionResponse(PredictionSchema):
    """Tennis, basketball and MMA: the adapter's projections regrouped (see ``score_predictor``)."""

    sport_key: str
    prediction: dict[str, Any]
    intel_sections: list[str] = Field(default_factory=list)
    developer_credit: str = DEVELOPER_CREDIT
