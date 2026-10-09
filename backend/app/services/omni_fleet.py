"""Omni ingestion fleet: route, throttle, run and heal every data source.

One run of a source::

    redis.asyncio Lock on the source id (no overlapping runs anywhere in the cluster)
      -> config + API key (vault, else the environment via ProviderSecrets)
      -> adapter.fetch(scope)   each request waits for a token from the provider's bucket
      -> OmniNormalizer (canonical ids, Shin/multiplicative true probability)
      -> board merge across sources -> Redis pub/sub (/ws/live-odds) + quorum buffer
      -> odds_snapshots rows (The Odds API, for the Arena/Oracle/backtests)
      -> health: latency, success window, quota; breaker closed

Self-healing:

* **Circuit breaker** per source: every failure (5xx, timeouts, 429 after backoff, schema drift)
  opens it for ``base x 2^(n-1)`` seconds; the first run after the cooldown is the half-open trial.
  ``OMNI_FLEET_FAILURE_THRESHOLD`` consecutive failures still dead-letter it (FATAL, paused).
* **Quota-aware failover** (``app.services.omni_router``): a metered source under
  ``OMNI_FLEET_QUOTA_RESERVE`` hands its market groups to the next source by priority; a free
  probe rechecks it and it takes them back after the reset.
* **Redis outage**: locks, buckets and breakers fall back to in-process state, results go to the
  local spool (``app.services.omni_spool``) and flush on recovery; API-process runs still reach
  their own sockets. Workers never crash for want of Redis.

The beat tick (Celery) and the API's in-process fallback both call ``fleet_tick``; the lock and
interval gate make it safe for both to fire at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.ingestion import INGESTORS, BaseDataIngestor
from app.adapters.ingestion.base import IngestionBatch, IngestionError, MissingApiKeyError, SchemaDriftError, ThrottledError
from app.adapters.ingestion.factory import SpecMapper, UniversalDataIngestor
from app.core.config import Settings
from app.core.events import publish_event
from app.core.live_odds import decode_ticks, live_odds_keys, publish_board_ticks
from app.core.omni_keys import OmniRedisKeys
from app.core.provider_secrets import provider_secret
from app.core.security_vault import VaultCrypto, VaultDecryptionError
from app.models.canonical import CanonicalEntity
from app.models.omni_vault import OmniFleetSource
from app.schemas.market import MarketTick
from app.services.aryabhata_pipeline import publish_market_quotes
from app.services.odds_poller import store_snapshots
from app.services.omni_normalizer import (
    AliasDictionary,
    NormalizationReport,
    OmniNormalizer,
    QuorumConsensusEngine,
    QuorumPolicy,
    default_alias_dictionary,
    merge_board_tick,
    tick_to_event,
)
from app.services.omni_quorum_buffer import buffer_events
from app.services.omni_router import Availability, GroupStatus, PlanEntry, SourceDescriptor, compute_plan, config_descriptor, load_registry
from app.services.omni_spool import SPOOL
from app.services.omni_throttle import TokenBucket
from app.services.sentinel_health import beat

logger = logging.getLogger("betdoc.omni.fleet")

Runner = Literal["celery", "inprocess", "manual"]
RunStatus = Literal["ok", "skipped", "failed", "fatal"]
ScheduleState = Literal["enabled", "disabled", "paused", "needs_key"]
Action = Literal["ingest", "probe"]

# Built-in sources whose key may also come from a named Settings field (the pre-fleet configuration)
ENV_KEY_FALLBACK: dict[str, str] = {"odds_api": "ODDS_API_KEY"}
_UNMAPPED_SHOWN = 25
_background: set[asyncio.Task[Any]] = set()
_entities_synced = False

# In-process stand-ins while Redis is unreachable
_local_locks: dict[str, threading.Lock] = {}
_local_breakers: dict[str, float] = {}  # source id -> monotonic time the breaker closes
_local_claims: dict[str, float] = {}
_local_guard = threading.Lock()


class TickSink(Protocol):
    async def broadcast_market_ticks(self, ticks: Sequence[MarketTick]) -> None: ...


@dataclass(slots=True)
class RunSummary:
    source_id: str
    status: RunStatus
    runner: str
    reason: str | None = None
    ticks: int = 0
    fixtures: int = 0
    latency_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FleetDeps:
    redis: Redis
    session_factory: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    vault: VaultCrypto | None
    settings: Settings
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # backoff/throttle waits (tests pass a recorder)
    local_sink: TickSink | None = None  # the API process's own sockets: still fed when Redis is down
    aliases: AliasDictionary = field(default_factory=default_alias_dictionary)


@dataclass(slots=True)
class SourceState:
    """Live state of one source as the router and Fleet Command see it."""

    availability: Availability
    schedule: ScheduleState
    breaker: Literal["closed", "open", "half_open"]
    breaker_remaining_seconds: float | None
    quota_fraction: float | None
    metrics: dict[str, str]
    runs: list[str]
    has_key: bool
    key_origin: str | None


def fleet_keys(settings: Settings) -> OmniRedisKeys:
    return OmniRedisKeys(settings.omni_redis_prefix)


def is_due(last_attempt: float | None, interval: float, state: str | None, now: float) -> bool:
    """One second of slack absorbs scheduler jitter."""
    if state in ("disabled", "paused"):
        return False
    return last_attempt is None or now - last_attempt >= interval - 1.0


def _float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _redact(message: str, secret: str | None) -> str:
    return message.replace(secret, "***") if secret else message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    """Drivers without timezone support (SQLite) hand back naive datetimes; they were stored as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# ---------------------------------------------------------------- config rows & keys
