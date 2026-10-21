"""Group 92: BHEESHMA's API rate-limit intelligence and quota guard.

Reads what the platform measured, never what it expects:

* fleet ingestion sources (``load_registry``): the quota headers each run mirrors into
  ``fleet_metrics``, the token bucket's stored level refilled to now, the circuit breaker's TTL and the
  schedule state; the polling cadence shown is the one ``fleet_tick`` applies (``tick_interval``);
* Omni router providers (``OmniProviderConfig``): requests counted in the current per-minute window
  against their RPM, and their own breaker.

A provider that never sent quota headers is UNMETERED. Retired feeds (``QUOTA_GUARD_EXCLUDED_SOURCES``)
are hidden from the board but named in ``excluded_sources``, so nothing disappears silently.

The operator's shield mode lives in Redis (``QUOTA_GUARD_SHIELD_MODE_KEY``), where the fleet beat reads it
every tick. A bucket reset closes the source's breaker, zeroes its failure streak and refills its bucket;
a dead-lettered (paused) source stays paused until it is re-enabled in Fleet Command.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.events import publish_event
from app.core.omni_keys import OmniRedisKeys
from app.domain.control_panel.quota_guard_math import (
    SourceQuotaEvaluation,
    bucket_tokens_now,
    evaluate_source,
    parse_shield_mode,
)
from app.models.omni_vault import OmniFleetSource, OmniProviderConfig
from app.schemas.control_panel import (
    ProviderBucketResetRequest,
    ProviderQuotaTelemetryRead,
    QuotaGuardDashboardResponse,
    QuotaShieldModeUpdateRequest,
    ShieldModeStateRead,
)
from app.services.omni_fleet import close_breaker, fleet_bucket_key, schedule_state
from app.services.omni_normalizer import default_alias_dictionary
from app.services.omni_router import load_registry
from app.services.twin.vetting import developer_credit

logger = logging.getLogger("betdoc.control_panel.quota_guard")


class QuotaGuardError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return None if value in (None, "") else str(value)


def _float(value: Any) -> float | None:
    try:
        text = _text(value)
        return float(text) if text is not None else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _decode(mapping: Any) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    return {(_text(k) or ""): (_text(v) or "") for k, v in mapping.items()}


class QuotaGuardService:
    """Quota and rate-limit telemetry, the cache shield's mode and the per-source bucket reset."""

    # ------------------------------------------------------------ shield mode
    async def shield_state(self, redis: Redis | None, settings: Settings) -> ShieldModeStateRead:
        raw: dict[str, str] = {}
        if redis is not None:
            with contextlib.suppress(RedisError, OSError):
                raw = _decode(await redis.hgetall(settings.QUOTA_GUARD_SHIELD_MODE_KEY))
        set_at = _text(raw.get("set_at"))
        return ShieldModeStateRead(
            mode=parse_shield_mode(raw.get("mode")),
            set_by=_text(raw.get("set_by")),
            set_at=datetime.fromisoformat(set_at) if set_at else None,
            reason=_text(raw.get("reason")),
        )

    async def set_shield_mode(
        self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: QuotaShieldModeUpdateRequest, *, actor: str
    ) -> QuotaGuardDashboardResponse:
        record = {"mode": payload.shield_mode, "set_by": actor, "set_at": datetime.now(UTC).isoformat(), "reason": payload.reason}
        try:
            if redis is None:
                raise OSError("no Redis client")
            await redis.hset(settings.QUOTA_GUARD_SHIELD_MODE_KEY, mapping=record)
        except (RedisError, OSError) as exc:
            # The fleet beat reads the mode from Redis: a mode that cannot be stored would not be applied
            raise QuotaGuardError("Redis is unreachable: the shield mode cannot be stored, so it would not take effect", status_code=503) from exc
        logger.info("Quota cache shield set to %s by %s: %s", payload.shield_mode, actor, payload.reason)
        await publish_event(redis, {"type": "mutation", "section": "control-panel", "method": "POST", "path": "/api/v1/control-panel/quota-guard/mode", "status": 200})
        return await self.dashboard(db, redis, settings)

    # ------------------------------------------------------------ bucket reset
    async def reset_provider_bucket(
        self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: ProviderBucketResetRequest, *, actor: str
    ) -> QuotaGuardDashboardResponse:
        sid = payload.source_id
        excluded = {s.lower() for s in settings.QUOTA_GUARD_EXCLUDED_SOURCES}
        registry, _ = await load_registry(db, settings, default_alias_dictionary())
        if sid not in registry or sid.lower() in excluded:
            raise QuotaGuardError(f"Unknown fleet source: {sid}", status_code=404)
        if redis is None:
            raise QuotaGuardError("Redis is unreachable: the bucket and breaker live there and cannot be reset", status_code=503)
        keys = OmniRedisKeys(settings.omni_redis_prefix)
        try:
            await redis.delete(fleet_bucket_key(settings, sid))  # the next take finds a full bucket
            await redis.hset(keys.fleet_metrics(sid), mapping={"consecutive_failures": "0"})
        except (RedisError, OSError) as exc:
            raise QuotaGuardError("Redis is unreachable: the bucket and breaker live there and cannot be reset", status_code=503) from exc
        await close_breaker(redis, settings, sid)
        row = await db.get(OmniFleetSource, sid)
        if row is not None and row.consecutive_failures:
            row.consecutive_failures = 0  # restarts the breaker backoff; a paused source stays paused
            await db.commit()
        logger.info("Quota guard: %s bucket and breaker reset by %s", sid, actor)
        await publish_event(redis, {"type": "mutation", "section": "control-panel", "method": "POST", "path": "/api/v1/control-panel/quota-guard/reset-bucket", "status": 200})
        await publish_event(redis, {"type": "fleet", "source": sid, "status": "breaker_reset"})
        return await self.dashboard(db, redis, settings)

    # ------------------------------------------------------------ dashboard
    async def dashboard(self, db: AsyncSession, redis: Redis | None, settings: Settings) -> QuotaGuardDashboardResponse:
        excluded = {s.lower() for s in settings.QUOTA_GUARD_EXCLUDED_SOURCES}
        registry, rows = await load_registry(db, settings, default_alias_dictionary())
        router_providers = (await db.execute(select(OmniProviderConfig).order_by(OmniProviderConfig.provider_name))).scalars().all()

        redis_ok = False
        if redis is not None:
            with contextlib.suppress(RedisError, OSError):
                await redis.ping()
                redis_ok = True
        live = redis if redis_ok else None
        shield = await self.shield_state(live, settings)
        keys = OmniRedisKeys(settings.omni_redis_prefix)
        now = time.time()
        hidden: list[str] = []
        items: list[ProviderQuotaTelemetryRead] = []

        def evaluate(**kwargs: Any) -> SourceQuotaEvaluation:
            return evaluate_source(
                reserve_fraction=settings.OMNI_FLEET_QUOTA_RESERVE,
                warn_fraction=settings.QUOTA_GUARD_WARN_FRACTION,
                shield_multiplier=settings.QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER,
                mode=shield.mode,
                **kwargs,
            )

        for sid, descriptor in sorted(registry.items()):
            if sid.lower() in excluded:
                hidden.append(sid)
                continue
            row = rows.get(sid)
            metrics, bucket, breaker_left, half_open = await self._fleet_redis_state(live, keys, settings, sid)
            capacity = float(descriptor.rate_limit.burst)
            rate = descriptor.rate_limit.per_second
            tokens = None
            if live is not None:
                stored = _float(bucket.get("tokens"))
                # An absent bucket (never used, or expired idle) is full: the next take starts at capacity
                tokens = capacity if stored is None else bucket_tokens_now(
                    stored_tokens=stored, stamp=_float(bucket.get("ts")), now=now, refill_per_second=rate, capacity=capacity)
            result = evaluate(
                source_id=sid, kind="fleet", cost=descriptor.cost, schedule=schedule_state(row, descriptor, settings),
                breaker_remaining_seconds=breaker_left, quota_fraction=_float(metrics.get("quota_fraction")),
                tokens_available=tokens, capacity=capacity, base_interval_seconds=descriptor.interval_seconds,
            )
            items.append(self._read(
                result, source_id=sid, display_name=descriptor.display_name, kind="fleet", cost=descriptor.cost,
                is_enabled=row is None or row.is_enabled, schedule=schedule_state(row, descriptor, settings),
                breaker_left=breaker_left, half_open=half_open, rpm=descriptor.rate_limit.requests_per_minute,
                capacity=descriptor.rate_limit.burst, rate=rate, metrics=metrics, base_interval=descriptor.interval_seconds,
                failures=self._failures(metrics, row),
            ))

        for provider in router_providers:
            if provider.provider_name.lower() in excluded:
                hidden.append(provider.provider_name)
                continue
            used, breaker_left, half_open = await self._router_redis_state(live, keys, provider, now)
            rpm = max(int(provider.requests_per_minute), 1)
            schedule = "enabled" if provider.is_active else "disabled"
            result = evaluate(
                source_id=provider.provider_name, kind="router", cost=None, schedule=schedule,
                breaker_remaining_seconds=breaker_left, quota_fraction=None,
                tokens_available=None if used is None else float(max(rpm - used, 0)), capacity=float(rpm), base_interval_seconds=None,
            )
            items.append(self._read(
                result, source_id=provider.provider_name, display_name=provider.provider_name, kind="router", cost=None,
                is_enabled=provider.is_active, schedule=schedule, breaker_left=breaker_left, half_open=half_open,
                rpm=float(rpm), capacity=rpm, rate=rpm / 60.0, metrics={}, base_interval=None, failures=0,
                category=provider.category_code,
            ))

        count = {state: sum(1 for i in items if i.guard_state == state) for state in ("HEALTHY", "WARNING_CACHE_SHIELD", "CRITICAL_LOCKED", "UNMETERED")}
        total = len(items)
        shielded = sum(1 for i in items if i.shield_engaged)
        return QuotaGuardDashboardResponse(
            global_rpm_limit=settings.RATE_LIMIT_GLOBAL_RPM,
            redis_limiter_enforcing=redis_ok,
            shield=shield,
            cache_shield_active=shielded > 0,
            warn_fraction_pct=round(settings.QUOTA_GUARD_WARN_FRACTION * 100.0, 2),
            reserve_fraction_pct=round(settings.OMNI_FLEET_QUOTA_RESERVE * 100.0, 2),
            shield_interval_multiplier=settings.QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER,
            total_sources=total,
            healthy_sources=count["HEALTHY"],
            warning_sources=count["WARNING_CACHE_SHIELD"],
            critical_sources=count["CRITICAL_LOCKED"],
            unmetered_sources=count["UNMETERED"],
            throttled_sources=sum(1 for i in items if i.is_throttled),
            shielded_sources=shielded,
            overall_quota_health_pct=round(100.0 * (total - count["CRITICAL_LOCKED"]) / total, 1) if total else 100.0,
            excluded_sources=sorted(hidden),
            architect=await developer_credit(db),
            providers=items,
        )

    @staticmethod
    def _failures(metrics: dict[str, str], row: OmniFleetSource | None) -> int:
        """The live streak from Redis; the stored row when Redis has none."""
        live = _int(metrics.get("consecutive_failures"))
        if live is not None:
            return live
        return int(row.consecutive_failures or 0) if row is not None else 0

    @staticmethod
    async def _fleet_redis_state(
        redis: Redis | None, keys: OmniRedisKeys, settings: Settings, sid: str
    ) -> tuple[dict[str, str], dict[str, str], float | None, bool]:
        if redis is None:
            return {}, {}, None, False
        try:
            metrics = _decode(await redis.hgetall(keys.fleet_metrics(sid)))
            bucket = _decode(await redis.hgetall(fleet_bucket_key(settings, sid)))
            ttl_ms = await redis.pttl(keys.breaker_open(sid))
            half_open = bool(await redis.exists(keys.breaker_half_open(sid)))
        except (RedisError, OSError):
            return {}, {}, None, False
        return metrics, bucket, (ttl_ms / 1000.0 if isinstance(ttl_ms, int) and ttl_ms > 0 else None), half_open

    @staticmethod
    async def _router_redis_state(
        redis: Redis | None, keys: OmniRedisKeys, provider: OmniProviderConfig, now: float
    ) -> tuple[int | None, float | None, bool]:
        if redis is None:
            return None, None, False
        try:
            used = _int(await redis.get(keys.rate_window(provider.id, int(now // 60)))) or 0  # the poller's fixed window
            ttl_ms = await redis.pttl(keys.breaker_open(provider.id))
            half_open = bool(await redis.exists(keys.breaker_half_open(provider.id)))
        except (RedisError, OSError):
            return None, None, False
        return used, (ttl_ms / 1000.0 if isinstance(ttl_ms, int) and ttl_ms > 0 else None), half_open

    @staticmethod
    def _read(
        result: SourceQuotaEvaluation, *, source_id: str, display_name: str, kind: str, cost: str | None, is_enabled: bool,
        schedule: str, breaker_left: float | None, half_open: bool, rpm: float, capacity: int, rate: float,
        metrics: dict[str, str], base_interval: float | None, failures: int, category: str | None = None,
    ) -> ProviderQuotaTelemetryRead:
        return ProviderQuotaTelemetryRead(
            source_id=source_id,
            display_name=display_name,
            kind=kind,  # type: ignore[arg-type]
            cost=cost,
            category=category,
            is_enabled=is_enabled,
            schedule_state=schedule,
            breaker_state="open" if breaker_left else ("half_open" if half_open else "closed"),
            breaker_remaining_seconds=round(breaker_left, 1) if breaker_left else None,
            requests_per_minute=rpm,
            burst_capacity=capacity,
            tokens_available=result.tokens_available,
            refill_per_second=round(rate, 4),
            bucket_utilization_pct=result.bucket_utilization_pct,
            is_throttled=result.is_throttled,
            quota_remaining=_float(metrics.get("quota_remaining")),
            quota_used=_float(metrics.get("quota_used")),
            quota_limit=_float(metrics.get("quota_limit")),
            remaining_pct=result.remaining_pct,
            used_pct=result.used_pct,
            guard_state=result.guard_state,
            polling_mode=result.polling_mode,
            shield_engaged=result.shield_engaged,
            base_interval_seconds=base_interval,
            effective_interval_seconds=result.effective_interval_seconds,
            consecutive_failures=failures,
            last_latency_ms=_int(metrics.get("latency_ms")),
            verdict_summary=result.verdict_summary,
        )
