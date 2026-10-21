"""
TODAR MAL's exchange-rate refresh (Group 80; the paper settlement task beside it was retired in Group 97).
Developed for Amit Ashok Kumar Patnaik.

* ``currency.refresh_rates`` (every ``CURRENCY_REFRESH_SECONDS``, 15 min): INR per USD from the ECB reference rate
  (Frankfurter) into the table and the ``fx:rates:latest`` cache (Group 98: INR and USD only).

A Redis lock keeps one run at a time; a run the beat could not start in time expires unrun.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _refresh_rates() -> dict[str, Any]:
    from app.services.currency_service import CurrencyService  # noqa: PLC0415

    async with _resources() as (redis, sessions, settings):
        lock = "currency:lock:refresh"
        if not await redis.set(lock, "1", nx=True, ex=int(settings.CURRENCY_REFRESH_SECONDS)):
            return {"skipped": "another run holds the lock"}
        try:
            async with sessions() as session:
                stored = await CurrencyService(settings, redis).refresh(session, datetime.now(UTC))
            return {"refreshed": sorted(stored), "missing": sorted(set(settings.CURRENCY_SUPPORTED) - set(stored) - {"INR"})}
        finally:
            await redis.delete(lock)


@celery_app.task(name="currency.refresh_rates", ignore_result=True)
def refresh_rates_task() -> dict[str, Any]:
    return asyncio.run(_refresh_rates())
