"""KARNA's schedule (Group 85). Developed for Amit Ashok Kumar Patnaik.

    competitive.live_sync   every COMPETITIVE_LIVE_SYNC_MINUTES: Parimatch and 1xBet against the sharp zero-vig consensus on the
                            live board, the margin benchmarks, and the public picks graded again
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _sync() -> dict[str, Any]:
    from app.services.competitive_intel_live_service import CompetitiveIntelLiveService, LiveIntelRefusal  # noqa: PLC0415 - pulls in the oracle engine

    async with _resources() as (redis, sessions, settings), sessions() as db:
        try:
            return await CompetitiveIntelLiveService().sync_live_consensus(db, redis, settings, datetime.now(UTC))
        except LiveIntelRefusal as exc:
            return {"skipped": exc.reason}


@celery_app.task(name="competitive.live_sync", ignore_result=True)
def live_sync() -> dict[str, Any]:
    return asyncio.run(_sync())
