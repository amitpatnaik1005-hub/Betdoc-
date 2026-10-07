"""Rate-aware dispatcher: turns DB config into Celery tasks without exceeding provider RPM.

Distributed-safe: each endpoint is claimed with ``SET NX PX <interval>``, so N dispatcher replicas
still dispatch each endpoint at most once per interval.
Run: ``python -m app.workers.omni_dispatcher``
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
import time
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from kombu.exceptions import KombuError
from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.logging import setup_json_logging
from app.core.omni_keys import OmniRedisKeys
from app.models.omni_vault import WS_STRATEGIES, OmniProviderConfig, OmniProviderEndpoint
from app.workers.omni_poller import poll_provider_endpoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchItem:
    provider_id: UUID
    endpoint_id: UUID
    queue: str
    interval_seconds: float


class OmniDispatcher:
    def __init__(self, *, settings: Settings, session_factory: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis
        self._keys = OmniRedisKeys(settings.omni_redis_prefix)

    def resolve_queue(self, queue_name: str | None, category_code: str) -> str:
        candidate = queue_name or self._settings.omni_category_queue_map.get(category_code) or self._settings.omni_default_queue
        if candidate not in self._settings.omni_queues:
            logger.warning("Unknown queue '%s' (category %s); using default.", candidate, category_code)
            return self._settings.omni_default_queue
        return candidate

    async def load_plan(self) -> list[DispatchItem]:
        stmt = (
            select(
                OmniProviderEndpoint.id,
                OmniProviderEndpoint.provider_id,
                OmniProviderEndpoint.min_interval_seconds,
                OmniProviderConfig.requests_per_minute,
                OmniProviderConfig.queue_name,
                OmniProviderConfig.category_code,
            )
            .join(OmniProviderConfig, OmniProviderConfig.id == OmniProviderEndpoint.provider_id)
            .where(
                OmniProviderConfig.is_active.is_(True),
                OmniProviderEndpoint.is_active.is_(True),
                OmniProviderConfig.auth_strategy.notin_(list(WS_STRATEGIES)),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()

        endpoints_per_provider = Counter(row.provider_id for row in rows)
        plan: list[DispatchItem] = []
        for row in rows:
            # Provider RPM is shared by all of its endpoints.
            rate_interval = 60.0 * endpoints_per_provider[row.provider_id] / max(row.requests_per_minute, 1)
            interval = max(rate_interval, row.min_interval_seconds or 0.0)
            plan.append(DispatchItem(row.provider_id, row.id, self.resolve_queue(row.queue_name, row.category_code), interval))
        return plan

    async def tick(self, plan: list[DispatchItem]) -> int:
        if not plan:
            return 0
        provider_ids = list({item.provider_id for item in plan})
        open_flags = await self._redis.mget([self._keys.breaker_open(pid) for pid in provider_ids])
        blocked = {pid for pid, flag in zip(provider_ids, open_flags, strict=True) if flag is not None}
        candidates = [item for item in plan if item.provider_id not in blocked]
        if not candidates:
            return 0

        pipe = self._redis.pipeline(transaction=False)
        for item in candidates:
            pipe.set(self._keys.dispatch_claim(item.endpoint_id), "1", nx=True, px=max(int(item.interval_seconds * 1000), 1))
        claims = await pipe.execute()

        dispatched = 0
        for item, claimed in zip(candidates, claims, strict=True):
            if not claimed:
                continue
            try:
                await asyncio.to_thread(self._send, item)
                dispatched += 1
            except (KombuError, OSError) as exc:
                logger.error("Broker publish failed for endpoint=%s: %s", item.endpoint_id, exc)
                await self._redis.delete(self._keys.dispatch_claim(item.endpoint_id))  # retry next tick
        return dispatched

    def _send(self, item: DispatchItem) -> None:
        poll_provider_endpoint.apply_async(
            args=[str(item.provider_id), str(item.endpoint_id)],
            queue=item.queue,
            # Expire stale tasks so a worker outage never causes an RPM-violating burst on recovery.
            expires=max(item.interval_seconds, self._settings.omni_task_min_expiry_seconds),
        )

    async def run(self, stop: asyncio.Event) -> None:
        plan: list[DispatchItem] = []
        last_refresh = -math.inf
        while not stop.is_set():
            now = time.monotonic()
            if now - last_refresh >= self._settings.omni_dispatch_refresh_seconds:
                try:
                    plan = await self.load_plan()
                    logger.info("Dispatch plan refreshed: %d endpoint(s).", len(plan))
                except SQLAlchemyError:
                    logger.exception("Dispatch plan refresh failed; keeping previous plan (%d items).", len(plan))
                last_refresh = now
            try:
                await self.tick(plan)
            except RedisError:
                logger.exception("Dispatcher tick failed (Redis).")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.omni_dispatch_tick_seconds)
            except TimeoutError:
                pass


async def main() -> None:
    settings = get_settings()
    setup_json_logging()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), pool_pre_ping=True)
    pool: ConnectionPool = ConnectionPool.from_url(settings.celery_broker_url.get_secret_value(), decode_responses=True)
    redis = Redis(connection_pool=pool)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import sys
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    else:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        await OmniDispatcher(
            settings=settings, session_factory=async_sessionmaker(engine, expire_on_commit=False), redis=redis
        ).run(stop)
    finally:
        await redis.aclose()
        await pool.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
