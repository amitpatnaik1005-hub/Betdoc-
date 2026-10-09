"""The Sentinel's watch: dependency health and the dead man's switch.

Dependency health (every ``SENTINEL_HEALTH_INTERVAL_SECONDS``, 30s): Postgres answers ``SELECT 1``,
Redis answers ``PING``, and every enabled bookmaker/odds source in the ingestion fleet is healthy.
The bookmaker check reads the fleet's own record (circuit breaker, last success, failure streak) and
makes no request of its own, so it costs no API quota. A dependency going down raises one CRITICAL
``DEPENDENCY_DOWN`` (re-raised every ``SENTINEL_HEALTH_REMIND_SECONDS`` while it stays down); coming
back raises the INFO ``DEPENDENCY_RECOVERED`` that resolves it. Verdicts move by compare-and-set in
Redis, so the Celery beat task and an API process checking at the same moment raise each alert once.
When Redis itself is down the stream cannot carry the alert: it is delivered directly.

The dead man's switch: Garuda (the ingestion fleet's scheduler, Celery's 5-second tick or the API's
in-process fallback) writes a heartbeat every ``SENTINEL_HEARTBEAT_SECONDS``. Silent for
``SENTINEL_LIVENESS_TIMEOUT_SECONDS`` (60s), whether it beat once and stopped or never beat at all,
means a FATAL ``GARUDA_SILENT``: one per silence, however many monitors notice it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.omni_keys import OmniRedisKeys
from app.models.sentinel import Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert, publish_live

logger = logging.getLogger("betdoc.sentinel.health")

GARUDA = "garuda"
_CAS = """
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old == ARGV[2] then return false end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
if old then return old end
return ''
"""


async def _cas(redis: Redis, key: str, name: str, value: str) -> str | None:
    """Set ``name`` to ``value``; the previous value ('' when there was none), or None if unchanged."""
    result = await redis.eval(_CAS, 1, key, name, value)
    if result is None:
        return None
    return result.decode() if isinstance(result, bytes) else str(result)


# ================================================================ the heartbeat
async def beat(redis: Redis | None, settings: Settings, *, runner: str, name: str = GARUDA, at: float | None = None) -> bool:
    """Garuda is alive. Called by whatever runs the ingestion schedule; never raises. ``at``: the
    beat's unix time (now; tests pass their own clock)."""
    if redis is None:
        return False
    keys = SentinelKeys(settings)
    payload = json.dumps({"at": time.time() if at is None else at, "runner": runner, "host": socket.gethostname(), "pid": os.getpid()}, separators=(",", ":"))
    try:
        async with asyncio.timeout(1.0):
            pipe = redis.pipeline(transaction=False)
            pipe.set(keys.heartbeat(name), payload, ex=max(int(settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS * 2), 10))
            pipe.set(keys.heartbeat_last(name), payload)
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        return False
    return True


def beat_sync(redis: Any, settings: Settings, *, runner: str, name: str = GARUDA) -> bool:
    """``beat`` for a synchronous Redis client (Celery's workers)."""
    keys = SentinelKeys(settings)
    payload = json.dumps({"at": time.time(), "runner": runner, "host": socket.gethostname(), "pid": os.getpid()}, separators=(",", ":"))
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.set(keys.heartbeat(name), payload, ex=max(int(settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS * 2), 10))
        pipe.set(keys.heartbeat_last(name), payload)
        pipe.execute()
    except (RedisError, OSError):
        return False
    return True


@dataclass(slots=True)
class LivenessReport:
    name: str
    status: Literal["ALIVE", "SILENT", "UNKNOWN"]
    last_beat_at: datetime | None
    silent_seconds: float | None
    runner: str | None = None
    fired: SentinelAlert | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "last_beat_at": self.last_beat_at.isoformat() if self.last_beat_at else None,
            "silent_seconds": None if self.silent_seconds is None else round(self.silent_seconds, 1),
            "runner": self.runner,
        }


Emit = Callable[[Redis | None, Settings, SentinelAlert], Awaitable[bool]]


