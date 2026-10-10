"""The feedback loop's schedule (Group 73).

    feedback.sweep          every FEEDBACK_SWEEP_INTERVAL_SECONDS: settle, then attribute what settled (CLV, model feedback,
                            root causes) and page the phone for each fresh settlement

Sweeps are safe to overlap: settlement and attribution lock their bets FOR UPDATE SKIP LOCKED. Pillar 1's
weights are the recalibration engine's (``app.workers.calibration_tasks``, Group 74).
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


@celery_app.task(name="feedback.sweep", ignore_result=True)
def feedback_sweep() -> dict[str, Any]:
    return asyncio.run(_sweep())
