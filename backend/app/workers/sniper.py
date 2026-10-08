"""Celery tasks for the Omni-Sniper.

* ``sniper.resolve_pending_orders``: ask each venue what happened to every due PENDING order;
  settle, confirm, back off or dead-letter (``app.services.order_resolver``).
* ``sniper.refresh_sessions``: refresh every venue token inside its 5-minute expiry margin, so a
  live shot never waits on (or fails at) a login.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, TypeVar

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.events import publish_event
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.services.order_resolver import OrderResolver
from app.services.sniper_runtime import SniperRuntime, build_sniper_runtime

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def _with_runtime(work: Callable[[SniperRuntime, Any], Awaitable[T]]) -> T:
    settings = get_settings()
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        vault: VaultCrypto | None = VaultCrypto.from_settings(settings)
    except VaultConfigurationError:
        vault = None
    runtime = build_sniper_runtime(sessions, redis, settings, vault)
    try:
        return await work(runtime, sessions)
    finally:
        await runtime.aclose()
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="sniper.resolve_pending_orders", acks_late=True, ignore_result=True)
def resolve_pending_orders_task() -> dict[str, Any]:
    async def work(runtime: SniperRuntime, sessions: Any) -> dict[str, Any]:
        summary = await OrderResolver(runtime.gateway, sessions, get_settings()).run()
        if summary.graded or summary.confirmed or summary.released or summary.dead_lettered:
            await publish_event(runtime.gateway.redis, {"type": "mutation", "section": "omni", "path": "/api/v1/omni/resolve", "method": "POST", "status": 200})
        result = asdict(summary)
        logger.info("Order resolver: %s", result)
        return result

    try:
        return asyncio.run(_with_runtime(work))
    except Exception as exc:
        logger.exception("Order resolver sweep failed")
        return {"error": type(exc).__name__}


@celery_app.task(name="sniper.refresh_sessions", acks_late=True, ignore_result=True)
def refresh_sessions_task() -> dict[str, str]:
    async def work(runtime: SniperRuntime, _: Any) -> dict[str, str]:
        return await runtime.gateway.sessions.refresh_due(list(await runtime.gateway.venues(fresh=True)))

    try:
        return asyncio.run(_with_runtime(work))
    except Exception as exc:
        logger.exception("Session refresh sweep failed")
        return {"error": type(exc).__name__}