class LivenessMonitor:
    """The dead man's switch for one heartbeat."""

    def __init__(self, redis: Redis, settings: Settings, *, name: str = GARUDA, clock: Callable[[], float] = time.time, emit: Emit = emit_alert) -> None:
        self.redis, self.settings, self.name, self.clock, self.emit = redis, settings, name, clock, emit
        self.keys = SentinelKeys(settings)

    async def check(self) -> LivenessReport:
        now = self.clock()
        state_key = self.keys.liveness(self.name)
        try:
            raw = await self.redis.get(self.keys.heartbeat_last(self.name))
            missing_since = await self.redis.hget(state_key, "missing_since")
        except (RedisError, OSError):
            return LivenessReport(self.name, "UNKNOWN", None, None)  # the dependency monitor reports Redis itself
        last: dict[str, Any] | None = None
        if raw is not None:
            try:
                last = json.loads(raw)
                float(last["at"])
            except (ValueError, KeyError, TypeError):
                last = None
        if last is None:
            if missing_since is None:
                await self.redis.hsetnx(state_key, "missing_since", f"{now:.3f}")
                missing_since = f"{now:.3f}"
            since = float(missing_since)
            silent, last_at, runner, episode = now - since, None, None, f"never-{since:.3f}"
        else:
            at = float(last["at"])
            silent, last_at, runner, episode = now - at, datetime.fromtimestamp(at, UTC), last.get("runner"), f"{at:.3f}"
        report = LivenessReport(self.name, "ALIVE", last_at, max(silent, 0.0), runner)
        timeout = self.settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS
        if silent >= timeout:
            report.status = "SILENT"
            previous = await _cas(self.redis, state_key, "status", "SILENT")
            claimed = await self.redis.set(self.keys.episode(self.name, episode), "1", nx=True, ex=86_400)
            if previous is not None and claimed:
                report.fired = self._silent_alert(silent, last_at, runner)
                await self.emit(self.redis, self.settings, report.fired)
        else:
            previous = await _cas(self.redis, state_key, "status", "ALIVE")
            if missing_since is not None and last is not None:
                await self.redis.hdel(state_key, "missing_since")
            if previous == "SILENT":
                report.fired = SentinelAlert(
                    kind=AlertKind.GARUDA_RECOVERED,
                    severity=Severity.INFO,
                    title=f"{self.name.title()} is beating again",
                    body=f"Heartbeat from {runner or 'an unknown runner'}; the ingestion schedule is running.",
                    source="sentinel.liveness",
                    dedupe_key=f"liveness:{self.name}",
                    resolves=True,
                    detail={"runner": runner},
                )
                await self.emit(self.redis, self.settings, report.fired)
        return report

    def _silent_alert(self, silent: float, last_at: datetime | None, runner: str | None) -> SentinelAlert:
        if last_at is None:
            body = f"No heartbeat has been seen for {silent:.0f}s: neither a Celery worker nor the API's in-process fallback is running the ingestion schedule. Prices are going stale."
        else:
            body = f"The last heartbeat came from {runner or 'an unknown runner'} at {last_at:%H:%M:%S} UTC, {silent:.0f}s ago. Odds ingestion has stopped; every live price is going stale."
        return SentinelAlert(
            kind=AlertKind.GARUDA_SILENT,
            severity=Severity.FATAL,
            title=f"Dead man's switch: {self.name.title()} has been silent for {silent:.0f}s",
            body=body,
            source="sentinel.liveness",
            dedupe_key=f"liveness:{self.name}",
            detail={"silent_seconds": round(silent, 1), "last_beat_at": last_at.isoformat() if last_at else None, "runner": runner, "timeout_seconds": self.settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS},
        )


# ================================================================ dependencies
@dataclass(slots=True)
class DependencyCheck:
    name: str  # "postgres", "redis", "bookmaker:<source id>"
    kind: Literal["postgres", "redis", "bookmaker"]
    ok: bool
    latency_ms: float | None
    detail: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["checked_at"] = self.checked_at.isoformat()
        return out


async def probe_postgres(session_factory: async_sessionmaker[AsyncSession], timeout: float) -> DependencyCheck:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout), session_factory() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        return DependencyCheck("postgres", "postgres", False, None, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else 'no answer'}")
    return DependencyCheck("postgres", "postgres", True, round((time.perf_counter() - started) * 1000, 1), "SELECT 1 answered")


async def probe_redis(redis: Redis | None, timeout: float) -> DependencyCheck:
    if redis is None:
        return DependencyCheck("redis", "redis", False, None, "no Redis client configured")
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            await redis.ping()
    except (RedisError, OSError, TimeoutError) as exc:
        return DependencyCheck("redis", "redis", False, None, f"{type(exc).__name__}: {str(exc)[:160] or 'no answer'}")
    return DependencyCheck("redis", "redis", True, round((time.perf_counter() - started) * 1000, 1), "PING answered")