async def get_or_create_source(session: AsyncSession, source_id: str) -> OmniFleetSource:
    row = await session.get(OmniFleetSource, source_id)
    if row is not None:
        return row
    session.add(OmniFleetSource(source_id=source_id, is_enabled=True, consecutive_failures=0))
    try:
        await session.commit()
    except IntegrityError:  # another worker created it first
        await session.rollback()
    row = await session.get(OmniFleetSource, source_id)
    if row is None:
        raise SQLAlchemyError(f"Fleet source row {source_id} could not be created")
    return row


def _env_secret_name(descriptor: SourceDescriptor) -> str | None:
    if descriptor.spec is not None:
        return descriptor.spec.secret_env(descriptor.source_id)
    return None


def key_origin(row: OmniFleetSource | None, descriptor: SourceDescriptor, settings: Settings) -> str | None:
    """Where the key would come from, without decrypting it (for display and scheduling)."""
    if row is not None and row.encrypted_api_key:
        return "vault"
    field_name = ENV_KEY_FALLBACK.get(descriptor.source_id)
    if field_name:
        value = getattr(settings, field_name, None)
        if value is not None and value.get_secret_value():
            return "environment"
    env_name = _env_secret_name(descriptor)
    if env_name and provider_secret(env_name) is not None:
        return "environment"
    return None


def resolve_api_key(row: OmniFleetSource | None, descriptor: SourceDescriptor, settings: Settings, vault: VaultCrypto | None) -> str | None:
    """Fleet Command's vault key wins; then the environment (a Settings field or the spec's variable)."""
    if row is not None and row.encrypted_api_key and vault is not None:
        return vault.decrypt_key(row.encrypted_api_key)
    field_name = ENV_KEY_FALLBACK.get(descriptor.source_id)
    if field_name:
        value = getattr(settings, field_name, None)
        if value is not None and value.get_secret_value():
            return str(value.get_secret_value())
    env_name = _env_secret_name(descriptor)
    secret = provider_secret(env_name) if env_name else None
    return secret.get_secret_value() if secret is not None else None


def schedule_state(row: OmniFleetSource | None, descriptor: SourceDescriptor, settings: Settings) -> ScheduleState:
    if row is not None and not row.is_enabled:
        return "disabled"
    if row is not None and row.paused_at is not None:
        return "paused"
    if descriptor.requires_api_key and key_origin(row, descriptor, settings) is None:
        return "needs_key"
    return "enabled"


async def publish_schedule(redis: Redis, row: OmniFleetSource, descriptor: SourceDescriptor, settings: Settings) -> None:
    """Mirror state and cadence into Redis so the beat tick never needs the DB to decide what's due."""
    mapping = {"state": schedule_state(row, descriptor, settings), "interval_seconds": descriptor.interval_seconds}
    try:
        await redis.hset(fleet_keys(settings).fleet_metrics(row.source_id), mapping=mapping)
    except (RedisError, OSError):
        SPOOL.metrics(row.source_id, mapping)


