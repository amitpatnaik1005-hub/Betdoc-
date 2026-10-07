"""Human Touch Mode (FA-8): confidence adjustment config, match narratives, override audit log."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import false

from app.models import Base

# Storage-level bounds: the single source of truth for DB constraints AND Pydantic schemas.
MAX_ADJUSTMENT_LIMIT_CEILING_PCT = 25.0
MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT = 5.0
WEIGHT_FLOOR = 0.0
WEIGHT_CEILING = 1.0
PROBABILITY_FLOOR = 0.0
PROBABILITY_CEILING = 1.0
VALID_OUTCOMES: tuple[float, ...] = (0.0, 0.5, 1.0)  # loss, draw, win
GLOBAL_CONFIG_KEY = "GLOBAL"
NARRATIVE_METRIC_COLUMNS: tuple[str, ...] = (
    "derby_intensity",
    "home_crowd_hostility",
    "new_manager_bounce",
    "fatigue_index",
    "injury_impact",
)

_OUTCOME_SQL = ", ".join(repr(value) for value in VALID_OUTCOMES)


class HumanTouchConfigModel(Base):
    __tablename__ = "human_touch_config"
    __table_args__ = (
        CheckConstraint(
            f"max_adjustment_limit_pct >= 0.0 AND max_adjustment_limit_pct <= {MAX_ADJUSTMENT_LIMIT_CEILING_PCT}",
            name="ck_human_touch_config_max_adjustment_range",
        ),
        CheckConstraint(
            f"sentiment_weight >= {WEIGHT_FLOOR} AND sentiment_weight <= {WEIGHT_CEILING}",
            name="ck_human_touch_config_sentiment_weight_range",
        ),
        CheckConstraint(
            f"momentum_weight >= {WEIGHT_FLOOR} AND momentum_weight <= {WEIGHT_CEILING}",
            name="ck_human_touch_config_momentum_weight_range",
        ),
        CheckConstraint(
            "min_adjustment_threshold_pct >= 0.0 AND "
            f"min_adjustment_threshold_pct <= {MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT}",
            name="ck_human_touch_config_min_threshold_range",
        ),
        CheckConstraint(
            "min_adjustment_threshold_pct <= max_adjustment_limit_pct",
            name="ck_human_touch_config_threshold_within_limit",
        ),
        CheckConstraint(f"config_key = '{GLOBAL_CONFIG_KEY}'", name="ck_human_touch_config_singleton"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_key: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, default=GLOBAL_CONFIG_KEY)
    is_blended_mode_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Neutral seed: every numeric default is the identity value (0 = no adjustment) until configured.
    max_adjustment_limit_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    sentiment_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    momentum_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    min_adjustment_threshold_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MatchNarrativeModel(Base):
    __tablename__ = "match_narratives"
    __table_args__ = (
        *(
            CheckConstraint(f"{column} >= 0.0 AND {column} <= 1.0", name=f"ck_match_narratives_{column}_range")
            for column in NARRATIVE_METRIC_COLUMNS
        ),
        CheckConstraint("length(match_id) > 0", name="ck_match_narratives_match_id_not_empty"),
        CheckConstraint("length(team_name) > 0", name="ck_match_narratives_team_not_empty"),
        UniqueConstraint("match_id", "team_name", name="uq_match_narratives_match_team"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    team_name: Mapped[str] = mapped_column(String(128), nullable=False)
    derby_intensity: Mapped[float] = mapped_column(Float, nullable=False)
    home_crowd_hostility: Mapped[float] = mapped_column(Float, nullable=False)
    new_manager_bounce: Mapped[float] = mapped_column(Float, nullable=False)
    fatigue_index: Mapped[float] = mapped_column(Float, nullable=False)
    injury_impact: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class HumanOverrideLogModel(Base):
    __tablename__ = "human_override_logs"
    __table_args__ = (
        CheckConstraint("pure_math_prob >= 0.0 AND pure_math_prob <= 1.0", name="ck_human_override_pure_prob_range"),
        CheckConstraint("blended_prob >= 0.0 AND blended_prob <= 1.0", name="ck_human_override_blended_prob_range"),
        CheckConstraint(
            f"actual_outcome IS NULL OR actual_outcome IN ({_OUTCOME_SQL})", name="ck_human_override_outcome_values"
        ),
        CheckConstraint(
            "math_brier_score IS NULL OR (math_brier_score >= 0.0 AND math_brier_score <= 1.0)",
            name="ck_human_override_math_brier_range",
        ),
        CheckConstraint(
            "blended_brier_score IS NULL OR (blended_brier_score >= 0.0 AND blended_brier_score <= 1.0)",
            name="ck_human_override_blended_brier_range",
        ),
        CheckConstraint(
            "(actual_outcome IS NULL AND math_brier_score IS NULL AND blended_brier_score IS NULL "
            "AND resolved_at IS NULL) OR "
            "(actual_outcome IS NOT NULL AND math_brier_score IS NOT NULL AND blended_brier_score IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="ck_human_override_resolution_consistency",
        ),
        CheckConstraint("length(match_id) > 0", name="ck_human_override_match_id_not_empty"),
        Index("ix_human_override_logs_resolved", "resolved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pure_math_prob: Mapped[float] = mapped_column(Float, nullable=False)
    blended_prob: Mapped[float] = mapped_column(Float, nullable=False)
    actual_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    math_brier_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    blended_brier_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_touch_improved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
