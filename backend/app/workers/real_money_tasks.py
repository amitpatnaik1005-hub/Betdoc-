"""
Celery tasks for Real-Money Desk automated settlement (Group 95).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

* ``real_money.settle_pending_real_tickets`` (every ``REAL_SETTLE_INTERVAL_SECONDS``, 60 s):
  Every placed real-money ticket whose matches have a recorded final score settles through its sport adapter.
  A win credits the liquid bankroll post-30% TDS, a loss forfeits the stake, and push/void refunds in full.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.models.real_money import RealBetTicket, RealTicketStatus
from app.models.user_bets_ledger import FixtureScore, ScoreStatus
from app.services import real_money_service as rms
from app.workers.oracle_tasks import _resources

logger = logging.getLogger("betdoc.real_money_tasks")


async def _auto_settle_real_tickets() -> dict[str, Any]:
    async with _resources() as (redis, sessions, settings):
        lock = "real_money:lock:settle"
        if not await redis.set(lock, "1", nx=True, ex=int(settings.REAL_SETTLE_INTERVAL_SECONDS) * 4):
            return {"skipped": "another settlement run holds the lock"}
        try:
            settled = 0
            async with sessions() as session:
                stmt = select(RealBetTicket).where(RealBetTicket.status == RealTicketStatus.PLACED.value)
                res = await session.execute(stmt)
                tickets = list(res.scalars().all())

                for ticket in tickets:
                    # Look up fixture score
                    score_stmt = select(FixtureScore).where(
                        FixtureScore.match_id == ticket.match_id,
                        FixtureScore.status.in_([ScoreStatus.FINAL.value, ScoreStatus.ABANDONED.value, "FINAL", "ABANDONED"]),
                    )
                    score_res = await session.execute(score_stmt)
                    fixture_score = score_res.scalar_one_or_none()
                    if fixture_score and fixture_score.score_data:
                        try:
                            await rms.settle_ticket(
                                db=session,
                                settings=settings,
                                user_id=ticket.user_id,
                                ticket_ref=ticket.ticket_reference,
                                scorecard=fixture_score.score_data,
                                notes="Auto-settled from official final score",
                            )
                            settled += 1
                        except Exception as exc:
                            logger.error(f"Error auto-settling real ticket {ticket.ticket_reference}: {exc}")
                if settled > 0:
                    await session.commit()
            return {"settled_tickets": settled}
        finally:
            await redis.delete(lock)


@celery_app.task(name="real_money.settle_pending_real_tickets", ignore_result=True)
def settle_pending_real_tickets_task() -> dict[str, Any]:
    return asyncio.run(_auto_settle_real_tickets())
