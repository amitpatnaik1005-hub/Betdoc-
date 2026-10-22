"""Commander supervisor: real heartbeats and live telemetry for the 15 commanders.

Each commander owns a subsystem. Every interval the supervisor runs a cheap, read-only health probe
against that subsystem and records the result through the Hive heartbeat API, so the UI shows what
the platform is actually doing (an open bet makes BAJIRAO "WORKING", an unreconciled order makes
KUMBHA "DEGRADED", a tripped kill switch puts KAUTILYA to "SLEEPING"), never a scripted status.

Group 90: every beat also carries the measured ``probe_latency_ms`` and an ``activity_summary`` built
only from the figures that probe read, so an indicator never claims more than was checked. A single
commander (or the whole roster) can be probed on demand, and ``build_telemetry_summary`` rolls the
stored beats up into fleet health, freshness and latency.

Probes are isolated: one failing or slow probe marks only its commander FATAL. After each sweep a
``commanders`` event carrying statuses, summaries and latencies is published so open dashboards
update before they refetch.
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

from app.core.config import Settings, get_settings
from app.core.events import publish_event
from app.domain.integration.models import PredictionRecord
from app.domain.math.models_v2.normal_distribution import NormalDistributionModel
from app.domain.the_hive import HiveOrchestrator
from app.models import BetLedger
from app.models.competitive_intel import CompetitorBotModel
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.real_money import RealBetTicket, RealTicketStatus
from app.models.risk import StopLossEventModel
from app.models.the_core import BacktestJobModel, EngineTaskStatus, SmallcaseRegistryModel, SmallcaseStatus
from app.models.the_hive import BotProfileModel, BotStatus, HiveTaskModel, LegendaryBot, TaskStatus
from app.models.the_lab import ExperimentModel, ExperimentStatus, ResearchReportModel
from app.schemas.the_hive import CommanderTelemetryItem, CommanderTelemetrySummaryResponse
from app.services.twin.vetting import developer_credit

logger = logging.getLogger("betdoc.hive.supervisor")

OPEN_BET_STATUSES = ("PENDING_NETWORK", "ACCEPTED", "UNKNOWN")
NO_SIGNAL = "NO SIGNAL"

Probe = Callable[[AsyncSession, "ProbeContext"], Awaitable[tuple[BotStatus, dict[str, Any]]]]

# Uptime for on-demand probes: how long this process has been up (the background sweep passes its own start).
_PROCESS_STARTED = time.monotonic()


class ProbeContext:
    def __init__(self, redis: Redis | None, vault_configured: bool, ws_clients: Callable[[], int]) -> None:
        self.redis = redis
        self.vault_configured = vault_configured
        self.ws_clients = ws_clients


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(stmt) or 0)


def _n(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def _utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored beat is UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# --------------------------------------------------------------------------- probes
# Each summary states only what its probe measured.
async def _kautilya(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    controls = await db.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
    halted = controls is not None and controls.max_daily_exposure <= 0
    bots_enabled = True if controls is None else controls.bots_enabled
    summary = "Trading halted · daily exposure cap is 0" if halted else f"Trading armed · bots {'enabled' if bots_enabled else 'disabled'}"
    return (BotStatus.SLEEPING if halted else BotStatus.ONLINE), {
        "trading_halted": halted, "bots_enabled": bots_enabled, "activity_summary": summary}


def _probability_self_test() -> tuple[bool, float]:
    """The math engine's self-test: one normal-distribution prediction must be a probability (and how long it took)."""
    started = time.perf_counter()
    probability = float(NormalDistributionModel().predict(np.array([[7.0, 3.0]]))[0])
    return 0.0 < probability < 1.0, round((time.perf_counter() - started) * 1000, 2)


