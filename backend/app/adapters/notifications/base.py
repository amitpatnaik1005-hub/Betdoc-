"""What every Sentinel dispatcher shares: the outbound message, the delivery result, retries.

A dispatcher turns one ``OutboundMessage`` into its service's API call(s). ``send`` never raises: a
failure comes back as a ``DeliveryResult`` with ``ok=False`` and an error that has been scrubbed of
every credential the dispatcher holds (a bot token sits in Telegram's URL path, a Discord webhook URL
*is* its credential). Transient failures (timeouts, connection errors, 429, 5xx) are retried with
exponential backoff, honouring ``Retry-After`` when the service sends one; 4xx are final.
"""

from __future__ import annotations

import asyncio
import html
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import httpx

from app.models.sentinel import ChannelName, Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert

_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 8.0
EMOJI = {Severity.FATAL: "🚨", Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🔵"}
HYPE_EMOJI = "🚀"
RESOLVED_EMOJI = "✅"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    severity: Severity
    kind: str
    title: str
    body: str
    occurred_at: datetime
    source: str
    dedupe_key: str | None = None
    resolves: bool = False
    alert_id: uuid.UUID | None = None
    batched: int = 0  # a digest: how many alerts it carries
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, alert: SentinelAlert) -> OutboundMessage:
        batched = len(alert.detail.get("alert_ids", ())) if alert.kind is AlertKind.DIGEST else 0
        return cls(alert.severity, str(alert.kind), alert.title, alert.body, alert.occurred_at, alert.source, alert.dedupe_key, alert.resolves, alert.id, batched, dict(alert.detail))

    @property
    def emoji(self) -> str:
        if self.resolves:
            return RESOLVED_EMOJI
        if self.kind == AlertKind.MARKET_HYPE:
            return HYPE_EMOJI
        return EMOJI[self.severity]

    def headline(self) -> str:
        return f"{self.emoji} [{self.severity}] {self.title}" if self.kind != AlertKind.MARKET_HYPE else f"{self.emoji} {self.title}"

    def plain(self, limit: int) -> str:
        text = self.headline() if not self.body else f"{self.headline()}\n{self.body}"
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def html(self, limit: int) -> str:
        """Telegram's HTML subset: bold headline, the body escaped, a muted footer."""
        footer = f"<i>{html.escape(self.kind)} · {self.occurred_at:%Y-%m-%d %H:%M:%S} UTC</i>"
        head = f"<b>{html.escape(self.headline())}</b>"
        body = html.escape(self.body)
        room = limit - len(head) - len(footer) - 2
        if len(body) > room:
            body = body[: max(room - 1, 0)] + "…"
        return "\n".join(part for part in (head, body, footer) if part)


@dataclass(slots=True)
class DeliveryResult:
    channel: ChannelName
    ok: bool
    status_code: int | None = None
    latency_ms: int = 0
    error: str | None = None
    attempts: int = 0
    deliveries: int = 0  # requests that succeeded (one per chat, number or call)


class DispatcherConfigurationError(ValueError):
    """The stored configuration cannot work (a missing secret, a webhook URL on the wrong host)."""


class TransientDeliveryError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class FinalDeliveryError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        try:
            body = response.json()
        except ValueError:
            return None
        value = body.get("retry_after") if isinstance(body, dict) else None  # Discord and Telegram say it in the body
        if value is None and isinstance(body, dict) and isinstance(body.get("parameters"), dict):
            value = body["parameters"].get("retry_after")
    try:
        return max(float(value), 0.0) if value is not None else None
    except (TypeError, ValueError):
        return None


def check(response: httpx.Response) -> httpx.Response:
    """Raise the right error for a non-2xx answer."""
    if response.is_success:
        return response
    if response.status_code in _RETRYABLE:
        raise TransientDeliveryError(f"HTTP {response.status_code}", response.status_code, retry_after(response))
    raise FinalDeliveryError(f"HTTP {response.status_code}: {response.text[:160]}", response.status_code)


class NotificationDispatcher(ABC):
    channel: ClassVar[ChannelName]

    def __init__(self, http: httpx.AsyncClient, *, max_attempts: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.http = http
        self.max_attempts = max_attempts
        self.sleep = sleep

    @abstractmethod
    def secrets(self) -> Iterable[str]:
        """Every credential value this dispatcher holds, for scrubbing error text."""

    @abstractmethod
    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        """Make the API call(s), adding each recipient that succeeded to ``done``; a retry skips those,
        so nobody gets the same alert twice. Raise Transient/FinalDeliveryError on a failure."""

    def redact(self, text: str) -> str:
        for secret in self.secrets():
            if secret:
                text = text.replace(secret, "***")
        return text

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        started = time.perf_counter()
        result = DeliveryResult(self.channel, ok=False)
        delay = 0.5
        done: set[str] = set()
        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            try:
                await self.deliver(message, done)
                result.ok, result.error = True, None
                break
            except TransientDeliveryError as exc:
                result.status_code, result.error = exc.status_code, self.redact(str(exc))
                if attempt < self.max_attempts:
                    await self.sleep(min(exc.retry_after if exc.retry_after is not None else delay, _MAX_BACKOFF_SECONDS))
                    delay *= 2
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                result.status_code, result.error = None, self.redact(f"{type(exc).__name__}: {exc}")
                if attempt < self.max_attempts:
                    await self.sleep(delay)
                    delay *= 2
            except FinalDeliveryError as exc:
                result.status_code, result.error = exc.status_code, self.redact(str(exc))
                break
            except Exception as exc:  # noqa: BLE001 - a dispatcher bug must not take the dispatcher loop down
                result.error = self.redact(f"{type(exc).__name__}: {exc}")
                break
        result.deliveries = len(done)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        if result.error:
            result.error = result.error[:300]
        return result