async def probe_bookmakers(session_factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, timeout: float, now: float) -> list[DependencyCheck]:
    """Every enabled source of the ingestion fleet, judged from the fleet's own record: zero quota."""
    from app.services.omni_fleet import schedule_state  # noqa: PLC0415 - the fleet module is heavy; only the watch needs it
    from app.services.omni_normalizer import default_alias_dictionary  # noqa: PLC0415
    from app.services.omni_router import load_registry  # noqa: PLC0415

    try:
        async with asyncio.timeout(timeout * 2):
            async with session_factory() as session:
                registry, rows = await load_registry(session, settings, default_alias_dictionary())
            enabled = [sid for sid, descriptor in registry.items() if schedule_state(rows.get(sid), descriptor, settings) == "enabled"]
            keys = OmniRedisKeys(settings.omni_redis_prefix)
            pipe = redis.pipeline(transaction=False)
            for sid in enabled:
                pipe.hgetall(keys.fleet_metrics(sid))
                pipe.pttl(keys.breaker_open(sid))
            raw = await pipe.execute() if enabled else []
    except (SQLAlchemyError, RedisError, OSError, TimeoutError):
        return []  # Postgres or Redis is down: their own checks say so
    checks = []
    for index, sid in enumerate(enabled):
        metrics, ttl_ms = raw[2 * index] or {}, raw[2 * index + 1]
        checks.append(judge_source(sid, registry[sid].interval_seconds, metrics, ttl_ms if isinstance(ttl_ms, int) else -2, settings, now))
    return checks


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def judge_source(source_id: str, interval: float, metrics: dict[str, Any], breaker_ttl_ms: int, settings: Settings, now: float) -> DependencyCheck:
    """Down when its circuit breaker is open, or nothing has succeeded for ``SENTINEL_SOURCE_STALE_INTERVALS``
    intervals while it keeps failing. A source that has not run yet is not down."""
    name = f"bookmaker:{source_id}"
    failures = int(_num(metrics.get("consecutive_failures")) or 0)
    last_success = _num(metrics.get("last_success_at"))
    error = str(metrics.get("last_error") or "")[:120]
    latency = _num(metrics.get("latency_ms"))
    if breaker_ttl_ms > 0:
        return DependencyCheck(name, "bookmaker", False, latency, f"circuit open for {breaker_ttl_ms / 1000:.0f}s more after {failures} failure(s){': ' + error if error else ''}")
    stale_after = interval * settings.SENTINEL_SOURCE_STALE_INTERVALS
    if failures and last_success is not None and now - last_success > stale_after:
        return DependencyCheck(name, "bookmaker", False, latency, f"no success for {(now - last_success) / 60:.0f} min ({failures} failure(s) in a row){': ' + error if error else ''}")
    if failures >= 3 and last_success is None:
        return DependencyCheck(name, "bookmaker", False, latency, f"never succeeded; {failures} failure(s) in a row{': ' + error if error else ''}")
    detail = f"last success {(now - last_success):.0f}s ago" if last_success is not None else "not polled yet"
    return DependencyCheck(name, "bookmaker", True, latency, detail)


@dataclass(slots=True)
class HealthReport:
    checks: list[DependencyCheck]
    fired: list[SentinelAlert]
    redis_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {"checks": [c.as_dict() for c in self.checks], "fired": [a.kind for a in self.fired], "redis_ok": self.redis_ok}


Deliver = Callable[[SentinelAlert], Awaitable[Any]]


