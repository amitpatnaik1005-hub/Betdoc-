"""
Multi-Sport Configuration & League Registry Models (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

* ``sports_configurations``: one row per sport adapter: whether the desk trades it, its sport-calibrated
  fractional Kelly (FA-4), the liquidity floor a market must clear, The Odds API key it polls first, the
  markets and universal bet types it offers, and the model's tunable parameters (passed to the adapter as
  context, e.g. soccer's ``rho`` or cricket's dew settings).
* ``sports_leagues_registry``: the competitions under each sport, with the feed key that prices them and,
  for cricket, an optional DLS resource-table override.

Both are seeded by migration ``f94a1206c83e``. The older ``sport_configs`` table (Groups 54-58) stays: it holds
the three-sport SportsManager's JSON settings, which this engine does not read.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now


class SportConfiguration(Base):
    """
    Stores quantitative engine hyperparameters and liquidity limits per sport.
    Architected by Amit Ashok Kumar Patnaik.
    """

    __tablename__ = "sports_configurations"
    __table_args__ = (
        CheckConstraint("length(sport_key) > 0", name="sport_key_not_empty"),
        CheckConstraint("default_kelly_fraction > 0 AND default_kelly_fraction <= 1", name="kelly_bounded"),
        CheckConstraint("min_market_liquidity >= 0", name="liquidity_not_negative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sport_key: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25, nullable=False)
    min_market_liquidity: Mapped[float] = mapped_column(Float, default=1000.0, nullable=False)
    primary_odds_api_sport_key: Mapped[str] = mapped_column(String(64), nullable=False)
    supported_markets: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supported_bet_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    math_model_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class LeagueRegistry(Base):
    """
    Stores individual league/competition settings under each parent sport.
    Architected by Amit Ashok Kumar Patnaik.
    """

    __tablename__ = "sports_leagues_registry"
    __table_args__ = (
        CheckConstraint("tier >= 1 AND tier <= 5", name="tier_bounded"),
        CheckConstraint("length(league_key) > 0", name="league_key_not_empty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sport_key: Mapped[str] = mapped_column(ForeignKey("sports_configurations.sport_key", ondelete="CASCADE"), index=True, nullable=False)
    league_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(64), default="International", nullable=False)
    tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    odds_api_sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)  # the feed key that prices it; None: not on The Odds API
    dls_resource_table_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
