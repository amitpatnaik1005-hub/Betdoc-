"""The recalibration engine's schedule (Group 74).

    calibration.recalibrate    weekly at TWIN_RECALIBRATION_DAY_OF_WEEK HOUR:MINUTE (ORACLE_TIMEZONE): score every model,
                               move it through the lifecycle, publish pillar 1's weights

A run of losses blamed on the models triggers one between schedules (from the feedback sweep), and an
administrator can run one at any time; the run lock lets one go at a time and a busy one is skipped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _recalibrate() -> dict[str, Any]:
    from app.models.model_calibration import RecalibrationTrigger  # noqa: PLC0415
    from app.services.twin import model_calibrator  # noqa: PLC0415 - pulls in the oracle engine

    async with _resources() as (redis, sessions, settings):
        try:
            run = await model_calibrator.recalibrate(sessions, redis, settings, datetime.now(UTC), RecalibrationTrigger.SCHEDULED)
        except model_calibrator.RecalibrationBusy as exc:
            return {"ran": False, "reason": str(exc)}
        return {"ran": True, "run_id": str(run.id), "published": run.published, "promoted": run.models_promoted, "demoted": run.models_demoted, "weights": run.published_weights}


@celery_app.task(name="calibration.recalibrate", ignore_result=True)
def recalibrate() -> dict[str, Any]:
    return asyncio.run(_recalibrate())
