"""The Sentinel's event bus (Group 68): every alert in BetDoc goes through here.

Producers (the Hive's flash-crash breaker, the CFO's whale and margin-call checks, Nalanda's ledger
verification, the dependency and liveness monitors, the daily hype forecast, the kill switch) call
``emit_alert`` or, from a hot path, ``emit_alert_soon``, and return at once. One alert takes two
roads from there:

* the Redis stream ``SENTINEL_STREAM`` (``sentinel_alerts``): durable, consumed by the dispatcher
  group (``app.workers.sentinel_dispatcher``), which routes it, debounces it and sends it to
  Telegram, Discord, Twilio and PagerDuty, acknowledging only once that is done;
* the pub/sub channel ``<stream>:live``: fanned out to every open Sentinel tab over the
  ``/ws/sentinel`` WebSocket, undebounced, so the local sirens hear everything the moment it happens.

Emitting never raises and never blocks for long (``_EMIT_TIMEOUT_SECONDS``): an alert that cannot
reach Redis is logged, and the one alert that is *about* Redis being down is delivered directly by
the health monitor instead (``app.workers.sentinel_dispatcher.deliver_direct``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.models.sentinel import Severity

logger = logging.getLogger("betdoc.sentinel")

_EMIT_TIMEOUT_SECONDS = 0.75
_BACKGROUND: set[asyncio.Task[Any]] = set()


class AlertKind(StrEnum):
    FLASH_CRASH = "FLASH_CRASH"  # the Hive's breaker halted every bot
    WHALE_ORDER = "WHALE_ORDER"  # one order at or above SENTINEL_WHALE_STAKE_INR
    MARGIN_CALL = "MARGIN_CALL"  # an account at its drawdown limit, or its exposure past SENTINEL_MARGIN_UTILISATION
    HASH_CHAIN_BROKEN = "HASH_CHAIN_BROKEN"  # Nalanda's settlement chain failed verification
    DEPENDENCY_DOWN = "DEPENDENCY_DOWN"
    DEPENDENCY_RECOVERED = "DEPENDENCY_RECOVERED"
    GARUDA_SILENT = "GARUDA_SILENT"  # the dead man's switch: no ingestion heartbeat for SENTINEL_LIVENESS_TIMEOUT_SECONDS
    GARUDA_RECOVERED = "GARUDA_RECOVERED"
    KILL_SWITCH = "KILL_SWITCH"
    KILL_SWITCH_LIFTED = "KILL_SWITCH_LIFTED"
    MARKET_HYPE = "MARKET_HYPE"  # the 08:00 forecast, when the day is worth it
    DIGEST = "DIGEST"  # the debouncer's summary of held CRITICAL alerts
    TEST = "TEST"  # the Sentinel tab's "send a test alert"


class SentinelAlert(BaseModel):
    """One alert. ``dedupe_key`` names the incident (PagerDuty's dedup key, the UI's grouping);
    ``resolves`` marks the alert that closes it (a dependency back up, Garuda beating again)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    kind: AlertKind
    severity: Severity
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    source: str = Field(min_length=1, max_length=64)
    dedupe_key: str | None = Field(default=None, max_length=200)
    resolves: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SentinelKeys:
    """Every Redis key the Sentinel owns."""

    def __init__(self, settings: Settings) -> None:
        p = self.prefix = settings.SENTINEL_PREFIX
        self.stream = settings.SENTINEL_STREAM
        self.live = f"{settings.SENTINEL_STREAM}:live"
        self.group = f"{p}:dispatchers"
        self.leader = f"{p}:dispatcher:leader"
        self.watchdog = f"{p}:watchdog:leader"
        self.health = f"{p}:health"  # hash: dependency -> its last verdict (JSON)
        self.debounce = f"{p}:debounce"  # hash: channel -> the debouncer's state, for the UI
        self.config_version = f"{p}:config:version"  # bumped on every routing or channel edit: dispatchers reload at once
        self.stats = f"{p}:stats"
        self.halt_snapshot = f"{p}:halt:snapshot"  # what a Telegram /halt overwrote, for /resume
        self.resume_code = f"{p}:telegram:resume"
        self.hype_recent = f"{p}:hype:recent"  # the last few hype lines used (no repeats)
        self.hype_last = f"{p}:hype:last"

    def heartbeat(self, name: str) -> str:
        return f"{self.prefix}:heartbeat:{name}"

    def heartbeat_last(self, name: str) -> str:
        """The last beat, kept without a TTL: how long a silence has lasted survives the beat key expiring."""
        return f"{self.heartbeat(name)}:last"

    def liveness(self, name: str) -> str:
        return f"{self.heartbeat(name)}:state"

    def episode(self, name: str, episode: str) -> str:
        return f"{self.heartbeat(name)}:fired:{episode}"

    def telegram_update(self, update_id: int) -> str:
        return f"{self.prefix}:telegram:update:{update_id}"


