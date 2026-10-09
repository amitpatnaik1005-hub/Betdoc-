"""Celery tasks for Omni ingestion.

* ``omni.poll_provider_endpoint``: poll one DB-configured provider endpoint, land the raw payload
  immutably, publish it live (driven by ``app.workers.omni_dispatcher``).
* ``omni.fleet.tick`` / ``omni.fleet.ingest``: the ingestion fleet. Beat fires the tick every few
  seconds; it computes the quota-aware failover plan over every source (built-in adapters and
  config-driven providers) and enqueues an ingest, scoped to that source's share of the plan, for
  each one that is due, or a quota probe for one held in reserve. The ingest itself is
  ``app.services.omni_fleet.run_source``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

import httpx
from celery.signals import worker_process_init, worker_process_shutdown
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.base_adapter import AdapterError, StandardizedEvent, build_adapter, parse_http_date
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import (
    UnsafeTargetError,
    VaultConfigurationError,
    VaultCrypto,
    VaultDecryptionError,
    assert_public_target,
    get_vault_crypto,
)
from app.services.omni_fleet import Action, FleetDeps, fleet_tick as plan_and_dispatch, run_source
from app.services.omni_quorum_buffer import buffer_event_sync
from app.services.sentinel_health import beat
from app.models.omni_vault import (
    WS_STRATEGIES,
    OmniAuthStrategy,
    OmniHealth,
    OmniProviderConfig,
    OmniProviderEndpoint,
    OmniRawPayload,
    SourceTimestampOrigin,
)

logger = logging.getLogger(__name__)
_settings = get_settings()
_keys = OmniRedisKeys(_settings.omni_redis_prefix)
T = TypeVar("T")


class ResponseTooLargeError(RuntimeError):
    pass


class ProviderConfigurationError(RuntimeError):
    pass


# ---------------------------------------------------------------- per-process resources
@dataclass(slots=True)
class _WorkerResources:
    http: httpx.Client
    redis: Redis
    vault: VaultCrypto


_resources: _WorkerResources | None = None
# App-data Redis (REDIS_URL): fleet schedule/health keys and the quorum buffer live here, where
# the API reads them. The broker may be a different instance.
_app_redis: Redis | None = None


def _get_app_redis() -> Redis:
    global _app_redis
    if _app_redis is None:
        _app_redis = Redis.from_url(_settings.REDIS_URL.get_secret_value(), decode_responses=True)
    return _app_redis


def _build_resources() -> _WorkerResources:
    limits = httpx.Limits(
        max_connections=_settings.omni_http_max_connections,
        max_keepalive_connections=_settings.omni_http_max_connections,
    )
    return _WorkerResources(
        http=httpx.Client(timeout=_settings.omni_http_timeout_seconds, limits=limits, follow_redirects=False),
        redis=Redis.from_url(_settings.celery_broker_url.get_secret_value(), decode_responses=True),
        vault=get_vault_crypto(),
    )


@worker_process_init.connect
def _init_worker(**_: object) -> None:
    global _resources
    _resources = _build_resources()


@worker_process_shutdown.connect
def _shutdown_worker(**_: object) -> None:
    global _resources, _app_redis
    if _resources is not None:
        _resources.http.close()
        _resources.redis.close()
        _resources = None
    if _app_redis is not None:
        _app_redis.close()
        _app_redis = None


def _get_resources() -> _WorkerResources:
    global _resources
    if _resources is None:  # solo pool / eager mode
        _resources = _build_resources()
    return _resources


# ---------------------------------------------------------------- async DB bridge (no shared loops)
@asynccontextmanager
async def _db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


def _run_db(factory: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(factory())  # type: ignore[arg-type]


# ---------------------------------------------------------------- snapshots
@dataclass(frozen=True, slots=True)
class _ProviderSnapshot:
    id: UUID
    name: str
    base_url: str
    auth_strategy: OmniAuthStrategy
    auth_param_name: str | None
    encrypted_api_key: str | None
    default_headers: dict[str, str]
    timeout_seconds: float
    requests_per_minute: int
    is_active: bool
    health_status: str
    adapter_key: str | None
    normalization_spec: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _EndpointSnapshot:
    id: UUID | None
    path: str
    method: str
    query_params: dict[str, str]
    request_body: dict[str, Any] | None
    topic: str | None


async def _load(provider_id: UUID, endpoint: str) -> tuple[_ProviderSnapshot, _EndpointSnapshot] | None:
    async with _db_session() as session:
        provider = await session.get(OmniProviderConfig, provider_id)
        if provider is None:
            return None
        try:
            endpoint_id: UUID | None = UUID(endpoint)
        except ValueError:
            endpoint_id = None
        if endpoint_id is not None:
            row = await session.get(OmniProviderEndpoint, endpoint_id)
            if row is None or row.provider_id != provider.id or not row.is_active:
                return None
            ep = _EndpointSnapshot(row.id, row.path, row.http_method, dict(row.query_params or {}), row.request_body, row.topic)
        else:
            ep = _EndpointSnapshot(None, endpoint, "GET", {}, None, None)
        return (
            _ProviderSnapshot(
                id=provider.id,
                name=provider.provider_name,
                base_url=provider.base_url,
                auth_strategy=provider.auth_strategy,
                auth_param_name=provider.auth_param_name,
                encrypted_api_key=provider.encrypted_api_key,
                default_headers=dict(provider.default_headers or {}),
                timeout_seconds=provider.timeout_seconds or _settings.omni_http_timeout_seconds,
                requests_per_minute=provider.requests_per_minute,
                is_active=provider.is_active,
                health_status=provider.health_status,
                adapter_key=provider.adapter_key,
                normalization_spec=provider.normalization_spec,
            ),
            ep,
        )


async def _persist(record: OmniRawPayload) -> UUID:
    async with _db_session() as session:
        session.add(record)
        await session.commit()
        return record.id


async def _set_health(provider_id: UUID, status: OmniHealth) -> None:
    async with _db_session() as session:
        await session.execute(
            update(OmniProviderConfig).where(OmniProviderConfig.id == provider_id).values(health_status=status.value)
        )
        await session.commit()


# ---------------------------------------------------------------- circuit breaker & rate guard (Redis)
def _breaker_open(redis: Redis, provider_id: UUID) -> bool:
    return bool(redis.exists(_keys.breaker_open(provider_id)))


def _record_failure(redis: Redis, provider_id: UUID) -> bool:
    """Returns True when this failure trips the breaker open."""
    failures_key = _keys.breaker_failures(provider_id)
    count = int(redis.incr(failures_key))
    if count == 1:
        redis.expire(failures_key, _settings.omni_breaker_window_seconds)
    half_open = bool(redis.exists(_keys.breaker_half_open(provider_id)))
    if count >= _settings.omni_breaker_failure_threshold or half_open:
        pipe = redis.pipeline(transaction=True)
        pipe.set(_keys.breaker_open(provider_id), "1", ex=_settings.omni_breaker_cooldown_seconds)
        pipe.set(_keys.breaker_half_open(provider_id), "1")  # first call after cooldown is a trial
        pipe.delete(failures_key)
        pipe.execute()
        return True
    return False


def _record_success(redis: Redis, provider_id: UUID) -> bool:
    """Returns True when a half-open breaker closes (provider recovered)."""
    pipe = redis.pipeline(transaction=True)
    pipe.delete(_keys.breaker_failures(provider_id))
    pipe.delete(_keys.breaker_half_open(provider_id))
    _, recovered = pipe.execute()
    return bool(recovered)


def _consume_rate(redis: Redis, provider_id: UUID, rpm: int) -> bool:
    window = int(time.time() // 60)
    key = _keys.rate_window(provider_id, window)
    count = int(redis.incr(key))
    if count == 1:
        redis.expire(key, 120)
    return count <= rpm


# ---------------------------------------------------------------- HTTP execution
@dataclass(frozen=True, slots=True)
class _HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    latency_ms: int


def _build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _execute(http: httpx.Client, provider: _ProviderSnapshot, endpoint: _EndpointSnapshot, api_key: str | None) -> _HttpResult:
    url = _build_url(provider.base_url, endpoint.path)
    schemes = frozenset({"https", "http"} if _settings.omni_allow_insecure_http else {"https"})
    assert_public_target(url, allowed_schemes=schemes, allow_private=_settings.omni_allow_private_networks)

    headers = {**provider.default_headers, "User-Agent": _settings.omni_user_agent}
    params = dict(endpoint.query_params)
    strategy = provider.auth_strategy
    if strategy in WS_STRATEGIES:
        raise ProviderConfigurationError(f"{strategy} is a WebSocket strategy; REST polling is not supported.")
    if strategy is not OmniAuthStrategy.NONE:
        if not api_key:
            raise ProviderConfigurationError("Provider requires an API key but none is stored.")
        if strategy is OmniAuthStrategy.BEARER:
            headers["Authorization"] = f"Bearer {api_key}"
        elif not provider.auth_param_name:
            raise ProviderConfigurationError(f"{strategy} requires auth_param_name.")
        elif strategy is OmniAuthStrategy.HEADER:
            headers[provider.auth_param_name] = api_key
        else:  # QUERY
            params[provider.auth_param_name] = api_key

    started = time.perf_counter()
    with http.stream(
        endpoint.method,
        url,
        headers=headers,
        params=params,
        json=endpoint.request_body if endpoint.method == "POST" else None,
        timeout=provider.timeout_seconds,
    ) as response:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > _settings.omni_http_max_response_bytes:
                raise ResponseTooLargeError(f"Response exceeded {_settings.omni_http_max_response_bytes} bytes.")
            chunks.append(chunk)
        return _HttpResult(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=b"".join(chunks),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _decode_body(body: bytes, content_type: str | None) -> tuple[dict[str, Any] | list[Any], bool]:
    try:
        parsed: object = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return parsed, True
    return {"_omni_non_json": True, "content_type": content_type, "body": body.decode("utf-8", errors="replace")}, False


# ---------------------------------------------------------------- task
TaskSummary = dict[str, str | int | None]


@celery_app.task(name="omni.poll_provider_endpoint", acks_late=True, max_retries=0)
def poll_provider_endpoint(provider_id: str | UUID, endpoint: str) -> TaskSummary:
    """Poll ``endpoint`` (an OmniProviderEndpoint UUID, or an ad-hoc path) for ``provider_id``."""
    res = _get_resources()
    pid = provider_id if isinstance(provider_id, UUID) else UUID(str(provider_id))
    summary: TaskSummary = {"provider_id": str(pid), "endpoint": endpoint, "status": "skipped", "reason": None}

    try:
        if _breaker_open(res.redis, pid):
            summary["reason"] = "circuit_open"
            return summary

        loaded = _run_db(lambda: _load(pid, endpoint))
        if loaded is None:
            summary["reason"] = "not_found"
            return summary
        provider, ep = loaded
        if not provider.is_active:
            summary["reason"] = "inactive"
            return summary
        if not _consume_rate(res.redis, pid, provider.requests_per_minute):
            summary["reason"] = "rate_limited"
            return summary

        api_key = res.vault.decrypt_key(provider.encrypted_api_key) if provider.encrypted_api_key else None
        try:
            result = _execute(res.http, provider, ep, api_key)
        except httpx.TransportError as exc:
            logger.warning("Omni transport error provider=%s path=%s: %s", provider.name, ep.path, type(exc).__name__)
            if _record_failure(res.redis, pid):
                _run_db(lambda: _set_health(pid, OmniHealth.DEGRADED))
            summary.update(status="failed", reason="transport_error")
            return summary
        finally:
            api_key = None  # drop plaintext reference promptly

        ingestion_ts = datetime.now(UTC)
        payload, is_json = _decode_body(result.body, result.headers.get("content-type"))

        # Circuit breaker
        if result.status >= _settings.omni_breaker_status_floor:
            if _record_failure(res.redis, pid):
                logger.error("Circuit OPEN for provider=%s (status=%s)", provider.name, result.status)
                _run_db(lambda: _set_health(pid, OmniHealth.DEGRADED))
        elif 200 <= result.status < 300:
            recovered = _record_success(res.redis, pid)
            if recovered or provider.health_status != OmniHealth.HEALTHY.value:
                _run_db(lambda: _set_health(pid, OmniHealth.HEALTHY))

        # Double-Timestamp Law
        adapter = build_adapter(provider.adapter_key, pid, provider.normalization_spec)
        source_ts = adapter.extract_source_timestamp(payload, result.headers) if adapter else None
        origin = SourceTimestampOrigin.PAYLOAD
        if source_ts is None:
            source_ts = parse_http_date(result.headers.get("date"))
            origin = SourceTimestampOrigin.HEADER
        if source_ts is None:
            source_ts = ingestion_ts
            origin = SourceTimestampOrigin.INGESTION

        record = OmniRawPayload(
            provider_id=pid,
            endpoint_id=ep.id,
            endpoint_path=ep.path,
            transport="rest",
            raw_payload=payload,
            is_json=is_json,
            content_type=result.headers.get("content-type"),
            payload_hash=hashlib.sha256(result.body).hexdigest(),
            source_timestamp=source_ts,
            source_timestamp_origin=origin.value,
            ingestion_timestamp=ingestion_ts,
            latency_ms=result.latency_ms,
            http_status=result.status,
        )
        raw_id = _run_db(lambda: _persist(record))
        summary.update(status="stored", reason=None, http_status=result.status, raw_payload_id=str(raw_id))

        if not 200 <= result.status < 300:
            return summary

        event: StandardizedEvent | None = None
        if adapter is not None and isinstance(payload, dict):
            try:
                event = adapter.normalize_payload(payload).model_copy(
                    update={"source_timestamp": source_ts, "provider_id": pid}
                )
            except AdapterError as exc:
                logger.warning("Normalisation failed provider=%s path=%s: %s", provider.name, ep.path, exc)

        _publish(res.redis, provider, ep, record, raw_id, payload, event)
        if event is not None:
            _buffer_for_quorum(ep.topic or event.entity_id, event)
        summary["status"] = "published"
        return summary

    except UnsafeTargetError as exc:
        logger.error("Blocked unsafe egress for provider_id=%s: %s", pid, exc)
        _run_db(lambda: _set_health(pid, OmniHealth.DOWN))
        summary.update(status="failed", reason="unsafe_target")
    except (ProviderConfigurationError, VaultDecryptionError, AdapterError) as exc:
        logger.error("Provider configuration error provider_id=%s: %s", pid, exc)
        summary.update(status="failed", reason="configuration_error")
    except ResponseTooLargeError as exc:
        logger.error("Oversized response provider_id=%s: %s", pid, exc)
        summary.update(status="failed", reason="response_too_large")
    except SQLAlchemyError:
        logger.exception("Database failure while polling provider_id=%s", pid)
        summary.update(status="failed", reason="database_error")
    except RedisError:
        logger.exception("Redis failure while polling provider_id=%s", pid)
        summary.update(status="failed", reason="redis_error")
    return summary


def _publish(
    redis: Redis,
    provider: _ProviderSnapshot,
    endpoint: _EndpointSnapshot,
    record: OmniRawPayload,
    raw_id: UUID,
    payload: dict[str, Any] | list[Any],
    event: StandardizedEvent | None,
) -> None:
    envelope: dict[str, Any] = {
        "kind": "omni.rest",
        "provider_id": str(provider.id),
        "provider_name": provider.name,
        "endpoint_path": endpoint.path,
        "topic": endpoint.topic,
        "raw_payload_id": str(raw_id),
        "payload_hash": record.payload_hash,
        "http_status": record.http_status,
        "source_timestamp": record.source_timestamp.isoformat(),
        "ingestion_timestamp": record.ingestion_timestamp.isoformat(),
        "latency_ms": record.latency_ms,
        "event": event.model_dump(mode="json") if event else None,
        "payload": payload,
        "payload_truncated": False,
    }
    message = json.dumps(envelope, default=str, separators=(",", ":"))
    if len(message.encode("utf-8")) > _settings.omni_publish_max_bytes:
        envelope.update(payload=None, payload_truncated=True)
        message = json.dumps(envelope, default=str, separators=(",", ":"))
    try:
        redis.publish(_settings.omni_live_channel, message)
    except RedisError:
        logger.exception("Publish to %s failed (payload %s is persisted)", _settings.omni_live_channel, raw_id)


def _buffer_for_quorum(topic: str, event: StandardizedEvent) -> None:
    """Every provider's latest event per topic feeds omni.run_scheduled_quorum."""
    try:
        buffer_event_sync(_get_app_redis(), _keys, topic, event, _settings.omni_quorum_max_age_seconds)
    except RedisError:
        logger.warning("Quorum buffer write failed for topic=%s", topic)


