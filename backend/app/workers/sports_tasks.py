"""Celery beat jobs of the multi-sport engine (Group 79). Developed for Amit Ashok Kumar Patnaik.

* ``sports.poll_multi_sport_consensus_odds`` (every ``SPORTS_ODDS_POLL_SECONDS``, 60 s): the boards of all nine sports'
  active feed keys, each key paced by ``SPORTS_FEED_MIN_INTERVAL_SECONDS`` and the quota floor.
* ``sports.screen_cross_provider_arbitrage`` (every ``SPORTS_ARB_SCREEN_SECONDS``, 30 s): cross-venue arbitrage and
  value lines over the stored boards, across every venue quoted.

A Redis lock keeps one run of each at a time; a run the beat could not start in time expires unrun.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.celery_app import celery_app
from app.workers.oracle_tasks import _resources


async def _run(name: str) -> dict[str, Any]:
    from app.services.multi_sport_odds_sync import MultiSportOddsSyncService  # noqa: PLC0415

    async with _resources() as (redis, sessions, settings):
        if not settings.SPORTS_SCAN_ENABLED:
            return {"enabled": False}
        lock = f"{settings.SPORTS_PREFIX}:lock:{name}"
        ttl = int(settings.SPORTS_ODDS_POLL_SECONDS if name == "poll" else settings.SPORTS_ARB_SCREEN_SECONDS) * 4
        if not await redis.set(lock, "1", nx=True, ex=ttl):
            return {"skipped": "another run holds the lock"}
        try:
            service = MultiSportOddsSyncService(settings=settings)
            now = datetime.now(UTC)
            if name == "poll":
                return await service.poll_consensus(sessions, redis, now)
            return await service.screen_arbitrage(redis, now)
        finally:
            await redis.delete(lock)


@celery_app.task(name="sports.poll_multi_sport_consensus_odds", ignore_result=True)
def poll_multi_sport_consensus_odds() -> dict[str, Any]:
    return asyncio.run(_run("poll"))


@celery_app.task(name="sports.screen_cross_provider_arbitrage", ignore_result=True)
def screen_cross_provider_arbitrage() -> dict[str, Any]:
    return asyncio.run(_run("screen"))
