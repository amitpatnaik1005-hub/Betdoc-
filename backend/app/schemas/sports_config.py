"""
Pydantic v2 Contracts for Multi-Sport Quantitative Configurations & APIs (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
Side = Literal["home", "away", "draw"]
StructureKind = Literal["parlay", "teaser", "pleaser", "round_robin", "if_bet", "action_reverse", "double_action", "futures"]
MAX_STATS_KEYS = 200


class StrictModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", allow_inf_nan=False)


def _bounded(value: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if value is not None and len(value) > MAX_STATS_KEYS:
        raise ValueError(f"{name} holds more than {MAX_STATS_KEYS} keys")
    return value


# ================================================================ configuration & leagues
class SupportedSport(StrictModel):
    sport_key: str
    display_name: str
    standard_markets: list[str]
    supported_markets: list[str]
    bet_types: list[str]
    model_family: str
    kelly_multiplier: float
    odds_api_family: str
    has_draw: bool
    developer_credit: str = DEVELOPER_CREDIT


class SportConfigResponse(StrictModel):
    id: str
    sport_key: str
    display_name: str
    is_active: bool
    default_kelly_fraction: float
    min_market_liquidity: float
    primary_odds_api_sport_key: str
    supported_markets: list[str]
    supported_bet_types: list[str]
    math_model_parameters: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    developer_credit: str = DEVELOPER_CREDIT


class SportConfigUpdate(StrictModel):
    is_active: bool | None = None
    default_kelly_fraction: float | None = Field(default=None, ge=0.01, le=1.0)
    min_market_liquidity: float | None = Field(default=None, ge=0.0)
    primary_odds_api_sport_key: str | None = Field(default=None, min_length=1, max_length=64)
    supported_markets: list[str] | None = Field(default=None, max_length=64)
    math_model_parameters: dict[str, Any] | None = None

    @field_validator("math_model_parameters")
    @classmethod
    def _params_bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _bounded(value, "math_model_parameters")


class LeagueResponse(StrictModel):
    id: str
    sport_key: str
    league_key: str
    display_name: str
    country: str
    tier: int
    is_active: bool
    odds_api_sport_key: str | None
    created_at: datetime
    developer_credit: str = DEVELOPER_CREDIT


class LeagueCreate(StrictModel):
    sport_key: str = Field(min_length=1, max_length=32)
    league_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=128)
    country: str = Field(default="International", min_length=1, max_length=64)
    tier: int = Field(default=1, ge=1, le=5)
    is_active: bool = True
    odds_api_sport_key: str | None = Field(default=None, max_length=64)


class LeagueUpdate(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    country: str | None = Field(default=None, min_length=1, max_length=64)
    tier: int | None = Field(default=None, ge=1, le=5)
    is_active: bool | None = None
    odds_api_sport_key: str | None = Field(default=None, max_length=64)


# ================================================================ pricing
class MatchProbabilitiesRequest(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    home_stats: dict[str, Any]
    away_stats: dict[str, Any]
    context: dict[str, Any] | None = None
    fixture_id: str | None = Field(default=None, max_length=128)  # read the twin's intel (weather, injuries, referee) for it

    @field_validator("home_stats", "away_stats", "context")
    @classmethod
    def _stats_bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _bounded(value, "stats")


class SubModelOut(StrictModel):
    name: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    expected_total: float | None
    weight: float


class MatchProbabilitiesResponse(StrictModel):
    sport_key: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    fair_home_odds: float
    fair_away_odds: float
    fair_draw_odds: float | None
    expected_total_score: float
    sport_specific_metrics: dict[str, Any]
    market_probabilities: dict[str, float]
    sub_models: list[SubModelOut] = Field(default_factory=list)
    consensus: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, bool] = Field(default_factory=dict)
    kelly_multiplier: float = 0.25
    model_family: str = ""
    latency_ms: float = 0.0
    developer_credit: str = DEVELOPER_CREDIT


class MarketResolutionRequest(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    market_type: str = Field(min_length=1, max_length=64)
    match_result: dict[str, Any]
    selection: str = Field(min_length=1, max_length=128)
    line: float | None = None
    params: dict[str, Any] | None = None


class MarketResolutionResponse(StrictModel):
    is_win: bool
    status: str  # WON | LOST | VOID | HALF_WON | HALF_LOST
    sport_key: str
    market_type: str
    canonical_market: str
    selection: str
    line: float | None = None
    developer_credit: str = DEVELOPER_CREDIT


# ================================================================ structures (parlay ... futures)
class StructureLegIn(StrictModel):
    leg_id: str = Field(min_length=1, max_length=64)
    odds: float = Field(default=1.0, ge=1.0, le=1000.0)
    sport_key: str | None = Field(default=None, max_length=64)
    market_type: str | None = Field(default=None, max_length=64)
    selection: str | None = Field(default=None, max_length=128)
    line: float | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    match_result: dict[str, Any] | None = None
    status: Literal["PENDING", "WON", "LOST", "VOID", "HALF_WON", "HALF_LOST"] | None = None


class StructureSettleRequest(StrictModel):
    structure: StructureKind
    legs: list[StructureLegIn] = Field(default_factory=list, max_length=20)
    stake: float | None = Field(default=None, gt=0, le=1_000_000)
    stakes: list[float] | None = Field(default=None, max_length=20)  # if-bets: one per leg
    points: float | None = Field(default=None, gt=0, le=20)  # teaser / pleaser
    sizes: list[int] | None = Field(default=None, max_length=20)  # round robin parlay sizes
    selection: str | None = Field(default=None, max_length=128)  # futures
    odds: float | None = Field(default=None, ge=1.0, le=100_000)  # futures
    winners: list[str] | None = Field(default=None, max_length=50)  # futures: None = not yet decided
    cancelled: bool = False


class StructureSettleResponse(StrictModel):
    structure: str
    status: str
    total_stake: float
    payout: float
    profit: float
    legs: dict[str, str]
    lines: list[dict[str, Any]]
    detail: dict[str, Any]
    developer_credit: str = DEVELOPER_CREDIT


class BetTypeOut(StrictModel):
    key: str
    label: str
    kind: str
    description: str
    sports: list[str]
    markets: dict[str, list[str]]


# ================================================================ the twin pipeline
class EvaluateRequest(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    home_stats: dict[str, Any]
    away_stats: dict[str, Any]
    context: dict[str, Any] | None = None
    fixture_id: str | None = Field(default=None, max_length=128)
    selection: Side
    venue_odds: dict[str, dict[str, float]] = Field(default_factory=dict)  # venue -> {"home": 2.1, "away": 1.8, "draw": 3.4}
    bankroll_inr: float = Field(default=100_000.0, gt=0, le=1e10)
    drawdown: float = Field(default=0.0, ge=0.0, lt=1.0)
    bet_kind: Literal["single", "multi", "prop"] = "single"


class EvaluateResponse(StrictModel):
    sport_key: str
    selection: str
    fair_probability: float
    fair_odds: float
    pillar_1_consensus: dict[str, Any]
    pillar_9_sharp_benchmark: dict[str, Any] | None
    route: dict[str, Any] | None
    edge_vs_model: float | None
    confidence: dict[str, Any]
    sizing: dict[str, Any] | None
    evidence: dict[str, bool]
    intel_sections: list[str]
    developer_credit: str = DEVELOPER_CREDIT


class EdgeScreenRequest(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    match_id: str = Field(min_length=1, max_length=128)
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    venue_odds: dict[str, dict[str, float]]
    home_stats: dict[str, Any]
    away_stats: dict[str, Any]
    context: dict[str, Any] | None = None


class InPlayRequest(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    home_stats: dict[str, Any] = Field(default_factory=dict)
    away_stats: dict[str, Any] = Field(default_factory=dict)
    live_state: dict[str, Any]
    context: dict[str, Any] | None = None
    fixture_id: str | None = Field(default=None, max_length=128)
    selection: Side | None = None  # the backed side: the shield is evaluated for it
    entry_probability: float | None = Field(default=None, gt=0, lt=1)
    stake_inr: float | None = Field(default=None, gt=0, le=1e9)
    entry_odds: float | None = Field(default=None, gt=1, le=1000)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    cashout_offer_inr: float | None = Field(default=None, ge=0, le=1e10)


class InPlayResponse(StrictModel):
    sport_key: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    detail: dict[str, Any]
    shield: dict[str, Any] | None
    published: bool
    developer_credit: str = DEVELOPER_CREDIT


class DisciplineCandidate(StrictModel):
    sport_key: str = Field(min_length=1, max_length=64)
    stake: float = Field(gt=0, le=1e9)
    odds: float = Field(gt=1, le=1000)
    model_probability: float | None = Field(default=None, gt=0, lt=1)
    bet_ref: str = Field(min_length=1, max_length=96)  # what earns the XP, once
    placed_at: datetime | None = None
    bankroll_inr: float | None = Field(default=None, gt=0, le=1e10)


class DisciplineHistoryItem(StrictModel):
    placed_at: datetime
    sport_key: str = Field(min_length=1, max_length=64)
    stake: float = Field(gt=0, le=1e9)
    odds: float = Field(ge=1, le=1000)
    result: Literal["WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "PENDING", "CASH_OUT"] = "PENDING"
    pnl: float | None = None


class DisciplineRequest(StrictModel):
    candidate: DisciplineCandidate
    history: list[DisciplineHistoryItem] | None = Field(default=None, max_length=500)  # None: the user's own ledger


class DisciplineResponse(StrictModel):
    score: float
    flags: list[str]
    reasons: dict[str, str]
    core_sports: list[str]
    disciplined: bool
    edge: float | None
    detail: dict[str, float]
    kelly_stake: float | None
    xp_awarded: int
    developer_credit: str = DEVELOPER_CREDIT
