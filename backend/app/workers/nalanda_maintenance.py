"""Nalanda's maintenance: the Celery Beat tasks, the in-process housekeeper, the admin trigger.

    nalanda.preallocate_partitions   Sundays 00:05 UTC   the next NALANDA_WEEKS_AHEAD weeks of every partitioned table
    nalanda.vacuum_partitions        daily 03:30 UTC     VACUUM (ANALYZE) closed partitions with dead tuples
    nalanda.compress_historical_ticks daily 02:15 UTC    rollups past 30 days, Parquet past 90 (then drop), archive backups
    nalanda.mirror_ledger            every minute        CFO tables -> the hash chain
    nalanda.anchor_chain             hourly              the chain head -> the external anchor file

Each run is logged in ``nalanda_maintenance_log`` (what it did, or why it failed). The housekeeper
(``Housekeeper``, started beside the firehose in the API process) repeats the cheap, idempotent ones on
its own clock, behind a Redis lease, so the archive keeps up even where no Celery Beat runs: the ledger
mirror every ``NALANDA_MIRROR_INTERVAL_SECONDS``, the anchor hourly, pre-allocation every six hours.
Running both is harmless: everything here is idempotent and the chain serialises its writers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.models.nalanda_lake import MaintenanceLog
from app.services.nalanda_chain import anchor_path, read_anchors, verify_chain, write_anchor
from app.services.nalanda_mirror import sweep
from app.services.nalanda_partitions import preallocate, vacuum_partitions
from app.services.nalanda_tiering import archive_root, backup_archive, export_cold, rollup_due
from app.workers.nalanda_firehose import NalandaKeys

logger = logging.getLogger("betdoc.nalanda")
TASKS = ("preallocate", "vacuum", "compress", "mirror", "anchor", "verify")
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""


async def _preallocate(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    return {"created": await preallocate(engine, now, settings.NALANDA_WEEKS_AHEAD)}


async def _vacuum(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    return {"vacuumed": await vacuum_partitions(engine, now, settings.NALANDA_VACUUM_MIN_DEAD_TUPLES)}


async def _compress(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    rollups = await rollup_due(sessions, now, settings.NALANDA_ROLLUP_AFTER_DAYS)
    cold = await export_cold(sessions, engine, settings, now)
    backups = await backup_archive(sessions, engine, settings, now)
    return {"rollups": rollups, "cold": cold, "backups": backups}


async def _mirror(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    return {"mirrored": await sweep(sessions, now=now, overlap_seconds=settings.NALANDA_MIRROR_OVERLAP_SECONDS, page=settings.NALANDA_MIRROR_BATCH)}


async def _anchor(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        return {"anchor": await write_anchor(session, anchor_path(archive_root(settings)))}


async def _verify(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        report = (await verify_chain(session, anchors=read_anchors(anchor_path(archive_root(settings))))).as_dict()
    if redis is not None:
        with contextlib.suppress(RedisError, OSError):
            await redis.set(NalandaKeys(settings).verify, json.dumps(report))
    return report


Runner = Callable[[AsyncEngine, async_sessionmaker[AsyncSession], Redis | None, Settings, datetime], Awaitable[dict[str, Any]]]
RUNNERS: dict[str, Runner] = {"preallocate": _preallocate, "vacuum": _vacuum, "compress": _compress, "mirror": _mirror, "anchor": _anchor, "verify": _verify}


async def run_maintenance(
    name: str, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime | None = None, *, log: bool = True,
) -> dict[str, Any]:
    """Run one task and log it. Never raises: a failure is logged with its reason and returned."""
    if name not in RUNNERS:
        raise ValueError(f"unknown Nalanda task {name!r}")
    started = datetime.now(UTC)
    status, detail = "OK", {}
    try:
        detail = await RUNNERS[name](engine, sessions, redis, settings, now or started)
    except Exception as exc:  # noqa: BLE001 - maintenance reports its failures, it never takes the process down
        logger.exception("Nalanda: %s failed", name)
        status, detail = "FAILED", {"error": f"{type(exc).__name__}: {exc}"}
    if log:
        with contextlib.suppress(Exception):
            async with sessions() as session:
                session.add(MaintenanceLog(task=name, status=status, detail=json.loads(json.dumps(detail, default=str)), started_at=started, finished_at=datetime.now(UTC)))
                await session.commit()
    return {"task": name, "status": status, "started_at": started.isoformat(), **detail}


# ---------------------------------------------------------------- the housekeeper (in-process)
class Housekeeper:
    """The cheap, idempotent tasks on their own clock, one process at a time (a Redis lease)."""

    SCHEDULE = (("mirror", None), ("anchor", 3_600.0), ("preallocate", 21_600.0))

    def __init__(self, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
        self.engine, self.sessions, self.redis, self.settings = engine, sessions, redis, settings
        self.token = uuid.uuid4().hex
        self.lease_key = f"{settings.NALANDA_PREFIX}:housekeeper"
        self._renew = redis.register_script(_RENEW)
        self._last: dict[str, float] = {}

    async def lead(self, lease_ms: int) -> bool:
        if await self._renew(keys=[self.lease_key], args=[self.token, lease_ms]):
            return True
        return bool(await self.redis.set(self.lease_key, self.token, nx=True, px=lease_ms))

    async def tick(self) -> list[str]:
        interval = self.settings.NALANDA_MIRROR_INTERVAL_SECONDS
        if not await self.lead(int(interval * 3_000)):
            return []
        ran = []
        for name, every in self.SCHEDULE:
            period = interval if every is None else every
            if time.monotonic() - self._last.get(name, float("-inf")) >= period:
                self._last[name] = time.monotonic()
                await run_maintenance(name, self.engine, self.sessions, self.redis, self.settings, log=name != "mirror")  # the minute-by-minute mirror would flood the log
                ran.append(name)
        return ran

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Nalanda housekeeper tick failed")
            await asyncio.sleep(self.settings.NALANDA_MIRROR_INTERVAL_SECONDS)


# ---------------------------------------------------------------- Celery
async def _celery(name: str) -> dict[str, Any]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        return await run_maintenance(name, engine, async_sessionmaker(engine, expire_on_commit=False), redis, settings)
    finally:
        with contextlib.suppress(Exception):
            await redis.aclose()
        await engine.dispose()


@celery_app.task(name="nalanda.preallocate_partitions", acks_late=True)
def preallocate_partitions() -> dict[str, Any]:
    return asyncio.run(_celery("preallocate"))


@celery_app.task(name="nalanda.vacuum_partitions", acks_late=True)
def vacuum_old_partitions() -> dict[str, Any]:
    return asyncio.run(_celery("vacuum"))


@celery_app.task(name="nalanda.compress_historical_ticks", acks_late=True)
def compress_historical_ticks() -> dict[str, Any]:
    return asyncio.run(_celery("compress"))


@celery_app.task(name="nalanda.mirror_ledger", acks_late=True)
def mirror_ledger() -> dict[str, Any]:
    return asyncio.run(_celery("mirror"))


@celery_app.task(name="nalanda.anchor_chain", acks_late=True)
def anchor_chain() -> dict[str, Any]:
    return asyncio.run(_celery("anchor"))