# ---------------------------------------------------------------- ingestion fleet
@asynccontextmanager
async def _fleet_deps() -> AsyncIterator[FleetDeps]:
    """Loop-bound clients for one asyncio.run (each Celery task gets a fresh event loop)."""
    redis = AsyncRedis.from_url(_settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(_settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    http = httpx.AsyncClient(
        timeout=_settings.omni_http_timeout_seconds,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=_settings.omni_http_max_connections),
    )
    try:
        vault: VaultCrypto | None = get_vault_crypto()
    except VaultConfigurationError:
        vault = None
    try:
        yield FleetDeps(
            redis=redis,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            http=http,
            vault=vault,
            settings=_settings,
        )
    finally:
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="omni.fleet.tick", ignore_result=True)
def fleet_tick() -> dict[str, Any]:
    """Beat-driven: heartbeat, plan the failover routing, enqueue what is due."""
    return asyncio.run(_tick())


async def _tick() -> dict[str, Any]:
    async with _fleet_deps() as deps:
        with contextlib.suppress(RedisError, OSError):
            await deps.redis.set(_keys.fleet_heartbeat(), "1", ex=int(_settings.OMNI_FLEET_HEARTBEAT_SECONDS))
        await beat(deps.redis, _settings, runner="celery")  # Garuda's heartbeat for the Sentinel's dead man's switch

        async def enqueue(source_id: str, scope: list[str], action: Action, interval: float) -> None:
            # expires: a run no worker picks up within one interval is dropped, never burst later
            ingest_source.apply_async(
                args=[source_id], kwargs={"scope": scope or None, "probe": action == "probe"}, expires=interval
            )

        return await plan_and_dispatch(deps, enqueue)


@celery_app.task(name="omni.fleet.ingest", acks_late=True, max_retries=0)
def ingest_source(source_id: str, force: bool = False, scope: list[str] | None = None, probe: bool = False) -> dict[str, Any]:
    """Run one fleet source end to end. Failures trip its breaker (and dead-letter it at the
    threshold) inside run_source; Celery never retries: the next beat tick is the retry."""
    return asyncio.run(_ingest(source_id, force, scope, probe))


async def _ingest(source_id: str, force: bool, scope: list[str] | None, probe: bool) -> dict[str, Any]:
    async with _fleet_deps() as deps:
        return (await run_source(source_id, deps, runner="celery", force=force, scope=scope, probe=probe)).as_dict()
