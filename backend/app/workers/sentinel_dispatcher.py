"""The Sentinel's dispatcher: from the ``sentinel_alerts`` stream to Telegram, Discord, Twilio, PagerDuty.

One process at a time holds the lease (a Redis key renewed by Lua, like Nalanda's firehose) and
consumes the stream through a consumer group, so each alert is dispatched once however many API
workers run. Per alert: record it (``sentinel_alerts``), look up its channels in the routing matrix,
and per channel either send it now or hand it to the spam debouncer, which holds CRITICAL bursts
and releases them as one digest per window. An entry is acknowledged only when every channel is done
with it: a held alert stays pending until its digest is out, so a dispatcher that dies mid-window
loses nothing (the next leader reclaims it with XAUTOCLAIM and offers it again).

Every attempt is a ``sentinel_deliveries`` row (SENT, FAILED, SKIPPED, BATCHED or DIGEST) and moves
the channel's ``last_success_at`` / ``last_error``. ``deliver_direct`` is the path without Redis: the
health monitor uses it for the one alert the stream cannot carry, "Redis is down".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.notifications.base import DeliveryResult, NotificationDispatcher, OutboundMessage
from app.adapters.notifications.registry import build_all
from app.core.config import Settings
from app.core.security_vault import VaultCrypto
from app.models.sentinel import ChannelName, DeliveryStatus, SentinelAlertLog, SentinelChannel, SentinelDelivery
from app.services.sentinel_bus import SentinelAlert, SentinelKeys, decode, publish_live
from app.services.sentinel_debouncer import Decision, Digest, SpamDebouncer
from app.services.sentinel_routing import channels_for, load_matrix, normalise

logger = logging.getLogger("betdoc.sentinel.dispatcher")

_READ_BLOCK_MS = 1_000  # also the debouncer's release granularity
_CLAIM_IDLE_MS = 60_000
_CONFIG_TTL_SECONDS = 10.0
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


Config = tuple[dict[str, list[str]], dict[ChannelName, NotificationDispatcher], dict[ChannelName, str]]


async def load_config(session_factory: async_sessionmaker[AsyncSession], vault: VaultCrypto | None, http: httpx.AsyncClient, *, strict: bool = False) -> Config:
    """(routing matrix, working dispatchers, why each other channel has none). ``strict`` raises when the
    database cannot be read; otherwise that yields the default routing and no dispatchers."""
    try:
        async with session_factory() as session:
            matrix = await load_matrix(session)
            rows = list((await session.execute(select(SentinelChannel))).scalars())
    except (SQLAlchemyError, OSError):
        if strict:
            raise
        logger.warning("Sentinel: channel configuration unreadable; using the default routing and no dispatchers")
        return normalise(None), {}, {c: "configuration unreadable" for c in ChannelName}
    dispatchers, problems = build_all(rows, vault, http)
    return matrix, dispatchers, problems


async def record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    alerts: Sequence[tuple[SentinelAlert, str | None]] = (),
    deliveries: Sequence[SentinelDelivery] = (),
    outcomes: Sequence[DeliveryResult] = (),
    now: datetime,
) -> None:
    """The alert rows (once each), the delivery rows, and each channel's last success / error."""
    if not alerts and not deliveries and not outcomes:
        return
    try:
        async with session_factory() as session:
            for alert, stream_id in alerts:
                if await session.get(SentinelAlertLog, alert.id) is None:
                    session.add(
                        SentinelAlertLog(
                            id=alert.id, kind=alert.kind, severity=alert.severity, title=alert.title, body=alert.body, source=alert.source,
                            dedupe_key=alert.dedupe_key, detail=alert.detail, occurred_at=alert.occurred_at, stream_id=stream_id, recorded_at=now,
                        )
                    )
            session.add_all(deliveries)
            for outcome in outcomes:
                row = await session.get(SentinelChannel, outcome.channel.value)
                if row is None:
                    continue
                if outcome.ok:
                    row.last_success_at = now
                else:
                    row.last_error, row.last_error_at = (outcome.error or "failed")[:300], now
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Sentinel: the delivery trail could not be written")


def _delivery(alert_id: uuid.UUID | None, channel: ChannelName, status: DeliveryStatus, now: datetime, **fields: Any) -> SentinelDelivery:
    return SentinelDelivery(alert_id=alert_id, channel=channel.value, status=status.value, attempted_at=now, **fields)


def _outcome_row(alert_id: uuid.UUID | None, result: DeliveryResult, now: datetime, **fields: Any) -> SentinelDelivery:
    return _delivery(alert_id, result.channel, DeliveryStatus.SENT if result.ok else DeliveryStatus.FAILED, now, error=result.error, latency_ms=result.latency_ms, **fields)


