"""The twin's schedule (Group 72).

    twin.inplay_tick    every TWIN_INPLAY_POLL_SECONDS: re-price every watched bet and send the pullout calls

A tick that finds another one running (the Redis lock) returns at once; one that waited past its own
interval in the queue expires unrun, so a backlog never replays stale ticks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _tick() -> dict[str, Any]:
    from app.services.twin import inplay  # noqa: PLC0415 - pulls in the oracle engine

    async with _resources() as (redis, sessions, settings):
        if not settings.TWIN_INPLAY_ENABLED:
            return {"ran": False, "disabled": True}
        report = await inplay.tick(sessions, redis, settings, datetime.now(UTC))
        return {"ran": report.ran, "watched": report.watched, "priced": report.priced, "closed": report.closed, "alerts": len(report.alerts)}


@celery_app.task(name="twin.inplay_tick", ignore_result=True)
def inplay_tick() -> dict[str, Any]:
    return asyncio.run(_tick())
