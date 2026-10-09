"""The Sentinel's schedule (Group 68): Celery beat tasks, and the API's in-process watchdog.

    sentinel.dependency_health     every SENTINEL_HEALTH_INTERVAL_SECONDS (30s): Postgres, Redis, bookmaker APIs
    sentinel.liveness_check        every SENTINEL_HEARTBEAT_SECONDS (5s): the dead man's switch on Garuda
    sentinel.market_forecast_hype  daily at SENTINEL_HYPE_HOUR:SENTINEL_HYPE_MINUTE, SENTINEL_TIMEZONE (08:00 IST)

Without a Celery worker (development, or Celery itself down), ``SentinelWatchdog`` runs the same three
inside the API: one process at a time (a Redis lease), standing down while a Celery worker heartbeats.
Both may briefly overlap; every alert they can raise is guarded by a compare-and-set or a once-only
key, so an overlap never doubles an alert. The watchdog keeps checking even without Redis: Redis
being down is exactly what it must report.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.services import sentinel_hype
from app.services.sentinel_bus import SentinelKeys
from app.services.sentinel_health import GARUDA, HealthMonitor, LivenessMonitor
from app.workers.sentinel_dispatcher import deliver_direct

logger = logging.getLogger("betdoc.sentinel.tasks")

_WATCHDOG_TICK_SECONDS = 5.0
_LEASE_MS = 20_000
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""


def _vault(settings: Settings) -> VaultCrypto | None:
    try:
        return VaultCrypto.from_settings(settings)
    except VaultConfigurationError:
        return None


@asynccontextmanager
async def _resources() -> AsyncIterator[tuple[Redis, async_sessionmaker[AsyncSession], Settings]]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        yield redis, async_sessionmaker(engine, expire_on_commit=False), settings
    finally:
        await redis.aclose()
        await engine.dispose()


async def _health() -> dict[str, Any]:
    async with _resources() as (redis, sessions, settings):
        monitor = HealthMonitor(redis, sessions, settings, deliver_without_redis=partial(deliver_direct, session_factory=sessions, settings=settings, vault=_vault(settings)))
        return (await monitor.check()).as_dict()


async def _liveness() -> dict[str, Any]:
    async with _resources() as (redis, _, settings):
        return (await LivenessMonitor(redis, settings, name=GARUDA).check()).as_dict()


async def _hype() -> dict[str, Any]:
    async with _resources() as (redis, _, settings):
        return await sentinel_hype.market_forecast_hype(redis, settings)


@celery_app.task(name="sentinel.dependency_health", ignore_result=True)
def dependency_health() -> dict[str, Any]:
    return asyncio.run(_health())


@celery_app.task(name="sentinel.liveness_check", ignore_result=True)
def liveness_check() -> dict[str, Any]:
    return asyncio.run(_liveness())


@celery_app.task(name="sentinel.market_forecast_hype", ignore_result=True)
def market_forecast_hype() -> dict[str, Any]:
    return asyncio.run(_hype())


class SentinelWatchdog:
    """The three checks inside the API process, while no Celery worker is alive."""

    def __init__(self, redis: Redis | None, session_factory: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None) -> None:
        self.redis, self.session_factory, self.settings = redis, session_factory, settings
        self.keys = SentinelKeys(settings)
        self.token = uuid.uuid4().hex
        self.health = HealthMonitor(redis, session_factory, settings, deliver_without_redis=partial(deliver_direct, session_factory=session_factory, settings=settings, vault=vault))
        self.liveness = LivenessMonitor(redis, settings, name=GARUDA) if redis is not None else None
        self._renew = redis.register_script(_RENEW) if redis is not None else None
        self._health_at = float("-inf")
        self._hype_checked_day: str | None = None

    async def _lead(self) -> bool:
        """The lease, or True when Redis cannot be asked (then every process watches: Redis is the news)."""
        if self.redis is None or self._renew is None:
            return True
        try:
            if await self._renew(keys=[self.keys.watchdog], args=[self.token, _LEASE_MS]):
                return True
            return bool(await self.redis.set(self.keys.watchdog, self.token, nx=True, px=_LEASE_MS))
        except (RedisError, OSError):
            return True

    async def _celery_alive(self) -> bool:
        if self.redis is None:
            return False
        from app.services.omni_fleet import celery_alive  # noqa: PLC0415 - the fleet module is heavy

        return await celery_alive(self.redis, self.settings)

    async def tick(self) -> None:
        if not await self._lead() or await self._celery_alive():
            return
        if self.liveness is not None:
            await self.liveness.check()
        if time.monotonic() - self._health_at >= self.settings.SENTINEL_HEALTH_INTERVAL_SECONDS:
            self._health_at = time.monotonic()
            await self.health.check()
        await self._maybe_hype()

    async def _maybe_hype(self) -> None:
        now = datetime.now(UTC)
        if self.redis is None or not sentinel_hype.is_due(now, self.settings):
            return
        day = now.astimezone(ZoneInfo(self.settings.SENTINEL_TIMEZONE)).date().isoformat()
        if self._hype_checked_day == day:
            return
        self._hype_checked_day = day
        try:
            if await self.redis.exists(f"{self.keys.prefix}:hype:sent:{day}"):
                return
            await sentinel_hype.market_forecast_hype(self.redis, self.settings, now=now)
        except (RedisError, OSError):
            self._hype_checked_day = None  # try again on the next tick

    async def run(self) -> None:
        logger.info("Sentinel watchdog armed (runs while no Celery worker heartbeats)")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Sentinel watchdog tick failed")
            await asyncio.sleep(_WATCHDOG_TICK_SECONDS)


async def run_sentinel_watchdog(redis: Redis | None, session_factory: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await SentinelWatchdog(redis, session_factory, settings, vault).run()
