"""The feedback loop's schedule (Group 73).

    feedback.sweep          every FEEDBACK_SWEEP_INTERVAL_SECONDS: settle, then attribute what settled (CLV, model feedback,
                            root causes) and page the phone for each fresh settlement
    feedback.recalibrate    nightly at FEEDBACK_RECALIBRATE_HOUR:MINUTE (ORACLE_TIMEZONE): publish the inverse-Brier weights
                            pillar 1 reads

Both are safe to overlap: settlement and attribution lock their bets FOR UPDATE SKIP LOCKED, and the
weights are replaced in one MULTI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _sweep() -> dict[str, Any]:
    from app.services.twin import feedback_tracker  # noqa: PLC0415 - pulls in the oracle engine

    async with _resources() as (redis, sessions, settings):
        if not settings.FEEDBACK_ENABLED:
            return {"ran": False, "disabled": True}
        return (await feedback_tracker.sweep(sessions, redis, settings, datetime.now(UTC))).as_dict()


async def _recalibrate() -> dict[str, Any]:
    from app.services.twin import feedback_tracker  # noqa: PLC0415

    async with _resources() as (redis, sessions, settings):
        if not settings.FEEDBACK_ENABLED:
            return {"ran": False, "disabled": True}
        result = await feedback_tracker.recalibrate(sessions, redis, settings, datetime.now(UTC))
        return {"published": result["published"], "weights": result["weights"]}


@celery_app.task(name="feedback.sweep", ignore_result=True)
def feedback_sweep() -> dict[str, Any]:
    return asyncio.run(_sweep())


@celery_app.task(name="feedback.recalibrate", ignore_result=True)
def feedback_recalibrate() -> dict[str, Any]:
    return asyncio.run(_recalibrate())
