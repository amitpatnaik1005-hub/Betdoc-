"""VIDUR's schedule (Group 78). Each task returns at once while ``WIRE_SCAN_ENABLED`` is off.

    wire.news_scan     every WIRE_NEWS_SCAN_MINUTES: ingest and read the news, page breaking news, catch catalysts
    wire.espn_sync     every WIRE_ESPN_SYNC_MINUTES: scores with the clock, venues, team sheets, injuries, officials, records
    wire.weather_scan  every WIRE_WEATHER_SCAN_MINUTES: each tracked fixture's match-window forecast and friction factor
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _run(name: str) -> dict[str, Any]:
    from app.services.the_wire import espn_sync, news, weather  # noqa: PLC0415 - pulls in the oracle engine

    scans: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {"news": news.scan, "espn": espn_sync.sync, "weather": weather.scan}
    async with _resources() as (redis, sessions, settings):
        if not settings.WIRE_SCAN_ENABLED:
            return {"enabled": False}
        return await scans[name](sessions, redis, settings, datetime.now(UTC))


@celery_app.task(name="wire.news_scan", ignore_result=True)
def news_scan() -> dict[str, Any]:
    return asyncio.run(_run("news"))


@celery_app.task(name="wire.espn_sync", ignore_result=True)
def espn_sync() -> dict[str, Any]:
    return asyncio.run(_run("espn"))


@celery_app.task(name="wire.weather_scan", ignore_result=True)
def weather_scan() -> dict[str, Any]:
    return asyncio.run(_run("weather"))
