"""Celery task ``cfo.settle_markets``: pay out every pending CFO-ledger bet whose market is graded.

Beat runs it every ``CFO_SETTLE_INTERVAL_SECONDS``. Each user settles in one locked transaction
(``app.services.cfo_ledger.settle_markets``); a user whose bankroll is mid-execution is skipped and
picked up by the next sweep, so settlement never waits on a bookmaker call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.events import publish_event
from app.services.cfo_ledger import settle_markets

logger = logging.getLogger(__name__)


@celery_app.task(name="cfo.settle_markets", acks_late=True, ignore_result=True)
def settle_markets_task() -> dict[str, Any]:
    try:
        return asyncio.run(_sweep())
    except Exception as exc:
        logger.exception("CFO settlement sweep failed")
        return {"error": type(exc).__name__}


async def _sweep() -> dict[str, Any]:
    settings = get_settings()
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    try:
        summary = await settle_markets(async_sessionmaker(engine, expire_on_commit=False), redis, settings)
        result = summary.as_dict()
        if summary.won or summary.lost or summary.void:
            # Open dashboards refresh balances and positions (same bus as every API write)
            await publish_event(redis, {"type": "mutation", "section": "omni", "path": "/api/v1/omni/settle", "method": "POST", "status": 200})
        logger.info("CFO settlement sweep: %s", result)
        return result
    finally:
        await redis.aclose()
        await engine.dispose()
