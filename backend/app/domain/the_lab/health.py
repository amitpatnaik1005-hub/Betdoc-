import asyncio
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from app.schemas.the_lab import ApiHealthStatus

MONITORED_SOURCES: Final[tuple[str, ...]] = ("FBref", "Transfermarkt", "football-data.co.uk")


class ApiHealthMonitor:
    def __init__(self, sources: Sequence[str] = MONITORED_SOURCES) -> None:
        self._sources: tuple[str, ...] = tuple(sources)

    async def check_all(self) -> list[ApiHealthStatus]:
        return list(await asyncio.gather(*(self._ping(source) for source in self._sources)))

    async def _ping(self, source: str) -> ApiHealthStatus:
        started = time.perf_counter()  # monotonic, high-resolution
        await asyncio.sleep(random.uniform(0.01, 0.15))  # noqa: S311 - simulated jitter
        latency_ms = round((time.perf_counter() - started) * 1000)
        return ApiHealthStatus(
            source_name=source,
            status="ONLINE",
            latency_ms=latency_ms,
            last_checked=datetime.now(UTC),
        )
