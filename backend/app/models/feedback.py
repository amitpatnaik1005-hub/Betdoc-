"""The post-execution feedback loop (Group 73): how every model's prediction fared, and why a bet lost.

* ``model_prediction_feedback``: one row per settled leg per predictor. The predictors are the fortress's
  own models as the twin audit snapshotted them when the slip was vetted (``poisson``, ``dixon_coles``,
  ``xg``, ``elo``, ``market``: the names pillar 1 weights), Ashoka's ensemble as recorded on the leg
  (``ensemble``) and the sharp closing line, de-vigged (``closing_sharp``): the benchmark every model is
  measured against. Each row keeps the Brier score, the log loss and, when the audit kept the model's
  whole outcome distribution, the ranked probability score. A voided leg tells nothing about a model and
  gets no row. One row per (leg, predictor): re-running attribution never duplicates.
* ``settlement_root_cause_audits``: for a bet that lost (or half lost), the cause the classifier found,
  the evidence it used and the models' mean Brier score on its legs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
FEEDBACK_ODDS = Numeric(12, 4)

ENSEMBLE = "ensemble"  # Ashoka's ensemble probability, as recorded on the leg
CLOSING_SHARP = "closing_sharp"  # the sharp books' closing market, Shin de-vigged
REFERENCE_PREDICTORS = frozenset({ENSEMBLE, CLOSING_SHARP})  # measured, never weighted in pillar 1


class RootCauseTag(StrEnum):
    NONE = "NONE"  # the bet did not lose
    INPLAY_SHOCK_RED_CARD = "INPLAY_SHOCK_RED_CARD"  # the twin's watch saw the win probability collapse in play
    STEAM_ADVERSE_SELECTION = "STEAM_ADVERSE_SELECTION"  # the sharp line moved against the bet before kickoff
    WEATHER_ANOMALY = "WEATHER_ANOMALY"  # the weather evidence was past the fortress's limits
    MODEL_UNDERESTIMATION = "MODEL_UNDERESTIMATION"  # the models were confident (>= FEEDBACK_RCA_CONFIDENT_PROB) and wrong
    REFEREE_STRICTNESS_BIAS = "REFEREE_STRICTNESS_BIAS"  # a referee past the strictness line
    VARIANCE_BAD_LUCK = "VARIANCE_BAD_LUCK"  # none of the above: a fairly priced chance that did not come in


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class ModelPredictionFeedback(Base):
    __tablename__ = "model_prediction_feedback"
    __table_args__ = (
        CheckConstraint("predicted_prob >= 0 AND predicted_prob <= 1", name="prob_bounded"),
        CheckConstraint("actual_outcome >= 0 AND actual_outcome <= 1", name="outcome_bounded"),
        CheckConstraint("brier_score >= 0 AND brier_score <= 1", name="brier_bounded"),
        CheckConstraint("log_loss >= 0", name="log_loss_not_negative"),
        CheckConstraint("rps IS NULL OR (rps >= 0 AND rps <= 1)", name="rps_bounded"),
        UniqueConstraint("leg_id", "model_name"),  # uq_model_prediction_feedback_leg_id
        Index("ix_model_prediction_feedback_model_created", "model_name", "created_at"),
        Index("ix_model_prediction_feedback_fixture_market", "fixture_id", "market"),
        Index("ix_model_prediction_feedback_bet", "bet_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="CASCADE"))
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_legs.id", ondelete="CASCADE"))
    fixture_id: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(16))
    model_name: Mapped[str] = mapped_column(String(32))
    predicted_prob: Mapped[float] = mapped_column(Float)  # the expected settlement score (the win chance on a line that cannot push)
    actual_outcome: Mapped[float] = mapped_column(Float)  # 1 won, 0.75 half won, 0.25 half lost, 0 lost
    brier_score: Mapped[float] = mapped_column(Float)
    log_loss: Mapped[float] = mapped_column(Float)
    rps: Mapped[float | None] = mapped_column(Float, nullable=True)  # ranked probability score over the five ordered results
    closing_odds: Mapped[Decimal | None] = mapped_column(FEEDBACK_ODDS, nullable=True)
    clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # the leg's own CLV
    details: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SettlementRootCauseAudit(Base):
    __tablename__ = "settlement_root_cause_audits"
    __table_args__ = (
        CheckConstraint(f"root_cause_tag IN ({_values(RootCauseTag)})", name="tag_known"),
        Index("ix_settlement_root_cause_audits_bet", "bet_id", "created_at"),
        Index("ix_settlement_root_cause_audits_tag", "root_cause_tag", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="CASCADE"))
    root_cause_tag: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[str] = mapped_column(Text)
    model_error_delta: Mapped[float | None] = mapped_column(Float, nullable=True)  # the models' mean Brier score on the bet's legs
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
