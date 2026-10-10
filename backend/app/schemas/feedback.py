"""Request bodies of the feedback loop's API (Group 73)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.user_bets_ledger import PlacedStatus


class OverrideSettlementRequest(BaseModel):
    """An administrator settles a bet by hand: its final status and what the bookmaker actually paid."""

    model_config = ConfigDict(extra="forbid")

    bet_id: uuid.UUID
    status: PlacedStatus
    return_inr: Decimal = Field(ge=0, le=Decimal("100000000"))
    notes: str | None = Field(default=None, max_length=300)
