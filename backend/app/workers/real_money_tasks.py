"""
Celery tasks for Real-Money Desk automated settlement (Group 95).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

* ``real_money.settle_pending_real_tickets`` (every ``REAL_SETTLE_INTERVAL_SECONDS``, 60 s):
  Every placed real-money ticket whose match has a recorded score settles (Group 113: graded by the shared grader that
  every ledger uses, the score read from ``fixture_scores``, which the settlement sweep fills from the results feeds).
  A win credits the liquid bankroll post-30% TDS, a loss forfeits the stake, and push/void refunds in full.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.core.events import publish_event
from app.workers.oracle_tasks import _resources


async def _auto_settle_real_tickets() -> dict[str, Any]:
    """Group 113: every placed ticket a recorded score decides, graded by the shared grader (the score is looked up by
    ``fixture_scores.fixture_id``; a market the grader cannot read stays placed, never voided)."""
    from app.services.live_result_settlement_service import settle_real_tickets  # noqa: PLC0415 - pulls in the feeds

    async with _resources() as (redis, sessions, settings):
        lock = "real_money:lock:settle"
        if not await redis.set(lock, "1", nx=True, ex=int(settings.REAL_SETTLE_INTERVAL_SECONDS) * 4):
            return {"skipped": "another settlement run holds the lock"}
        try:
            async with sessions() as session:
                report = await settle_real_tickets(session, settings, datetime.now(UTC))
                await session.commit()
            if report.settled:
                await publish_event(redis, {"type": "settlement", "section": "settlement", "settled": report.settled, "tickets": report.settled,
                                            "tds_withheld": str(report.tds_withheld)})
            return {"settled": report.settled, "left_pending": report.pending_ungradeable}
        finally:
            await redis.delete(lock)


@celery_app.task(name="real_money.settle_pending_real_tickets", ignore_result=True)
def settle_pending_real_tickets_task() -> dict[str, Any]:
    return asyncio.run(_auto_settle_real_tickets())
