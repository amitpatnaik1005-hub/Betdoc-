"""Contracts for The Hive's trading bots (``/api/v1/hive/trading``, Group 65)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.hive_bots import BotExecutionMode, BotStatus
from app.schemas.market import WireDecimal

Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"), max_digits=14, decimal_places=2, allow_inf_nan=False)]
Keys = Annotated[list[Annotated[str, Field(min_length=3, max_length=96)]], Field(max_length=40)]


class ComponentRead(BaseModel):
    key: str
    kind: str
    name: str
    description: str
    category: str | None
    implementation: str | None
    live_capable: bool
    source: str


class RegistryRead(BaseModel):
    components: list[ComponentRead]
    counts: dict[str, int]
    live_counts: dict[str, int]
    expected: dict[str, int]


class _BotFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str | None = Field(default=None, max_length=2_000)
    math_models: Keys | None = None
    risk_models: Keys | None = None
    target_bet_types: Keys | None = None
    risk_params: dict[str, dict[str, float]] | None = None
    kelly_multiplier: Annotated[Decimal, Field(gt=0, le=1, max_digits=5, decimal_places=4)] | None = None
    max_stake_pct: Annotated[Decimal, Field(ge=Decimal("0.1"), le=25, max_digits=5, decimal_places=2)] | None = None
    min_edge_pct: Annotated[Decimal, Field(ge=Decimal("0.5"), le=25, max_digits=5, decimal_places=2)] | None = None
    min_quoting_books: Annotated[int, Field(ge=1, le=50)] | None = None
    min_market_liquidity: Money | None = None
    enable_order_slicing: bool | None = None
    slice_size_inr: Annotated[Decimal, Field(gt=0, le=Decimal("100000000"), max_digits=12, decimal_places=2)] | None = None
    max_bets_per_minute: Annotated[int, Field(ge=1, le=20)] | None = None
    drawdown_limit_pct: Annotated[Decimal, Field(ge=1, le=90, max_digits=5, decimal_places=2)] | None = None
    cooldown_seconds: Annotated[int, Field(ge=0, le=86_400)] | None = None

    @field_validator("risk_params")
    @classmethod
    def _params(cls, value: dict[str, dict[str, float]] | None) -> dict[str, dict[str, float]] | None:
        if value is not None and (len(value) > 30 or any(len(p) > 10 for p in value.values())):
            raise ValueError("too many risk parameters")
        return value


class BotCreate(_BotFields):
    name: str = Field(min_length=1, max_length=80)
    execution_mode: BotExecutionMode = BotExecutionMode.PAPER_TRADE
    math_models: Keys = Field(default_factory=lambda: ["math.consensus", "math.kelly_criterion"])
    risk_models: Keys = Field(default_factory=lambda: ["risk.drawdown", "risk.exposure"])
    target_bet_types: Keys = Field(default_factory=lambda: ["bet.match_winner_1x2", "bet.single"])


class BotUpdate(_BotFields):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    execution_mode: BotExecutionMode | None = None


class BotStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ACTIVE", "PAUSED"]


class CapitalChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allocated_capital: Money


class SubAccountRead(BaseModel):
    funding: str | None
    available: WireDecimal
    exposure: WireDecimal
    equity: WireDecimal
    realized_pnl: WireDecimal
    open_positions: int


class BotRead(BaseModel):
    id: UUID
    name: str
    description: str
    execution_mode: BotExecutionMode
    status: BotStatus
    math_models: list[str]
    risk_models: list[str]
    target_bet_types: list[str]
    risk_params: dict[str, Any]
    allocated_capital: WireDecimal
    kelly_multiplier: WireDecimal
    max_stake_pct: WireDecimal
    min_edge_pct: WireDecimal
    min_quoting_books: int
    min_market_liquidity: WireDecimal
    enable_order_slicing: bool
    slice_size_inr: WireDecimal
    max_bets_per_minute: int
    drawdown_limit_pct: WireDecimal
    cooldown_seconds: int
    suspended_reason: str | None
    suspended_at: datetime | None
    created_at: datetime
    updated_at: datetime
    account: SubAccountRead
    orders_last_minute: int
    pipeline_problems: list[str]


class HaltRead(BaseModel):
    halted: bool
    reason: str | None = None
    by: str | None = None
    at: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class HaltChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    halted: bool
    reason: str = Field(default="MASTER_KILL_SWITCH", min_length=1, max_length=64)


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_id: UUID | None
    event: str
    reason: str
    signal_id: UUID | None
    fixture_id: str | None
    market: str | None
    selection: str | None
    stake_inr: WireDecimal | None
    odds: WireDecimal | None
    conviction: WireDecimal | None
    detail: dict[str, Any]
    created_at: datetime


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_id: UUID
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    odds: WireDecimal
    total_stake_inr: WireDecimal
    slices: list[dict[str, Any]]
    status: str
    created_at: datetime
    updated_at: datetime


class TopologyLink(BaseModel):
    holder: str  # a bot id, or "main" for the owner's own account
    market_key: str
    fixture_id: str
    market: str
    selection: str
    stake_inr: WireDecimal
    source: Literal["ledger", "shadow"]
    strategy: str | None = None


class TopologyMarket(BaseModel):
    market_key: str
    fixture_id: str
    market: str
    home: str
    away: str
    commence_time: datetime | None
    holders: list[str]
    selections: list[str]
    collision: bool  # two or more holders on this market
    opposing: bool  # real positions (not shadow, not Group 64 legs) on different outcomes: a wash trade
    shadow_overlap: bool = False  # a shadow bot's hypothetical position sits on another outcome


class TopologyRead(BaseModel):
    bots: list[dict[str, Any]]
    markets: list[TopologyMarket]
    links: list[TopologyLink]
