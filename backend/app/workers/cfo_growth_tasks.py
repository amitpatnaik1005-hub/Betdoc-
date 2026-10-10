"""KUMBHA's capital growth schedule (Group 76).

    cfo.growth_scan    every CFO_ADVISORY_SCAN_MINUTES: each user's drawdown regime, written (and paged) when it changes;
                       a steady regime re-confirmed at most every CFO_ADVISORY_CONFIRM_HOURS; a halt latched until signed off

The fortress observes the same regime on every vetting run, so a halt never waits for the scan.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _scan() -> dict[str, Any]:
    from app.services.cfo import growth_optimizer as growth  # noqa: PLC0415 - pulls in the twin's services

    async with _resources() as (redis, sessions, settings):
        return await growth.scan(sessions, redis, settings, datetime.now(UTC))


@celery_app.task(name="cfo.growth_scan", ignore_result=True)
def growth_scan() -> dict[str, Any]:
    return asyncio.run(_scan())
