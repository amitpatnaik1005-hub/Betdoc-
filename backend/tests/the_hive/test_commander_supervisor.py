"""Group 90: commander probes, live telemetry roll-up and the on-demand probe API.

Runs every real probe against a SQLite database holding exactly the tables the probes read.
Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, MetaData, Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import CurrentUser, get_db
from app.api.v1 import the_hive as hive_api
from app.core.config import get_settings
from app.domain.integration.models import PredictionRecord
from app.domain.the_hive import HiveOrchestrator
from app.models import BetLedger
from app.models.competitive_intel import CompetitorBotModel
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.paper_trading import PaperBet
from app.models.risk import StopLossEventModel
from app.models.the_core import BacktestJobModel, SmallcaseRegistryModel
from app.models.the_hive import BotProfileModel, BotStatus, HiveTaskModel, LegendaryBot
from app.models.the_lab import ExperimentModel, ResearchReportModel
from app.services import commander_supervisor as supervisor
from app.services.commander_supervisor import (
    NO_SIGNAL,
    ProbeContext,
    build_telemetry_summary,
    commanders_event,
    probe_single_commander,
    stale_after_seconds,
    sweep,
)

# Every table a probe reads, plus the beats themselves.
_TABLES: list[Table] = [
    model.__table__  # type: ignore[attr-defined]
    for model in (
        BotProfileModel, HiveTaskModel, SystemSettingsModel, PredictionRecord, BetLedger, ExperimentModel,
        SmallcaseRegistryModel, StopLossEventModel, BacktestJobModel,
        ResearchReportModel, CompetitorBotModel, PaperBet,
    )
]
DEVELOPER = "Amit Ashok Kumar Patnaik"


def _sqlite_metadata() -> MetaData:
    """The probed tables minus their Postgres-only checks (regex ``~``, ``jsonb_typeof``)."""
    wanted: dict[str, Table] = {}
    pending = list(_TABLES)
    while pending:  # the probed tables and everything they reference
        table = pending.pop()
        if table.name not in wanted:
            wanted[table.name] = table
            pending.extend(fk.column.table for fk in table.foreign_keys)
    md = MetaData()
    for table in wanted.values():
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'supervisor.db'}", connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.run_sync(_sqlite_metadata().create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def ctx() -> ProbeContext:
    return ProbeContext(redis=None, vault_configured=True, ws_clients=lambda: 2)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def record(_redis: object, event: dict[str, Any]) -> None:
        events.append(event)

    monkeypatch.setattr(hive_api, "publish_event", record)
    return events


@pytest_asyncio.fixture
async def client(factory: async_sessionmaker[AsyncSession], published: list[dict[str, Any]]) -> AsyncIterator[AsyncClient]:  # noqa: ARG001
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    auth_dependency = CurrentUser.__metadata__[0].dependency
    app = FastAPI()
    app.include_router(hive_api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[auth_dependency] = lambda: SimpleNamespace(id=uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------- probes
@pytest.mark.asyncio
async def test_sweep_records_latency_and_summary_for_every_commander(factory: async_sessionmaker[AsyncSession], ctx: ProbeContext) -> None:
    statuses = await sweep(factory, HiveOrchestrator(), ctx, started_at=0.0)

    assert set(statuses) == set(LegendaryBot)
    assert BotStatus.FATAL not in statuses.values()
    # No Redis client in the test: the rate limiter is honestly reported uninitialised, nothing else degrades
    assert {b for b, s in statuses.items() if s is BotStatus.DEGRADED} == {LegendaryBot.BHEESHMA}

    async with factory() as db:
        summary = await build_telemetry_summary(db)
    assert summary.total_commanders == summary.reporting_count == len(LegendaryBot)
    assert summary.stale_count == 0
    assert summary.degraded_count == 1 and summary.fatal_count == 0
    assert summary.online_count + summary.working_count == len(LegendaryBot) - 1
    assert summary.overall_health_pct == round(100.0 * (len(LegendaryBot) - 1) / len(LegendaryBot), 1)
    assert summary.avg_probe_latency_ms is not None and summary.avg_probe_latency_ms >= 0.0
    assert summary.last_sweep_at is not None and summary.last_sweep_at.tzinfo is not None
    for item in summary.commanders:
        assert item.effective_status == item.status.value and item.is_stale is False
        assert item.probe_latency_ms is not None and item.probe_latency_ms >= 0.0
        assert item.activity_summary and item.activity_summary == item.resource_metrics["activity_summary"]
        assert "probed_at" in item.resource_metrics


@pytest.mark.asyncio
async def test_summaries_state_the_measured_state(factory: async_sessionmaker[AsyncSession], ctx: ProbeContext) -> None:
    hive = HiveOrchestrator()
    async with factory() as db:
        empty = await probe_single_commander(db, hive, ctx, LegendaryBot.BAJIRAO)
        assert empty.status == BotStatus.ONLINE
        assert empty.resource_metrics["activity_summary"] == "No open positions"

        db.add(SystemSettingsModel(id=SETTINGS_SINGLETON_ID, max_daily_exposure=0))
        await db.commit()
        halted = await probe_single_commander(db, hive, ctx, LegendaryBot.KAUTILYA)
        assert halted.status == BotStatus.SLEEPING
        assert halted.resource_metrics["trading_halted"] is True
        assert halted.resource_metrics["activity_summary"] == "Trading halted · daily exposure cap is 0"

        router = await probe_single_commander(db, hive, ctx, LegendaryBot.ASHVATTHAMA)
        assert router.resource_metrics["activity_summary"] == "0 pending slips · 0 fills in 24h · 2 live sockets"


@pytest.mark.asyncio
async def test_failing_and_slow_probes_mark_only_that_commander_fatal(
    factory: async_sessionmaker[AsyncSession], ctx: ProbeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_db: AsyncSession, _ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
        raise RuntimeError("subsystem unreachable")

    async def slow(_db: AsyncSession, _ctx: ProbeContext) -> tuple[BotStatus, dict[str, Any]]:
        await asyncio.sleep(5)
        return BotStatus.ONLINE, {}

    monkeypatch.setitem(supervisor.PROBES, LegendaryBot.ARJUNA, broken)
    monkeypatch.setitem(supervisor.PROBES, LegendaryBot.KARNA, slow)
    fast_timeout = get_settings().model_copy(update={"HIVE_SUPERVISOR_PROBE_TIMEOUT_SECONDS": 0.05})

    statuses = await sweep(factory, HiveOrchestrator(), ctx, started_at=0.0, settings=fast_timeout)
    assert statuses[LegendaryBot.ARJUNA] is BotStatus.FATAL
    assert statuses[LegendaryBot.KARNA] is BotStatus.FATAL
    assert statuses[LegendaryBot.ASHOKA] is not BotStatus.FATAL

    async with factory() as db:
        summary = await build_telemetry_summary(db)
    arjuna = next(i for i in summary.commanders if i.bot_name is LegendaryBot.ARJUNA)
    karna = next(i for i in summary.commanders if i.bot_name is LegendaryBot.KARNA)
    assert summary.fatal_count == 2
    assert arjuna.activity_summary == "Probe failed · RuntimeError: subsystem unreachable"
    assert karna.activity_summary.startswith("Probe failed · TimeoutError")
    assert arjuna.probe_latency_ms is not None and karna.probe_latency_ms is not None


# --------------------------------------------------------------------------- telemetry
@pytest.mark.asyncio
async def test_unreported_and_stale_commanders_are_no_signal(factory: async_sessionmaker[AsyncSession], ctx: ProbeContext) -> None:
    settings = get_settings()
    async with factory() as db:
        before = await build_telemetry_summary(db)
        assert before.reporting_count == 0 and before.stale_count == len(LegendaryBot)
        assert before.overall_health_pct == 0.0 and before.avg_probe_latency_ms is None and before.last_sweep_at is None
        assert all(i.effective_status == NO_SIGNAL and i.activity_summary == "Awaiting first heartbeat" for i in before.commanders)

        await probe_single_commander(db, HiveOrchestrator(), ctx, LegendaryBot.ASHOKA)
        later = datetime.now(UTC) + timedelta(seconds=stale_after_seconds(settings) + 1)
        stale = await build_telemetry_summary(db, now=later)
        ashoka = next(i for i in stale.commanders if i.bot_name is LegendaryBot.ASHOKA)
        assert stale.reporting_count == 1 and stale.stale_count == len(LegendaryBot)
        assert ashoka.is_stale and ashoka.effective_status == NO_SIGNAL and ashoka.status in (BotStatus.ONLINE, BotStatus.WORKING)
        assert stale.online_count == stale.working_count == 0 and stale.overall_health_pct == 0.0
        assert commanders_event(stale)["statuses"] == {}  # a stale beat is never broadcast as live


@pytest.mark.asyncio
async def test_latency_warning_uses_the_configured_threshold(factory: async_sessionmaker[AsyncSession], ctx: ProbeContext) -> None:
    async with factory() as db:
        await probe_single_commander(db, HiveOrchestrator(), ctx, LegendaryBot.TODAR_MAL)
        strict = await build_telemetry_summary(db, settings=get_settings().model_copy(update={"HIVE_SUPERVISOR_LATENCY_WARN_MS": 1e-9}))
        lax = await build_telemetry_summary(db, settings=get_settings().model_copy(update={"HIVE_SUPERVISOR_LATENCY_WARN_MS": 1e9}))
    pick = lambda s: next(i for i in s.commanders if i.bot_name is LegendaryBot.TODAR_MAL)  # noqa: E731
    assert pick(strict).latency_warning is True and pick(lax).latency_warning is False
    assert strict.latency_warn_ms == 1e-9


def test_stale_window_follows_the_supervisor_interval() -> None:
    base = get_settings()
    off = base.model_copy(update={"HIVE_SUPERVISOR_INTERVAL_SECONDS": 0.0})
    slow = base.model_copy(update={"HIVE_SUPERVISOR_INTERVAL_SECONDS": 60.0, "HIVE_SUPERVISOR_STALE_MULTIPLIER": 3.2})
    fast = base.model_copy(update={"HIVE_SUPERVISOR_INTERVAL_SECONDS": 5.0})
    assert stale_after_seconds(off) == base.HIVE_SUPERVISOR_STALE_FLOOR_SECONDS
    assert stale_after_seconds(slow) == pytest.approx(192.0)
    assert stale_after_seconds(fast) == base.HIVE_SUPERVISOR_STALE_FLOOR_SECONDS


@pytest.mark.asyncio
async def test_developer_credit_comes_from_system_settings(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        unseeded = await build_telemetry_summary(db)
        assert unseeded.developer_credit == DEVELOPER  # the column default, not a literal in the service
        db.add(SystemSettingsModel(id=SETTINGS_SINGLETON_ID, developer_name="Desk Owner"))
        await db.commit()
        assert (await build_telemetry_summary(db)).developer_credit == "Desk Owner"


# --------------------------------------------------------------------------- API
@pytest.mark.asyncio
async def test_probe_endpoints_record_broadcast_and_summarise(client: AsyncClient, published: list[dict[str, Any]]) -> None:
    one = await client.post("/api/v1/hive/bots/probe", json={"bot_name": "TODAR MAL"})
    assert one.status_code == 200
    body = one.json()
    assert body["total_commanders"] == len(LegendaryBot) and body["reporting_count"] == 1
    todar = next(c for c in body["commanders"] if c["bot_name"] == "TODAR MAL")
    assert todar["effective_status"] == "ONLINE" and todar["probe_latency_ms"] is not None
    assert todar["activity_summary"].startswith("Database ping ")
    assert published[-1]["type"] == "commanders" and set(published[-1]["statuses"]) == {"TODAR MAL"}
    assert published[-1]["summaries"]["TODAR MAL"] == todar["activity_summary"]

    every = await client.post("/api/v1/hive/bots/probe", json={})
    assert every.status_code == 200 and every.json()["reporting_count"] == len(LegendaryBot)
    assert len(published[-1]["statuses"]) == len(LegendaryBot) and published[-1]["swept_at"]

    no_body = await client.post("/api/v1/hive/bots/probe")
    assert no_body.status_code == 200

    summary = await client.get("/api/v1/hive/telemetry-summary")
    assert summary.status_code == 200
    data = summary.json()
    assert data["reporting_count"] == len(LegendaryBot) and data["stale_count"] == 0
    assert data["developer_credit"] == DEVELOPER and data["stale_after_seconds"] > 0


@pytest.mark.asyncio
async def test_probe_rejects_unknown_commanders_and_fields(client: AsyncClient, published: list[dict[str, Any]]) -> None:
    assert (await client.post("/api/v1/hive/bots/probe", json={"bot_name": "DEVRAYA"})).status_code == 422
    assert (await client.post("/api/v1/hive/bots/probe", json={"bot_name": "ASHOKA", "force": True})).status_code == 422
    assert published == []
