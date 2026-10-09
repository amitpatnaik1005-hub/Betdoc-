"""Pydantic V2 contracts for Oracle Popular Picks (FA-1)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PickTypeLiteral = Literal["TRENDING", "AI_PREDICTED", "SHARP_MONEY"]
ReviewDecisionLiteral = Literal["ACCEPTED", "REJECTED", "MODIFIED"]


class PopularPicksSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ParlayLegSchema(PopularPicksSchema):
    match_id: str = Field(min_length=1, max_length=128)
    selection: str = Field(min_length=1, max_length=64)
    odds: float = Field(ge=1.0, allow_inf_nan=False)
    # Group 69: Ashoka's trend scan fills these in
    market: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=160)
    fixture: str | None = Field(default=None, max_length=260)
    fair_probability: float | None = Field(default=None, ge=0, le=1)
    book: str | None = Field(default=None, max_length=32)


class PopularParlayRead(PopularPicksSchema):
    id: UUID
    title: str
    pick_type: PickTypeLiteral
    legs: list[ParlayLegSchema]
    total_odds: float
    historical_success_rate: float
    is_active: bool
    created_at: datetime
    expires_at: datetime
    category: Literal["SHARP_STEAM", "PUBLIC_TRAP", "AI_HYBRID"] | None = None
    true_ev_pct: float | None = None
    true_probability: float | None = None
    public_share_pct: float | None = None
    public_share_source: str | None = None
    warning: str | None = None
    analysis: dict[str, Any] | None = None


class ExternalParlayLeg(PopularPicksSchema):
    home: str = Field(min_length=1, max_length=128)
    away: str = Field(min_length=1, max_length=128)
    market: str = Field(default="Match Odds", min_length=1, max_length=64)
    selection: str = Field(min_length=1, max_length=16)
    odds: float = Field(ge=1.01, le=1000, allow_inf_nan=False)


class ExternalParlaySubmit(PopularPicksSchema):
    """A popular parlay seen on a bookmaker's own "popular bets" page, entered by an administrator with
    where the public share came from. Ashoka prices it against the live market and flags a trap."""

    title: str = Field(min_length=1, max_length=120)
    bookmaker: str = Field(default="Parimatch", min_length=1, max_length=32)
    legs: list[ExternalParlayLeg] = Field(min_length=2, max_length=12)
    public_share_pct: float | None = Field(default=None, ge=0, le=100)
    public_share_source: str | None = Field(default=None, max_length=64)


class ReviewDecisionRequest(PopularPicksSchema):
    decision: ReviewDecisionLiteral


class ReviewDecisionResponse(PopularPicksSchema):
    id: UUID
    parlay_id: UUID
    user_id: UUID | None
    decision: ReviewDecisionLiteral
    created_at: datetime
