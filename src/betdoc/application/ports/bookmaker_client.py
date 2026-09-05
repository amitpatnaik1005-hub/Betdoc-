"""Port definition for every odds source: WebSocket, SSE, REST or replay.

This ABC is the *only* contract the scanner, risk engine and executor know
about. An adapter that satisfies it is indistinguishable from any other, which
is what makes the ingestion layer universal.

Error taxonomy is part of the port, not the adapter. Adapters MUST translate
their transport-specific failures into these types so retry, circuit-breaking
and alerting policy live in one place.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self, TypeAlias

from betdoc.domain.models.odds import OddsTick, SourceTransport

__all__ = [
    "AdapterMetrics",
    "AuthenticationError",
    "BaseBookmakerAdapter",
    "BookmakerError",
    "HealthStatus",
    "PayloadSchemaError",
    "RateLimitedError",
    "RawPayload",
    "StreamClosedError",
    "TransientBookmakerError",
]

RawPayload: TypeAlias = Mapping[str, Any]

logger: Final[logging.Logger] = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


class BookmakerError(Exception):
    """Base class for every fault raised by an odds source adapter."""

    def __init__(self, message: str, *, bookmaker: str | None = None) -> None:
        super().__init__(message)
        self.bookmaker = bookmaker


class TransientBookmakerError(BookmakerError):
    """Retryable: timeout, connection reset, 5xx, malformed frame on a live socket."""


class RateLimitedError(TransientBookmakerError):
    """HTTP 429 or a venue-side throttle. Carries the server's own backoff hint."""

    def __init__(
        self,
        message: str,
        *,
        bookmaker: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, bookmaker=bookmaker)
        self.retry_after = retry_after


class AuthenticationError(BookmakerError):
    """401/403 or an expired key. NEVER retry: it burns quota and trips bans."""


class PayloadSchemaError(BookmakerError):
    """The venue sent something we cannot normalise. Quarantine, do not crash."""

    def __init__(
        self,
        message: str,
        *,
        bookmaker: str | None = None,
        payload: RawPayload | None = None,
    ) -> None:
        super().__init__(message, bookmaker=bookmaker)
        self.payload = payload


class StreamClosedError(BookmakerError):
    """The upstream stream terminated and cannot be resumed by this instance."""


# --------------------------------------------------------------------------- #
# Observability value objects
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class AdapterMetrics:
    """Per-adapter counters. Scraped by the observability layer, never by logic."""

    ticks_emitted: int = 0
    ticks_suppressed: int = 0
    ticks_dropped: int = 0
    payload_errors: int = 0
    transport_errors: int = 0
    reconnects: int = 0
    requests_sent: int = 0
    quota_remaining: int | None = None
    last_tick_monotonic_ns: int | None = None

    def snapshot(self) -> dict[str, int | None]:
        return {
            "ticks_emitted": self.ticks_emitted,
            "ticks_suppressed": self.ticks_suppressed,
            "ticks_dropped": self.ticks_dropped,
            "payload_errors": self.payload_errors,
            "transport_errors": self.transport_errors,
            "reconnects": self.reconnects,
            "requests_sent": self.requests_sent,
            "quota_remaining": self.quota_remaining,
        }


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Liveness signal consumed by the supervisor and the kill switch."""

    bookmaker: str
    is_connected: bool
    detail: str = ""
    metrics: dict[str, int | None] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The port
# --------------------------------------------------------------------------- #


class BaseBookmakerAdapter(abc.ABC):
    """Every odds source implements exactly this surface.

    Lifecycle::

        async with SomeAdapter(...) as adapter:
            async for tick in adapter.stream_live_ticks():
                await bus.publish(tick)

    Implementation rules:

    #. ``stream_live_ticks`` is infinite and self-healing. It reconnects on
       ``TransientBookmakerError`` internally and only propagates terminal
       faults (``AuthenticationError``, ``StreamClosedError``).
    #. ``normalize_payload`` is **pure and synchronous**: no I/O, no clock reads
       other than stamping ``received_at``. This is what makes replay-based
       backtests bit-identical to live ingestion.
    #. Cancellation is honoured: ``asyncio.CancelledError`` must propagate.
    """

    #: Stable identifier persisted on every tick and every ledger entry.
    bookmaker: str
    #: Declares how this adapter moves data, for latency budgeting.
    transport: SourceTransport

    def __init__(self, *, bookmaker: str, transport: SourceTransport) -> None:
        self.bookmaker = bookmaker
        self.transport = transport
        self.metrics = AdapterMetrics()
        self._logger = logger.getChild(bookmaker)

    # ------------------------------ lifecycle ------------------------------ #

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish transport (pool, socket, auth handshake). Must be idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release every resource. Must be safe to call twice and after failure."""

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------ streaming ------------------------------ #

    @abc.abstractmethod
    async def stream_live_ticks(
        self,
        *,
        sports: Sequence[str] | None = None,
        markets: Sequence[str] | None = None,
    ) -> AsyncIterator[OddsTick]:
        """Yield normalised ticks continuously until cancelled or closed.

        Args:
            sports: Optional narrowing of the configured sport keys.
            markets: Optional narrowing of the configured market keys.

        Yields:
            Fully validated :class:`OddsTick` instances, newest first-seen order.
        """
        raise NotImplementedError
        yield  # pragma: no cover - marks this as an async generator for typing

    # ---------------------------- normalisation ---------------------------- #

    @abc.abstractmethod
    def normalize_payload(self, payload: RawPayload) -> OddsTick:
        """Convert one raw venue payload into an :class:`OddsTick`.

        Raises:
            PayloadSchemaError: The payload cannot be mapped to the domain model.
        """

    def normalize_batch(self, payloads: Sequence[RawPayload]) -> tuple[OddsTick, ...]:
        """Normalise many payloads, quarantining individual failures.

        A single malformed event must never take down a whole snapshot: we drop
        it, count it, and keep the rest of the book flowing.
        """
        ticks: list[OddsTick] = []
        for payload in payloads:
            try:
                ticks.append(self.normalize_payload(payload))
            except PayloadSchemaError:
                self.metrics.payload_errors += 1
                self._logger.warning("quarantined unparsable payload", exc_info=True)
        return tuple(ticks)

    # ------------------------------- health -------------------------------- #

    async def health_check(self) -> HealthStatus:
        """Default: report connectivity plus counters. Override for a real ping."""
        return HealthStatus(
            bookmaker=self.bookmaker,
            is_connected=await self.is_connected(),
            metrics=self.metrics.snapshot(),
        )

    @abc.abstractmethod
    async def is_connected(self) -> bool:
        """True when the transport is usable right now."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} bookmaker={self.bookmaker!r} transport={self.transport}>"
