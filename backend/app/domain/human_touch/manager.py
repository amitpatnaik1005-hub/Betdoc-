"""HumanTouchManager: logit-space confidence adjustment and Brier-score accountability.

Every weight, limit and threshold comes from the stored configuration or the request.
The only literals are mathematical identities (percent = /100, the 0.5 probability
midpoint) and the spec-mandated logit clamp epsilon and 4-decimal output precision.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.domain.human_touch.errors import (
    HumanTouchDomainError,
    OverrideLogAlreadyResolvedError,
    OverrideLogNotFoundError,
)
from app.models.human_touch import (
    GLOBAL_CONFIG_KEY,
    MAX_ADJUSTMENT_LIMIT_CEILING_PCT,
    MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT,
    PROBABILITY_CEILING,
    PROBABILITY_FLOOR,
    VALID_OUTCOMES,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    HumanOverrideLogModel,
    HumanTouchConfigModel,
)

logger = logging.getLogger("betdoc.human_touch")

LOGIT_EPSILON = 0.0001          # spec: clamp p into [0.0001, 0.9999] before the logit
OUTPUT_PRECISION = 4            # spec: round every returned probability to 4 decimals
PERCENT_DIVISOR = 100.0
PROBABILITY_MIDPOINT = 0.5      # no-lean point used to classify confidence tiers
SENTIMENT_FLOOR, SENTIMENT_CEILING = -1.0, 1.0
FACTOR_VALUE_FLOOR, FACTOR_VALUE_CEILING = 0.0, 1.0
FACTOR_IMPACT_FLOOR, FACTOR_IMPACT_CEILING = -1.0, 1.0

ConfidenceTier = Literal["HIGH_CONFIDENCE", "NEUTRAL", "CONTRARIAN"]


class BlendConfig(Protocol):
    is_blended_mode_active: bool
    max_adjustment_limit_pct: float
    sentiment_weight: float
    momentum_weight: float
    min_adjustment_threshold_pct: float


@dataclass(frozen=True, slots=True)
class NarrativeFactorInput:
    name: str
    value: float   # observed intensity, [0, 1]
    impact: float  # signed direction/strength for this team, [-1, 1]


@dataclass(frozen=True, slots=True)
class NarrativeMetrics:
    sentiment_score: float
    factors: tuple[NarrativeFactorInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ConfigValues:
    is_blended_mode_active: bool
    max_adjustment_limit_pct: float
    sentiment_weight: float
    momentum_weight: float
    min_adjustment_threshold_pct: float


@dataclass(frozen=True, slots=True)
class BlendResult:
    pure_math_prob: float
    adjusted_prob: float
    adjustment_delta: float
    confidence_tier: ConfidenceTier
    narrative_modifier: float
    raw_blended_prob: float
    bypassed: bool
    below_threshold: bool
    clamped: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- pure helpers


def _require_finite(**values: Any) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HumanTouchDomainError(f"{name} must be a finite number.")


def _require_range(name: str, value: float, floor: float, ceiling: float) -> None:
    if not floor <= value <= ceiling:
        raise HumanTouchDomainError(f"{name} must be between {floor} and {ceiling}.")


def _round(value: float) -> float:
    return round(value, OUTPUT_PRECISION) + 0.0  # + 0.0 normalises -0.0


def _logit(probability: float) -> float:
    clamped = min(max(probability, LOGIT_EPSILON), 1.0 - LOGIT_EPSILON)
    return math.log(clamped / (1.0 - clamped))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)  # numerically stable branch for large negative inputs
    return exp_value / (1.0 + exp_value)


def _confidence_tier(pure: float, delta: float) -> ConfidenceTier:
    if delta == 0:
        return "NEUTRAL"
    lean = pure - PROBABILITY_MIDPOINT
    return "CONTRARIAN" if lean * delta < 0 else "HIGH_CONFIDENCE"


def validate_config_values(config: BlendConfig) -> None:
    if not isinstance(config.is_blended_mode_active, bool):
        raise HumanTouchDomainError("is_blended_mode_active must be a boolean.")
    _require_finite(
        max_adjustment_limit_pct=config.max_adjustment_limit_pct,
        sentiment_weight=config.sentiment_weight,
        momentum_weight=config.momentum_weight,
        min_adjustment_threshold_pct=config.min_adjustment_threshold_pct,
    )
    _require_range("max_adjustment_limit_pct", config.max_adjustment_limit_pct, 0.0, MAX_ADJUSTMENT_LIMIT_CEILING_PCT)
    _require_range(
        "min_adjustment_threshold_pct", config.min_adjustment_threshold_pct, 0.0, MIN_ADJUSTMENT_THRESHOLD_CEILING_PCT
    )
    _require_range("sentiment_weight", config.sentiment_weight, WEIGHT_FLOOR, WEIGHT_CEILING)
    _require_range("momentum_weight", config.momentum_weight, WEIGHT_FLOOR, WEIGHT_CEILING)
    if config.min_adjustment_threshold_pct > config.max_adjustment_limit_pct:
        raise HumanTouchDomainError("min_adjustment_threshold_pct cannot exceed max_adjustment_limit_pct.")


def _validate_metrics(metrics: NarrativeMetrics) -> None:
    _require_finite(sentiment_score=metrics.sentiment_score)
    _require_range("sentiment_score", metrics.sentiment_score, SENTIMENT_FLOOR, SENTIMENT_CEILING)
    for index, factor in enumerate(metrics.factors):
        _require_finite(**{f"factors[{index}].value": factor.value, f"factors[{index}].impact": factor.impact})
        _require_range(f"factors[{index}].value", factor.value, FACTOR_VALUE_FLOOR, FACTOR_VALUE_CEILING)
        _require_range(f"factors[{index}].impact", factor.impact, FACTOR_IMPACT_FLOOR, FACTOR_IMPACT_CEILING)


def _momentum_score(factors: Sequence[NarrativeFactorInput]) -> float:
    if not factors:
        return 0.0
    return sum(factor.value * factor.impact for factor in factors) / len(factors)


# --------------------------------------------------------------------------- manager


class HumanTouchManager:
    # ------------------------------------------------------------------ core engine

    def calculate_blended_probability(
        self, pure_prob: float, narrative_metrics: NarrativeMetrics, config: BlendConfig
    ) -> BlendResult:
        _require_finite(pure_prob=pure_prob)
        _require_range("pure_prob", pure_prob, PROBABILITY_FLOOR, PROBABILITY_CEILING)

        if not config.is_blended_mode_active:  # bypass layer: no calculations at all
            pure = _round(pure_prob)
            return BlendResult(
                pure_math_prob=pure,
                adjusted_prob=pure,
                adjustment_delta=0.0,
                confidence_tier="NEUTRAL",
                narrative_modifier=0.0,
                raw_blended_prob=pure,
                bypassed=True,
                below_threshold=False,
                clamped=False,
            )

        validate_config_values(config)
        _validate_metrics(narrative_metrics)

        narrative_modifier = (config.sentiment_weight * narrative_metrics.sentiment_score) + (
            config.momentum_weight * _momentum_score(narrative_metrics.factors)
        )
        try:
            raw_blended = _sigmoid(_logit(pure_prob) + narrative_modifier)
        except (OverflowError, ValueError, ZeroDivisionError) as exc:
            raise HumanTouchDomainError("Narrative modifier produced an invalid probability.") from exc

        delta = raw_blended - pure_prob
        # The logit clamp at p=0/1 can create a tiny move against the modifier; never allow that.
        if narrative_modifier == 0 or delta * narrative_modifier <= 0:
            delta = 0.0

        min_shift = config.min_adjustment_threshold_pct / PERCENT_DIVISOR
        max_shift = config.max_adjustment_limit_pct / PERCENT_DIVISOR
        below_threshold = delta != 0.0 and abs(delta) < min_shift
        if below_threshold:
            delta = 0.0
        clamped = abs(delta) > max_shift
        if clamped:
            delta = math.copysign(max_shift, delta)

        adjusted = min(max(pure_prob + delta, PROBABILITY_FLOOR), PROBABILITY_CEILING)
        pure_rounded = _round(pure_prob)
        adjusted_rounded = _round(adjusted)
        delta_rounded = _round(adjusted_rounded - pure_rounded)

        return BlendResult(
            pure_math_prob=pure_rounded,
            adjusted_prob=adjusted_rounded,
            adjustment_delta=delta_rounded,
            confidence_tier=_confidence_tier(pure_prob, delta_rounded),
            narrative_modifier=_round(narrative_modifier),
            raw_blended_prob=_round(raw_blended),
            bypassed=False,
            below_threshold=below_threshold,
            clamped=clamped,
        )

    # ------------------------------------------------------------------ configuration

    async def _find_config(self, db: AsyncSession) -> HumanTouchConfigModel | None:
        result = await db.execute(
            select(HumanTouchConfigModel)
            .where(HumanTouchConfigModel.config_key == GLOBAL_CONFIG_KEY)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_or_seed_config(self, db: AsyncSession) -> HumanTouchConfigModel:
        config = await self._find_config(db)
        if config is None:
            candidate = HumanTouchConfigModel(config_key=GLOBAL_CONFIG_KEY)
            db.add(candidate)
            try:
                await db.commit()
                config = candidate
                logger.info("HUMAN-TOUCH: seeded neutral configuration (blending inactive).")
            except IntegrityError:
                await db.rollback()
                logger.warning("HUMAN-TOUCH: concurrent config seed detected; loading the existing row.")
                config = await self._find_config(db)
                if config is None:
                    raise HumanTouchDomainError("Configuration could not be created or loaded.") from None
        await db.refresh(config)
        return config

    async def update_config(self, db: AsyncSession, values: ConfigValues) -> HumanTouchConfigModel:
        validate_config_values(values)
        config = await self.get_or_seed_config(db)
        for name, value in asdict(values).items():
            setattr(config, name, value)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HumanTouchDomainError("Configuration rejected by storage constraints.") from exc
        await db.refresh(config)
        logger.info(
            "HUMAN-TOUCH: config updated (active=%s, max=%.2f%%, min=%.2f%%, w_sent=%.3f, w_mom=%.3f).",
            config.is_blended_mode_active,
            config.max_adjustment_limit_pct,
            config.min_adjustment_threshold_pct,
            config.sentiment_weight,
            config.momentum_weight,
        )
        return config

    # ------------------------------------------------------------------ audited execution

    async def execute_blend(
        self, db: AsyncSession, match_id: str, pure_prob: float, narrative_metrics: NarrativeMetrics
    ) -> tuple[BlendResult, HumanOverrideLogModel]:
        normalized_match_id = (match_id or "").strip()
        if not normalized_match_id:
            raise HumanTouchDomainError("match_id must not be blank.")
        config = await self.get_or_seed_config(db)
        result = self.calculate_blended_probability(pure_prob, narrative_metrics, config)

        log = HumanOverrideLogModel(
            match_id=normalized_match_id,
            pure_math_prob=result.pure_math_prob,
            blended_prob=result.adjusted_prob,
        )
        db.add(log)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HumanTouchDomainError("Override log rejected by storage constraints.") from exc
        await db.refresh(log)
        logger.info(
            "HUMAN-TOUCH: logged %s for %s (pure %.4f -> blended %.4f, tier %s).",
            log.id,
            normalized_match_id,
            result.pure_math_prob,
            result.adjusted_prob,
            result.confidence_tier,
        )
        return result, log

    async def resolve_override_log(
        self, db: AsyncSession, log_id: UUID, actual_outcome: float
    ) -> tuple[HumanOverrideLogModel, bool, float]:
        _require_finite(actual_outcome=actual_outcome)
        if actual_outcome not in VALID_OUTCOMES:
            raise HumanTouchDomainError(f"actual_outcome must be one of {VALID_OUTCOMES}.")

        log = (
            await db.execute(select(HumanOverrideLogModel).where(HumanOverrideLogModel.id == log_id))
        ).scalar_one_or_none()
        if log is None:
            raise OverrideLogNotFoundError(log_id)
        if log.actual_outcome is not None:
            raise OverrideLogAlreadyResolvedError(log_id)

        math_brier = (log.pure_math_prob - actual_outcome) ** 2
        blended_brier = (log.blended_prob - actual_outcome) ** 2
        improved = blended_brier < math_brier

        result = await db.execute(
            update(HumanOverrideLogModel)
            .where(HumanOverrideLogModel.id == log_id, HumanOverrideLogModel.actual_outcome.is_(None))
            .values(
                actual_outcome=actual_outcome,
                math_brier_score=_round(math_brier),
                blended_brier_score=_round(blended_brier),
                human_touch_improved=improved,
                resolved_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise OverrideLogAlreadyResolvedError(log_id)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HumanTouchDomainError("Resolution rejected by storage constraints.") from exc

        resolved = await db.get(HumanOverrideLogModel, log_id, populate_existing=True)
        if resolved is None:
            raise OverrideLogNotFoundError(log_id)
        logger.info(
            "HUMAN-TOUCH: resolved %s (outcome %.1f): math Brier %.4f vs blended %.4f -> improved=%s.",
            log_id,
            actual_outcome,
            math_brier,
            blended_brier,
            improved,
        )
        return resolved, improved, _round(math_brier - blended_brier)