def encode(alert: SentinelAlert) -> str:
    return alert.model_dump_json()


def decode(raw: str | bytes) -> SentinelAlert:
    return SentinelAlert.model_validate_json(raw)


def live_frame(alert: SentinelAlert) -> str:
    return json.dumps({"type": "alert", "alert": json.loads(encode(alert))}, separators=(",", ":"))


async def emit_alert(redis: Redis | None, settings: Settings, alert: SentinelAlert) -> bool:
    """Put one alert on the stream and the live channel. True once the stream has it; never raises."""
    if redis is None or not settings.SENTINEL_ENABLED:
        return False
    keys = SentinelKeys(settings)
    try:
        async with asyncio.timeout(_EMIT_TIMEOUT_SECONDS):
            pipe = redis.pipeline(transaction=False)
            pipe.xadd(keys.stream, {"a": encode(alert)}, maxlen=settings.SENTINEL_STREAM_MAXLEN, approximate=True)
            pipe.publish(keys.live, live_frame(alert))
            pipe.hincrby(keys.stats, f"emitted:{alert.severity}", 1)
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        logger.warning("Sentinel: %s alert %s could not reach Redis (%s)", alert.severity, alert.kind, alert.title)
        return False
    log = logger.critical if alert.severity in (Severity.CRITICAL, Severity.FATAL) else logger.info
    log("Sentinel: %s %s: %s", alert.severity, alert.kind, alert.title)
    return True


def emit_alert_soon(redis: Redis | None, settings: Settings, alert: SentinelAlert) -> None:
    """``emit_alert`` from a hot path (an order, a maintenance pass): scheduled, never awaited. A strong
    reference keeps the task alive; outside an event loop the alert is logged and dropped."""
    if redis is None or not settings.SENTINEL_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Sentinel: %s %s raised outside an event loop: %s", alert.severity, alert.kind, alert.title)
        return
    task = loop.create_task(emit_alert(redis, settings, alert))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def publish_live(redis: Redis | None, settings: Settings, event: dict[str, Any]) -> None:
    """A non-alert frame for the Sentinel tab (a digest went out, the debouncer's state, liveness)."""
    if redis is None:
        return
    try:
        async with asyncio.timeout(_EMIT_TIMEOUT_SECONDS):
            await redis.publish(SentinelKeys(settings).live, json.dumps({**event, "at": datetime.now(UTC).isoformat()}, default=str, separators=(",", ":")))
    except (RedisError, OSError, TimeoutError):
        pass


async def recent_alerts(redis: Redis, settings: Settings, count: int = 100) -> list[SentinelAlert]:
    """The newest alerts on the stream (newest first), skipping any entry that does not parse."""
    out: list[SentinelAlert] = []
    for _, fields in await redis.xrevrange(SentinelKeys(settings).stream, count=count):
        try:
            out.append(decode(fields["a"]))
        except (KeyError, ValueError):
            continue
    return out
