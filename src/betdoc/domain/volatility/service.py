from __future__ import annotations

import asyncio
import math
import statistics
from collections import OrderedDict, deque
from datetime import timedelta
from typing import Final

import structlog

from betdoc.domain.shared.clock import Clock, SystemClock
from betdoc.domain.shared.money import BPS_DENOMINATOR
from betdoc.domain.volatility.models import OddsTick, VolatilityAssessment

_SECONDS_PER_MINUTE: Final[float] = 60.0

class OddsVolatilityService:
    __slots__ = (
        "_clock", "_log", "_max_instruments", "_min_samples",
        "_rejected_tick_count", "_stale_after", "_window_size", "_windows"
    )

    def __init__(self, *, clock: Clock | None = None, window_size: int = 256,
                 max_instruments: int = 4_096, min_samples: int = 8,
                 stale_after: timedelta = timedelta(seconds=15)) -> None:
        if window_size < 3:
            raise ValueError("window_size must be >= 3")
        if not 3 <= min_samples <= window_size:
            raise ValueError("min_samples must fall within [3, window_size]")
        if max_instruments < 1:
            raise ValueError("max_instruments must be >= 1")
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be a positive duration")

        self._clock: Clock = clock if clock is not None else SystemClock()
        self._window_size: int = window_size
        self._max_instruments: int = max_instruments
        self._min_samples: int = min_samples
        self._stale_after: timedelta = stale_after
        self._windows: OrderedDict[str, deque[OddsTick]] = OrderedDict()
        self._rejected_tick_count: int = 0
        self._log: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger("betdoc.volatility")

    @property
    def tracked_instruments(self) -> int:
        return len(self._windows)

    @property
    def rejected_tick_count(self) -> int:
        return self._rejected_tick_count

    def record_tick(self, tick: OddsTick) -> bool:
        key = tick.instrument_key
        window = self._windows.get(key)

        if window is not None and window and tick.observed_at <= window[-1].observed_at:
            self._rejected_tick_count += 1
            self._log.warning(
                "odds_tick.rejected_out_of_order",
                instrument_key=key,
                observed_at=tick.observed_at.isoformat(),
                newest_observed_at=window[-1].observed_at.isoformat(),
                rejected_tick_count=self._rejected_tick_count,
            )
            return False

        if window is None:
            window = deque(maxlen=self._window_size)
            self._windows[key] = window

        window.append(tick)
        self._windows.move_to_end(key)
        self._evict_least_recently_used()
        return True

    def assess(self, instrument_key: str) -> VolatilityAssessment | None:
        window = self._windows.get(instrument_key)
        if window is None or len(window) < self._min_samples:
            return None

        ticks: tuple[OddsTick, ...] = tuple(window)
        log_returns: list[float] = [
            math.log(current.offered_odds / previous.offered_odds)
            for previous, current in zip(ticks, ticks[1:], strict=False)
            if math.isfinite(math.log(current.offered_odds / previous.offered_odds))
        ]
        if len(log_returns) < 2:
            return None

        first, last = ticks[0], ticks[-1]
        sigma_per_tick = statistics.stdev(log_returns)
        span_seconds = (last.observed_at - first.observed_at).total_seconds()

        if span_seconds > 0.0:
            ticks_per_minute = (len(log_returns) / span_seconds) * _SECONDS_PER_MINUTE
            sigma_per_minute = sigma_per_tick * math.sqrt(ticks_per_minute)
        else:
            sigma_per_minute = sigma_per_tick

        now = self._clock.now()
        drift_ratio = (last.offered_odds - first.offered_odds) / first.offered_odds
        return VolatilityAssessment(
            instrument_key=instrument_key,
            sample_count=len(ticks),
            realized_volatility=sigma_per_tick,
            volatility_per_minute=sigma_per_minute,
            drift_bps=math.floor(drift_ratio * BPS_DENOMINATOR),
            latest_odds=last.offered_odds,
            window_span_seconds=span_seconds,
            is_stale=(now - last.observed_at) > self._stale_after,
            assessed_at=now,
        )

    async def prune_stale(self, *, retain_for: timedelta | None = None, yield_every: int = 128) -> int:
        if yield_every < 1:
            raise ValueError("yield_every must be >= 1")

        cutoff = self._clock.now() - (retain_for if retain_for is not None else self._stale_after * 4)
        removed = 0
        for index, key in enumerate(list(self._windows.keys()), start=1):
            window = self._windows.get(key)
            if window is not None and window and window[-1].observed_at < cutoff:
                del self._windows[key]
                removed += 1
            if index % yield_every == 0:
                await asyncio.sleep(0)

        if removed:
            self._log.info("volatility.pruned_instruments", removed=removed, tracked_instruments=len(self._windows))
        return removed

    def _evict_least_recently_used(self) -> None:
        while len(self._windows) > self._max_instruments:
            evicted_key, _ = self._windows.popitem(last=False)
            self._log.warning("volatility.instrument_evicted", instrument_key=evicted_key, max_instruments=self._max_instruments)
