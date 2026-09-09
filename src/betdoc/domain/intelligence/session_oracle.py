from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field

from betdoc.domain.intelligence.advisor_models import MarketOpportunity
from betdoc.domain.shared.clock import Clock, SystemClock
from betdoc.domain.shared.money import BPS_DENOMINATOR

_VOLUME_SATURATION_COUNT: Final[int] = 10
_QUALITY_SATURATION_BPS: Final[int] = 200
_WEIGHT_CEILING: Final[float] = 5.0
_MAX_SCORE: Final[float] = 10.0

DRY_MARKET_PAYLOAD: Final[str] = (
    "Market is completely dry and highly efficient. "
    "Stand down and preserve capital."
)
VARIANCE_WARNING_PAYLOAD: Final[str] = (
    "Variance Warning: You are up heavily today. Statistically, mean-reversion is "
    "highly probable. Oracle recommends reducing base stakes by 20% for the "
    "remainder of the session."
)


@runtime_checkable
class NotificationPort(Protocol):
    async def dispatch_alert(self, payload: str) -> None: ...


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MarketHealthScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float = Field(ge=0.0, le=_MAX_SCORE)
    volume_component: float = Field(ge=0.0, le=_WEIGHT_CEILING)
    quality_component: float = Field(ge=0.0, le=_WEIGHT_CEILING)
    positive_ev_count: int = Field(ge=0)
    average_ev_bps: int
    sample_size: int = Field(ge=0)
    computed_at: datetime


