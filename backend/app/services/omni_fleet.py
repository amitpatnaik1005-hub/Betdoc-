"""Omni ingestion fleet: run one data source end to end, track its health, dead-letter it.

One run of a source::

    redis.asyncio Lock on the source id (no overlapping runs anywhere in the cluster)
      -> config + API key (Fleet Command, vault-encrypted)        -> adapter.fetch()
      -> OmniNormalizer (canonical ids, true probability)         -> board merge across sources
      -> Redis pub/sub (/ws/live-odds) + quorum buffer + Omni live channel
      -> odds_snapshots rows (The Odds API, for the Arena/Oracle/backtests)
      -> health: latency, success window, last sync; FATAL + pause after N consecutive failures

The same code runs under Celery (``app.workers.omni_poller``) and in the API process (the fallback
loop below, active only while no Celery worker heartbeats). The lock and the interval gate make
it safe for both to fire at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.ingestion import INGESTORS, BaseDataIngestor
from app.adapters.ingestion.base import IngestionBatch, IngestionError, MissingApiKeyError
from app.core.config import Settings
from app.core.events import publish_event
from app.core.live_odds import decode_ticks, live_odds_keys, publish_board_ticks
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto, VaultDecryptionError
from app.models.canonical import CanonicalEntity
from app.models.omni_vault import OmniFleetSource
from app.schemas.market import MarketTick
from app.services.odds_poller import store_snapshots
from app.services.omni_normalizer import (
    AliasDictionary,
    NormalizationReport,
    OmniNormalizer,
    QuorumConsensusEngine,
    QuorumPolicy,
    merge_board_tick,
    tick_to_event,
)
from app.services.omni_quorum_buffer import buffer_events

logger = logging.getLogger("betdoc.omni.fleet")

Runner = Literal["celery", "inprocess", "manual"]
RunStatus = Literal["ok", "skipped", "failed", "fatal"]
ScheduleState = Literal["enabled", "disabled", "paused", "needs_key"]

# Sources whose key may also come from the environment (the pre-fleet configuration)
ENV_KEY_FALLBACK: dict[str, str] = {"odds_api": "ODDS_API_KEY"}
_UNMAPPED_SHOWN = 25
_background: set[asyncio.Task[Any]] = set()
_entities_synced = False


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
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # backoff waits (tests pass a recorder)


def fleet_keys(settings: Settings) -> OmniRedisKeys:
    return OmniRedisKeys(settings.omni_redis_prefix)


def default_interval(source_id: str, settings: Settings) -> float:
    return INGESTORS[source_id].interval_seconds(settings)


def effective_interval(row: OmniFleetSource, settings: Settings) -> float:
    return row.interval_seconds or default_interval(row.source_id, settings)


def is_due(last_attempt: float | None, interval: float, state: str | None, now: float) -> bool:
    """Shared by the beat tick (sync) and run_source (async). One second of slack absorbs scheduler jitter."""
    if state in ("disabled", "paused"):
        return False
    return last_attempt is None or now - last_attempt >= interval - 1.0


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _redact(message: str, secret: str | None) -> str:
    return message.replace(secret, "***") if secret else message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    """Drivers without timezone support (SQLite) hand back naive datetimes; they were stored as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# ---------------------------------------------------------------- config rows
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


def resolve_api_key(row: OmniFleetSource, settings: Settings, vault: VaultCrypto | None) -> tuple[str | None, str | None]:
    """(plaintext key, origin). Fleet Command's vault key wins over the environment fallback."""
    if row.encrypted_api_key and vault is not None:
        return vault.decrypt_key(row.encrypted_api_key), "vault"
    env_name = ENV_KEY_FALLBACK.get(row.source_id)
    env_value = getattr(settings, env_name, None) if env_name else None
    if env_value is not None and env_value.get_secret_value():
        return env_value.get_secret_value(), "environment"
    return None, None


def key_origin(row: OmniFleetSource, settings: Settings) -> str | None:
    """Where the key would come from, without decrypting it (for display)."""
    if row.encrypted_api_key:
        return "vault"
    env_name = ENV_KEY_FALLBACK.get(row.source_id)
    env_value = getattr(settings, env_name, None) if env_name else None
    return "environment" if env_value is not None and env_value.get_secret_value() else None


def schedule_state(row: OmniFleetSource, settings: Settings) -> ScheduleState:
    if not row.is_enabled:
        return "disabled"
    if row.paused_at is not None:
        return "paused"
    if INGESTORS[row.source_id].requires_api_key and key_origin(row, settings) is None:
        return "needs_key"
    return "enabled"


async def publish_schedule(redis: Redis, row: OmniFleetSource, settings: Settings) -> None:
    """Mirror the config the beat tick needs (state, interval) into Redis, so it never reads the DB."""
    await redis.hset(
        fleet_keys(settings).fleet_metrics(row.source_id),
        mapping={"state": schedule_state(row, settings), "interval_seconds": effective_interval(row, settings)},
    )


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


