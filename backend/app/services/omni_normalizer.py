"""Quorum consensus across providers: recency/confidence weighting + variance quarantine."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base_adapter import StandardizedEvent
from app.core.config import Settings
from app.models.omni_vault import OmniQuarantineLog

logger = logging.getLogger(__name__)


class QuorumError(ValueError):
    """Events cannot form a quorum (empty, stale, mixed entities/types)."""


class QuorumVarianceException(Exception):
    def __init__(self, topic: str, variance: float, threshold: float, events: Sequence[StandardizedEvent]) -> None:
        super().__init__(f"Quorum variance {variance:.4%} exceeds {threshold:.4%} for topic '{topic}'.")
        self.topic = topic
        self.variance = variance
        self.threshold = threshold
        self.events = list(events)


@dataclass(frozen=True, slots=True)
class QuorumPolicy:
    variance_threshold: float
    half_life_seconds: float
    max_age_seconds: float
    zero_tolerance: float

    @classmethod
    def from_settings(cls, settings: Settings) -> QuorumPolicy:
        return cls(
            variance_threshold=settings.omni_quorum_variance_threshold,
            half_life_seconds=settings.omni_quorum_half_life_seconds,
            max_age_seconds=settings.omni_quorum_max_age_seconds,
            zero_tolerance=settings.omni_quorum_zero_tolerance,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights, strict=True))
    half = sum(weights) / 2.0
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


class QuorumConsensusEngine:
    r"""Weight :math:`w_i = c_i \cdot 2^{-\Delta t_i / h}` (confidence x recency half-life).

    Variance is the maximum relative deviation from the weighted median,
    :math:`\max_i |x_i - \tilde x| / |\tilde x|` (absolute when :math:`|\tilde x| \le \epsilon`).
    Above the threshold, ``QuorumVarianceException`` is raised. Otherwise the consensus value is the
    weighted mean, stamped with the freshest source timestamp.
    """

    def __init__(self, policy: QuorumPolicy, clock: Callable[[], datetime] = _utcnow) -> None:
        self._policy = policy
        self._clock = clock

    def resolve_conflict(self, topic: str, events: list[StandardizedEvent]) -> StandardizedEvent:
        if not events:
            raise QuorumError(f"No events supplied for topic '{topic}'.")
        if any(e.source_timestamp is None for e in events):
            raise QuorumError("Every event must carry source_timestamp (Double-Timestamp Law).")
        if len({e.entity_id for e in events}) != 1:
            raise QuorumError(f"Topic '{topic}' mixes entity_ids.")

        now = self._clock()
        fresh = [e for e in events if (now - e.source_timestamp).total_seconds() <= self._policy.max_age_seconds]  # type: ignore[operator]
        if not fresh:
            raise QuorumError(f"All events for '{topic}' are older than {self._policy.max_age_seconds}s.")

        weights = [self._weight(e, now) for e in fresh]
        if sum(weights) <= 0.0:
            weights = [1.0] * len(fresh)
        winner = max(zip(fresh, weights, strict=True), key=lambda pair: (pair[1], pair[0].source_timestamp))[0]
        freshest_ts = max(e.source_timestamp for e in fresh)  # type: ignore[type-var]

        if all(isinstance(e.normalized_value, float) for e in fresh):
            values = [float(e.normalized_value) for e in fresh]  # type: ignore[arg-type]
            variance = self._variance(values, weights)
            if variance > self._policy.variance_threshold:
                raise QuorumVarianceException(topic, variance, self._policy.variance_threshold, fresh)
            consensus: float | dict[str, float | int | str | bool | None] = sum(
                v * w for v, w in zip(values, weights, strict=True)
            ) / sum(weights)
        elif all(isinstance(e.normalized_value, dict) for e in fresh):
            dicts = [e.normalized_value for e in fresh if isinstance(e.normalized_value, dict)]
            shared = set.intersection(*(set(d) for d in dicts))
            numeric_keys = [k for k in shared if all(isinstance(d[k], float) and not isinstance(d[k], bool) for d in dicts)]
            merged = dict(winner.normalized_value) if isinstance(winner.normalized_value, dict) else {}
            worst = 0.0
            for key in numeric_keys:
                values = [float(d[key]) for d in dicts]  # type: ignore[arg-type]
                worst = max(worst, self._variance(values, weights))
                merged[key] = sum(v * w for v, w in zip(values, weights, strict=True)) / sum(weights)
            if worst > self._policy.variance_threshold:
                raise QuorumVarianceException(topic, worst, self._policy.variance_threshold, fresh)
            variance, consensus = worst, merged
        else:
            raise QuorumError(f"Topic '{topic}' mixes numeric and structured values.")

        mean_confidence = sum(e.confidence_score * w for e, w in zip(fresh, weights, strict=True)) / sum(weights)
        return winner.model_copy(
            update={
                "normalized_value": consensus,
                "confidence_score": max(0.0, min(1.0, mean_confidence * (1.0 - variance))),
                "source_timestamp": freshest_ts,
                "provider_id": None,  # consensus, not a single provider
            }
        )

    def _weight(self, event: StandardizedEvent, now: datetime) -> float:
        age = max((now - event.source_timestamp).total_seconds(), 0.0)  # type: ignore[operator]
        return event.confidence_score * math.pow(0.5, age / self._policy.half_life_seconds)

    def _variance(self, values: Sequence[float], weights: Sequence[float]) -> float:
        if len(values) < 2:
            return 0.0
        center = _weighted_median(values, weights)
        deviation = max(abs(v - center) for v in values)
        if abs(center) <= self._policy.zero_tolerance:
            return deviation
        return deviation / abs(center)


class QuorumService:
    """Runs the engine and routes variance failures to ``OmniQuarantineLog``."""

    def __init__(self, engine: QuorumConsensusEngine) -> None:
        self._engine = engine

    async def resolve(self, session: AsyncSession, topic: str, events: list[StandardizedEvent]) -> StandardizedEvent | None:
        try:
            return self._engine.resolve_conflict(topic, events)
        except QuorumVarianceException as exc:
            logger.warning("Quarantining topic=%s variance=%.4f threshold=%.4f", topic, exc.variance, exc.threshold)
            await self._quarantine(session, topic, "variance_exceeded", exc.variance, exc.threshold, exc.events)
        except QuorumError as exc:
            logger.warning("Quorum rejected topic=%s: %s", topic, exc)
            await self._quarantine(session, topic, "invalid_quorum", None, None, events)
        return None

    async def _quarantine(
        self,
        session: AsyncSession,
        topic: str,
        reason: str,
        variance: float | None,
        threshold: float | None,
        events: Sequence[StandardizedEvent],
    ) -> None:
        session.add(
            OmniQuarantineLog(
                topic=topic,
                reason=reason,
                variance=variance,
                threshold=threshold,
                events=[e.model_dump(mode="json") for e in events],
            )
        )
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            logger.exception("Failed to persist quarantine record for topic=%s", topic)
            raise
