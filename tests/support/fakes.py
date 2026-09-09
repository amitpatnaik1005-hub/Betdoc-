from __future__ import annotations
from datetime import UTC, datetime, timedelta

from betdoc.domain.volatility.models import VolatilityAssessment

class FrozenClock:
    __slots__ = ("_now",)

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now if now is not None else datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

class StubExposureLedger:
    __slots__ = ("calls", "_exposure_paise", "_realized_loss_paise")

    def __init__(self, *, realized_loss_paise: int = 0, exposure_paise: int = 0) -> None:
        self._realized_loss_paise = realized_loss_paise
        self._exposure_paise = exposure_paise
        self.calls: list[tuple[str, str]] = []

    async def realized_loss_today_paise(self, *, profile_id: str, as_of: datetime) -> int:
        self.calls.append(("realized_loss_today_paise", profile_id))
        return self._realized_loss_paise

    async def open_exposure_paise(self, *, profile_id: str, sport_type: str) -> int:
        self.calls.append(("open_exposure_paise", sport_type))
        return self._exposure_paise

class StubVolatilityOracle:
    __slots__ = ("_assessment",)

    def __init__(self, assessment: VolatilityAssessment | None) -> None:
        self._assessment = assessment

    def assess(self, instrument_key: str) -> VolatilityAssessment | None:
        return self._assessment

def calm_assessment(*, instrument_key: str = "ipl:mi-v-csk:mi", assessed_at: datetime | None = None) -> VolatilityAssessment:
    return VolatilityAssessment(
        instrument_key=instrument_key, sample_count=64, realized_volatility=0.0005,
        volatility_per_minute=0.004, drift_bps=12, latest_odds=2.10, window_span_seconds=60.0,
        is_stale=False, assessed_at=assessed_at if assessed_at is not None else datetime(2026, 1, 1, tzinfo=UTC),
    )

class RecordingNotifier:
    __slots__ = ("payloads",)

    def __init__(self) -> None:
        self.payloads: list[str] = []

    async def dispatch_alert(self, payload: str) -> None:
        self.payloads.append(payload)

class FailingNotifier:
    __slots__ = ("attempts",)

    def __init__(self) -> None:
        self.attempts = 0

    async def dispatch_alert(self, payload: str) -> None:
        self.attempts += 1
        raise ConnectionError("websocket closed")
