"""Request bodies of the BetDoc Consultant (Group 105). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from pydantic import Field

from app.schemas.backtest_and_parlay import InspectRequest


class ApplyPrescriptionRequest(InspectRequest):
    """The slip with the Doctor's prescription applied: ``replace_leg_id`` swapped for ``with_leg_id`` in place."""

    replace_leg_id: str = Field(min_length=3, max_length=300)
    with_leg_id: str = Field(min_length=3, max_length=300)