async def deliver_direct(
    alert: SentinelAlert, session_factory: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None, *, http: httpx.AsyncClient | None = None
) -> list[DeliveryResult]:
    """Route and send one alert without Redis (no stream, no debouncer). For "Redis is down" itself."""
    own = http is None
    client = http or httpx.AsyncClient(timeout=settings.SENTINEL_HTTP_TIMEOUT_SECONDS)
    try:
        matrix, dispatchers, problems = await load_config(session_factory, vault, client)
        now = _utcnow()
        results: list[DeliveryResult] = []
        rows: list[SentinelDelivery] = []
        for channel in channels_for(alert, matrix):
            dispatcher = dispatchers.get(channel)
            if dispatcher is None:
                rows.append(_delivery(alert.id, channel, DeliveryStatus.SKIPPED, now, error=problems.get(channel)))
                continue
            result = await dispatcher.send(OutboundMessage.of(alert))
            results.append(result)
            rows.append(_outcome_row(alert.id, result, now))
        await record(session_factory, alerts=[(alert, None)], deliveries=rows, outcomes=results, now=now)
        return results
    finally:
        if own:
            await client.aclose()


class SentinelDispatcher:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        vault: VaultCrypto | None,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.redis, self.session_factory, self.settings, self.vault, self.clock = redis, session_factory, settings, vault, clock
        self.keys = SentinelKeys(settings)
        self.http = http or httpx.AsyncClient(timeout=settings.SENTINEL_HTTP_TIMEOUT_SECONDS)
        self._own_http = http is None
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.token = uuid.uuid4().hex
        self.debouncer = SpamDebouncer(settings.SENTINEL_DEBOUNCE_SECONDS)
        self._renew = redis.register_script(_RENEW)
        self._holds: Counter[str] = Counter()  # stream entry -> channels still holding its alert
        self._entry_of: dict[uuid.UUID, str] = {}  # held alert -> its stream entry
        self._config: Config | None = None
        self._config_at = float("-inf")
        self._config_version: str | None = None
        self._claimed_at = 0.0
        self.stats: Counter[str] = Counter()

    def invalidate(self) -> None:
        """Re-read the routing matrix and the channels on the next step (after an operator edit)."""
        self._config_at = float("-inf")

    async def config(self) -> Config:
        """Cached for ``_CONFIG_TTL_SECONDS``. If the database cannot be read, the last good config is kept:
        "PostgreSQL is down" still reaches the channels that were configured a moment ago."""
        try:
            version = await self.redis.get(self.keys.config_version)
        except (RedisError, OSError):
            version = self._config_version
        if version != self._config_version:  # an operator edited the routing or a channel: reload now, not in 10s
            self._config_version, self._config_at = version, float("-inf")
        if self._config is None or time.monotonic() - self._config_at > _CONFIG_TTL_SECONDS:
            try:
                self._config = await load_config(self.session_factory, self.vault, self.http, strict=True)
            except (SQLAlchemyError, OSError):
                if self._config is None:
                    self._config = normalise(None), {}, {c: "configuration unreadable" for c in ChannelName}
                logger.warning("Sentinel dispatcher: channel configuration unreadable; keeping the last one")
            self._config_at = time.monotonic()
        return self._config

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.keys.stream, self.keys.group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def lead(self) -> bool:
        lease_ms = _READ_BLOCK_MS * 15
        if await self._renew(keys=[self.keys.leader], args=[self.token, lease_ms]):
            return True
        return bool(await self.redis.set(self.keys.leader, self.token, nx=True, px=lease_ms))

    async def _entries(self) -> list[tuple[str, dict[str, str]]]:
        entries: list[tuple[str, dict[str, str]]] = []
        held = set(self._entry_of.values())
        if time.monotonic() - self._claimed_at > _CLAIM_IDLE_MS / 1000:
            self._claimed_at = time.monotonic()
            claimed = await self.redis.xautoclaim(self.keys.stream, self.keys.group, self.consumer, min_idle_time=_CLAIM_IDLE_MS, start_id="0-0", count=500)
            pending = claimed[1] if isinstance(claimed, list | tuple) and len(claimed) > 1 else []
            entries += [(entry_id, fields) for entry_id, fields in pending if fields and entry_id not in held]
        timeout = self.debouncer.next_due()
        block = _READ_BLOCK_MS if timeout is None else max(min(int((timeout - self.clock()).total_seconds() * 1000), _READ_BLOCK_MS), 1)
        batches = await self.redis.xreadgroup(self.keys.group, self.consumer, {self.keys.stream: ">"}, count=200, block=block)
        for _, items in batches or []:
            entries += list(items)
        return entries

    async def step(self) -> int:
        """Read, route, debounce, send, acknowledge. Returns how many entries it took."""
        if not await self.lead():
            await asyncio.sleep(_READ_BLOCK_MS / 1000)
            return 0
        entries = await self._entries()
        matrix, dispatchers, problems = await self.config()
        now = self.clock()
        alerts: list[tuple[SentinelAlert, str | None]] = []
        rows: list[SentinelDelivery] = []
        outcomes: list[DeliveryResult] = []
        ack: list[str] = []
        for entry_id, fields in entries:
            try:
                alert = decode(fields["a"])
            except (KeyError, ValueError):
                self.stats["malformed"] += 1
                ack.append(entry_id)
                continue
            if alert.id in self._entry_of:  # reclaimed while this process still holds it
                continue
            alerts.append((alert, entry_id))
            holds = 0
            for channel in channels_for(alert, matrix):
                dispatcher = dispatchers.get(channel)
                if dispatcher is None:
                    rows.append(_delivery(alert.id, channel, DeliveryStatus.SKIPPED, now, error=problems.get(channel)))
                    continue
                if self.debouncer.offer(channel.value, alert, now) is Decision.HOLD:
                    holds += 1
                    continue
                result = await dispatcher.send(OutboundMessage.of(alert))
                outcomes.append(result)
                rows.append(_outcome_row(alert.id, result, now))
                self.stats["sent" if result.ok else "failed"] += 1
            if holds:
                self._holds[entry_id] = holds
                self._entry_of[alert.id] = entry_id
            else:
                ack.append(entry_id)
        released, digest_rows, digest_outcomes = await self._release(self.clock(), dispatchers, problems)
        rows += digest_rows
        outcomes += digest_outcomes
        await record(self.session_factory, alerts=alerts, deliveries=rows, outcomes=outcomes, now=now)
        ack += released
        if ack:
            await self.redis.xack(self.keys.stream, self.keys.group, *ack)
        if entries or released:
            await self._publish_state()
        return len(entries)

    async def _release(
        self, now: datetime, dispatchers: dict[ChannelName, NotificationDispatcher], problems: dict[ChannelName, str]
    ) -> tuple[list[str], list[SentinelDelivery], list[DeliveryResult]]:
        """Send every digest whose window has closed; return the entries now free to acknowledge."""
        acks: list[str] = []
        rows: list[SentinelDelivery] = []
        outcomes: list[DeliveryResult] = []
        for digest in self.debouncer.due(now):
            channel = ChannelName(digest.channel)
            rows += await self._send_digest(digest, channel, dispatchers.get(channel), problems, outcomes, now)
            for alert in digest.alerts:
                entry = self._entry_of.pop(alert.id, None)
                if entry is None:
                    continue
                self._holds[entry] -= 1
                if self._holds[entry] <= 0:
                    del self._holds[entry]
                    acks.append(entry)
            await publish_live(self.redis, self.settings, {"type": "digest", "channel": digest.channel, "count": len(digest.alerts), "digest_id": str(digest.id)})
        return acks, rows, outcomes

    async def _send_digest(
        self, digest: Digest, channel: ChannelName, dispatcher: NotificationDispatcher | None, problems: dict[ChannelName, str], outcomes: list[DeliveryResult], now: datetime
    ) -> list[SentinelDelivery]:
        batched = [_delivery(a.id, channel, DeliveryStatus.BATCHED, now, digest_id=digest.id) for a in digest.alerts]
        if dispatcher is None:  # the channel was disabled while the window was open
            return [*batched, _delivery(None, channel, DeliveryStatus.SKIPPED, now, digest_id=digest.id, batched=len(digest.alerts), error=problems.get(channel))]
        result = await dispatcher.send(OutboundMessage.of(digest.as_alert()))
        outcomes.append(result)
        self.stats["digests"] += 1
        status = DeliveryStatus.DIGEST if result.ok else DeliveryStatus.FAILED
        return [*batched, _delivery(None, channel, status, now, digest_id=digest.id, batched=len(digest.alerts), error=result.error, latency_ms=result.latency_ms)]

    async def _publish_state(self) -> None:
        snapshot = self.debouncer.snapshot()
        with contextlib.suppress(RedisError, OSError):
            if snapshot:
                await self.redis.hset(self.keys.debounce, mapping={k: _json(v) for k, v in snapshot.items()})
            await publish_live(self.redis, self.settings, {"type": "debounce", "channels": snapshot})

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.ensure_group()
                while True:
                    await self.step()
                    backoff = 1.0
            except asyncio.CancelledError:
                await self.aclose()
                raise
            except (RedisError, OSError) as exc:
                logger.warning("Sentinel dispatcher: Redis unavailable (%s); retrying in %.0fs", exc, backoff)
            except Exception:
                logger.exception("Sentinel dispatcher: step failed; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


async def run_sentinel_dispatcher(redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None) -> None:
    await SentinelDispatcher(redis, session_factory, settings, vault).run()
