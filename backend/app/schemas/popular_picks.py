"""Pydantic V2 contracts for Oracle Popular Picks (FA-1)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PickTypeLiteral = Literal["TRENDING", "AI_PREDICTED", "SHARP_MONEY"]
ReviewDecisionLiteral = Literal["ACCEPTED", "REJECTED", "MODIFIED"]


class PopularPicksSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ParlayLegSchema(PopularPicksSchema):
    match_id: str = Field(min_length=1, max_length=64)
    selection: str = Field(min_length=1, max_length=64)
    odds: float = Field(ge=1.0, allow_inf_nan=False)


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


class ReviewDecisionRequest(PopularPicksSchema):
    decision: ReviewDecisionLiteral


class ReviewDecisionResponse(PopularPicksSchema):
    id: UUID
    parlay_id: UUID
    user_id: UUID | None
    decision: ReviewDecisionLiteral
    created_at: datetime
