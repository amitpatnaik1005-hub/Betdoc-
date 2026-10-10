"""KUMBHA's capital growth record (Group 76): forecasts, the regime advisories, and venue rebalancing plans.

* ``cfo_growth_simulations``: one row per Monte Carlo forecast. The history it bootstrapped (the user's own
  settled bets), the strategy, the seed (the run reproduces), the percentile curve and the risk figures.
* ``cfo_advisory_logs``: KUMBHA's advisories per user. A row is written when the drawdown regime changes
  (VARIANCE_THROTTLE, CAPITAL_PRESERVATION_HALT), when a steady regime is re-confirmed (OPTIMAL_GROWTH_TRAJECTORY,
  at most every ``CFO_ADVISORY_CONFIRM_HOURS``) and when a rebalance plan is recorded (VENUE_REBALANCE). An
  unacknowledged CAPITAL_PRESERVATION_HALT is the latch: sizing stays at zero until an administrator signs it
  off, whatever the drawdown does meanwhile.
* ``cfo_rebalance_recommendations``: the transfers of a recorded rebalance plan (one ``plan_id``), from a venue
  holding more than its EV share to one holding less. Nothing moves money: the operator withdraws and deposits
  at the bookmakers, then marks the transfer EXECUTED (or dismisses it). A new plan supersedes the pending ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
GROWTH_MONEY = Numeric(18, 2)


class AdvisorySeverity(StrEnum):
    INFO = "INFO"
    RECOMMENDATION = "RECOMMENDATION"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class InsightCode(StrEnum):
    OPTIMAL_GROWTH_TRAJECTORY = "OPTIMAL_GROWTH_TRAJECTORY"  # the damper is at 1
    VARIANCE_THROTTLE = "VARIANCE_THROTTLE"  # the cautious or defensive regime cuts sizing
    CAPITAL_PRESERVATION_HALT = "CAPITAL_PRESERVATION_HALT"  # the halt line: zero sizing, latched until signed off
    VENUE_REBALANCE = "VENUE_REBALANCE"  # a rebalance plan with transfers was recorded


REGIME_CODES = frozenset({InsightCode.OPTIMAL_GROWTH_TRAJECTORY.value, InsightCode.VARIANCE_THROTTLE.value, InsightCode.CAPITAL_PRESERVATION_HALT.value})


class RebalanceStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"  # the operator moved the money at the bookmakers
    DISMISSED = "DISMISSED"
    SUPERSEDED = "SUPERSEDED"  # a newer plan replaced it while it was still pending


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class CFOGrowthSimulation(Base):
    __tablename__ = "cfo_growth_simulations"
    __table_args__ = (
        CheckConstraint("horizon_days > 0 AND simulated_paths > 0 AND trades > 0", name="sizes_positive"),
        CheckConstraint("prob_circuit_breaker >= 0 AND prob_circuit_breaker <= 1 AND prob_ruin >= 0 AND prob_ruin <= 1", name="probabilities_bounded"),
        Index("ix_cfo_growth_simulations_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    strategy: Mapped[str] = mapped_column(String(32))
    horizon_days: Mapped[int] = mapped_column(Integer)
    simulated_paths: Mapped[int] = mapped_column(Integer)
    trades: Mapped[int] = mapped_column(Integer)  # bets per path over the horizon, at the user's own rate
    seed: Mapped[int] = mapped_column(BigInteger)
    starting_bankroll_inr: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    median_ending_bankroll_inr: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    mean_ending_bankroll_inr: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    expected_cagr_pct: Mapped[float] = mapped_column(Float)  # from the median path
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)  # annualised, daily returns
    sortino_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    prob_circuit_breaker: Mapped[float] = mapped_column(Float)  # P(the drawdown reaches the halt line)
    prob_ruin: Mapped[float] = mapped_column(Float)  # P(the bankroll falls under CFO_RUIN_LEVEL of its start)
    median_max_drawdown: Mapped[float] = mapped_column(Float)
    p95_max_drawdown: Mapped[float] = mapped_column(Float)
    percentile_curves: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # {"points": [{day, p1 .. p99}]}
    parameters: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # the history, the policy, the skill used
    developer_credit: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CFOAdvisoryLog(Base):
    __tablename__ = "cfo_advisory_logs"
    __table_args__ = (
        CheckConstraint(f"severity IN ({_values(AdvisorySeverity)})", name="severity_known"),
        CheckConstraint(f"insight_code IN ({_values(InsightCode)})", name="insight_known"),
        Index("ix_cfo_advisory_logs_user_created", "user_id", "created_at"),
        Index("ix_cfo_advisory_logs_user_code_ack", "user_id", "insight_code", "is_acknowledged"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    insight_code: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    regime: Mapped[str | None] = mapped_column(String(40), nullable=True)  # the drawdown regime it reports
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    action_directive: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledgement_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # a halt's sign-off reason
    developer_credit: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CFORebalanceRecommendation(Base):
    __tablename__ = "cfo_rebalance_recommendations"
    __table_args__ = (
        CheckConstraint("amount_inr > 0", name="amount_positive"),
        CheckConstraint("source_venue <> destination_venue", name="distinct_venues"),
        CheckConstraint(f"status IN ({_values(RebalanceStatus)})", name="status_known"),
        Index("ix_cfo_rebalance_recommendations_plan", "plan_id"),
        Index("ix_cfo_rebalance_recommendations_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source_venue: Mapped[str] = mapped_column(String(32))  # canonical bookmaker id
    destination_venue: Mapped[str] = mapped_column(String(32))
    amount_inr: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=RebalanceStatus.PENDING.value)
    source_balance_before: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    dest_balance_before: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    source_target: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    dest_target: Mapped[Decimal] = mapped_column(GROWTH_MONEY)
    status_changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    developer_credit: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
