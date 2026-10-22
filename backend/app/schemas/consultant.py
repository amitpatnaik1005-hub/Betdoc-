"""Request bodies of the BetDoc Consultant (Group 105) and its Bet-Slip ornament (Group 106). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.backtest_and_parlay import InspectRequest


class ApplyPrescriptionRequest(InspectRequest):
    """The slip with the Doctor's prescription applied: ``replace_leg_id`` swapped for ``with_leg_id`` in place."""

    replace_leg_id: str = Field(min_length=3, max_length=300)
    with_leg_id: str = Field(min_length=3, max_length=300)


class BookingCodeIn(BaseModel):
    """The bookmaker's booking code for a placed Bet-Slip ticket (Group 106); null clears it."""

    model_config = ConfigDict(extra="forbid")

    booking_code: str | None = Field(default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