async def sync_canonical_entities(session: AsyncSession, aliases: AliasDictionary) -> int:
    """Upsert the alias dictionary into canonical_entities (ids never change; names/aliases may)."""
    existing = {row.id: row for row in (await session.execute(select(CanonicalEntity))).scalars()}
    changed = 0
    for ref in aliases.entities:
        row = existing.get(ref.id)
        if row is None:
            session.add(CanonicalEntity(id=ref.id, sport_key=ref.sport, kind=ref.kind, canonical_name=ref.name, aliases=list(ref.aliases)))
            changed += 1
        elif row.canonical_name != ref.name or list(row.aliases or []) != list(ref.aliases) or row.sport_key != ref.sport:
            row.canonical_name, row.aliases, row.sport_key = ref.name, list(ref.aliases), ref.sport
            changed += 1
    if changed:
        await session.commit()
    return changed


# ---------------------------------------------------------------- breaker
def breaker_cooldown(failures: int, settings: Settings) -> float:
    return min(settings.OMNI_FLEET_BREAKER_BASE_SECONDS * (2 ** max(failures - 1, 0)), settings.OMNI_FLEET_BREAKER_MAX_SECONDS)


async def _breaker_remaining(deps: FleetDeps, source_id: str) -> float | None:
    """Seconds until the breaker half-opens, or None when it is closed."""
    try:
        ttl_ms = await deps.redis.pttl(fleet_keys(deps.settings).breaker_open(source_id))
        return ttl_ms / 1000.0 if isinstance(ttl_ms, int) and ttl_ms > 0 else None
    except (RedisError, OSError):
        until = _local_breakers.get(source_id)
        remaining = until - time.monotonic() if until else 0.0
        return remaining if remaining > 0 else None


async def _trip_breaker(deps: FleetDeps, source_id: str, failures: int) -> float:
    cooldown = breaker_cooldown(failures, deps.settings)
    _local_breakers[source_id] = time.monotonic() + cooldown
    keys = fleet_keys(deps.settings)
    with contextlib.suppress(RedisError, OSError):
        pipe = deps.redis.pipeline(transaction=False)
        pipe.set(keys.breaker_open(source_id), "1", px=max(int(cooldown * 1000), 1))
        pipe.set(keys.breaker_half_open(source_id), "1")
        await pipe.execute()
    return cooldown


async def _close_breaker(deps: FleetDeps, source_id: str) -> None:
    _local_breakers.pop(source_id, None)
    keys = fleet_keys(deps.settings)
    with contextlib.suppress(RedisError, OSError):
        await deps.redis.delete(keys.breaker_open(source_id), keys.breaker_half_open(source_id))


async def reset_breaker(deps: FleetDeps, source_id: str) -> None:
    """Operator acknowledgement (re-enable, new key, new spec): close the breaker and zero the streak."""
    await _close_breaker(deps, source_id)
    await _metrics(deps, source_id, {"consecutive_failures": 0})


# ---------------------------------------------------------------- state for the router / UI
async def read_states(
    deps: FleetDeps, registry: dict[str, SourceDescriptor], rows: dict[str, OmniFleetSource]
) -> tuple[dict[str, SourceState], bool]:
    """(state per source, redis_available). Never raises for a Redis outage."""
    keys = fleet_keys(deps.settings)
    ids = list(registry)
    redis_ok = True
    try:
        pipe = deps.redis.pipeline(transaction=False)
        for sid in ids:
            pipe.hgetall(keys.fleet_metrics(sid))
            pipe.lrange(keys.fleet_runs(sid), 0, -1)
            pipe.pttl(keys.breaker_open(sid))
            pipe.exists(keys.breaker_half_open(sid))
        raw: list[Any] = await pipe.execute()
    except (RedisError, OSError):
        redis_ok = False
        raw = []
    states: dict[str, SourceState] = {}
    for index, sid in enumerate(ids):
        descriptor, row = registry[sid], rows.get(sid)
        if redis_ok:
            metrics, runs, ttl_ms, half_open = raw[4 * index : 4 * index + 4]
            metrics, runs = metrics or {}, runs or []
            remaining = ttl_ms / 1000.0 if isinstance(ttl_ms, int) and ttl_ms > 0 else None
        else:
            metrics, runs, half_open = {}, [], False
            until = _local_breakers.get(sid)
            remaining = (until - time.monotonic()) if until and until > time.monotonic() else None
        breaker = "open" if remaining else ("half_open" if half_open else "closed")
        fraction = _float(metrics.get("quota_fraction"))
        schedule = schedule_state(row, descriptor, deps.settings)
        availability: Availability
        if schedule != "enabled":
            availability = schedule
        elif remaining:
            availability = "circuit_open"
        elif descriptor.cost == "metered" and fraction is not None and fraction < deps.settings.OMNI_FLEET_QUOTA_RESERVE:
            availability = "quota_reserve"
        else:
            availability = "available"
        origin = key_origin(row, descriptor, deps.settings)
        states[sid] = SourceState(availability, schedule, breaker, remaining, fraction, metrics, runs, origin is not None, origin)
    return states, redis_ok


