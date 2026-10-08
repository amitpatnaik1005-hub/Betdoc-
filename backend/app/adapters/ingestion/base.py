"""Fleet ingestion adapters: one class per sanctioned sports-data API, or one config per API.

An adapter only fetches. It knows its provider's URLs, auth and quota headers, waits for a token
from the provider's rate-limit bucket before every request, retries 429/5xx with exponential
backoff, and hands back raw JSON in an ``IngestionBatch``. Mapping that JSON to canonical BetDoc ids
and true probabilities is ``app.services.omni_normalizer``'s job; locking, circuit breaking,
failover routing and health are ``app.services.omni_fleet``'s.

``fetch(scope)`` takes the provider-native keys (sport keys, league codes) the failover router
assigned this run; ``None`` means everything the source covers.

Error messages never contain request URLs: some providers take the API key as a query parameter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx

from app.core.config import Settings

__all__ = [
    "BackoffPolicy",
    "BaseDataIngestor",
    "IngestionBatch",
    "IngestionError",
    "MissingApiKeyError",
    "ProviderAuthError",
    "QuotaExhaustedError",
    "RateLimitedError",
    "SchemaDriftError",
    "SourcePayload",
    "ThrottledError",
    "parse_retry_after",
]

# httpx logs every request URL at INFO, and a query-string API key would land in the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_RESPONSE_BYTES = 10_000_000

Sleeper = Callable[[float], Awaitable[None]]
Limiter = Callable[[], Awaitable[float]]


class IngestionError(RuntimeError):
    """A fetch failed. Trips the source's circuit breaker and counts towards its dead-letter threshold."""


class RateLimitedError(IngestionError):
    """Still HTTP 429 after every backoff attempt."""


class ProviderAuthError(IngestionError):
    """401/403: the key is wrong, revoked or out of plan. Retrying cannot help."""


class QuotaExhaustedError(IngestionError):
    """The provider's remaining request credits fell below the configured floor."""


class SchemaDriftError(IngestionError):
    """The provider answered, but nothing in the answer could be parsed: its format changed."""


class MissingApiKeyError(IngestionError):
    """The source needs a key and none is configured. A setup state, not a failure."""


class ThrottledError(RuntimeError):
    """Our own rate limiter has no token within the wait budget. The run is deferred, not failed."""


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with "equal jitter": half the window fixed, half random, so retries from
    several workers spread out instead of hitting the provider in lockstep. A server-sent
    Retry-After is honoured whenever it asks for longer than the computed delay (up to the cap)."""

    max_attempts: int = 4
    base_seconds: float = 1.0
    max_seconds: float = 30.0

    @classmethod
    def from_settings(cls, settings: Settings) -> BackoffPolicy:
        return cls(
            max_attempts=settings.OMNI_FLEET_MAX_ATTEMPTS,
            base_seconds=settings.OMNI_FLEET_BACKOFF_BASE_SECONDS,
            max_seconds=settings.OMNI_FLEET_BACKOFF_MAX_SECONDS,
        )

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        window = min(self.max_seconds, self.base_seconds * (2**attempt))
        delay = window / 2 + random.random() * window / 2
        if retry_after is not None and retry_after > delay:
            delay = retry_after
        return min(delay, self.max_seconds)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds: either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _header_number(headers: Mapping[str, str], name: str | None) -> float | None:
    if not name:
        return None
    raw = headers.get(name)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SourcePayload:
    """One provider response: ``key`` is what was asked for (a sport key, a league code)."""

    key: str
    data: Any


@dataclass(frozen=True, slots=True)
class IngestionBatch:
    source_id: str
    payloads: list[SourcePayload]
    fetched_at: datetime
    latency_ms: int  # mean round-trip of this run's requests: the "ping" on the health matrix
    requests: int
    retries: int
    quota_remaining: float | None = None
    quota_used: float | None = None
    quota_limit: float | None = None
    throttled_ms: int = 0  # time spent waiting on our own rate limiter
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def quota_fraction(self) -> float | None:
        """Share of the provider's quota still unspent, when the provider reports enough to know."""
        return quota_fraction(self.quota_remaining, self.quota_used, self.quota_limit)


def quota_fraction(remaining: float | None, used: float | None, limit: float | None) -> float | None:
    if remaining is None:
        return None
    total = limit if limit else (remaining + used if used is not None else None)
    if not total or total <= 0:
        return None
    return max(0.0, min(1.0, remaining / total))


