"""Group 86: the emergency fortress's state and its SHA-256 audit trail (KAUTILYA, CHANAKYA & SHIVAJI).
Developer Credit: Amit Ashok Kumar Patnaik

* ``emergency_kill_switch_states``: one row. The global lockout and its mode, the Parimatch and 1xBet safety locks,
  the breaker's thresholds, and the last measured reference bankroll, rolling loss and loss streak (INR primary;
  USD only at a fresh rate, else empty).
* ``emergency_kill_switch_audits``: append-only. Every kill, resume, automatic trip and bookmaker lock or unlock,
  each entry chained to the one before by SHA-256.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.expression import false

from app.models import Base, utc_now

EMERGENCY_STATE_SINGLETON_ID = 1


class LockoutMode(StrEnum):
    ARMED = "ARMED"
    MANUAL_KILL = "MANUAL_KILL"
    AUTO_DRAWDOWN_TRIPPED = "AUTO_DRAWDOWN_TRIPPED"
    STREAK_COOLDOWN = "STREAK_COOLDOWN"


class EmergencyEventType(StrEnum):
    GLOBAL_KILL_ENGAGED = "GLOBAL_KILL_ENGAGED"
    GLOBAL_KILL_RESUMED = "GLOBAL_KILL_RESUMED"
    AUTO_DRAWDOWN_TRIPPED = "AUTO_DRAWDOWN_TRIPPED"
    STREAK_LOCKOUT_TRIPPED = "STREAK_LOCKOUT_TRIPPED"
    BOOKMAKER_LOCKED = "BOOKMAKER_LOCKED"
    BOOKMAKER_UNLOCKED = "BOOKMAKER_UNLOCKED"


class EmergencyTargetScope(StrEnum):
    GLOBAL = "GLOBAL"
    PARIMATCH = "PARIMATCH"
    ONEXBET = "1XBET"


def _in(column: str, enum_cls: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum_cls)})"


class EmergencyKillSwitchStateModel(Base):
    """The fortress's single state row. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "emergency_kill_switch_states"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(_in("lockout_mode", LockoutMode), name="lockout_mode_known"),
        CheckConstraint("auto_kill_drawdown_pct >= 1 AND auto_kill_drawdown_pct <= 95", name="drawdown_threshold_bounded"),
        CheckConstraint("auto_kill_window_hours >= 1 AND auto_kill_window_hours <= 168", name="window_hours_bounded"),
        CheckConstraint("max_consecutive_losses >= 1 AND max_consecutive_losses <= 25", name="max_streak_bounded"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=EMERGENCY_STATE_SINGLETON_ID, autoincrement=False)
    global_kill_engaged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lockout_mode: Mapped[str] = mapped_column(String(32), default=LockoutMode.ARMED.value, server_default=LockoutMode.ARMED.value)
    parimatch_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    onexbet_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    auto_kill_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    auto_kill_window_hours: Mapped[int] = mapped_column(Integer)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer)
    reference_bankroll_inr: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    reference_bankroll_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    current_drawdown_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"), server_default="0.00")
    current_drawdown_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    current_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    current_consecutive_losses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_triggered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_trigger_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cooldown_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EmergencyKillSwitchAuditModel(Base):
    """One emergency event, chained to the previous by SHA-256. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "emergency_kill_switch_audits"
    __table_args__ = (
        CheckConstraint(_in("event_type", EmergencyEventType), name="event_type_known"),
        CheckConstraint(_in("target_scope", EmergencyTargetScope), name="target_scope_known"),
        Index("ix_emergency_audit_created_at", "created_at"),
        Index("ix_emergency_audit_scope_type", "target_scope", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(32))
    target_scope: Mapped[str] = mapped_column(String(24), default=EmergencyTargetScope.GLOBAL.value)
    triggered_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(512))
    drawdown_pct_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    protected_capital_inr: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    protected_capital_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
