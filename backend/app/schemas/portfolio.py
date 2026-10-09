"""Contracts for hedging, arbitrage execution and FX (``/api/v1/omni``, Group 64)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.cfo_vault import _max_places
from app.schemas.market import WireDecimal

Odds = Annotated[Decimal, Field(gt=1, le=1000, allow_inf_nan=False)]
Stake = Annotated[Decimal, Field(gt=0, le=Decimal("1000000000"), allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _uuid4(value: UUID) -> UUID:
    if value.version != 4:
        raise ValueError("idempotency_key must be a uuid4")
    return value


class ArbitrageLegRequest(_Strict):
    selection: str = Field(min_length=1, max_length=64)
    bookmaker_id: str = Field(min_length=1, max_length=64)
    odds: Odds  # the price the scanner showed: the leg is refused if the book now offers less

    @field_validator("odds")
    @classmethod
    def _places(cls, value: Decimal) -> Decimal:
        return _max_places(value, 4, "odds")


class ArbitrageExecuteRequest(_Strict):
    idempotency_key: UUID  # one per arbitrage; each leg's key derives from it
    fixture_id: str = Field(min_length=1, max_length=128)
    market: str = Field(default="Match Odds", min_length=1, max_length=64)
    total_stake_inr: Stake
    legs: list[ArbitrageLegRequest] = Field(min_length=2, max_length=4)

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("total_stake_inr")
    @classmethod
    def _paise(cls, value: Decimal) -> Decimal:
        return _max_places(value, 2, "total_stake_inr")

    @field_validator("legs")
    @classmethod
    def _one_per_outcome(cls, legs: list[ArbitrageLegRequest]) -> list[ArbitrageLegRequest]:
        if len({leg.selection for leg in legs}) != len(legs):
            raise ValueError("one leg per outcome")
        return legs


class HedgeLegExpectation(_Strict):
    selection: str = Field(min_length=1, max_length=64)
    bookmaker_id: str = Field(min_length=1, max_length=64)
    odds: Odds
    stake_inr: Stake


class HedgeExecuteRequest(_Strict):
    idempotency_key: UUID
    fixture_id: str = Field(min_length=1, max_length=128)
    market: str = Field(default="Match Odds", min_length=1, max_length=64)
    fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]  # 0 = free bet ... 1 = balanced
    anchor: str | None = Field(default=None, max_length=64)  # the outcome that keeps the upside
    expected_legs: list[HedgeLegExpectation] | None = Field(default=None, max_length=4)  # what the modal showed

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, value: UUID) -> UUID:
        return _uuid4(value)

    @field_validator("fraction")
    @classmethod
    def _fraction_places(cls, value: Decimal) -> Decimal:
        return _max_places(value, 4, "fraction")


LegStatus = Literal["FILLED", "PARTIAL", "UNCONFIRMED", "FAILED", "ABORTED", "SKIPPED"]


class LegReceipt(BaseModel):
    selection: str
    bookmaker_id: str
    odds: WireDecimal
    min_acceptable_odds: WireDecimal | None = None
    planned_stake_inr: WireDecimal  # sized before any leg fired
    requested_stake_inr: WireDecimal | None = None  # re-sized from the fills before it
    filled_stake_inr: WireDecimal | None = None
    matched_odds: WireDecimal | None = None
    status: LegStatus
    reason: str | None = None
    message: str | None = None
    ledger_id: UUID | None = None
    remote_bet_id: str | None = None


class MultiLegReceipt(BaseModel):
    group_id: UUID
    strategy: Literal["arbitrage", "hedge"]
    status: Literal["COMPLETE", "LEGGED", "ABORTED"]  # LEGGED: something is open and the plan is incomplete
    message: str
    legs: list[LegReceipt]
    outcome_profits: dict[str, WireDecimal]  # what the book makes on each outcome, from what actually filled
    worst_case: WireDecimal
    best_case: WireDecimal
    total_staked_inr: WireDecimal
    execution_mode: Literal["paper", "live"]


class FxRateUpdate(_Strict):
    currency: str = Field(min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")
    inr_per_unit: Annotated[Decimal, Field(gt=0, le=Decimal("100000"), allow_inf_nan=False)]
    source: str = Field(default="manual", min_length=1, max_length=64)