class BaseDataIngestor(ABC):
    """Subclass per provider (``app.adapters.ingestion.INGESTORS``) or describe one declaratively
    (``app.adapters.ingestion.factory.UniversalDataIngestor``)."""

    source_id: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str]
    requires_api_key: ClassVar[bool] = False
    docs_url: ClassVar[str | None] = None
    # Self-imposed ceiling (token bucket), kept under the provider's published limit
    requests_per_minute: ClassVar[float] = 60.0
    burst: ClassVar[int] = 10
    # Response headers carrying the provider's quota, when it reports one
    quota_remaining_header: ClassVar[str | None] = None
    quota_used_header: ClassVar[str | None] = None
    quota_limit: ClassVar[float | None] = None

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        api_key: str | None = None,
        backoff: BackoffPolicy | None = None,
        sleep: Sleeper = asyncio.sleep,
        limiter: Limiter | None = None,
    ) -> None:
        if self.requires_api_key and not api_key:
            raise MissingApiKeyError(f"{self.display_name} needs an API key; add one in Fleet Command.")
        self._http = http
        self._settings = settings
        self._api_key = api_key
        self._backoff = backoff or BackoffPolicy.from_settings(settings)
        self._sleep = sleep
        self._limiter = limiter
        self._latencies_ms: list[float] = []
        self._retries = 0
        self._throttled = 0.0
        self._quota_remaining: float | None = None
        self._quota_used: float | None = None

    @classmethod
    @abstractmethod
    def interval_seconds(cls, settings: Settings) -> float:
        """Default polling cadence (Fleet Command can override it per source)."""

    @abstractmethod
    async def fetch(self, scope: Sequence[str] | None = None) -> IngestionBatch:
        """Fetch one round of raw provider JSON for ``scope`` (provider-native keys; None = all)."""

    async def probe(self) -> IngestionBatch | None:
        """A request that costs no quota but reports it, used to notice a quota reset. None = unsupported."""
        return None

    # ---------------------------------------------------------------- helpers for subclasses
    def _batch(self, payloads: list[SourcePayload], meta: dict[str, Any] | None = None) -> IngestionBatch:
        latency = sum(self._latencies_ms) / len(self._latencies_ms) if self._latencies_ms else 0.0
        return IngestionBatch(
            source_id=self.source_id,
            payloads=payloads,
            fetched_at=datetime.now(UTC),
            latency_ms=round(latency),
            requests=len(self._latencies_ms),
            retries=self._retries,
            quota_remaining=self._quota_remaining,
            quota_used=self._quota_used,
            quota_limit=self.quota_limit,
            throttled_ms=round(self._throttled * 1000),
            meta=meta or {},
        )

    def _on_response(self, response: httpx.Response) -> None:
        """Reads the provider's quota headers from every response, success or not."""
        remaining = _header_number(response.headers, self.quota_remaining_header)
        if remaining is not None:
            self._quota_remaining = remaining
        used = _header_number(response.headers, self.quota_used_header)
        if used is not None:
            self._quota_used = used

    async def _get_json(self, url: str, *, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None) -> Any:
        """GET with retries: 429 and 5xx back off exponentially (Retry-After honoured), as do transport errors."""
        request_headers = {"Accept": "application/json", "User-Agent": self._settings.omni_user_agent, **(headers or {})}
        attempts = self._backoff.max_attempts
        for attempt in range(attempts):
            last = attempt == attempts - 1
            if self._limiter is not None:
                self._throttled += await self._limiter()  # ThrottledError propagates: the run is deferred
            started = time.perf_counter()
            try:
                status, response_headers, body = await self._read(url, params, request_headers)
            except httpx.TransportError as exc:
                if last:
                    raise IngestionError(f"{self.display_name}: transport failure ({type(exc).__name__})") from None
                await self._retry(attempt, None)
                continue
            self._latencies_ms.append((time.perf_counter() - started) * 1000)

            if status == 429:
                if last:
                    raise RateLimitedError(f"{self.display_name}: HTTP 429 after {attempts} attempts")
                await self._retry(attempt, parse_retry_after(response_headers.get("retry-after")))
                continue
            if status >= 500:
                if last:
                    raise IngestionError(f"{self.display_name}: HTTP {status} after {attempts} attempts")
                await self._retry(attempt, None)
                continue
            if status in (401, 403):
                raise ProviderAuthError(f"{self.display_name}: HTTP {status} (key rejected or out of plan)")
            if status >= 400:
                raise IngestionError(f"{self.display_name}: HTTP {status}")
            try:
                return json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise SchemaDriftError(f"{self.display_name}: response was not JSON") from None
        raise IngestionError(f"{self.display_name}: no attempts made")  # unreachable: max_attempts >= 1

    async def _retry(self, attempt: int, retry_after: float | None) -> None:
        self._retries += 1
        await self._sleep(self._backoff.delay(attempt, retry_after))

    async def _read(
        self, url: str, params: Mapping[str, str] | None, headers: dict[str, str]
    ) -> tuple[int, httpx.Headers, bytes]:
        async with self._http.stream("GET", url, params=params, headers=headers) as response:
            self._on_response(response)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise IngestionError(f"{self.display_name}: response exceeded {MAX_RESPONSE_BYTES} bytes")
                chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks)