async def fleet_plan(deps: FleetDeps) -> tuple[dict[str, SourceDescriptor], dict[str, OmniFleetSource], dict[str, SourceState], dict[str, PlanEntry], dict[str, GroupStatus], bool]:
    async with deps.session_factory() as session:
        registry, rows = await load_registry(session, deps.settings, deps.aliases)
    states, redis_ok = await read_states(deps, registry, rows)
    plan, groups = compute_plan(registry, {sid: s.availability for sid, s in states.items()}, deps.settings.OMNI_FLEET_REDUNDANCY)
    return registry, rows, states, plan, groups, redis_ok


# ---------------------------------------------------------------- publishing
async def publish_fleet_ticks(deps: FleetDeps, ticks: Sequence[MarketTick]) -> list[MarketTick]:
    """Store each source's tick, merge every source's view of each cell, publish the merged board.

    Redis down: the batch's own ticks are spooled for later and, in the API process, broadcast to
    this worker's sockets straight away.
    """
    if not ticks:
        return []
    settings = deps.settings
    engine = QuorumConsensusEngine(QuorumPolicy.from_settings(settings))
    keys = live_odds_keys()
    try:
        pipe = deps.redis.pipeline(transaction=False)
        for tick in ticks:
            pipe.hset(keys.sources(tick.board_key), tick.source or "unknown", tick.model_dump_json(by_alias=True))
            pipe.expire(keys.sources(tick.board_key), settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS)
        await pipe.execute()
        pipe = deps.redis.pipeline(transaction=False)
        for tick in ticks:
            pipe.hgetall(keys.sources(tick.board_key))
        views: list[dict[str, str]] = await pipe.execute()
        merged = [
            merge_board_tick([t for raw in view.values() for t in decode_ticks(raw)] or [tick], engine)
            for tick, view in zip(ticks, views, strict=True)
        ]
        if not await publish_board_ticks(deps.redis, merged):
            raise RedisError("live-odds publish failed")
        return merged
    except (RedisError, OSError):
        merged = [merge_board_tick([tick], engine) for tick in ticks]
        SPOOL.ticks(list(ticks), merged)
        if deps.local_sink is not None:
            await deps.local_sink.broadcast_market_ticks(merged)
        logger.warning("Redis unavailable: %d tick(s) spooled for later", len(ticks))
        return merged


async def _metrics(deps: FleetDeps, source_id: str, mapping: dict[str, Any], run: str | None = None) -> None:
    keys = fleet_keys(deps.settings)
    try:
        pipe = deps.redis.pipeline(transaction=False)
        pipe.hset(keys.fleet_metrics(source_id), mapping=mapping)
        if run is not None:
            pipe.lpush(keys.fleet_runs(source_id), run)
            pipe.ltrim(keys.fleet_runs(source_id), 0, deps.settings.OMNI_FLEET_SUCCESS_WINDOW - 1)
        await pipe.execute()
    except (RedisError, OSError):
        SPOOL.metrics(source_id, mapping)


async def _announce(deps: FleetDeps, descriptor: SourceDescriptor, batch: IngestionBatch, report: NormalizationReport) -> None:
    """One summary per run on the Omni live channel (/omni/ws/stream): the Wire's ingestion feed."""
    message = {
        "kind": "omni.fleet.batch",
        "topic": f"fleet.{batch.source_id}",
        "provider_name": descriptor.display_name,
        "source_id": batch.source_id,
        "payload": {
            "fixtures": report.events_normalized,
            "ticks": len(report.ticks),
            "latency_ms": batch.latency_ms,
            "scope": [p.key for p in batch.payloads],
        },
        "at": batch.fetched_at.isoformat(),
    }
    with contextlib.suppress(RedisError, OSError):
        await deps.redis.publish(deps.settings.omni_live_channel, json.dumps(message, separators=(",", ":")))


def _quota_mapping(batch: IngestionBatch, now: datetime) -> dict[str, Any]:
    if batch.quota_remaining is None:
        return {}
    fraction = batch.quota_fraction
    return {
        "quota_remaining": batch.quota_remaining,
        "quota_used": "" if batch.quota_used is None else batch.quota_used,
        "quota_limit": "" if batch.quota_limit is None else batch.quota_limit,
        "quota_fraction": "" if fraction is None else round(fraction, 6),
        "quota_checked_at": now.timestamp(),
    }


