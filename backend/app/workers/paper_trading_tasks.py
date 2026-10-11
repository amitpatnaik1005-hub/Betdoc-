"""
Celery tasks for Paper Trading automated settlement and TODAR MAL's exchange rates (Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

* ``paper_trading.settle_pending_paper_bets`` (every ``PAPER_SETTLE_INTERVAL_SECONDS``, 60 s): every pending paper bet
  whose matches have a recorded final (or abandoned) score settles through its sport adapter; a market a final
  score cannot settle (a player prop, a corner line) is reported for a hand settlement.
* ``currency.refresh_rates`` (every ``CURRENCY_REFRESH_SECONDS``, 15 min): the supported currencies' INR rates from
  Frankfurter (ECB) and CoinGecko into the table and the ``fx:rates:latest`` cache.

A Redis lock keeps one run of each at a time; a run the beat could not start in time expires unrun.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _settle_pending_paper_bets() -> dict[str, Any]:
    from app.services.paper_trading_service import auto_settle  # noqa: PLC0415

    async with _resources() as (redis, sessions, settings):
        lock = "paper_trading:lock:settle"
        if not await redis.set(lock, "1", nx=True, ex=int(settings.PAPER_SETTLE_INTERVAL_SECONDS) * 4):
            return {"skipped": "another run holds the lock"}
        try:
            return await auto_settle(sessions, settings, datetime.now(UTC))
        finally:
            await redis.delete(lock)


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


@celery_app.task(name="paper_trading.settle_pending_paper_bets", ignore_result=True)
def settle_pending_paper_bets_task() -> dict[str, Any]:
    return asyncio.run(_settle_pending_paper_bets())


@celery_app.task(name="currency.refresh_rates", ignore_result=True)
def refresh_rates_task() -> dict[str, Any]:
    return asyncio.run(_refresh_rates())
