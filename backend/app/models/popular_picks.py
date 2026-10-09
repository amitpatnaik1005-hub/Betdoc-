"""Oracle Popular Picks (FA-1) persistence models, overseen by ASHOKA."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, String, Uuid, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base

JSONPayload = JSON().with_variant(JSONB(), "postgresql")


class PickType(StrEnum):
    TRENDING = "TRENDING"
    AI_PREDICTED = "AI_PREDICTED"
    SHARP_MONEY = "SHARP_MONEY"


class TrendCategory(StrEnum):
    """Group 69: what Ashoka's trend scan made of a parlay."""

    SHARP_STEAM = "SHARP_STEAM"  # sharp money moving the lines, and positive EV
    PUBLIC_TRAP = "PUBLIC_TRAP"  # what the crowd backs, priced below fair by the vig
    AI_HYBRID = "AI_HYBRID"  # a popular anchor leg with a sharp value leg on another fixture


class ReviewDecision(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"


def _in_clause(column: str, enum_cls: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class PopularParlayModel(Base):
    __tablename__ = "popular_parlays"
    __table_args__ = (
        CheckConstraint(_in_clause("pick_type", PickType), name="ck_popular_parlays_pick_type"),
        CheckConstraint("total_odds >= 1.0", name="ck_popular_parlays_total_odds_min"),
        CheckConstraint(
            "historical_success_rate >= 0.0 AND historical_success_rate <= 1.0",
            name="ck_popular_parlays_success_rate_range",
        ),
        CheckConstraint("length(title) > 0", name="ck_popular_parlays_title_not_empty"),
        CheckConstraint(f"category IS NULL OR {_in_clause('category', TrendCategory)}", name="ck_popular_parlays_category"),
        CheckConstraint("public_share_pct IS NULL OR (public_share_pct >= 0 AND public_share_pct <= 100)", name="ck_popular_parlays_public_share"),
        Index("ix_popular_parlays_active_expires", "is_active", "expires_at"),
        Index("ix_popular_parlays_expires_at", "expires_at"),
        Index("ix_popular_parlays_pick_type", "pick_type"),
        Index("ix_popular_parlays_legs_gin", "legs", postgresql_using="gin").ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    pick_type: Mapped[str] = mapped_column(String(32), nullable=False)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSONPayload, nullable=False, default=list)
    total_odds: Mapped[float] = mapped_column(Float, nullable=False)
    historical_success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Group 69: the trend scan's verdict. public_share_pct is only ever a measured share (BetDoc's own
    # recorded bets, or a figure an administrator entered with its source), never an estimate.
    category: Mapped[str | None] = mapped_column(String(16), nullable=True)
    true_ev_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    public_share_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    public_share_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warning: Mapped[str | None] = mapped_column(String(300), nullable=True)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONPayload, nullable=True)


class ParlayReviewGateModel(Base):
    __tablename__ = "parlay_review_gates"
    __table_args__ = (
        CheckConstraint(_in_clause("decision", ReviewDecision), name="ck_parlay_review_gates_decision"),
        Index("ix_parlay_review_gates_parlay_created", "parlay_id", "created_at"),
        Index("ix_parlay_review_gates_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    parlay_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("popular_parlays.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
