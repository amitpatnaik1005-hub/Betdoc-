"""Reachability of the external data sources the Lab's research depends on (real HTTP probes)."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

import httpx

from app.schemas.the_lab import ApiHealthStatus

MONITORED_SOURCES: Final[tuple[str, ...]] = ("FBref", "Transfermarkt", "football-data.co.uk")
SOURCE_URLS: Final[Mapping[str, str]] = {
    "FBref": "https://fbref.com/en/",
    "Transfermarkt": "https://www.transfermarkt.com/",
    "football-data.co.uk": "https://www.football-data.co.uk/",
}
PROBE_TIMEOUT_SECONDS: Final[float] = 4.0
SLOW_THRESHOLD_MS: Final[int] = 1_500

# Returns the HTTP status code; raises on transport failure (DNS, TLS, timeout, refused).
Probe = Callable[[str], Awaitable[int]]


async def http_probe(url: str) -> int:
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": "BetDoc-Lab-Health/1.0"}) as client:
        response = await client.head(url)
        if response.status_code == 405:  # some origins refuse HEAD; any answer still proves reachability
            response = await client.get(url)
        return response.status_code


class ApiHealthMonitor:
    def __init__(self, sources: Sequence[str] = MONITORED_SOURCES, *, probe: Probe = http_probe) -> None:
        self._sources: tuple[str, ...] = tuple(sources)
        self._probe = probe

    async def check_all(self) -> list[ApiHealthStatus]:
        return list(await asyncio.gather(*(self._ping(source) for source in self._sources)))

    async def _ping(self, source: str) -> ApiHealthStatus:
        started = time.perf_counter()  # monotonic, high-resolution
        url = SOURCE_URLS.get(source, source)
        try:
            code = await asyncio.wait_for(self._probe(url), timeout=PROBE_TIMEOUT_SECONDS + 1)
        except Exception:  # noqa: BLE001 - any transport failure means unreachable
            code = None
        latency_ms = round((time.perf_counter() - started) * 1000)
        if code is None:
            status = "OFFLINE"
        elif code >= 500 or latency_ms > SLOW_THRESHOLD_MS:
            status = "DEGRADED"
        else:  # 2xx-4xx: the origin answered (403/429 bot walls still mean "up")
            status = "ONLINE"
        return ApiHealthStatus(source_name=source, status=status, latency_ms=latency_ms, last_checked=datetime.now(UTC))
