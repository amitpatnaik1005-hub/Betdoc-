"""Commander supervisor: real heartbeats for the 17 commanders.

Each commander owns a subsystem. Every interval the supervisor runs a cheap, read-only health probe
against that subsystem and records the result through the Hive heartbeat API, so the UI shows what
the platform is actually doing (an open bet makes BAJIRAO "WORKING", an unreconciled order makes
ARJUNA "DEGRADED", a tripped kill switch puts KAUTILYA to "SLEEPING"), never a scripted status.

Probes are isolated: one failing probe marks only its commander FATAL. After each sweep a
``commanders`` event is published so open dashboards refresh the roster immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.events import publish_event
from app.domain.integration.models import PredictionRecord
from app.domain.math.models_v2.normal_distribution import NormalDistributionModel
from app.domain.the_hive import HiveOrchestrator
from app.models import BetLedger
from app.models.competitive_intel import CompetitorBotModel
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.phantom import ArbitrageOpportunityModel
from app.models.risk import StopLossEventModel
from app.models.the_core import BacktestJobModel, EngineTaskStatus, SmallcaseRegistryModel, SmallcaseStatus
from app.models.the_hive import BotStatus, HiveTaskModel, LegendaryBot, TaskStatus
from app.models.the_lab import ExperimentModel, ExperimentStatus, ResearchReportModel

logger = logging.getLogger("betdoc.hive.supervisor")

PROBE_TIMEOUT_SECONDS = 5.0
OPEN_BET_STATUSES = ("PENDING_NETWORK", "ACCEPTED", "UNKNOWN")

Probe = Callable[[AsyncSession, "ProbeContext"], Awaitable[tuple[BotStatus, dict[str, Any]]]]


class ProbeContext:
    def __init__(self, redis: Redis | None, vault_configured: bool, ws_clients: Callable[[], int]) -> None:
        self.redis = redis
        self.vault_configured = vault_configured
        self.ws_clients = ws_clients


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(stmt) or 0)


# --------------------------------------------------------------------------- probes
async def _kautilya(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    controls = await db.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
    halted = controls is not None and controls.max_daily_exposure <= 0
    return (BotStatus.SLEEPING if halted else BotStatus.ONLINE), {
        "trading_halted": halted,
        "bots_enabled": True if controls is None else controls.bots_enabled,
    }


async def _ashoka(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=24)
    total = await _count(db, select(func.count()).select_from(PredictionRecord))
    recent = await _count(db, select(func.count()).where(PredictionRecord.created_at >= since))
    return (BotStatus.WORKING if recent else BotStatus.ONLINE), {"predictions_total": total, "predictions_24h": recent}


async def _bajirao(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    open_bets = await _count(db, select(func.count()).where(BetLedger.status.in_(OPEN_BET_STATUSES)))
    return (BotStatus.WORKING if open_bets else BotStatus.ONLINE), {"open_positions": open_bets}


async def _vidur(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    rows = (await db.execute(select(HiveTaskModel.status, func.count()).group_by(HiveTaskModel.status))).all()
    counts = {str(status): int(n) for status, n in rows}
    working = counts.get(TaskStatus.IN_PROGRESS.value, 0) > 0
    return (BotStatus.WORKING if working else BotStatus.ONLINE), {"tasks": counts}


async def _kumbha(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    total = await _count(db, select(func.count()).select_from(BetLedger))
    return BotStatus.ONLINE, {"ledger_entries": total}


async def _panini(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    running = await _count(db, select(func.count()).where(ExperimentModel.status == ExperimentStatus.RUNNING.value))
    return (BotStatus.WORKING if running else BotStatus.ONLINE), {"experiments_running": running}


async def _pratap(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    active = await _count(db, select(func.count()).where(SmallcaseRegistryModel.status == SmallcaseStatus.ACTIVE))
    total = await _count(db, select(func.count()).select_from(SmallcaseRegistryModel))
    return BotStatus.ONLINE, {"smallcases_active": active, "smallcases_total": total}


async def _garuda(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    live = await _count(db, select(func.count()).where(ArbitrageOpportunityModel.is_active.is_(True)))
    return (BotStatus.WORKING if live else BotStatus.ONLINE), {"active_arbitrage": live}


async def _todar_mal(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    started = time.perf_counter()
    await db.execute(text("SELECT 1"))
    return BotStatus.ONLINE, {"db_latency_ms": round((time.perf_counter() - started) * 1000, 2)}


async def _aryabhata(_: AsyncSession, __: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    started = time.perf_counter()
    probability = float(NormalDistributionModel().predict(np.array([[7.0, 3.0]]))[0])
    healthy = 0.0 < probability < 1.0
    return (BotStatus.ONLINE if healthy else BotStatus.DEGRADED), {
        "self_test_ms": round((time.perf_counter() - started) * 1000, 2)
    }


async def _chanakya(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=24)
    trips = await _count(db, select(func.count()).where(StopLossEventModel.triggered_at >= since))
    return (BotStatus.DEGRADED if trips else BotStatus.ONLINE), {"stop_loss_trips_24h": trips}


async def _shivaji(_: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    return (BotStatus.ONLINE if ctx.vault_configured else BotStatus.DEGRADED), {"vault_key_configured": ctx.vault_configured}


async def _drona(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    running = await _count(
        db,
        select(func.count()).where(BacktestJobModel.status.in_((EngineTaskStatus.QUEUED, EngineTaskStatus.RUNNING))),
    )
    reports = await _count(db, select(func.count()).select_from(ResearchReportModel))
    return (BotStatus.WORKING if running else BotStatus.ONLINE), {"backtests_in_flight": running, "research_reports": reports}


async def _bheeshma(_: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    if ctx.redis is None:
        return BotStatus.DEGRADED, {"rate_limiter": "uninitialised"}
    try:
        await asyncio.wait_for(ctx.redis.ping(), timeout=1.0)
    except (RedisError, OSError, TimeoutError):
        return BotStatus.DEGRADED, {"rate_limiter": "redis unreachable (failing open)"}
    return BotStatus.ONLINE, {"rate_limiter": "enforcing"}


async def _karna(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    rows = (await db.execute(select(CompetitorBotModel.status, func.count()).group_by(CompetitorBotModel.status))).all()
    counts = {str(s): int(n) for s, n in rows}
    if counts.get("ERROR"):
        return BotStatus.DEGRADED, {"scrapers": counts}
    return (BotStatus.WORKING if counts.get("SCANNING") else BotStatus.ONLINE), {"scrapers": counts}


async def _arjuna(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    unresolved = await _count(db, select(func.count()).where(BetLedger.status == "UNKNOWN"))
    return (BotStatus.DEGRADED if unresolved else BotStatus.ONLINE), {"orders_awaiting_reconciliation": unresolved}


async def _devraya(_: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    return BotStatus.ONLINE, {"live_clients": ctx.ws_clients()}


PROBES: dict[LegendaryBot, Probe] = {
    LegendaryBot.ASHOKA: _ashoka,
    LegendaryBot.KAUTILYA: _kautilya,
    LegendaryBot.BAJIRAO: _bajirao,
    LegendaryBot.VIDUR: _vidur,
    LegendaryBot.KUMBHA: _kumbha,
    LegendaryBot.PANINI: _panini,
    LegendaryBot.PRATAP: _pratap,
    LegendaryBot.GARUDA: _garuda,
    LegendaryBot.TODAR_MAL: _todar_mal,
    LegendaryBot.ARYABHATA: _aryabhata,
    LegendaryBot.CHANAKYA: _chanakya,
    LegendaryBot.SHIVAJI: _shivaji,
    LegendaryBot.DRONA: _drona,
    LegendaryBot.BHEESHMA: _bheeshma,
    LegendaryBot.KARNA: _karna,
    LegendaryBot.ARJUNA: _arjuna,
    LegendaryBot.DEVRAYA: _devraya,
}


# --------------------------------------------------------------------------- loop
async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    hive: HiveOrchestrator,
    ctx: ProbeContext,
    started_at: float,
) -> dict[LegendaryBot, BotStatus]:
    """Probe every commander once and record a heartbeat for each. Returns the statuses written."""
    uptime = int(time.monotonic() - started_at)
    results: dict[LegendaryBot, BotStatus] = {}
    for bot, probe in PROBES.items():
        async with session_factory() as db:
            try:
                bot_status, metrics = await asyncio.wait_for(probe(db, ctx), timeout=PROBE_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - a broken subsystem must not stop the sweep
                await db.rollback()
                bot_status, metrics = BotStatus.FATAL, {"error": f"{type(exc).__name__}: {exc}"[:240]}
                logger.warning("Commander probe failed: %s -> %s", bot.value, metrics["error"])
            metrics["probed_at"] = datetime.now(UTC).isoformat()
            await hive.record_heartbeat(db, bot, status=bot_status, uptime_seconds=uptime, resource_metrics=metrics)
            results[bot] = bot_status
    return results


async def run_supervisor(
    session_factory: async_sessionmaker[AsyncSession],
    hive: HiveOrchestrator,
    ctx: ProbeContext,
    interval_seconds: float,
) -> None:
    started_at = time.monotonic()
    logger.info("Commander supervisor online: %d commanders, every %.0fs", len(PROBES), interval_seconds)
    while True:
        try:
            statuses = await sweep(session_factory, hive, ctx, started_at)
            await publish_event(ctx.redis, {"type": "commanders", "statuses": {b.value: s.value for b, s in statuses.items()}})
        except Exception:  # noqa: BLE001 - e.g. database down: retry next interval
            logger.exception("Commander sweep failed")
        await asyncio.sleep(interval_seconds)
