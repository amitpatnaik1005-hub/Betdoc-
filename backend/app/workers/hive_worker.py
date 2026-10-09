"""The Hive worker (Group 65): autonomous bots on the live signal stream, plus their Celery tasks.

* ``HiveWorker`` (an asyncio loop in every API worker, or ``python -m app.workers.hive_worker``)
  joins the ``hive`` consumer group on ``<HIVE_PREFIX>:signals`` (every new Aryabhata edge is
  appended there), so each signal is decided once across all workers, and hands it to
  ``HiveEngine.process``. One worker at a time (a Redis lease) also runs the maintenance: the
  flash-crash scan, shadow grading and the drawdown sweep.
* ``hive.fire_slice``: one TWAP slice, queued with a countdown when a sliced order is planned.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.models.hive_bots import BotExecutionMode, BotStatus, TradingBot
from app.schemas.aryabhata import EdgeSignal
from app.services.bookmaker_gateway import BookmakerGateway, PaperBookmaker
from app.services.hive_engine import GatewayFor, HiveEngine, HiveKeys

logger = logging.getLogger("betdoc.hive")

_READ_BLOCK_MS = 2_000
_READ_COUNT = 50
_BACKOFF_MAX_SECONDS = 30.0
_SLOW_MAINTENANCE_SECONDS = 60.0
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""


class CelerySliceScheduler:
    """TWAP slices go to Celery with their countdowns: they survive this process restarting."""

    def schedule(self, plan_id: uuid.UUID, index: int, countdown: int) -> None:
        fire_slice.apply_async(args=[str(plan_id), index], countdown=countdown)


def gateways(settings: Settings, live: BookmakerGateway | None) -> GatewayFor:
    """PAPER_TRADE always fills on paper; LIVE_EXECUTION only through a live bookmaker gateway, and
    only while the platform itself runs live. Otherwise a live bot's orders are refused."""
    paper = PaperBookmaker()

    def gateway_for(mode: BotExecutionMode) -> BookmakerGateway | None:
        if mode is BotExecutionMode.PAPER_TRADE:
            return paper
        if mode is BotExecutionMode.LIVE_EXECUTION and settings.CFO_EXECUTION_MODE == "live":
            return live
        return None

    return gateway_for


class HiveWorker:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        gateway_for: GatewayFor,
        scheduler: Any = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings
        self.keys = HiveKeys(settings)
        self.engine = HiveEngine(redis, session_factory, settings, gateway_for, scheduler or CelerySliceScheduler(), clock)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.token = uuid.uuid4().hex
        self._bots: tuple[float, list[TradingBot]] = (0.0, [])
        self._scanned = 0.0
        self._slow = 0.0
        self._renew = redis.register_script(_RENEW)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.ensure_group()
                while True:
                    await self.step()
                    await self.maintenance()
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                logger.warning("Hive worker lost Redis (%s); retrying in %.0fs", type(exc).__name__, backoff)
            except Exception:  # noqa: BLE001 - a bad batch must not stop the bots' stream
                logger.exception("Hive worker step failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.keys.signals, self.keys.group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def active_bots(self) -> list[TradingBot]:
        at, cached = self._bots
        if time.monotonic() - at < self.settings.HIVE_BOT_CACHE_SECONDS:
            return cached
        async with self.session_factory() as session:
            bots = list((await session.execute(select(TradingBot).where(TradingBot.status == BotStatus.ACTIVE))).scalars().all())
        self._bots = (time.monotonic(), bots)
        return bots

    async def step(self) -> int:
        batches = await self.redis.xreadgroup(self.keys.group, self.consumer, {self.keys.signals: ">"}, count=_READ_COUNT, block=_READ_BLOCK_MS)
        handled = 0
        for _, entries in batches or []:
            for entry_id, fields in entries:
                try:
                    await self.handle(fields)
                except Exception:  # noqa: BLE001 - one signal's failure is logged, the stream moves on
                    logger.exception("Hive: signal %s failed", entry_id)
                finally:
                    await self.redis.xack(self.keys.signals, self.keys.group, entry_id)
                handled += 1
        return handled

    async def handle(self, fields: dict[str, Any]) -> list[Any]:
        try:
            edge = EdgeSignal.model_validate_json(fields.get("e", ""))
        except ValidationError:
            return []
        bots = await self.active_bots()
        return await self.engine.process(edge, [b for b in bots if b.status is BotStatus.ACTIVE])

    async def maintenance(self) -> None:
        now = time.monotonic()
        if now - self._scanned < self.settings.HIVE_SCAN_INTERVAL_SECONDS:
            return
        self._scanned = now
        lease_ms = int(self.settings.HIVE_SCAN_INTERVAL_SECONDS * 3000)
        if not await self._renew(keys=[self.keys.leader], args=[self.token, lease_ms]):  # ours: extend it
            if not await self.redis.set(self.keys.leader, self.token, nx=True, px=lease_ms):  # free: take it
                return  # another worker leads
        await self.engine.flash_crash_scan()
        if now - self._slow >= _SLOW_MAINTENANCE_SECONDS:
            self._slow = now
            await self.engine.grade_shadow()
            await self.engine.sweep_breakers()


async def run_hive_worker(redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings, live: BookmakerGateway | None) -> None:
    await HiveWorker(redis, session_factory, settings, gateways(settings, live)).run()


# ---------------------------------------------------------------- Celery
async def _fire_slice(plan_id: uuid.UUID, index: int) -> dict[str, Any]:
    settings = get_settings()
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    runtime = None
    try:
        live: BookmakerGateway | None = None
        if settings.CFO_EXECUTION_MODE == "live":
            from app.core.security_vault import VaultConfigurationError, VaultCrypto  # noqa: PLC0415
            from app.services.sniper_runtime import build_sniper_runtime  # noqa: PLC0415

            try:
                vault: VaultCrypto | None = VaultCrypto.from_settings(settings)
            except VaultConfigurationError:
                vault = None
            runtime = build_sniper_runtime(sessions, redis, settings, vault)
            live = runtime.gateway
        hive = HiveEngine(redis, sessions, settings, gateways(settings, live), CelerySliceScheduler())
        decision = await hive.run_slice(plan_id, index)
        return {"plan_id": str(plan_id), "slice": index, "event": None if decision is None else str(decision.event), "reason": None if decision is None else decision.reason}
    finally:
        if runtime is not None:
            await runtime.aclose()
        with contextlib.suppress(Exception):
            await redis.aclose()
        await engine.dispose()


@celery_app.task(name="hive.fire_slice", acks_late=True)
def fire_slice(plan_id: str, index: int) -> dict[str, Any]:
    return asyncio.run(_fire_slice(uuid.UUID(plan_id), int(index)))


if __name__ == "__main__":  # a standalone worker, outside the API process
    async def _main() -> None:
        from app.core.database import AsyncSessionLocal  # noqa: PLC0415

        settings = get_settings()
        redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
        await run_hive_worker(redis, AsyncSessionLocal, settings, None)

    asyncio.run(_main())
