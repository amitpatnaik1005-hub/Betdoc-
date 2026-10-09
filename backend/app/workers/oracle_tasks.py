"""Ashoka's schedule (Group 69).

    oracle.settle_user_bets     every ORACLE_SETTLE_INTERVAL_SECONDS: settle the users' placed bets from recorded scores
    oracle.poll_scores          every 15 minutes: fetch scores only for sports a pending bet waits on (each sport
                                at most once per ORACLE_SCORES_POLL_MINUTES, never under the quota floor)
    oracle.scan_trending        every 10 minutes: sharp steam parlays, public traps, AI hybrids

The API also settles a user's bets whenever they open their bets or scorecard, so nothing waits on Celery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.services.oracle_scores import poll_scores
from app.services.user_pnl_tracker import bump, settle_pending


@asynccontextmanager
async def _resources() -> AsyncIterator[tuple[Redis, async_sessionmaker[AsyncSession], Settings]]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        yield redis, async_sessionmaker(engine, expire_on_commit=False), settings
    finally:
        await redis.aclose()
        await engine.dispose()


async def _settle() -> dict[str, Any]:
    async with _resources() as (redis, sessions, _):
        report = await settle_pending(sessions, datetime.now(UTC))
        await bump(redis, report.users)
        return {"legs": report.legs, "bets": report.bets}


async def _poll() -> dict[str, Any]:
    async with _resources() as (redis, sessions, settings):
        try:
            vault = VaultCrypto.from_settings(settings)
        except VaultConfigurationError:
            vault = None
        return await poll_scores(sessions, redis, settings, vault)


async def _scan() -> dict[str, Any]:
    from app.domain.popular_picks import trends  # noqa: PLC0415 - pulls in the oracle engine

    async with _resources() as (redis, sessions, settings):
        async with sessions() as session:
            rows = await trends.scan(session, redis, settings, datetime.now(UTC))
        return {"rows": len(rows)}


@celery_app.task(name="oracle.settle_user_bets", ignore_result=True)
def settle_user_bets() -> dict[str, Any]:
    return asyncio.run(_settle())


@celery_app.task(name="oracle.poll_scores", ignore_result=True)
def poll_user_scores() -> dict[str, Any]:
    return asyncio.run(_poll())


@celery_app.task(name="oracle.scan_trending", ignore_result=True)
def scan_trending() -> dict[str, Any]:
    return asyncio.run(_scan())
