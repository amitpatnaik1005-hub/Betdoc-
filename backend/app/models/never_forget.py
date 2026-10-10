"""The Never-Forget shield and experience (Group 75): lessons from lost legs, the vetoes they cast, and XP.

* ``ashoka_mistake_memories``: one row per lost (or half-lost) leg of a bet placed from a twin audit. The
  situation is the one the fortress recorded when it vetted the slip (pillar 15's metrics): what was known
  before kickoff, never reconstructed afterwards. The root cause and its explanation are Group 73's
  post-mortem; the lesson is written from those numbers alone. One memory per leg, so a re-run sweep never
  memorises twice.
* ``never_forget_rules``: the guard each memory becomes. ACTIVE vetoes the same shape of bet in a situation
  at least ``NEVER_FORGET_SIMILARITY_THRESHOLD`` alike; EXPERIMENTAL only reports (a variance loss, or a
  lesson too broad to be a trap); ARCHIVED is retired by an administrator, with the reason kept.
* ``never_forget_prevention_audits``: one row per leg a rule vetoed (per user), with the stake the fortress
  had sized for the slip and, once the fixture's score is in, how the vetoed leg actually finished: the
  shield's record is measured, not assumed.
* ``user_xp_profiles`` / ``user_xp_audit_logs``: experience (FA-2). Every award is a log row keyed by what
  earned it (a slip, a bet, a memory, a veto, a streak day), unique per profile and kind: an award is never
  paid twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.domain.oracle.markets import LegResult
from app.models import Base, utc_now
from app.models.feedback import RootCauseTag

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
NF_MONEY = Numeric(18, 2)
NF_ODDS = Numeric(12, 4)


class RuleStatus(StrEnum):
    ACTIVE = "ACTIVE"  # vetoes
    EXPERIMENTAL = "EXPERIMENTAL"  # reports shadow matches only
    ARCHIVED = "ARCHIVED"  # retired by an administrator


class XPActionType(StrEnum):
    SLIP_VETTED = "SLIP_VETTED"
    BET_WON = "BET_WON"
    LOSS_PREVENTED = "LOSS_PREVENTED"  # a leg pillar 15 vetoed went on to lose
    MISTAKE_MEMORIZED = "MISTAKE_MEMORIZED"
    STREAK_BONUS = "STREAK_BONUS"


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class AshokaMistakeMemory(Base):
    __tablename__ = "ashoka_mistake_memories"
    __table_args__ = (
        CheckConstraint("placed_odds >= 1", name="odds_valid"),
        CheckConstraint(f"loss_root_cause IN ({_values(RootCauseTag)})", name="cause_known"),
        UniqueConstraint("leg_id"),  # uq_ashoka_mistake_memories_leg_id: one memory per lost leg
        Index("ix_ashoka_mistake_memories_created", "created_at"),
        Index("ix_ashoka_mistake_memories_fixture", "fixture_id"),
        Index("ix_ashoka_mistake_memories_bet", "bet_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="CASCADE"))
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_legs.id", ondelete="CASCADE"))
    vetting_audit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("twin_vetting_audits.id", ondelete="SET NULL"), nullable=True)
    fixture_id: Mapped[str] = mapped_column(String(128))
    home: Mapped[str] = mapped_column(String(128))
    away: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    league: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(16))
    placed_odds: Mapped[Decimal] = mapped_column(NF_ODDS)
    leg_result: Mapped[str] = mapped_column(String(12))  # LOST | HALF_LOST
    shape: Mapped[str] = mapped_column(String(48))  # the market kind and side of it (never_forget.bet_shape)
    loss_root_cause: Mapped[str] = mapped_column(String(32))  # RootCauseTag
    root_cause_explanation: Mapped[str] = mapped_column(Text)
    situational_fingerprint: Mapped[dict[str, float]] = mapped_column(JsonColumn, default=dict)  # the scaled features, as vetted
    situation_raw: Mapped[dict[str, float]] = mapped_column(JsonColumn, default=dict)  # the evidence as read (mm/h, km/h, hours, odds)
    extracted_lesson: Mapped[str] = mapped_column(Text)
    developer_credit: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NeverForgetRule(Base):
    __tablename__ = "never_forget_rules"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(RuleStatus)})", name="status_known"),
        CheckConstraint("times_triggered >= 0", name="triggers_not_negative"),
        UniqueConstraint("rule_code"),  # uq_never_forget_rules_rule_code
        UniqueConstraint("mistake_id"),  # uq_never_forget_rules_mistake_id: one guard per memory
        Index("ix_never_forget_rules_status", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mistake_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ashoka_mistake_memories.id", ondelete="CASCADE"))
    rule_code: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)  # the lesson
    shape: Mapped[str] = mapped_column(String(48))
    rule_conditions: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # the vector, and the threshold and weights it was memorised under
    action: Mapped[str] = mapped_column(String(16), default="VETO")
    status: Mapped[str] = mapped_column(String(16), default=RuleStatus.ACTIVE.value)
    status_reason: Mapped[str] = mapped_column(Text)
    status_changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # the administrator; NULL: the engine
    specificity: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # {comparable, matched, rate} over the recent legs when memorised
    times_triggered: Mapped[int] = mapped_column(Integer, default=0)  # vetoes cast (one per leg per user)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class NeverForgetPreventionAudit(Base):
    __tablename__ = "never_forget_prevention_audits"
    __table_args__ = (
        CheckConstraint("similarity_score >= 0 AND similarity_score <= 1", name="similarity_bounded"),
        CheckConstraint("stake_withheld_inr IS NULL OR stake_withheld_inr >= 0", name="stake_not_negative"),
        CheckConstraint(f"outcome IS NULL OR outcome IN ({_values(LegResult)})", name="outcome_known"),
        UniqueConstraint("dedupe_key"),  # uq_never_forget_prevention_audits_dedupe_key: one veto per rule, leg and user
        Index("ix_never_forget_prevention_audits_rule_created", "rule_id", "created_at"),
        Index("ix_never_forget_prevention_audits_user_created", "user_id", "created_at"),
        Index("ix_never_forget_prevention_audits_unresolved", "outcome", "fixture_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("never_forget_rules.id", ondelete="CASCADE"))
    mistake_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ashoka_mistake_memories.id", ondelete="CASCADE"))
    vetting_audit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("twin_vetting_audits.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(240))
    leg_ref: Mapped[str] = mapped_column(String(220))  # fixture|market|selection
    fixture_id: Mapped[str] = mapped_column(String(128))
    home: Mapped[str] = mapped_column(String(128))
    away: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(16))
    odds: Mapped[Decimal] = mapped_column(NF_ODDS)
    similarity_score: Mapped[float] = mapped_column(Float)
    stake_withheld_inr: Mapped[Decimal | None] = mapped_column(NF_MONEY, nullable=True)  # the slip's sized stake; NULL: nothing was sized
    veto_reason: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)  # how the vetoed leg finished (LegResult); NULL: not yet known
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UserXPProfile(Base):
    __tablename__ = "user_xp_profiles"
    __table_args__ = (
        CheckConstraint("total_xp >= 0", name="xp_not_negative"),
        CheckConstraint("level >= 1", name="level_positive"),
        UniqueConstraint("user_id"),  # uq_user_xp_profiles_user_id
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    total_xp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 1-based position in XP_TIERS
    rank_title: Mapped[str] = mapped_column(String(32))
    slips_vetted_count: Mapped[int] = mapped_column(Integer, default=0)
    bets_won_count: Mapped[int] = mapped_column(Integer, default=0)
    losses_prevented_count: Mapped[int] = mapped_column(Integer, default=0)
    mistakes_learned_count: Mapped[int] = mapped_column(Integer, default=0)
    streak_bonuses_count: Mapped[int] = mapped_column(Integer, default=0)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class XPAuditLog(Base):
    __tablename__ = "user_xp_audit_logs"
    __table_args__ = (
        CheckConstraint("xp_amount > 0", name="amount_positive"),
        CheckConstraint(f"action_type IN ({_values(XPActionType)})", name="action_known"),
        UniqueConstraint("profile_id", "action_type", "source_ref"),  # uq_user_xp_audit_logs_profile_id: never paid twice
        Index("ix_user_xp_audit_logs_profile_created", "profile_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_xp_profiles.id", ondelete="CASCADE"))
    action_type: Mapped[str] = mapped_column(String(32))
    xp_amount: Mapped[int] = mapped_column(Integer)
    source_ref: Mapped[str] = mapped_column(String(160))  # what earned it: slip:<id>, bet:<id>, memory:<id>, veto:<id>, streak:<date>
    description: Mapped[str] = mapped_column(String(255))
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
