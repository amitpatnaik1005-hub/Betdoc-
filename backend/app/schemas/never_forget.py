"""Request bodies of the Never-Forget shield's API (Group 75)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RuleStatusRequest(BaseModel):
    """An administrator archives a lesson, or promotes one to ACTIVE; the reason is kept on the rule."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=512)
