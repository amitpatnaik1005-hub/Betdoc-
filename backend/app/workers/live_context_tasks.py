"""The fixture context's schedule (Group 112). Developed for Amit Ashok Kumar Patnaik.

    live.context_refresh  every LIVE_CONTEXT_REFRESH_SECONDS: Football-Data.org and Sportmonks paired to the stored Odds
                          API fixtures (one schedule call per provider, a table call per competition every few hours).
                          Neither feed is metered against the Odds API quota. Returns at once while LIVE_CONTEXT_ENABLED
                          is off.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.celery_app import celery_app
from app.core.config import get_settings


async def _refresh() -> dict[str, Any]:
    from app.services import live_sports_ingestion_service as live  # noqa: PLC0415 - pulls in the fleet

    settings = get_settings()
    if not settings.LIVE_CONTEXT_ENABLED:
        return {"enabled": False}
    async with live.standalone_fleet_deps(settings) as deps:
        return await live.refresh_context(deps)


@celery_app.task(name="live.context_refresh", ignore_result=True)
def context_refresh() -> dict[str, Any]:
    return asyncio.run(_refresh())
