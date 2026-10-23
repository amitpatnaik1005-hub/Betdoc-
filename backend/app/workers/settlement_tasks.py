"""The settlement sweep's schedule (Group 113). Developed for Amit Ashok Kumar Patnaik.

    settlement.sweep  every SETTLEMENT_SWEEP_SECONDS: the fixtures bets wait on that should be over get their results
                      from Football-Data.org and Sportmonks (called only when such a fixture exists, only for its days),
                      then the Oracle's bets, the real-money tickets and the exchange orders settle from fixture_scores.
                      One run at a time (a Redis lock); returns at once while SETTLEMENT_SWEEP_ENABLED is off.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources

LOCK = "betdoc:settlement:sweep:lock"


async def _sweep() -> dict[str, Any]:
    from app.services.live_result_settlement_service import sweep  # noqa: PLC0415 - pulls in the feeds and the ledgers

    async with _resources() as (redis, sessions, settings):
        if not settings.SETTLEMENT_SWEEP_ENABLED:
            return {"enabled": False}
        if not await redis.set(LOCK, "1", nx=True, ex=int(settings.SETTLEMENT_SWEEP_SECONDS * 2)):
            return {"skipped": "another sweep holds the lock"}
        try:
            async with httpx.AsyncClient(timeout=settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS) as http:
                report = await sweep(sessions, http, redis, settings)
            return {"waiting": report["waiting_fixtures"], "settled": report["settled_total"], "tds_withheld": report["tds_withheld"]}
        finally:
            await redis.delete(LOCK)


@celery_app.task(name="settlement.sweep", ignore_result=True)
def settlement_sweep() -> dict[str, Any]:
    return asyncio.run(_sweep())