async def _ashoka(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    """The Oracle: predictions logged, and (since ARYABHATA's retirement in Group 103, when the Sports Engine joined
    The Oracle) the probability self-test of the math engine."""
    since = datetime.now(UTC) - timedelta(hours=24)
    total = await _count(db, select(func.count()).select_from(PredictionRecord))
    recent = await _count(db, select(func.count()).where(PredictionRecord.created_at >= since))
    healthy, ms = _probability_self_test()
    summary = f"{_n(recent, 'prediction')} in 24h · {total} total" if total else "No predictions logged yet"
    summary += f" · self-test {'passed' if healthy else 'FAILED'} in {ms} ms"
    status = BotStatus.DEGRADED if not healthy else BotStatus.WORKING if recent else BotStatus.ONLINE
    return status, {"predictions_total": total, "predictions_24h": recent, "self_test_ms": ms, "self_test_passed": healthy,
                    "activity_summary": summary}


async def _bajirao(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    open_bets = await _count(db, select(func.count()).where(BetLedger.status.in_(OPEN_BET_STATUSES)))
    summary = f"{_n(open_bets, 'open position')}" if open_bets else "No open positions"
    return (BotStatus.WORKING if open_bets else BotStatus.ONLINE), {"open_positions": open_bets, "activity_summary": summary}


async def _vidur(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    rows = (await db.execute(select(HiveTaskModel.status, func.count()).group_by(HiveTaskModel.status))).all()
    counts = {str(status): int(n) for status, n in rows}
    in_progress = counts.get(TaskStatus.IN_PROGRESS.value, 0)
    done = counts.get(TaskStatus.DONE.value, 0)
    summary = f"{_n(in_progress, 'task')} in progress · {done} done" if counts else "No hive tasks yet"
    return (BotStatus.WORKING if in_progress else BotStatus.ONLINE), {"tasks": counts, "activity_summary": summary}


async def _kumbha(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    """The Vault: the ledger, and (since ARJUNA's retirement in Group 97) any order still awaiting reconciliation."""
    total = await _count(db, select(func.count()).select_from(BetLedger))
    unresolved = await _count(db, select(func.count()).where(BetLedger.status == "UNKNOWN"))
    summary = f"{_n(total, 'ledger entry', 'ledger entries')}" + (f" · {_n(unresolved, 'order')} awaiting reconciliation" if unresolved else "")
    return (BotStatus.DEGRADED if unresolved else BotStatus.ONLINE), {
        "ledger_entries": total, "orders_awaiting_reconciliation": unresolved, "activity_summary": summary}


async def _panini(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    running = await _count(db, select(func.count()).where(ExperimentModel.status == ExperimentStatus.RUNNING.value))
    summary = f"{_n(running, 'experiment')} running" if running else "No experiments running"
    return (BotStatus.WORKING if running else BotStatus.ONLINE), {"experiments_running": running, "activity_summary": summary}


async def _pratap(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    active = await _count(db, select(func.count()).where(SmallcaseRegistryModel.status == SmallcaseStatus.ACTIVE))
    total = await _count(db, select(func.count()).select_from(SmallcaseRegistryModel))
    return BotStatus.ONLINE, {
        "smallcases_active": active, "smallcases_total": total, "activity_summary": f"{active}/{total} smallcases active"}


async def _todar_mal(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    started = time.perf_counter()
    await db.execute(text("SELECT 1"))
    db_ms = round((time.perf_counter() - started) * 1000, 2)
    return BotStatus.ONLINE, {"db_latency_ms": db_ms, "activity_summary": f"Database ping {db_ms} ms"}


async def _chanakya(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=24)
    trips = await _count(db, select(func.count()).where(StopLossEventModel.triggered_at >= since))
    summary = f"{_n(trips, 'stop-loss trip')} in 24h" if trips else "No stop-loss trips in 24h"
    return (BotStatus.DEGRADED if trips else BotStatus.ONLINE), {"stop_loss_trips_24h": trips, "activity_summary": summary}


async def _shivaji(_: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    summary = "Vault key configured" if ctx.vault_configured else "Vault key not configured"
    return (BotStatus.ONLINE if ctx.vault_configured else BotStatus.DEGRADED), {
        "vault_key_configured": ctx.vault_configured, "activity_summary": summary}


async def _drona(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    running = await _count(
        db,
        select(func.count()).where(BacktestJobModel.status.in_((EngineTaskStatus.QUEUED, EngineTaskStatus.RUNNING))),
    )
    reports = await _count(db, select(func.count()).select_from(ResearchReportModel))
    summary = f"{_n(running, 'backtest')} queued or running · {_n(reports, 'research report')}"
    return (BotStatus.WORKING if running else BotStatus.ONLINE), {
        "backtests_in_flight": running, "research_reports": reports, "activity_summary": summary}


async def _bheeshma(_: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    if ctx.redis is None:
        return BotStatus.DEGRADED, {"rate_limiter": "uninitialised", "activity_summary": "No Redis client · rate limiter uninitialised"}
    try:
        await asyncio.wait_for(ctx.redis.ping(), timeout=1.0)
    except (RedisError, OSError, TimeoutError):
        return BotStatus.DEGRADED, {
            "rate_limiter": "redis unreachable (failing open)", "activity_summary": "Redis unreachable · rate limiter failing open"}
    return BotStatus.ONLINE, {"rate_limiter": "enforcing", "activity_summary": "Rate limiter enforcing · Redis reachable"}


async def _karna(db: AsyncSession, _: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    rows = (await db.execute(select(CompetitorBotModel.status, func.count()).group_by(CompetitorBotModel.status))).all()
    counts = {str(s): int(n) for s, n in rows}
    errors, scanning, total = counts.get("ERROR", 0), counts.get("SCANNING", 0), sum(counts.values())
    if errors:
        return BotStatus.DEGRADED, {"scrapers": counts, "activity_summary": f"{_n(errors, 'source')} in error of {total}"}
    summary = f"{_n(scanning, 'source')} scanning of {total}" if total else "No competitor sources registered"
    return (BotStatus.WORKING if scanning else BotStatus.ONLINE), {"scrapers": counts, "activity_summary": summary}


async def _ashvatthama(db: AsyncSession, ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
    """The consultant and ticket router: real tickets placed and still open, and the live WebSocket lane."""
    open_tickets = await _count(db, select(func.count()).where(RealBetTicket.status == RealTicketStatus.PLACED.value))
    clients = ctx.ws_clients()
    summary = f"{_n(open_tickets, 'open real ticket')} · {_n(clients, 'live socket')}"
    return (BotStatus.WORKING if open_tickets else BotStatus.ONLINE), {
        "open_real_tickets": open_tickets, "live_clients": clients, "activity_summary": summary}


PROBES: dict[LegendaryBot, Probe] = {
    LegendaryBot.ASHOKA: _ashoka,
    LegendaryBot.KAUTILYA: _kautilya,
    LegendaryBot.BAJIRAO: _bajirao,
    LegendaryBot.VIDUR: _vidur,
    LegendaryBot.KUMBHA: _kumbha,
    LegendaryBot.PANINI: _panini,
    LegendaryBot.PRATAP: _pratap,
    LegendaryBot.TODAR_MAL: _todar_mal,
    LegendaryBot.CHANAKYA: _chanakya,
    LegendaryBot.SHIVAJI: _shivaji,
    LegendaryBot.DRONA: _drona,
    LegendaryBot.BHEESHMA: _bheeshma,
    LegendaryBot.KARNA: _karna,
    LegendaryBot.ASHVATTHAMA: _ashvatthama,
}


# --------------------------------------------------------------------------- probing
async def probe_single_commander(
    db: AsyncSession,
    hive: HiveOrchestrator,
    ctx: ProbeContext,
    bot: LegendaryBot,
    *,
    settings: Settings | None = None,
    started_at: float | None = None,
) -> BotProfileModel:
    """Run one commander's probe under the timeout, time it and record the heartbeat."""
    cfg = settings or get_settings()
    uptime = max(0, int(time.monotonic() - (_PROCESS_STARTED if started_at is None else started_at)))
    t0 = time.perf_counter()
    try:
        bot_status, metrics = await asyncio.wait_for(PROBES[bot](db, ctx), timeout=cfg.HIVE_SUPERVISOR_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a broken subsystem must not stop the sweep
        await db.rollback()
        error = f"{type(exc).__name__}: {exc}"[:240]
        bot_status, metrics = BotStatus.FATAL, {"error": error, "activity_summary": f"Probe failed · {error}"}
        logger.warning("Commander probe failed: %s -> %s", bot.value, error)
    metrics["probe_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    metrics["probed_at"] = datetime.now(UTC).isoformat()
    return await hive.record_heartbeat(db, bot, status=bot_status, uptime_seconds=uptime, resource_metrics=metrics)


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    hive: HiveOrchestrator,
    ctx: ProbeContext,
    started_at: float,
    *,
    settings: Settings | None = None,
) -> dict[LegendaryBot, BotStatus]:
    """Probe every commander once, each in its own session. Returns the statuses written."""
    results: dict[LegendaryBot, BotStatus] = {}
    for bot in PROBES:
        async with session_factory() as db:
            profile = await probe_single_commander(db, hive, ctx, bot, settings=settings, started_at=started_at)
            results[bot] = BotStatus(profile.status)
    return results


# --------------------------------------------------------------------------- telemetry
def stale_after_seconds(settings: Settings) -> float:
    """A beat older than this is NO SIGNAL: missed beats of the running supervisor, never under the floor."""
    interval = settings.HIVE_SUPERVISOR_INTERVAL_SECONDS
    floor = settings.HIVE_SUPERVISOR_STALE_FLOOR_SECONDS
    return max(floor, interval * settings.HIVE_SUPERVISOR_STALE_MULTIPLIER) if interval > 0 else floor


def _summary_text(bot: LegendaryBot, status: BotStatus, metrics: dict[str, Any]) -> str:
    raw = metrics.get("activity_summary")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(metrics.get("error"), str):
        return f"Probe failed · {metrics['error']}"
    return f"{bot.value} {status.value.lower()}"  # a beat written before Group 90, or by an external reporter


async def build_telemetry_summary(
    db: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> CommanderTelemetrySummaryResponse:
    """Roll the stored beats up into fleet health, freshness and latency for all 15 commanders."""
    cfg = settings or get_settings()
    stale_after = stale_after_seconds(cfg)
    warn_ms = cfg.HIVE_SUPERVISOR_LATENCY_WARN_MS
    at = now or datetime.now(UTC)

    rows = (await db.execute(select(BotProfileModel))).scalars().all()
    by_bot = {LegendaryBot(row.bot_name): row for row in rows}
    counts = dict.fromkeys(BotStatus, 0)
    stale = 0
    latencies: list[float] = []
    last_sweep_at: datetime | None = None
    items: list[CommanderTelemetryItem] = []

    for bot in LegendaryBot:
        row = by_bot.get(bot)
        if row is None:
            stale += 1
            items.append(CommanderTelemetryItem(
                bot_name=bot, status=BotStatus.OFFLINE, effective_status=NO_SIGNAL, is_stale=True,
                activity_summary="Awaiting first heartbeat"))
            continue

        status = BotStatus(row.status)
        ping = _utc(row.last_ping_at)
        last_sweep_at = ping if last_sweep_at is None else max(last_sweep_at, ping)
        freshness = round(max(0.0, (at - ping).total_seconds()), 1)
        is_stale = freshness > stale_after
        if is_stale:
            stale += 1
        else:
            counts[status] += 1

        metrics = dict(row.resource_metrics or {})
        raw_latency = metrics.get("probe_latency_ms")
        latency = float(raw_latency) if isinstance(raw_latency, int | float) and not isinstance(raw_latency, bool) else None
        if latency is not None:
            latencies.append(latency)

        items.append(CommanderTelemetryItem(
            bot_name=bot,
            status=status,
            effective_status=NO_SIGNAL if is_stale else status.value,
            is_stale=is_stale,
            freshness_seconds=freshness,
            uptime_seconds=row.uptime_seconds,
            tasks_completed=row.tasks_completed,
            error_count=row.error_count,
            probe_latency_ms=latency,
            latency_warning=latency is not None and latency >= warn_ms,
            activity_summary=_summary_text(bot, status, metrics),
            last_ping_at=ping,
            resource_metrics=metrics,
        ))

    total = len(LegendaryBot)
    return CommanderTelemetrySummaryResponse(
        total_commanders=total,
        reporting_count=len(by_bot),
        online_count=counts[BotStatus.ONLINE],
        working_count=counts[BotStatus.WORKING],
        degraded_count=counts[BotStatus.DEGRADED],
        fatal_count=counts[BotStatus.FATAL],
        sleeping_count=counts[BotStatus.SLEEPING],
        offline_count=counts[BotStatus.OFFLINE],
        stale_count=stale,
        overall_health_pct=round(100.0 * (counts[BotStatus.ONLINE] + counts[BotStatus.WORKING]) / total, 1),
        avg_probe_latency_ms=round(sum(latencies) / len(latencies), 2) if latencies else None,
        stale_after_seconds=stale_after,
        latency_warn_ms=warn_ms,
        last_sweep_at=last_sweep_at,
        developer_credit=await developer_credit(db),
        commanders=items,
    )


def commanders_event(summary: CommanderTelemetrySummaryResponse) -> dict[str, Any]:
    """The ``commanders`` bus event: enough for open dashboards to repaint before they refetch.

    Only commanders with a current beat are included, so a dashboard never paints a stale status as live.
    """
    current = [item for item in summary.commanders if not item.is_stale]
    return {
        "type": "commanders",
        "statuses": {item.bot_name.value: item.status.value for item in current},
        "summaries": {item.bot_name.value: item.activity_summary for item in current},
        "latencies_ms": {item.bot_name.value: item.probe_latency_ms for item in current if item.probe_latency_ms is not None},
        "swept_at": (summary.last_sweep_at or datetime.now(UTC)).isoformat(),
    }


# --------------------------------------------------------------------------- loop
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
            await sweep(session_factory, hive, ctx, started_at)
            async with session_factory() as db:
                summary = await build_telemetry_summary(db)
            await publish_event(ctx.redis, commanders_event(summary))
        except Exception:  # noqa: BLE001 - e.g. database down: retry next interval
            logger.exception("Commander sweep failed")
        await asyncio.sleep(interval_seconds)
