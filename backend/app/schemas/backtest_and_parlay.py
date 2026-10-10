"""Request bodies of the manual parlay workbench and the in-play stop-loss shield (Group 77).

Backtests are requested through the Lab's own ``BacktestParams`` (``app/schemas/lab_quant.py``), which Group 77
extended with rolling walk-forward folds, the square-root impact model and the risk-free rate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Skin = Literal["parimatch", "one_xbet", "stake", "betdoc"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectRequest(_Body):
    leg_ids: list[str] = Field(min_length=1, max_length=20)  # "<fixture>|<market>|<selection>", from the board
    kind: str | None = None  # SINGLE .. ACCUMULATOR, or a system: TRIXIE, PATENT, YANKEE, CANADIAN, HEINZ, SUPER_HEINZ, GOLIATH
    skin: Skin = "betdoc"
    bankroll_inr: Decimal | None = Field(default=None, gt=0)


class SubmitRequest(_Body):
    audit_id: uuid.UUID  # the inspection's audit
    skin: Skin
    stake_inr: Decimal = Field(gt=0, le=Decimal("10000000"))
    placed_odds: Decimal | None = Field(default=None, ge=Decimal("1.01"), le=Decimal("1000000"))
    booking_code: str | None = Field(default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)  # clamped to TWIN_STOP_LOSS_MIN/MAX_PCT; None: the default
    placed_at: datetime | None = None

    @field_validator("stake_inr")
    @classmethod
    def _paise(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class EmergencyCashoutRequest(_Body):
    amount_inr: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))  # the cashout taken at the book; None: issue the ticket now