class SessionIntelligenceOracle:
    __slots__ = (
        "_clock",
        "_dry_alert_cooldown",
        "_dry_horizon",
        "_hot_alert_cooldown",
        "_hot_score_threshold",
        "_last_dry_alert_at",
        "_last_hot_alert_at",
        "_last_positive_ev_at",
        "_log",
        "_notifier",
        "_opportunities",
        "_poll_interval",
        "_variance_roi_threshold_bps",
        "_window",
    )

    def __init__(
        self,
        *,
        notifier: NotificationPort,
        clock: Clock | None = None,
        window: timedelta = timedelta(minutes=15),
        max_tracked_opportunities: int = 8_192,
        poll_interval: timedelta = timedelta(seconds=60),
        hot_score_threshold: float = 7.5,
        hot_alert_cooldown: timedelta = timedelta(minutes=30),
        dry_horizon: timedelta = timedelta(minutes=120),
        dry_alert_cooldown: timedelta = timedelta(minutes=60),
        variance_roi_threshold_bps: int = 1_500,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("window must be a positive duration")
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be a positive duration")
        if max_tracked_opportunities < 1:
            raise ValueError("max_tracked_opportunities must be >= 1")
        if not 0.0 < hot_score_threshold <= _MAX_SCORE:
            raise ValueError(f"hot_score_threshold must be within (0, {_MAX_SCORE}]")
        if variance_roi_threshold_bps < 0:
            raise ValueError("variance_roi_threshold_bps must be non-negative")

        self._notifier = notifier
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._window = window
        self._poll_interval = poll_interval
        self._hot_score_threshold = hot_score_threshold
        self._hot_alert_cooldown = hot_alert_cooldown
        self._dry_horizon = dry_horizon
        self._dry_alert_cooldown = dry_alert_cooldown
        self._variance_roi_threshold_bps = variance_roi_threshold_bps

        self._opportunities: deque[MarketOpportunity] = deque(
            maxlen=max_tracked_opportunities
        )
        self._last_hot_alert_at: datetime | None = None
        self._last_dry_alert_at: datetime | None = None
        self._last_positive_ev_at: datetime = self._clock.now()
        self._log: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(
            "betdoc.oracle"
        )

    @property
    def tracked_opportunities(self) -> int:
        return len(self._opportunities)

    @property
    def last_hot_alert_at(self) -> datetime | None:
        return self._last_hot_alert_at

    @property
    def last_dry_alert_at(self) -> datetime | None:
        return self._last_dry_alert_at

    def ingest_opportunity(self, opp: MarketOpportunity) -> None:
        self._opportunities.append(opp)
        if self._expected_value_bps(opp) > 0:
            self._last_positive_ev_at = self._clock.now()

    def market_health_score(self) -> MarketHealthScore:
        now = self._clock.now()
        positive_ev_bps = [
            ev_bps
            for ev_bps in (self._expected_value_bps(opp) for opp in self._opportunities)
            if ev_bps > 0
        ]
        count = len(positive_ev_bps)

        if count == 0:
            return MarketHealthScore(
                score=0.0,
                volume_component=0.0,
                quality_component=0.0,
                positive_ev_count=0,
                average_ev_bps=0,
                sample_size=len(self._opportunities),
                computed_at=now,
            )

        average_ev_bps = sum(positive_ev_bps) // count
        volume = (
            min(count, _VOLUME_SATURATION_COUNT) / _VOLUME_SATURATION_COUNT
        ) * _WEIGHT_CEILING
        quality = (
            min(average_ev_bps, _QUALITY_SATURATION_BPS) / _QUALITY_SATURATION_BPS
        ) * _WEIGHT_CEILING

        return MarketHealthScore(
            score=min(volume + quality, _MAX_SCORE),
            volume_component=volume,
            quality_component=quality,
            positive_ev_count=count,
            average_ev_bps=average_ev_bps,
            sample_size=len(self._opportunities),
            computed_at=now,
        )

    async def monitor_market_pulse(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval.total_seconds())
                await self._prune_stale_opportunities()
                await self.evaluate_pulse()
            except asyncio.CancelledError:
                self._log.info("oracle.monitor_cancelled")
                raise
            except Exception:
                self._log.exception("oracle.pulse_iteration_failed")

    async def evaluate_pulse(self) -> None:
        now = self._clock.now()
        health = self.market_health_score()

        if health.score >= self._hot_score_threshold:
            if self._is_cooled_down(self._last_hot_alert_at, now, self._hot_alert_cooldown):
                payload = (
                    f"Hot market detected: health score {health.score:.1f}/"
                    f"{_MAX_SCORE:.1f} across {health.positive_ev_count} positive-EV "
                    f"opportunities with a mean edge of {health.average_ev_bps}bps. "
                    "Deploy capital."
                )
                if await self._dispatch(payload, alert="hot"):
                    self._last_hot_alert_at = now
            return

        dry_for = now - self._last_positive_ev_at
        if health.positive_ev_count == 0 and dry_for >= self._dry_horizon:
            if self._is_cooled_down(self._last_dry_alert_at, now, self._dry_alert_cooldown):
                if await self._dispatch(DRY_MARKET_PAYLOAD, alert="dry"):
                    self._last_dry_alert_at = now

    async def _prune_stale_opportunities(self, *, yield_every: int = 256) -> None:
        if yield_every < 1:
            raise ValueError("yield_every must be >= 1")

        cutoff = self._clock.now() - self._window
        evicted = 0
        while self._opportunities and _as_utc(self._opportunities[0].quoted_at) < cutoff:
            self._opportunities.popleft()
            evicted += 1
            if evicted % yield_every == 0:
                await asyncio.sleep(0)

        if evicted:
            self._log.debug(
                "oracle.pruned_opportunities",
                evicted=evicted,
                retained=len(self._opportunities),
            )

    def evaluate_session_variance(
        self, session_start_balance_paise: int, current_balance_paise: int
    ) -> str | None:
        roi_bps = self.session_roi_bps(
            session_start_balance_paise, current_balance_paise
        )
        if roi_bps > self._variance_roi_threshold_bps:
            self._log.info("oracle.variance_warning", roi_bps=roi_bps)
            return VARIANCE_WARNING_PAYLOAD
        return None

    @staticmethod
    def session_roi_bps(
        session_start_balance_paise: int, current_balance_paise: int
    ) -> int:
        if session_start_balance_paise <= 0:
            raise ValueError(
                "session_start_balance_paise must be positive to compute ROI, got "
                f"{session_start_balance_paise}"
            )
        delta = current_balance_paise - session_start_balance_paise
        return (delta * BPS_DENOMINATOR) // session_start_balance_paise

    async def _dispatch(self, payload: str, *, alert: str) -> bool:
        try:
            await self._notifier.dispatch_alert(payload)
        except Exception:
            self._log.exception("oracle.alert_dispatch_failed", alert=alert)
            return False
        self._log.info("oracle.alert_dispatched", alert=alert)
        return True

    @staticmethod
    def _is_cooled_down(
        last_at: datetime | None, now: datetime, cooldown: timedelta
    ) -> bool:
        return last_at is None or (now - last_at) >= cooldown

    @staticmethod
    def _expected_value_bps(opp: MarketOpportunity) -> int:
        expected_value = (opp.fair_probability * opp.offered_odds) - 1.0 # ADDED MISSING PROPERTY DIRECTLY HERE
        if not math.isfinite(expected_value):
            return -BPS_DENOMINATOR
        return math.floor(expected_value * BPS_DENOMINATOR)
