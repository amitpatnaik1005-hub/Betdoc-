"""Requests to the neural event bus (Group 108, point b.9). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.backtest_and_parlay import Skin

LegId = Annotated[str, StringConstraints(min_length=3, max_length=300)]


class ChainRequest(BaseModel):
    """Run the chain on one item of VIDUR's board; with the Oracle's slip, the Consultant re-scores it."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=3, max_length=400)  # as the board carries it: news:<id>, absences:<fixture>, weather:<fixture>, steam:<...>
    leg_ids: list[LegId] = Field(default_factory=list, max_length=20)  # "<fixture>|<market>|<selection>"
    skin: Skin = "betdoc"
