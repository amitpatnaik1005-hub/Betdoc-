"""The Lab (Group 66): backtest requests and their records."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Dec = Annotated[Decimal, Field(allow_inf_nan=False)]


class StrategySpec(BaseModel):
    """An ad-hoc pipeline (no saved bot): the same fields a Hive bot has."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    math_models: list[str] = Field(min_length=1, max_length=20)
    risk_models: list[str] = Field(default_factory=list, max_length=20)
    target_bet_types: list[str] = Field(min_length=1, max_length=20)
    kelly_multiplier: Annotated[Dec, Field(gt=0, le=1)] = Decimal("0.25")
    max_stake_pct: Annotated[Dec, Field(gt=0, le=100)] = Decimal("5")
    min_edge_pct: Annotated[Dec, Field(ge=0, le=25)] = Decimal("1")
    min_quoting_books: int = Field(default=2, ge=1, le=20)
    min_market_liquidity: Annotated[Dec, Field(ge=0)] = Decimal("0")
    enable_order_slicing: bool = False
    slice_size_inr: Annotated[Dec, Field(gt=0)] = Decimal("10000")
    max_bets_per_minute: int = Field(default=3, ge=1, le=100)
    drawdown_limit_pct: Annotated[Dec, Field(gt=0, le=100)] = Decimal("20")
    cooldown_seconds: int = Field(default=300, ge=0, le=86_400)
    risk_params: dict[str, dict[str, float]] = Field(default_factory=dict)
    capital_inr: Annotated[Dec, Field(gt=0, le=Decimal("1e12"))] = Decimal("100000")


class BacktestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Backtest", min_length=1, max_length=120)
    bot_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    strategies: list[StrategySpec] = Field(default_factory=list, max_length=8)
    start: datetime | None = None  # None: the dataset's first tick
    end: datetime | None = None  # None: its last
    train_ratio: float = Field(default=0.75, ge=0.5, le=0.9)
    oos_enabled: bool = True
    walk_forward_folds: int = Field(default=1, ge=1, le=10)  # Group 77: > 1 adds rolling folds (each tuned in-sample, judged on the window after it)
    sweep_enabled: bool = True
    kelly_min: Annotated[Dec, Field(gt=0, le=1)] = Decimal("0.1")
    kelly_max: Annotated[Dec, Field(gt=0, le=1)] = Decimal("0.5")
    sweep_steps: int = Field(default=10, ge=2, le=20)
    latency_min_ms: int = Field(default=1500, ge=0, le=20_000)
    latency_max_ms: int = Field(default=3000, ge=0, le=20_000)
    slippage_pct: Annotated[Dec, Field(ge=0, le=5)] = Decimal("0.25")
    max_slippage_pct: Annotated[Dec, Field(ge=0, le=5)] = Decimal("0.5")
    void_rate_pct: Annotated[Dec, Field(ge=0, le=20)] = Decimal("2")
    impact_threshold_pct: Annotated[Dec, Field(ge=0, lt=100)] = Decimal("5")
    impact_coefficient: Annotated[Dec, Field(ge=0, le=50)] = Decimal("2")
    impact_model: Literal["quadratic", "sqrt"] = "quadratic"  # Group 77: sqrt is 1 - k sqrt(stake / liquidity) on the net price
    bets_per_second: float = Field(default=2.0, gt=0, le=50)
    burst: int = Field(default=2, ge=1, le=50)
    max_queue_seconds: float = Field(default=3.0, ge=0, le=10)
    unreported_liquidity_inr: Annotated[Dec, Field(gt=0)] = Decimal("25000")
    capital_inr: Annotated[Dec, Field(gt=0, le=Decimal("1e12"))] | None = None  # every bot's starting bankroll (None: its own allocation)
    monte_carlo_iterations: int = Field(default=1000, ge=100, le=10_000)
    ruin_floor_pct: float = Field(default=0.0, ge=0, le=90)
    risk_free_rate: float | None = Field(default=None, ge=0, le=0.5)  # a year, in Sharpe and Sortino; None: LAB_RISK_FREE_RATE
    resume_after_hours: float = Field(default=6.0, ge=0, le=720)  # a breaker's or a halt's simulated human review; 0: never within the run
    seed: int = Field(default=66, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _coherent(self) -> BacktestParams:
        if not self.bot_ids and not self.strategies:
            raise ValueError("pick at least one bot or strategy")
        if len(set(self.bot_ids)) != len(self.bot_ids):
            raise ValueError("each bot once")
        if len(self.bot_ids) + len(self.strategies) > 8:
            raise ValueError("at most 8 bots and strategies in one backtest")
        if self.kelly_min > self.kelly_max:
            raise ValueError("kelly_min must not exceed kelly_max")
        if self.latency_min_ms > self.latency_max_ms:
            raise ValueError("latency_min_ms must not exceed latency_max_ms")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class BacktestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    progress: float
    stage: str
    params: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    summary: dict[str, Any] | None = None


class BacktestDetail(BacktestRead):
    result: dict[str, Any] | None


class DatasetSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int = Field(default=10_000, ge=500, le=200_000)
    seed: int = Field(default=66, ge=0, le=2**31 - 1)
    start: datetime | None = None
    days: int = Field(default=365, ge=30, le=1460)
    replace: bool = False
