"""Request bodies of the recalibration engine's API (Group 74)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ManualWeightOverrideRequest(BaseModel):
    """An administrator sets one model's weight in pillar 1. 0 benches it (no vote, no veto). A pinned weight
    survives every recalibration until it is overridden again without the pin, or reset."""

    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    weight: float = Field(ge=0)
    reason: str = Field(min_length=5, max_length=512)
    pin: bool = False


class WeightResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=512)