async def _quota_watch(deps: FleetDeps, descriptor: SourceDescriptor, batch: IngestionBatch) -> None:
    fraction = batch.quota_fraction
    if descriptor.cost == "metered" and fraction is not None and fraction < deps.settings.OMNI_FLEET_QUOTA_RESERVE:
        logger.warning(
            "Fleet %s quota at %.1f%% (reserve %.0f%%): its market groups fail over until the quota resets",
            descriptor.source_id, fraction * 100, deps.settings.OMNI_FLEET_QUOTA_RESERVE * 100,
        )
        await publish_event(deps.redis, {"type": "fleet", "source": descriptor.source_id, "status": "quota_reserve"})


# ---------------------------------------------------------------- bookkeeping
async def _record_success(deps: FleetDeps, descriptor: SourceDescriptor, summary: RunSummary, batch: IngestionBatch, report: NormalizationReport) -> RunSummary:
    now = _utcnow()
    source_id = descriptor.source_id
    async with deps.session_factory() as session:
        row = await session.get(OmniFleetSource, source_id)
        if row is not None:
            row.consecutive_failures, row.last_error, row.last_success_at = 0, None, now
            await session.commit()
    await _close_breaker(deps, source_id)
    await _metrics(
        deps,
        source_id,
        {
            "last_status": "ok",
            "last_error": "",
            "last_success_at": now.timestamp(),
            "latency_ms": batch.latency_ms,
            "requests": batch.requests,
            "retries": batch.retries,
            "throttled_ms": batch.throttled_ms,
            "ticks": len(report.ticks),
            "fixtures": report.events_normalized,
            "fixtures_seen": report.events_seen,
            "malformed": report.malformed,
            "unmapped": json.dumps(sorted(report.unmapped)[:_UNMAPPED_SHOWN]),
            "unmapped_count": len(report.unmapped),
            "devig": json.dumps(dict(report.devig_methods)),
            "scope": json.dumps([p.key for p in batch.payloads]),
            "runner": summary.runner,
            "consecutive_failures": 0,
            **_quota_mapping(batch, now),
        },
        run="1",
    )
    await _quota_watch(deps, descriptor, batch)
    summary.status, summary.ticks, summary.fixtures, summary.latency_ms = "ok", len(report.ticks), report.events_normalized, batch.latency_ms
    logger.info(
        "Fleet %s ok: %d fixtures, %d ticks, %dms, runner=%s",
        source_id, report.events_normalized, len(report.ticks), batch.latency_ms, summary.runner,
    )
    await publish_event(deps.redis, {"type": "fleet", "source": source_id, "status": "ok", "ticks": len(report.ticks)})
    return summary


async def _record_failure(deps: FleetDeps, source_id: str, summary: RunSummary, message: str) -> RunSummary:
    """Count the failure and trip the breaker; at the threshold, log FATAL, pause and dead-letter."""
    settings = deps.settings
    keys = fleet_keys(settings)
    now = _utcnow()
    failures, fatal = 1, False
    try:
        async with deps.session_factory() as session:
            row = await get_or_create_source(session, source_id)
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = message[:2000]
            failures = row.consecutive_failures
            fatal = failures >= settings.OMNI_FLEET_FAILURE_THRESHOLD
            if fatal:
                row.paused_at = now
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Fleet %s: could not record the failure in the database", source_id)

    cooldown = await _trip_breaker(deps, source_id, failures)
    mapping: dict[str, Any] = {"last_status": "failed", "last_error": message[:500], "runner": summary.runner, "consecutive_failures": failures}
    if fatal:
        mapping["state"] = "paused"
    await _metrics(deps, source_id, mapping, run="0")
    if fatal:
        record = {"source_id": source_id, "status": "FATAL", "failures": failures, "error": message[:500], "runner": summary.runner, "at": now.isoformat()}
        with contextlib.suppress(RedisError, OSError):
            await deps.redis.lpush(keys.fleet_deadletter(), json.dumps(record))
            await deps.redis.ltrim(keys.fleet_deadletter(), 0, settings.OMNI_FLEET_DEADLETTER_MAX - 1)
        logger.critical(
            "FATAL: fleet source %s failed %d consecutive times and is paused until re-enabled: %s",
            source_id, failures, message, extra={"status": "FATAL", "source_id": source_id},
        )
        summary.status = "fatal"
    else:
        logger.warning(
            "Fleet %s failed (%d/%d), circuit open for %.0fs: %s",
            source_id, failures, settings.OMNI_FLEET_FAILURE_THRESHOLD, cooldown, message,
        )
        summary.status = "failed"
    summary.reason = message[:200]
    await publish_event(deps.redis, {"type": "fleet", "source": source_id, "status": "FATAL" if fatal else "circuit_open"})
    return summary


