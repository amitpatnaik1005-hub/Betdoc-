"""The Wire's contracts (Group 39, extended in Group 78). Developed for Amit Ashok Kumar Patnaik.

Every addition is optional, so the Group 39 aggregator and its providers keep their shape: a provider that knows
nothing of sentiment or friction still builds a valid item.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    sentiment_score: float | None = None  # -1 .. +1, about the side named first
    tactical_impact: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    source_credibility: float | None = None
    fixture_ids: list[str] = Field(default_factory=list)
    associated_steam_move_id: str | None = None  # "<fixture>|Match Odds|<selection>" when the market followed


class WeatherReport(_WireModel):
    match_id: str = Field(min_length=1)
    temperature_c: float | None = None
    condition: str
    wind_speed_kmh: float | None = Field(default=None, ge=0.0)
    wind_cardinal: str | None = None
    humidity_pct: float | None = None
    precipitation_mmh: float | None = None
    pitch_impact_score: float | None = None
    is_indoor_dome: bool | None = None
    roof_may_close: bool | None = None
    tactical_advisory: str | None = None
    venue_name: str | None = None
    kickoff_at: datetime | None = None
    fetched_at: datetime | None = None


class MatchScore(_WireModel):
    match_id: str = Field(min_length=1)
    home_team: str
    away_team: str
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    status: str
    clock: str | None = None
    detail: str | None = None  # ESPN's status line: "FT", "45'+2'", "Q3 4:12"
    source: str | None = None


class SteamCatalystAlert(_WireModel):
    article_id: str
    headline: str
    match_id: str
    selection: str
    probability_before: float
    probability_after: float
    shift_pct: float  # points of probability, signed
    latency_seconds: float
    tactical_impact: str
    source_credibility: float
    published_at: datetime


class WireDashboard(_WireModel):
    news: list[NewsItem] = Field(default_factory=list)
    scores: list[MatchScore] = Field(default_factory=list)
    weather: dict[str, WeatherReport] = Field(default_factory=dict)
    catalysts: list[SteamCatalystAlert] = Field(default_factory=list)
    developer_credit: str | None = None


# ---------------------------------------------------------------- request bodies
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


Roof = Literal["OPEN_AIR", "RETRACTABLE", "FIXED_DOME", "INDOOR"]
SurfaceName = Literal["NATURAL_GRASS", "HYBRID", "ARTIFICIAL_TURF", "HARDCOURT", "CLAY"]
AbsenceStatus = Literal["OUT", "SUSPENDED", "DOUBTFUL", "QUESTIONABLE"]


class VenueIn(_Body):
    sport: str = Field(min_length=2, max_length=32)  # sport key prefix: soccer, americanfootball ...
    team_name: str = Field(min_length=1, max_length=128)  # the home side, or the tournament for tennis
    venue_name: str = Field(min_length=1, max_length=160)
    city: str | None = Field(default=None, max_length=96)
    country: str | None = Field(default=None, max_length=64)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    elevation_m: float | None = Field(default=None, ge=-500, le=6000)
    roof_type: Roof
    surface_type: SurfaceName | None = None


class AbsenceIn(_Body):
    fixture_id: str = Field(min_length=1, max_length=128)
    side: Literal["HOME", "AWAY"]
    player_name: str = Field(min_length=1, max_length=128)
    position: str | None = Field(default=None, max_length=32)
    status: AbsenceStatus
    injury_nature: str | None = Field(default=None, max_length=128)
    rating: float | None = Field(default=None, ge=0)
    replacement_quality: float | None = Field(default=None, ge=0, lt=1)


class AbsenceRating(_Body):
    rating: float = Field(ge=0)
    replacement_quality: float | None = Field(default=None, ge=0, lt=1)


class RefereeAssignment(_Body):
    fixture_id: str = Field(min_length=1, max_length=128)
    referee_name: str = Field(min_length=2, max_length=96)


class OfficiatingRecordIn(_Body):
    referee_name: str = Field(min_length=2, max_length=96)
    league: str = Field(min_length=2, max_length=64)  # ESPN league path ("soccer/eng.1") or the feed's sport key
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    played_at: datetime
    home_yellow: int = Field(ge=0, le=20)
    away_yellow: int = Field(ge=0, le=20)
    home_red: int = Field(default=0, ge=0, le=5)
    away_red: int = Field(default=0, ge=0, le=5)
    home_penalties: int = Field(default=0, ge=0, le=5)
    away_penalties: int = Field(default=0, ge=0, le=5)
    home_goals: int = Field(ge=0, le=30)
    away_goals: int = Field(ge=0, le=30)


class OfficiatingImport(_Body):
    source: str = Field(min_length=2, max_length=48)  # where the figures come from ("premier league match reports")
    records: list[OfficiatingRecordIn] = Field(min_length=1, max_length=2000)
