"""The Hive (Group 65): autonomous trading bots.

* ``TradingBot``: one strategy. Its pipeline is three arrays of component keys from the model
  registry (``core_smallcase_registry``, seeded by ``app.db.seed_110_models``): math models,
  risk models, target bet types. Its money lives in its own CFO sub-account (``bot_id`` on
  ``cfo_bankroll_accounts``): a bot can lose at most what it was allocated.
* ``HiveBotEvent``: every decision a bot made about a signal, with its exact reason (fired,
  merged into a stronger bot's order, blocked as a wash trade, suspended by a breaker...).
* ``HiveOrderPlan``: an order sliced over time (TWAP): its slices, their schedule and outcomes.
* ``HiveShadowPosition``: a SHADOW_MODE bot's hypothetical bet, graded for its paper P&L.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
ODDS = Numeric(10, 4)
PROBABILITY = Numeric(12, 10)
PERCENT = Numeric(5, 2)
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class BotExecutionMode(StrEnum):
    PAPER_TRADE = "PAPER_TRADE"  # simulated fills against a virtually funded sub-account
    SHADOW_MODE = "SHADOW_MODE"  # no orders at all: hypothetical positions and their P&L
    LIVE_EXECUTION = "LIVE_EXECUTION"  # real orders through the Omni-Sniper, funded from the main bankroll


class BotStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"  # by its owner
    SUSPENDED = "SUSPENDED"  # by a circuit breaker; only its owner resumes it
    ARCHIVED = "ARCHIVED"


class HiveEventKind(StrEnum):
    FIRED = "FIRED"
    SHADOW_FILLED = "SHADOW_FILLED"
    SLICED = "SLICED"
    SLICE_FIRED = "SLICE_FIRED"
    SLICE_CANCELLED = "SLICE_CANCELLED"
    MERGED = "MERGED"  # matched, but a stronger bot of the same owner took the one order
    BLOCKED = "BLOCKED"  # wash trade, halt, liquidity, guard, execution refusal...
    SKIPPED = "SKIPPED"  # the pipeline passed on the signal (no edge after its models, risk veto)
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    ALLOCATED = "ALLOCATED"


class PlanStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class ShadowStatus(StrEnum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(enum_cls, name=name, native_enum=False, create_constraint=True, length=24, validate_strings=True)


class TradingBot(Base):
    __tablename__ = "hive_trading_bots"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_hive_trading_bots_user_name"),
        CheckConstraint("allocated_capital >= 0", name="allocation_non_negative"),
        CheckConstraint("kelly_multiplier > 0 AND kelly_multiplier <= 1", name="kelly_range"),
        CheckConstraint("max_stake_pct >= 0.1 AND max_stake_pct <= 25", name="stake_pct_range"),
        CheckConstraint("min_edge_pct >= 0.5 AND min_edge_pct <= 25", name="edge_range"),
        CheckConstraint("min_quoting_books >= 1 AND min_quoting_books <= 50", name="books_range"),
        CheckConstraint("min_market_liquidity >= 0", name="liquidity_non_negative"),
        CheckConstraint("slice_size_inr > 0", name="slice_positive"),
        CheckConstraint("max_bets_per_minute >= 1 AND max_bets_per_minute <= 20", name="velocity_range"),
        CheckConstraint("drawdown_limit_pct >= 1 AND drawdown_limit_pct <= 90", name="drawdown_range"),
        CheckConstraint("cooldown_seconds >= 0", name="cooldown_non_negative"),
        Index("ix_hive_trading_bots_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    execution_mode: Mapped[BotExecutionMode] = mapped_column(_enum(BotExecutionMode, "hive_bot_execution_mode"), default=BotExecutionMode.PAPER_TRADE)
    status: Mapped[BotStatus] = mapped_column(_enum(BotStatus, "hive_bot_status"), default=BotStatus.PAUSED)
    # The pipeline: registry component keys, in the order they are chained
    math_models: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    risk_models: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    target_bet_types: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    risk_params: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # per-risk-model limits
    # Capital: what its sub-account was funded with (net of releases)
    allocated_capital: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    kelly_multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.25"))
    max_stake_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("5.00"))  # of the sub-bankroll's equity
    min_edge_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("1.00"))
    # Liquidity: independent books quoting the line, and traded volume where the source reports it
    min_quoting_books: Mapped[int] = mapped_column(Integer, default=3)
    min_market_liquidity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # Order slicing (TWAP): stakes above slice_size_inr go out as several orders over time
    enable_order_slicing: Mapped[bool] = mapped_column(Boolean, default=False)
    slice_size_inr: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("10000.00"))
    # Circuit breakers
    max_bets_per_minute: Mapped[int] = mapped_column(Integer, default=3)
    drawdown_limit_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("20.00"))
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=900)  # one entry per selection per window
    suspended_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pipeline_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class HiveBotEvent(Base):
    __tablename__ = "hive_bot_events"
    __table_args__ = (
        Index("ix_hive_bot_events_user_created", "user_id", "created_at"),
        Index("ix_hive_bot_events_bot_created", "bot_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="CASCADE"), nullable=True)
    event: Mapped[HiveEventKind] = mapped_column(_enum(HiveEventKind, "hive_event_kind"))
    reason: Mapped[str] = mapped_column(String(64))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fixture_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stake_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)
    conviction: Mapped[Decimal | None] = mapped_column(PROBABILITY, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


class HiveOrderPlan(Base):
    __tablename__ = "hive_order_plans"
    __table_args__ = (
        CheckConstraint("total_stake_inr > 0", name="total_positive"),
        Index("ix_hive_order_plans_bot_status", "bot_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="CASCADE"))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    odds: Mapped[Decimal] = mapped_column(ODDS)  # the price the plan was made at; each slice re-checks the live edge
    true_prob: Mapped[Decimal] = mapped_column(PROBABILITY)
    commence_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_stake_inr: Mapped[Decimal] = mapped_column(MONEY)
    # [{"index", "stake_inr", "countdown_s", "status", "reason", "ledger_id", "fired_at"}]
    slices: Mapped[list[dict[str, Any]]] = mapped_column(JsonColumn, default=list)
    status: Mapped[PlanStatus] = mapped_column(_enum(PlanStatus, "hive_plan_status"), default=PlanStatus.SCHEDULED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class HiveShadowPosition(Base):
    __tablename__ = "hive_shadow_positions"
    __table_args__ = (
        CheckConstraint("stake_inr > 0", name="stake_positive"),
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint("commission_rate >= 0 AND commission_rate < 0.5", name="commission_rate_range"),
        Index("ix_hive_shadow_positions_bot_status", "bot_id", "status"),
        Index("ix_hive_shadow_positions_fixture_status", "fixture_id", "market", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="CASCADE"))
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    odds: Mapped[Decimal] = mapped_column(ODDS)
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal(0), server_default="0")  # graded net of it
    stake_inr: Mapped[Decimal] = mapped_column(MONEY)
    true_prob: Mapped[Decimal | None] = mapped_column(PROBABILITY, nullable=True)
    status: Mapped[ShadowStatus] = mapped_column(_enum(ShadowStatus, "hive_shadow_status"), default=ShadowStatus.OPEN)
    pnl_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
