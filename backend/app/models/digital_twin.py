"""The True Digital Betting Twin (Group 72): the 14-pillar audit of a slip, and the in-play watch on a placed bet.

* ``twin_vetting_audits``: one row per run of the 14-pillar fortress over a slip (its legs as
  ``fixture|market|selection`` ids). Every pillar's status (PASS / FAIL / UNVERIFIED / ADVISORY), its
  reason and its numbers are kept in ``pillars``; the slip as priced (book, odds, legs, stake) in
  ``slip``. ``is_vetted`` is true only when every enforced pillar passed. A pillar with no evidence is
  UNVERIFIED, never assumed to pass.
* ``twin_inplay_monitors``: one per placed bet the twin watches (Pathway B). The bet itself lives in
  Ashoka's ledger (``user_placed_bets``, Group 69): one P&L, settled once. The monitor keeps the slip's
  win probability at the start and now, the fair value's peak, and the pullout it recommended (the twin
  alerts; the cashout is taken at the bookmaker by the user and recorded on the bet).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
TWIN_MONEY = Numeric(18, 2)
TWIN_ODDS = Numeric(12, 4)


class PillarStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"  # the evidence the pillar needs is missing or stale: not a pass
    ADVISORY = "ADVISORY"  # failed or unverified, but configured (TWIN_ADVISORY_PILLARS) not to veto


class PulloutReason(StrEnum):
    TARGET_PROFIT_REACHED = "TARGET_PROFIT_REACHED"
    PROBABILITY_COLLAPSE = "PROBABILITY_COLLAPSE"
    HEDGE_LOCK = "HEDGE_LOCK"  # one leg left and a hedge locks more than the offer
    CASHOUT_ADVISED = "CASHOUT_ADVISED"  # the book's offer is at or above what holding is worth
    MANUAL_USER_REQUEST = "MANUAL_USER_REQUEST"


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class TwinVettingAudit(Base):
    __tablename__ = "twin_vetting_audits"
    __table_args__ = (
        CheckConstraint("pillars_passed >= 0 AND pillars_passed <= 14", name="pillars_bounded"),
        CheckConstraint("conviction_score >= 0 AND conviction_score <= 100", name="conviction_bounded"),
        CheckConstraint("stake_inr >= 0", name="stake_not_negative"),
        Index("ix_twin_vetting_audits_user_created", "user_id", "created_at"),
        Index("ix_twin_vetting_audits_vetted_created", "is_vetted", "created_at"),
        Index("ix_twin_vetting_audits_slip", "slip_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    slip_id: Mapped[str] = mapped_column(String(32))  # Ashoka's slip id for these legs and structure
    kind: Mapped[str] = mapped_column(String(16))  # SlipKind
    leg_ids: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    bookmaker: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the retail book the twin priced it at
    total_odds: Mapped[Decimal | None] = mapped_column(TWIN_ODDS, nullable=True)
    stake_inr: Mapped[Decimal] = mapped_column(TWIN_MONEY, default=Decimal(0))
    bankroll_inr: Mapped[Decimal | None] = mapped_column(TWIN_MONEY, nullable=True)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.0)  # of bankroll, after every cap and the drawdown scaling
    joint_ev: Mapped[float | None] = mapped_column(Float, nullable=True)
    joint_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    consensus_ev: Mapped[float | None] = mapped_column(Float, nullable=True)  # the weakest leg's weighted model EV
    sharp_edge: Mapped[float | None] = mapped_column(Float, nullable=True)  # the weakest leg's edge over the de-vigged sharp price
    pillars_passed: Mapped[int] = mapped_column(Integer, default=0)
    conviction_score: Mapped[float] = mapped_column(Float, default=0.0)  # pillars passed / 14, in percent
    is_vetted: Mapped[bool] = mapped_column(Boolean, default=False)
    pillars: Mapped[list[dict[str, Any]]] = mapped_column(JsonColumn, default=list)  # [{number, key, status, reason, metrics}]
    rejection_reasons: Mapped[list[str]] = mapped_column(JsonColumn, default=list)
    slip: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # legs, prices, search codes, quick copy
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TwinInPlayMonitor(Base):
    __tablename__ = "twin_inplay_monitors"
    __table_args__ = (
        CheckConstraint("initial_win_prob >= 0 AND initial_win_prob <= 1", name="initial_prob_bounded"),
        CheckConstraint("current_win_prob >= 0 AND current_win_prob <= 1", name="current_prob_bounded"),
        CheckConstraint(f"pullout_reason IS NULL OR pullout_reason IN ({_values(PulloutReason)})", name="pullout_reason_known"),
        Index("ix_twin_inplay_monitors_active", "is_active", "last_tick_at"),
        Index("ix_twin_inplay_monitors_user", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    vetting_audit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("twin_vetting_audits.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    target_profit_pct: Mapped[float] = mapped_column(Float)  # of stake: fair value (or the offer) this far up recommends banking it
    initial_win_prob: Mapped[float] = mapped_column(Float)
    current_win_prob: Mapped[float] = mapped_column(Float)
    fair_value_inr: Mapped[Decimal | None] = mapped_column(TWIN_MONEY, nullable=True)
    peak_fair_value_inr: Mapped[Decimal | None] = mapped_column(TWIN_MONEY, nullable=True)
    cashout_offer_inr: Mapped[Decimal | None] = mapped_column(TWIN_MONEY, nullable=True)  # the book's latest offer, as the user read it
    last_advice: Mapped[str | None] = mapped_column(String(16), nullable=True)  # HOLD / CASH_OUT / HEDGE_LEG
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ticks: Mapped[int] = mapped_column(Integer, default=0)
    pullout_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    pullout_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pullout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # the last tick's legs, reasons and hedge
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