# ---------------------------------------------------------------- locks & claims (Redis, else in-process)
class _SourceLock:
    def __init__(self, deps: FleetDeps, source_id: str) -> None:
        self._redis_lock = Lock(deps.redis, fleet_keys(deps.settings).fleet_lock(source_id), timeout=deps.settings.OMNI_FLEET_LOCK_TIMEOUT_SECONDS, blocking=False)
        with _local_guard:
            self._local = _local_locks.setdefault(source_id, threading.Lock())
        self._mode: Literal["redis", "local"] | None = None

    async def acquire(self) -> bool:
        try:
            if await self._redis_lock.acquire():
                self._mode = "redis"
                return True
            return False
        except (RedisError, OSError):
            if self._local.acquire(blocking=False):  # Redis down: at least no overlap in this process
                self._mode = "local"
                return True
            return False

    async def release(self) -> None:
        if self._mode == "redis":
            with contextlib.suppress(LockError, RedisError, OSError):
                await self._redis_lock.release()
        elif self._mode == "local":
            self._local.release()


async def _claim(deps: FleetDeps, key: str, seconds: float) -> bool:
    try:
        return bool(await deps.redis.set(key, "1", nx=True, ex=max(5, int(seconds))))
    except (RedisError, OSError):
        with _local_guard:
            now = time.monotonic()
            if _local_claims.get(key, 0.0) > now:
                return False
            _local_claims[key] = now + max(5.0, seconds)
            return True


# ---------------------------------------------------------------- one run
def build_ingestor(descriptor: SourceDescriptor, deps: FleetDeps, api_key: str | None) -> BaseDataIngestor:
    bucket = TokenBucket(deps.redis, f"{deps.settings.omni_redis_prefix}:fleet", deps.settings.OMNI_FLEET_THROTTLE_MAX_WAIT_SECONDS, deps.sleep)
    kwargs: dict[str, Any] = {"api_key": api_key, "sleep": deps.sleep, "limiter": bucket.limiter(descriptor.source_id, descriptor.rate_limit)}
    if descriptor.spec is not None:
        return UniversalDataIngestor(descriptor.source_id, descriptor.spec, deps.http, deps.settings, **kwargs)
    return INGESTORS[descriptor.source_id](deps.http, deps.settings, **kwargs)


async def run_source(
    source_id: str,
    deps: FleetDeps,
    *,
    runner: Runner = "inprocess",
    force: bool = False,
    scope: Sequence[str] | None = None,
    probe: bool = False,
    normalizer: OmniNormalizer | None = None,
) -> RunSummary:
    """Run one source if it is due. ``force`` (Fleet Command "Run now") skips the interval gate, the
    breaker and the quota reserve (it is the operator's trial), never the lock or a FATAL pause.
    ``scope``: provider-native ids from the failover plan; None = everything the source covers.
    ``probe``: only refresh the quota reading (free where the provider allows)."""
    summary = RunSummary(source_id=source_id, status="skipped", runner=runner)
    settings = deps.settings
    keys = fleet_keys(settings)
    if runner == "celery":
        with contextlib.suppress(RedisError, OSError):
            await deps.redis.set(keys.fleet_heartbeat(), "1", ex=int(settings.OMNI_FLEET_HEARTBEAT_SECONDS))
    if SPOOL.pending:
        await SPOOL.flush(deps.redis, settings)

    if not force and not probe:
        if await _breaker_remaining(deps, source_id):
            summary.reason = "circuit_open"
            return summary
        try:
            last, interval, state = await deps.redis.hmget(keys.fleet_metrics(source_id), ["last_attempt_at", "interval_seconds", "state"])
            if interval is not None and not is_due(_float(last), float(interval), state, time.time()):
                summary.reason = "not_due"
                return summary
        except (RedisError, OSError):
            pass  # the DB gate inside the lock still prevents a double run

    lock = _SourceLock(deps, source_id)
    if not await lock.acquire():
        summary.reason = "locked"  # a run of this source is in progress somewhere in the cluster
        return summary
    try:
        return await _run_locked(source_id, deps, summary, force, scope, probe, normalizer or OmniNormalizer(deps.aliases))
    finally:
        await lock.release()


