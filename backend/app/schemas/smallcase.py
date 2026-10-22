"""Request bodies of the live smallcases (Group 107). Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.backtest_and_parlay import Skin


class SlipAsSmallcase(BaseModel):
    """The Oracle's slip, to be rebalanced as a smallcase at the skin's book (BetDoc: the best retail price per leg)."""

    model_config = ConfigDict(extra="forbid")

    leg_ids: list[str] = Field(min_length=1, max_length=20)  # "<fixture>|<market>|<selection>", as the board carries them
    skin: Skin = "betdoc"
    bankroll_inr: Decimal | None = Field(default=None, gt=0)
