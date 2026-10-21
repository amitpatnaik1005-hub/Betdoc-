"""Contracts of the pre-match board (Group 101, point b.13a). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PreMatchSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PreMatchFixtureSummary(PreMatchSchema):
    id: UUID
    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    consensus_fair_odds: dict[str, Any] = Field(default_factory=dict)
    bookmaker_odds: dict[str, Any] = Field(default_factory=dict)
    last_polled_at: datetime
    # The 3-tier cadence: the tier the fixture is on now, the board's age and the age its tier allows
    tier: Literal[1, 2, 3]
    age_seconds: float = Field(ge=0)
    max_age_seconds: float = Field(gt=0)
    is_stale: bool = False


class PreMatchFixtureDetail(PreMatchFixtureSummary):
    markets_json: dict[str, Any] = Field(default_factory=dict)


class PreMatchRefreshResponse(PreMatchSchema):
    match_id: str
    status: Literal["SYNCED", "FRESH", "NOT_OFFERED"]
    synced_at: datetime  # when the provider served the board now stored
    source: Literal["the_odds_api", "cache"]
    detail: str
    fixture: PreMatchFixtureDetail
