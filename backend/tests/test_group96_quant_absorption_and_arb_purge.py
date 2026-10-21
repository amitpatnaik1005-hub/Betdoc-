"""Group 96: the quant toolkit lives in The Lab (PANINI); arbitrage, Phantom and GARUDA are gone.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import importlib.util
import math
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user, get_db
from app.api.v1.router import api_router
from app.api.v1.the_lab import router as lab_router
from app.models import Base
from app.models.phantom import PhantomCalculationLogModel
from app.models.the_hive import LegendaryBot
from app.services.commander_supervisor import PROBES
from app.services.multi_sport_odds_sync import screen_event

QUANT = "/api/v1/lab/quant-toolkit"
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f96a2b7c4d10_retire_garuda_into_panini.py"


# --------------------------------------------------------------------------- the roster
def test_garuda_is_retired() -> None:
    assert "GARUDA" not in {b.value for b in LegendaryBot}  # Group 97 also retired ARJUNA: 15 remain
    assert {b.value for b in LegendaryBot} == {
        "ASHOKA", "KAUTILYA", "BAJIRAO", "VIDUR", "KUMBHA", "PANINI", "PRATAP", "TODAR MAL", "ARYABHATA",
        "CHANAKYA", "SHIVAJI", "DRONA", "BHEESHMA", "KARNA", "ASHVATTHAMA",
    }
    assert set(PROBES) == set(LegendaryBot)  # every commander still has its health probe


# --------------------------------------------------------------------------- the purge
@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/api/v1/phantom/opportunities"), ("POST", "/api/v1/phantom/arbitrage"), ("POST", "/api/v1/phantom/dutching"),
    ("GET", "/api/v1/omni/arbitrage"), ("POST", "/api/v1/omni/arbitrage/execute"), ("GET", "/api/v1/portfolio/arbitrage"),
])
async def test_arbitrage_and_phantom_routes_are_gone(method: str, path: str) -> None:
    app = FastAPI()
    app.include_router(api_router)  # already prefixed /api/v1
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role="ADMIN")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.request(method, path, json={})).status_code == 404
    paths = set(app.openapi()["paths"])
    assert not any(p.startswith(("/api/v1/phantom", "/api/v1/omni/arbitrage")) for p in paths)
    assert "/api/v1/lab/quant-toolkit/dutching" in paths and "/api/v1/omni/portfolio" in paths  # the app really is mounted


def test_board_screening_emits_value_lines_only() -> None:
    # Best prices book well under 1 (a 10%+ arbitrage once): nothing but the +EV line may come out
    event = {
        "event_id": "e1", "sport_key": "soccer", "feed_key": "soccer_epl", "home": "Arsenal", "away": "Chelsea",
        "commence_time": "2026-10-25T15:00:00Z", "outcomes": ["home", "away"],
        "venue_odds": {"pinnacle": {"home": 1.95, "away": 1.95}, "stake": {"home": 2.40, "away": 1.80}, "1xbet": {"home": 1.80, "away": 2.40}},
        "sharp": {"book": "pinnacle", "fair_probabilities": {"home": 0.5, "away": 0.5}},
    }
    edges = screen_event(event, min_edge=0.02)
    assert edges and {e["kind"] for e in edges} == {"value"}
    assert {(e["venue"], e["outcome"]) for e in edges} == {("stake", "home"), ("1xbet", "away")}
    assert all("legs" not in e and "arb_return_pct" not in e for e in edges)


# --------------------------------------------------------------------------- the quant toolkit in The Lab
@pytest_asyncio.fixture
async def lab() -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PhantomCalculationLogModel.__table__])
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(lab_router, prefix="/api/v1/lab")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[CurrentUser.__metadata__[0].dependency] = lambda: SimpleNamespace(id=uuid.uuid4(), role="USER")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, sessions
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_four_calculators_compute_and_log(lab: tuple[AsyncClient, Any]) -> None:
    client, _ = lab
    dutch = (await client.post(f"{QUANT}/dutching", json={"target_total_stake": 100.0, "odds": [2.0, 4.0, 4.0]})).json()
    assert dutch["stakes"] == [50.0, 25.0, 25.0] and dutch["leg_returns"] == [100.0, 100.0, 100.0] and dutch["log"]["calc_type"] == "DUTCHING"

    matched = (await client.post(f"{QUANT}/matched-betting", json={
        "back_stake": 10.0, "back_odds": 3.0, "lay_odds": 3.2, "lay_commission_pct": 2.0, "mode": "STANDARD"})).json()
    assert matched["lay_stake"] == pytest.approx(10.0 * 3.0 / (3.2 - 0.02), abs=0.01)  # back stake x back odds / (lay odds - commission)
    assert matched["back_win_profit"] == pytest.approx(matched["lay_win_profit"], abs=0.03) and matched["log"]["calc_type"] == "MATCHED_BETTING"

    maker = (await client.post(f"{QUANT}/market-maker", json={
        "mid_price": 2.0, "inventory": 5.0, "gamma": 0.1, "volatility_sigma": 0.2, "time_horizon_t": 1.0, "current_time_t": 0.0, "liquidity_k": 1.5})).json()
    reservation = 2.0 - 5.0 * 0.1 * 0.2**2 * 1.0
    spread = 0.1 * 0.2**2 * 1.0 + (2.0 / 0.1) * math.log(1.0 + 0.1 / 1.5)
    assert maker["reservation_price"] == pytest.approx(reservation) and maker["optimal_spread"] == pytest.approx(spread)
    assert maker["optimal_bid"] < maker["reservation_price"] < maker["optimal_ask"] and maker["log"]["calc_type"] == "AVELLANEDA"

    coint = (await client.post(f"{QUANT}/cointegration", json={
        "current_z_score": 2.4, "entry_threshold": 2.0, "exit_threshold": 0.5, "stop_loss_threshold": 3.5})).json()
    assert coint["signal"] == "SELL_SPREAD" and coint["log"]["calc_type"] == "COINTEGRATION"

    history = (await client.get(f"{QUANT}/calculations")).json()
    assert sorted(h["calc_type"] for h in history) == ["AVELLANEDA", "COINTEGRATION", "DUTCHING", "MATCHED_BETTING"]
    stamps = [h["created_at"] for h in history]
    assert stamps == sorted(stamps, reverse=True)  # newest first (SQLite stamps to the second, so runs may tie)
    only = (await client.get(f"{QUANT}/calculations", params={"calc_type": "dutching", "limit": 5})).json()
    assert len(only) == 1 and only[0]["inputs"]["odds"] == [2.0, 4.0, 4.0]
    assert (await client.get(f"{QUANT}/calculations", params={"calc_type": "ARBITRAGE"})).status_code == 400
    assert (await client.get(f"{QUANT}/calculations", params={"limit": 0})).status_code == 422


@pytest.mark.asyncio
async def test_calculator_errors_are_400_not_500(lab: tuple[AsyncClient, Any]) -> None:
    client, _ = lab
    overflow = await client.post(f"{QUANT}/market-maker", json={
        "mid_price": 2.0, "inventory": 5.0, "gamma": 0.1, "volatility_sigma": 1e200, "time_horizon_t": 1.0, "current_time_t": 0.0, "liquidity_k": 1.5})
    assert overflow.status_code == 400 and "overflow" in overflow.json()["detail"]


# --------------------------------------------------------------------------- the data migration
def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("retire_garuda", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hive_tables(md: sa.MetaData) -> tuple[sa.Table, sa.Table, sa.Table]:
    # The Hive tables as a database migrated before Group 96 holds them: bot names as plain text, GARUDA allowed
    profiles = sa.Table("hive_bot_profiles", md, sa.Column("bot_name", sa.String(32), primary_key=True),
                        sa.Column("tasks_completed", sa.Integer), sa.Column("error_count", sa.Integer))
    tasks = sa.Table("hive_tasks", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("assignee_name", sa.String(32)))
    logs = sa.Table("hive_self_learning_logs", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("bot_name", sa.String(32)))
    return profiles, tasks, logs


@pytest.mark.parametrize("panini_reported", [True, False])
def test_migration_hands_garudas_history_to_panini(panini_reported: bool) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    md = sa.MetaData()
    profiles, tasks, logs = _hive_tables(md)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(profiles.insert(), [{"bot_name": "GARUDA", "tasks_completed": 7, "error_count": 2}]
                     + ([{"bot_name": "PANINI", "tasks_completed": 3, "error_count": 1}] if panini_reported else []))
        conn.execute(tasks.insert(), [{"id": 1, "assignee_name": "GARUDA"}, {"id": 2, "assignee_name": "KARNA"}, {"id": 3, "assignee_name": None}])
        conn.execute(logs.insert(), [{"id": 1, "bot_name": "GARUDA"}, {"id": 2, "bot_name": "DRONA"}])
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = {r.bot_name: (r.tasks_completed, r.error_count) for r in conn.execute(sa.select(profiles))}
        assert rows == {"PANINI": (10, 3) if panini_reported else (7, 2)}
        assert [r.assignee_name for r in conn.execute(sa.select(tasks).order_by(tasks.c.id))] == ["PANINI", "KARNA", None]
        assert [r.bot_name for r in conn.execute(sa.select(logs).order_by(logs.c.id))] == ["PANINI", "DRONA"]
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()  # a second run finds nothing left to move
        assert {r.bot_name: (r.tasks_completed, r.error_count) for r in conn.execute(sa.select(profiles))} == rows
    assert migration.down_revision == "e87c4521b38d"