async def _load_descriptor(session: AsyncSession, deps: FleetDeps, source_id: str) -> tuple[SourceDescriptor, OmniFleetSource] | None:
    row = await session.get(OmniFleetSource, source_id)
    if source_id in INGESTORS:
        row = row or await get_or_create_source(session, source_id)
        registry, _ = await load_registry(session, deps.settings, deps.aliases)
        return registry[source_id], row
    if row is None or (descriptor := config_descriptor(row)) is None:
        return None
    return descriptor, row


async def _run_locked(
    source_id: str,
    deps: FleetDeps,
    summary: RunSummary,
    force: bool,
    scope: Sequence[str] | None,
    probe: bool,
    normalizer: OmniNormalizer,
) -> RunSummary:
    global _entities_synced
    settings = deps.settings
    api_key: str | None = None
    try:
        async with deps.session_factory() as session:
            loaded = await _load_descriptor(session, deps, source_id)
            if loaded is None:
                summary.reason = "unknown_source"
                return summary
            descriptor, row = loaded
            await publish_schedule(deps.redis, row, descriptor, settings)
            state = schedule_state(row, descriptor, settings)
            if state in ("disabled", "paused", "needs_key"):
                summary.reason = state
                return summary
            if not force and not probe:
                quota = _float((await _safe_hgetall(deps, fleet_keys(settings).fleet_metrics(source_id))).get("quota_fraction"))
                if descriptor.cost == "metered" and quota is not None and quota < settings.OMNI_FLEET_QUOTA_RESERVE:
                    summary.reason = "quota_reserve"
                    return summary
            now = _utcnow()
            if not force and not probe and row.last_attempt_at and (now - _aware(row.last_attempt_at)).total_seconds() < descriptor.interval_seconds - 1.0:
                summary.reason = "not_due"
                return summary
            api_key = resolve_api_key(row, descriptor, settings, deps.vault)
            if not _entities_synced:
                await sync_canonical_entities(session, normalizer.aliases)
                _entities_synced = True
            row.last_attempt_at = now
            await session.commit()
        await _metrics(deps, source_id, {"last_attempt_at": now.timestamp(), "runner": summary.runner})

        ingestor = build_ingestor(descriptor, deps, api_key)
        if probe:
            checked = await ingestor.probe()
            if checked is not None:
                await _metrics(deps, source_id, _quota_mapping(checked, now))
                summary.status, summary.reason = "ok", "probe"
                return summary
        batch = await ingestor.fetch(list(scope) if scope is not None else list(descriptor.coverage.values()))
        mapper = SpecMapper(descriptor.spec, normalizer.aliases) if descriptor.spec is not None else None
        report = normalizer.normalize(batch, mapper=mapper, devig_method=descriptor.devig)
        if report.events_normalized == 0 and (report.malformed > 0 or report.events_seen > 0):
            raise SchemaDriftError(
                f"{descriptor.display_name}: {report.events_seen} event(s) received, none parseable "
                f"({report.malformed} malformed): the provider's format may have changed"
            )
        await publish_fleet_ticks(deps, report.ticks)
        await publish_market_quotes(deps.redis, report.quotes, settings)  # Aryabhata prices them off this path
        events = [(t.board_key, tick_to_event(t, now)) for t in report.ticks]
        try:
            await buffer_events(deps.redis, fleet_keys(settings), events, settings.omni_quorum_max_age_seconds)
        except (RedisError, OSError):
            SPOOL.events(events)
        await _announce(deps, descriptor, batch, report)
        if source_id == "odds_api":
            async with deps.session_factory() as session:
                for payload in batch.payloads:
                    await store_snapshots(session, payload.data, payload.key, batch.fetched_at)
                await session.commit()
    except ThrottledError as exc:
        summary.reason = "throttled"  # our own limiter deferred it: not the provider's fault
        logger.info("Fleet %s deferred: %s", source_id, exc)
        return summary
    except MissingApiKeyError:
        summary.reason = "needs_key"
        return summary
    except VaultDecryptionError:
        return await _record_failure(deps, source_id, summary, "Stored API key cannot be decrypted (MASTER_VAULT_KEY changed?)")
    except IngestionError as exc:
        return await _record_failure(deps, source_id, summary, _redact(str(exc), api_key))
    except Exception as exc:  # noqa: BLE001 - any failure is a failed run; CancelledError is not an Exception
        logger.debug("Fleet %s run raised", source_id, exc_info=True)
        return await _record_failure(deps, source_id, summary, _redact(f"{type(exc).__name__}: {str(exc)[:300]}", api_key))
    return await _record_success(deps, descriptor, summary, batch, report)