class HealthMonitor:
    def __init__(
        self,
        redis: Redis | None,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        deliver_without_redis: Deliver | None = None,
        clock: Callable[[], float] = time.time,
        emit: Emit = emit_alert,
    ) -> None:
        self.redis, self.session_factory, self.settings, self.clock, self.emit = redis, session_factory, settings, clock, emit
        self.deliver_without_redis = deliver_without_redis
        self.keys = SentinelKeys(settings)
        self._local: dict[str, str] = {}  # verdicts while Redis is down (per process)

    async def probe(self) -> list[DependencyCheck]:
        timeout = self.settings.SENTINEL_HEALTH_TIMEOUT_SECONDS
        postgres, redis = await asyncio.gather(probe_postgres(self.session_factory, timeout), probe_redis(self.redis, timeout))
        checks = [postgres, redis]
        if redis.ok and self.redis is not None:
            checks += await probe_bookmakers(self.session_factory, self.redis, self.settings, timeout, self.clock())
        return checks

    async def check(self) -> HealthReport:
        checks = await self.probe()
        redis_ok = next(c.ok for c in checks if c.name == "redis")
        fired: list[SentinelAlert] = []
        for check in checks:
            alert = await self._judge(check, redis_ok)
            if alert is not None:
                fired.append(alert)
        if redis_ok and self.redis is not None:
            try:
                await self.redis.hset(self.keys.health, mapping={c.name: json.dumps(c.as_dict(), separators=(",", ":")) for c in checks})
                known = await self.redis.hkeys(self.keys.health)
                gone = [k for k in known if k.startswith("bookmaker:") and k not in {c.name for c in checks}]
                if gone:  # a source disabled since: no longer watched
                    await self.redis.hdel(self.keys.health, *gone)
                    await self.redis.hdel(f"{self.keys.health}:status", *gone)
            except (RedisError, OSError):
                pass
            await publish_live(self.redis, self.settings, {"type": "health", "checks": [c.as_dict() for c in checks]})
        for alert in fired:
            if redis_ok and await self.emit(self.redis, self.settings, alert):
                continue
            if self.deliver_without_redis is not None:  # the stream is unreachable: straight to the channels
                await self.deliver_without_redis(alert)
        return HealthReport(checks, fired, redis_ok)

    async def _judge(self, check: DependencyCheck, redis_ok: bool) -> SentinelAlert | None:
        verdict = "UP" if check.ok else "DOWN"
        if redis_ok and self.redis is not None:
            try:
                previous = await _cas(self.redis, f"{self.keys.health}:status", check.name, verdict)
                remind = False
                if previous is None and verdict == "DOWN":
                    remind = bool(await self.redis.set(f"{self.keys.health}:remind:{check.name}", "1", nx=True, ex=int(self.settings.SENTINEL_HEALTH_REMIND_SECONDS)))
                elif previous is not None and verdict == "DOWN":
                    await self.redis.set(f"{self.keys.health}:remind:{check.name}", "1", ex=int(self.settings.SENTINEL_HEALTH_REMIND_SECONDS))
            except (RedisError, OSError):
                previous, remind = self._local_transition(check.name, verdict), False
        else:
            previous, remind = self._local_transition(check.name, verdict), False
        if verdict == "DOWN" and (previous is not None or remind):
            return SentinelAlert(
                kind=AlertKind.DEPENDENCY_DOWN,
                severity=Severity.CRITICAL,
                title=f"{_label(check)} is down" if not remind else f"{_label(check)} is still down",
                body=check.detail,
                source="sentinel.health",
                dedupe_key=f"dependency:{check.name}",
                detail=check.as_dict(),
            )
        if verdict == "UP" and previous == "DOWN":
            return SentinelAlert(
                kind=AlertKind.DEPENDENCY_RECOVERED,
                severity=Severity.INFO,
                title=f"{_label(check)} is back",
                body=check.detail,
                source="sentinel.health",
                dedupe_key=f"dependency:{check.name}",
                resolves=True,
                detail=check.as_dict(),
            )
        return None

    def _local_transition(self, name: str, verdict: str) -> str | None:
        previous = self._local.get(name, "")
        if previous == verdict:
            return None
        self._local[name] = verdict
        return previous


def _label(check: DependencyCheck) -> str:
    if check.kind == "bookmaker":
        return f"Bookmaker API {check.name.split(':', 1)[1]}"
    return {"postgres": "PostgreSQL", "redis": "Redis"}[check.kind]


async def read_health(redis: Redis, settings: Settings) -> list[dict[str, Any]]:
    out = []
    for value in (await redis.hgetall(SentinelKeys(settings).health)).values():
        try:
            out.append(json.loads(value))
        except ValueError:
            continue
    return sorted(out, key=lambda c: (c.get("kind") != "postgres", c.get("kind") != "redis", c.get("name", "")))


def summarise(checks: Sequence[DependencyCheck]) -> str:
    down = [c.name for c in checks if not c.ok]
    return "all dependencies up" if not down else f"down: {', '.join(down)}"
