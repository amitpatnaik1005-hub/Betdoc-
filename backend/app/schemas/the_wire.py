from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _WireModel(BaseModel):
    # frozen: cached instances are shared across requests and must be immutable.
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewsItem(_WireModel):
    id: str = Field(min_length=1)
    source: str
    title: str
    summary: str
    url: str
    published_at: datetime


class WeatherReport(_WireModel):
    match_id: str = Field(min_length=1)
    temperature_c: float | None = None
    condition: str
    wind_speed_kmh: float | None = Field(default=None, ge=0.0)


class MatchScore(_WireModel):
    match_id: str = Field(min_length=1)
    home_team: str
    away_team: str
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    status: str
    clock: str | None = None


class WireDashboard(_WireModel):
    news: list[NewsItem] = Field(default_factory=list)
    scores: list[MatchScore] = Field(default_factory=list)
    weather: dict[str, WeatherReport] = Field(default_factory=dict)
