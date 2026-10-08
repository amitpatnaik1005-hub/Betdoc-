"""Contracts for the CFO ledger API (``/api/v1/omni``): execution, bankroll, risk settings, results."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.cfo_vault import AuditEvent, LedgerStatus
from app.schemas.market import WireDecimal

Money = Annotated[Decimal, Field(allow_inf_nan=False)]


def _max_places(value: Decimal, places: int, name: str) -> Decimal:
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -places:
        raise ValueError(f"{name} allows at most {places} decimal places")
    return value


class ExecuteTradeRequest(BaseModel):
    """One order from the betslip. ``idempotency_key`` is a fresh uuid4 per logical order."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    idempotency_key: UUID
    fixture_id: str = Field(min_length=1, max_length=128)
    market: str = Field(default="Match Odds", min_length=1, max_length=64)
    selection: str = Field(min_length=1, max_length=64)
    bookmaker_id: str = Field(min_length=1, max_length=64)
    odds: Annotated[Decimal, Field(gt=1, le=1000, allow_inf_nan=False)]
    stake_inr: Annotated[Decimal, Field(gt=0, le=Decimal("1000000000"), allow_inf_nan=False)]
    true_prob: Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)] | None = None
    signal_id: UUID | None = None  # set when the order comes from an Aryabhata signal
    signal_expires_at: datetime | None = None
    commence_time: datetime | None = None  # kick-off: the order resolver dead-letters 24h past it

    @field_validator("idempotency_key")
    @classmethod
    def _uuid4(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("idempotency_key must be a uuid4")
        return value

    @field_validator("odds")
    @classmethod
    def _odds_places(cls, value: Decimal) -> Decimal:
        return _max_places(value, 4, "odds")

    @field_validator("stake_inr")
    @classmethod
    def _stake_places(cls, value: Decimal) -> Decimal:
        return _max_places(value, 2, "stake_inr")


class ExecutionReceipt(BaseModel):
    status: Literal["EXECUTED", "UNKNOWN"]
    message: str
    ledger_id: UUID
    remote_bet_id: str | None
    bookmaker_id: str
    fixture_id: str
    selection: str
    stake_inr: WireDecimal
    odds: WireDecimal
    potential_pnl: WireDecimal
    available_balance: WireDecimal
    exposure_balance: WireDecimal
    execution_mode: Literal["paper", "live"]


class RiskSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    daily_drawdown_pct: WireDecimal
    max_market_exposure_pct: WireDecimal
    max_loss_streak: int
    velocity_max_cv_pct: WireDecimal
    max_slippage_pct: WireDecimal


class RiskSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_drawdown_pct: Annotated[Decimal, Field(ge=1, le=50, allow_inf_nan=False)] | None = None
    max_market_exposure_pct: Annotated[Decimal, Field(ge=1, le=50, allow_inf_nan=False)] | None = None
    max_loss_streak: Annotated[int, Field(ge=1, le=20)] | None = None
    velocity_max_cv_pct: Annotated[Decimal, Field(ge=Decimal("0.5"), le=20, allow_inf_nan=False)] | None = None
    max_slippage_pct: Annotated[Decimal, Field(ge=0, le=5, allow_inf_nan=False)] | None = None

    @field_validator("daily_drawdown_pct", "max_market_exposure_pct", "velocity_max_cv_pct", "max_slippage_pct")
    @classmethod
    def _two_places(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _max_places(value, 2, "percentage")


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    remote_bet_id: str | None
    stake_inr: WireDecimal
    odds: WireDecimal
    potential_pnl: WireDecimal
    realized_pnl: WireDecimal | None
    status: LedgerStatus
    reconcile_required: bool
    commence_time: datetime | None = None
    resolve_attempts: int = 0
    last_resolve_error: str | None = None
    created_at: datetime
    settled_at: datetime | None


class BankrollRead(BaseModel):
    opened: bool  # False: no execution yet; balances show what the account will open with
    currency: str
    available_balance: WireDecimal
    exposure_balance: WireDecimal
    equity: WireDecimal
    peak_balance: WireDecimal
    pnl_24h: WireDecimal
    drawdown_limit: WireDecimal | None
    loss_streak: int | None  # None: Redis unavailable
    kill_switch: bool
    execution_mode: Literal["paper", "live"]
    limits: RiskSettingsRead
    open_positions: list[PositionRead]


class MarketResultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fixture_id: str = Field(min_length=1, max_length=128)
    market: str = Field(default="Match Odds", min_length=1, max_length=64)
    winning_selection: str | None = Field(default=None, min_length=1, max_length=64)
    is_void: bool = False
    source: str = Field(default="manual", min_length=1, max_length=64)

    @model_validator(mode="after")
    def _consistent(self) -> MarketResultCreate:
        if self.is_void == (self.winning_selection is not None):
            raise ValueError("give a winning_selection, or is_void=true, not both")
        return self


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placed: bool  # what the bookmaker says: does this bet exist?
    remote_bet_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _ref_when_placed(self) -> ReconcileRequest:
        if self.placed and self.remote_bet_id is None:
            raise ValueError("a placed bet needs the bookmaker's reference")
        return self


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event: AuditEvent
    reason: str
    fixture_id: str | None
    selection: str | None
    stake_inr: WireDecimal | None
    odds: WireDecimal | None
    pnl_inr: WireDecimal | None
    created_at: datetime