# ---------------------------------------------------------------- publishing
async def publish_fleet_ticks(redis: Redis, settings: Settings, ticks: Sequence[MarketTick]) -> list[MarketTick]:
    """Store each source's tick, merge every source's view of the cell, publish the merged board ticks."""
    if not ticks:
        return []
    keys = live_odds_keys()
    ttl = settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS
    pipe = redis.pipeline(transaction=False)
    for tick in ticks:
        pipe.hset(keys.sources(tick.board_key), tick.source or "unknown", tick.model_dump_json(by_alias=True))
        pipe.expire(keys.sources(tick.board_key), ttl)
    await pipe.execute()

    pipe = redis.pipeline(transaction=False)
    for tick in ticks:
        pipe.hgetall(keys.sources(tick.board_key))
    views: list[dict[str, str]] = await pipe.execute()

    engine = QuorumConsensusEngine(QuorumPolicy.from_settings(settings))
    merged = [
        merge_board_tick([t for raw in view.values() for t in decode_ticks(raw)] or [tick], engine)
        for tick, view in zip(ticks, views, strict=True)
    ]
    if not await publish_board_ticks(redis, merged):
        raise RedisError("live-odds publish failed")
    return merged


async def _announce(deps: FleetDeps, ingestor: type[BaseDataIngestor], batch: IngestionBatch, report: NormalizationReport) -> None:
    """One summary per run on the Omni live channel (/omni/ws/stream): the Wire's ingestion feed."""
    message = {
        "kind": "omni.fleet.batch",
        "topic": f"fleet.{batch.source_id}",
        "provider_name": ingestor.display_name,
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


# ---------------------------------------------------------------- bookkeeping
async def _record_success(
    deps: FleetDeps, row_id: str, summary: RunSummary, batch: IngestionBatch, report: NormalizationReport
) -> RunSummary:
    now = _utcnow()
    keys = fleet_keys(deps.settings)
    async with deps.session_factory() as session:
        row = await session.get(OmniFleetSource, row_id)
        if row is not None:
            row.consecutive_failures, row.last_error, row.last_success_at = 0, None, now
            await session.commit()
    pipe = deps.redis.pipeline(transaction=False)
    pipe.lpush(keys.fleet_runs(row_id), "1")
    pipe.ltrim(keys.fleet_runs(row_id), 0, deps.settings.OMNI_FLEET_SUCCESS_WINDOW - 1)
    pipe.hset(
        keys.fleet_metrics(row_id),
        mapping={
            "last_status": "ok",
            "last_error": "",
            "last_success_at": now.timestamp(),
            "latency_ms": batch.latency_ms,
            "requests": batch.requests,
            "retries": batch.retries,
            "ticks": len(report.ticks),
            "fixtures": report.events_normalized,
            "fixtures_seen": report.events_seen,
            "unmapped": json.dumps(sorted(report.unmapped)[:_UNMAPPED_SHOWN]),
            "unmapped_count": len(report.unmapped),
            "quota_remaining": "" if batch.quota_remaining is None else batch.quota_remaining,
            "runner": summary.runner,
            "consecutive_failures": 0,
        },
    )
    await pipe.execute()
    summary.status, summary.ticks, summary.fixtures, summary.latency_ms = "ok", len(report.ticks), report.events_normalized, batch.latency_ms
    logger.info(
        "Fleet %s ok: %d fixtures, %d ticks, %dms, runner=%s",
        row_id, report.events_normalized, len(report.ticks), batch.latency_ms, summary.runner,
    )
    await publish_event(deps.redis, {"type": "fleet", "source": row_id, "status": "ok", "ticks": len(report.ticks)})
    return summary


async def _record_failure(deps: FleetDeps, row_id: str, summary: RunSummary, message: str) -> RunSummary:
    """Count the failure; at the threshold, log FATAL, pause the source and dead-letter the run."""
    settings = deps.settings
    keys = fleet_keys(settings)
    now = _utcnow()
    failures, fatal = 1, False
    try:
        async with deps.session_factory() as session:
            row = await get_or_create_source(session, row_id)
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = message[:2000]
            failures = row.consecutive_failures
            fatal = failures >= settings.OMNI_FLEET_FAILURE_THRESHOLD
            if fatal:
                row.paused_at = now
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Fleet %s: could not record the failure in the database", row_id)

    pipe = deps.redis.pipeline(transaction=False)
    pipe.lpush(keys.fleet_runs(row_id), "0")
    pipe.ltrim(keys.fleet_runs(row_id), 0, settings.OMNI_FLEET_SUCCESS_WINDOW - 1)
    pipe.hset(
        keys.fleet_metrics(row_id),
        mapping={"last_status": "failed", "last_error": message[:500], "runner": summary.runner, "consecutive_failures": failures},
    )
    if fatal:
        pipe.hset(keys.fleet_metrics(row_id), "state", "paused")
        record = {"source_id": row_id, "status": "FATAL", "failures": failures, "error": message[:500], "runner": summary.runner, "at": now.isoformat()}
        pipe.lpush(keys.fleet_deadletter(), json.dumps(record))
        pipe.ltrim(keys.fleet_deadletter(), 0, settings.OMNI_FLEET_DEADLETTER_MAX - 1)
    with contextlib.suppress(RedisError, OSError):
        await pipe.execute()

    if fatal:
        logger.critical(
            "FATAL: fleet source %s failed %d consecutive times and is paused until re-enabled: %s",
            row_id, failures, message, extra={"status": "FATAL", "source_id": row_id},
        )
        summary.status = "fatal"
    else:
        logger.warning("Fleet %s failed (%d/%d): %s", row_id, failures, settings.OMNI_FLEET_FAILURE_THRESHOLD, message)
        summary.status = "failed"
    summary.reason = message[:200]
    await publish_event(deps.redis, {"type": "fleet", "source": row_id, "status": "FATAL" if fatal else "failed"})
    return summary


# ---------------------------------------------------------------- one run
async def run_source(
    source_id: str,
    deps: FleetDeps,
    *,
    runner: Runner = "inprocess",
    force: bool = False,
    normalizer: OmniNormalizer | None = None,
) -> RunSummary:
    """Run one source if it is due (``force`` skips the interval gate, never the lock or the pause)."""
    summary = RunSummary(source_id=source_id, status="skipped", runner=runner)
    ingestor = INGESTORS.get(source_id)
    if ingestor is None:
        summary.reason = "unknown_source"
        return summary
    settings = deps.settings
    keys = fleet_keys(settings)
    if runner == "celery":
        with contextlib.suppress(RedisError, OSError):
            await deps.redis.set(keys.fleet_heartbeat(), "1", ex=int(settings.OMNI_FLEET_HEARTBEAT_SECONDS))

    if not force:
        last, interval, state = await deps.redis.hmget(keys.fleet_metrics(source_id), ["last_attempt_at", "interval_seconds", "state"])
        if not is_due(_float(last), _float(interval) or ingestor.interval_seconds(settings), state, time.time()):
            summary.reason = "not_due"
            return summary

    lock = Lock(deps.redis, keys.fleet_lock(source_id), timeout=settings.OMNI_FLEET_LOCK_TIMEOUT_SECONDS, blocking=False)
    if not await lock.acquire():
        summary.reason = "locked"  # a run of this source is in progress somewhere in the cluster
        return summary
    try:
        return await _run_locked(ingestor, deps, summary, force, normalizer or OmniNormalizer())
    finally:
        with contextlib.suppress(LockError, RedisError, OSError):
            await lock.release()


async def _run_locked(
    ingestor: type[BaseDataIngestor], deps: FleetDeps, summary: RunSummary, force: bool, normalizer: OmniNormalizer
) -> RunSummary:
    global _entities_synced
    settings = deps.settings
    source_id = ingestor.source_id
    keys = fleet_keys(settings)
    api_key: str | None = None
    try:
        async with deps.session_factory() as session:
            row = await get_or_create_source(session, source_id)
            await publish_schedule(deps.redis, row, settings)
            state = schedule_state(row, settings)
            if state in ("disabled", "paused", "needs_key"):
                summary.reason = state
                return summary
            now = _utcnow()
            interval = effective_interval(row, settings)
            if not force and row.last_attempt_at and (now - _aware(row.last_attempt_at)).total_seconds() < interval - 1.0:
                summary.reason = "not_due"
                return summary
            api_key, _ = resolve_api_key(row, settings, deps.vault)
            if not _entities_synced:
                await sync_canonical_entities(session, normalizer.aliases)
                _entities_synced = True
            row.last_attempt_at = now
            await session.commit()
        await deps.redis.hset(keys.fleet_metrics(source_id), mapping={"last_attempt_at": now.timestamp(), "runner": summary.runner})

        batch = await ingestor(deps.http, settings, api_key=api_key, sleep=deps.sleep).fetch()
        report = normalizer.normalize(batch)
        await publish_fleet_ticks(deps.redis, settings, report.ticks)
        await buffer_events(
            deps.redis,
            keys,
            [(t.board_key, tick_to_event(t, now)) for t in report.ticks],
            settings.omni_quorum_max_age_seconds,
        )
        await _announce(deps, ingestor, batch, report)
        if source_id == "odds_api":
            async with deps.session_factory() as session:
                for payload in batch.payloads:
                    await store_snapshots(session, payload.data, payload.key, batch.fetched_at)
                await session.commit()
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
    return await _record_success(deps, source_id, summary, batch, report)


# ---------------------------------------------------------------- API-process fallback + manual runs
async def run_inprocess_fallback(deps: FleetDeps) -> None:
    """Scheduler for the API process while no Celery worker heartbeats (dev without a worker).

    Every API worker runs this loop; the per-source lock and interval gate keep it to one run per
    interval cluster-wide, and it stands down the moment a worker starts heartbeating.
    """
    keys = fleet_keys(deps.settings)
    logger.info("Fleet in-process fallback armed (runs only while no Celery worker heartbeats)")
    while True:
        await asyncio.sleep(deps.settings.OMNI_FLEET_FALLBACK_TICK_SECONDS)
        try:
            if await deps.redis.exists(keys.fleet_heartbeat()):
                continue
            for source_id in INGESTORS:
                await run_source(source_id, deps, runner="inprocess")
        except (RedisError, OSError):
            logger.debug("Fleet fallback tick skipped: Redis unavailable")
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
