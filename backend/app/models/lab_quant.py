"""The Lab (Group 66): historical market data and quantitative backtests.

The historical tables are an append-only change log of a market: each ``LabOddsTick`` is a book's
price for one selection as of ``created_at`` (a price persists until the book's next tick for that
selection, or a suspension). ``LabFixtureResult.created_at`` is when the result became known, and
``LabFxRate.created_at`` when a fixing was published. ``created_at`` is the point-in-time key the
backtester's anti-leakage lock enforces: a simulated bot at time T can read no row stamped after T.

``source`` says where a row came from. The seeder (``app.db.seed_historical_ticks``) writes
``synthetic`` rows: a generated market, realistic in structure, never presented as real prices.
``LabBacktestRun`` is one backtest request, its progress and its full result.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.models import Base, utc_now

ODDS = Numeric(10, 4)
MONEY = Numeric(18, 2)
JsonColumn = JSON().with_variant(JSONB(), "postgresql")
TickId = BigInteger().with_variant(Integer(), "sqlite")  # SQLite autoincrements INTEGER PRIMARY KEY only


class ResultStatus(StrEnum):
    FINISHED = "FINISHED"
    POSTPONED = "POSTPONED"  # every bet on the fixture is void: stakes are refunded


class BacktestStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class LabFixture(Base):
    __tablename__ = "lab_hist_fixtures"
    __table_args__ = (Index("ix_lab_hist_fixtures_commence_time", "commence_time"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64))
    sport_key: Mapped[str] = mapped_column(String(64))
    league: Mapped[str] = mapped_column(String(64))
    home_team: Mapped[str] = mapped_column(String(128))
    away_team: Mapped[str] = mapped_column(String(128))
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the fixture was listed


class LabOddsTick(Base):
    __tablename__ = "lab_hist_ticks"
    __table_args__ = (
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint("liquidity IS NULL OR liquidity >= 0", name="liquidity_non_negative"),
        Index("ix_lab_hist_ticks_created_at", "created_at"),
        Index("ix_lab_hist_ticks_market_time", "fixture_id", "market", "created_at"),
    )

    id: Mapped[int] = mapped_column(TickId, primary_key=True, autoincrement=True)
    fixture_id: Mapped[str] = mapped_column(ForeignKey("lab_hist_fixtures.id", ondelete="CASCADE"))
    market: Mapped[str] = mapped_column(String(32))
    selection: Mapped[str] = mapped_column(String(16))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    odds: Mapped[Decimal] = mapped_column(ODDS)
    liquidity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)  # available at this price, venue currency (None: not reported)
    currency: Mapped[str] = mapped_column(String(3))
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LabFixtureResult(Base):
    __tablename__ = "lab_hist_results"
    __table_args__ = (
        CheckConstraint("status IN ('FINISHED', 'POSTPONED')", name="status_known"),
        CheckConstraint(
            "(status = 'FINISHED' AND home_goals >= 0 AND away_goals >= 0) OR (status = 'POSTPONED' AND home_goals IS NULL AND away_goals IS NULL)",
            name="score_consistent",
        ),
        Index("ix_lab_hist_results_created_at", "created_at"),
    )

    fixture_id: Mapped[str] = mapped_column(ForeignKey("lab_hist_fixtures.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    home_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the result (or postponement) became known


class LabFxRate(Base):
    __tablename__ = "lab_hist_fx_rates"
    __table_args__ = (
        UniqueConstraint("currency", "created_at", name="uq_lab_hist_fx_rates_currency_time"),
        CheckConstraint("inr_per_unit > 0", name="rate_positive"),
        Index("ix_lab_hist_fx_rates_lookup", "currency", "created_at"),
    )

    id: Mapped[int] = mapped_column(TickId, primary_key=True, autoincrement=True)
    currency: Mapped[str] = mapped_column(String(3))
    inr_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    source: Mapped[str] = mapped_column(String(32), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the fixing's publication time


class LabBacktestRun(Base):
    __tablename__ = "lab_backtest_runs"
    __table_args__ = (
        CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')", name="status_known"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="progress_range"),
        Index("ix_lab_backtest_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default=BacktestStatus.QUEUED)
    params: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage: Mapped[str] = mapped_column(String(120), default="queued")
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
