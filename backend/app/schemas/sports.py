"""Pydantic V2 contracts for Multi-Sport Support."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", allow_inf_nan=False)


# --------------------------------------------------------------------------- stored configuration payloads


class CricketConfig(StrictSchema):
    dls_weight_factor: float = Field(default=1.0, gt=0)
    total_overs: float = Field(default=50.0, gt=0)
    wickets_per_innings: int = Field(default=10, ge=1)


class BasketballConfig(StrictSchema):
    std_dev: float = Field(default=11.5, gt=0)


class TennisConfig(StrictSchema):
    elo_scale: float = Field(default=400.0, gt=0)


SPORT_CONFIG_SCHEMAS: dict[str, type[StrictSchema]] = {
    "cricket": CricketConfig,
    "basketball": BasketballConfig,
    "tennis": TennisConfig,
}


class SportConfigRead(StrictSchema):
    id: UUID
    sport_name: str
    is_active: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _decode_config_json(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            if "config_json" not in data:
                return data
            payload = {k: v for k, v in data.items() if k in cls.model_fields and k != "config"}
            raw = data["config_json"]
        elif hasattr(data, "config_json"):
            payload = {name: getattr(data, name) for name in cls.model_fields if name != "config"}
            raw = data.config_json
        else:
            return data
        try:
            payload["config"] = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("config_json is not valid JSON.") from exc
        return payload


class SportConfigUpdate(StrictSchema):
    is_active: bool
    config: dict[str, Any]


# --------------------------------------------------------------------------- cricket


class DlsRequest(StrictSchema):
    resources_left_pct: float
    original_target: float

    @model_validator(mode="after")
    def _bounds(self) -> "DlsRequest":
        if not 0.0 <= self.resources_left_pct <= 1.0:
            raise ValueError("resources_left_pct must be a fraction between 0 and 1.")
        if self.original_target < 0:
            raise ValueError("original_target must be >= 0.")
        return self


class DlsResponse(StrictSchema):
    par_score: float
    original_target: float
    resources_left_pct: float
    dls_weight_factor: float


class ProjectScoreRequest(StrictSchema):
    current_score: float
    overs_bowled: float
    wickets_lost: int
    pitch_degradation_factor: float
    total_overs: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> "ProjectScoreRequest":
        if self.current_score < 0:
            raise ValueError("current_score must be >= 0.")
        if self.overs_bowled < 0:
            raise ValueError("overs_bowled must be >= 0.")
        if self.wickets_lost < 0:
            raise ValueError("wickets_lost must be >= 0.")
        if self.pitch_degradation_factor < 0:
            raise ValueError("pitch_degradation_factor must be >= 0.")
        if self.total_overs is not None:
            if self.total_overs <= 0:
                raise ValueError("total_overs must be > 0.")
            if self.overs_bowled > self.total_overs:
                raise ValueError("overs_bowled cannot exceed total_overs.")
        return self


class ProjectScoreResponse(StrictSchema):
    projected_score: float
    total_overs: float
    wickets_per_innings: int
    innings_complete: bool


# --------------------------------------------------------------------------- basketball


class SpreadRequest(StrictSchema):
    home_rating: float
    away_rating: float
    home_pace: float
    away_pace: float
    league_avg_pace: float

    @model_validator(mode="after")
    def _bounds(self) -> "SpreadRequest":
        for name in ("home_pace", "away_pace", "league_avg_pace"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0.")
        return self


class SpreadResponse(StrictSchema):
    pace_adjusted_spread: float
    home_win_probability: float
    away_win_probability: float
    std_dev: float


# --------------------------------------------------------------------------- tennis


class TennisGameRequest(StrictSchema):
    base_serve_prob: float
    player_surface_elo: float
    opponent_surface_elo: float

    @model_validator(mode="after")
    def _bounds(self) -> "TennisGameRequest":
        if not 0.0 <= self.base_serve_prob <= 1.0:
            raise ValueError("base_serve_prob must be between 0 and 1.")
        return self


class TennisGameResponse(StrictSchema):
    elo_win_probability: float
    adjusted_serve_prob: float
    game_win_probability: float
    elo_scale: float