async def _safe_hgetall(deps: FleetDeps, key: str) -> dict[str, str]:
    try:
        return await deps.redis.hgetall(key) or {}
    except (RedisError, OSError):
        return {}


# ---------------------------------------------------------------- scheduling (beat tick + API fallback)
Dispatch = Callable[[str, list[str], Action, float], Awaitable[None]]


async def fleet_tick(deps: FleetDeps, dispatch: Dispatch) -> dict[str, Any]:
    """Plan, then dispatch every source that is due for its share of the plan.

    ``dispatch(source_id, native_scope, action, interval)`` is a Celery ``apply_async`` under beat,
    or a direct ``run_source`` call in the API fallback.
    """
    if SPOOL.pending:
        await SPOOL.flush(deps.redis, deps.settings)
    registry, _, states, plan, groups, _ = await fleet_plan(deps)
    keys = fleet_keys(deps.settings)
    now = time.time()
    dispatched: list[str] = []
    for sid, entry in plan.items():
        descriptor, state = registry[sid], states[sid]
        if entry.availability == "quota_reserve":
            checked = _float(state.metrics.get("quota_checked_at")) or 0.0
            if now - checked >= deps.settings.OMNI_FLEET_QUOTA_RECHECK_SECONDS and await _claim(
                deps, keys.fleet_probe_claim(sid), deps.settings.OMNI_FLEET_QUOTA_RECHECK_SECONDS
            ):
                await dispatch(sid, [], "probe", descriptor.interval_seconds)
                dispatched.append(f"{sid}:probe")
            continue
        if not entry.native_scope:
            continue
        if not is_due(_float(state.metrics.get("last_attempt_at")), descriptor.interval_seconds, None, now):
            continue
        # The claim stops a run that is queued but not started from being queued again
        if not await _claim(deps, keys.fleet_claim(sid), descriptor.interval_seconds):
            continue
        await dispatch(sid, entry.native_scope, "ingest", descriptor.interval_seconds)
        dispatched.append(sid)
    failovers = sorted(g for g, s in groups.items() if s.failover)
    return {"dispatched": dispatched, "failover_groups": failovers, "uncovered": sorted(g for g, s in groups.items() if s.uncovered)}


async def run_inprocess_fallback(deps: FleetDeps) -> None:
    """Scheduler for the API process while no Celery worker heartbeats (dev without a worker), or
    while Redis itself is down (a worker could not be reached anyway)."""
    keys = fleet_keys(deps.settings)
    logger.info("Fleet in-process fallback armed (runs only while no Celery worker heartbeats)")

    async def run_here(source_id: str, scope: list[str], action: Action, _: float) -> None:
        await run_source(source_id, deps, runner="inprocess", scope=scope or None, probe=action == "probe")

    while True:
        await asyncio.sleep(deps.settings.OMNI_FLEET_FALLBACK_TICK_SECONDS)
        try:
            try:
                if await deps.redis.exists(keys.fleet_heartbeat()):
                    continue
            except (RedisError, OSError):
                pass  # no Redis, no way to reach a worker: keep ingesting here
            await beat(deps.redis, deps.settings, runner="inprocess")  # Garuda's heartbeat (the Sentinel's dead man's switch)
            await fleet_tick(deps, run_here)
        except Exception:
            logger.exception("Fleet fallback tick failed")


async def celery_alive(redis: Redis, settings: Settings) -> bool:
    try:
        return bool(await redis.exists(fleet_keys(settings).fleet_heartbeat()))
    except (RedisError, OSError):
        return False


async def dispatch_run(source_id: str, deps: FleetDeps, send_task: Callable[..., Any]) -> Runner:
    """Fleet Command's "Run now": to a Celery worker when one is alive, else in this process."""
    if await celery_alive(deps.redis, deps.settings):
        await asyncio.to_thread(
            send_task, "omni.fleet.ingest", args=[source_id], kwargs={"force": True}, queue=deps.settings.omni_default_queue
        )
        return "celery"
    task = asyncio.create_task(run_source(source_id, deps, runner="manual", force=True), name=f"fleet-run-{source_id}")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return "manual"
