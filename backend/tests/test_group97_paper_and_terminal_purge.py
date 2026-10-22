"""Group 97: the Paper Trading Sandbox, its execution terminal and ARJUNA are retired; TODAR MAL's FX and the 115BBJ
ledger stay, ASHVATTHAMA watches real tickets and KUMBHA inherits ARJUNA's reconciliation watch.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
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

from app.api.deps import get_current_user
from app.api.v1.router import api_router
from app.core.celery_app import celery_app
from app.models import Base, User
from app.models.consultant import ConsultantVerdictModel
from app.models.real_money import RealBetTicket, RealTicketStatus
from app.models.tax_and_fx import CurrencyExchangeRate, TaxLedgerEntry
from app.models.the_hive import BotStatus, LegendaryBot
from app.services.commander_supervisor import PROBES, ProbeContext

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "a97c5e1d2f30_retire_arjuna_and_paper_trading.py"


# --------------------------------------------------------------------------- the roster
def test_arjuna_and_garuda_stay_retired() -> None:
    # The exact roster is test_commander_parity's (and the latest retirement's) to pin; later groups retire more
    assert {"ARJUNA", "GARUDA"}.isdisjoint(b.value for b in LegendaryBot)
    assert "KUMBHA" in {b.value for b in LegendaryBot}  # ARJUNA's successor
    assert set(PROBES) == set(LegendaryBot)


# --------------------------------------------------------------------------- the purge
@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/api/v1/paper-trading/account"), ("POST", "/api/v1/paper-trading/place-bet"), ("GET", "/api/v1/paper-trading/bets"),
    ("GET", "/api/v1/paper-trading/performance"), ("POST", "/api/v1/paper-trading/execution/live-ticket"),
    ("GET", "/api/v1/paper-trading/currency/board"), ("GET", "/api/v1/paper-trading/tax/estimate"),
])
async def test_sandbox_routes_are_gone(method: str, path: str) -> None:
    app = FastAPI()
    app.include_router(api_router)  # already prefixed /api/v1
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role="ADMIN")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.request(method, path, json={})).status_code == 404
    paths = set(app.openapi()["paths"])
    assert not any(p.startswith("/api/v1/paper-trading") for p in paths)
    moved = {"/api/v1/currency/rate", "/api/v1/currency/convert", "/api/v1/currency/board", "/api/v1/currency/refresh", "/api/v1/tax/estimate"}
    assert moved <= paths and "/api/v1/real-money/tax-certificate" in paths  # moved, not lost; the real-money vault stays


def test_the_beat_no_longer_settles_paper_bets() -> None:
    import app.workers.currency_tasks  # noqa: F401, PLC0415 - registers the task

    assert "paper-trading-auto-settlement" not in celery_app.conf.beat_schedule
    assert "currency-refresh-rates" in celery_app.conf.beat_schedule and "real-money-auto-settlement" in celery_app.conf.beat_schedule
    assert "paper_trading.settle_pending_paper_bets" not in celery_app.tasks and "currency.refresh_rates" in celery_app.tasks


def test_paper_models_are_unmapped_and_the_tax_ledger_stands_alone() -> None:
    assert not {"paper_accounts", "paper_bets", "paper_ledger_entries"} & set(Base.metadata.tables)
    assert {fk.column.table.name for fk in TaxLedgerEntry.__table__.foreign_keys} == {"users"}  # no FK to the unmapped paper_bets
    assert TaxLedgerEntry.__table__.c.paper_bet_id.nullable


# --------------------------------------------------------------------------- what stays, on a database
@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__, RealBetTicket.__table__, TaxLedgerEntry.__table__, CurrencyExchangeRate.__table__,
                                                                  ConsultantVerdictModel.__table__])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user(db: AsyncSession) -> uuid.UUID:
    user = User(id=uuid.uuid4(), username=f"trader-{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
    db.add(user)
    await db.flush()
    return user.id


@pytest.mark.asyncio
async def test_tax_ledger_and_fx_rates_persist(sessions: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    async with sessions() as db:
        uid = await _user(db)
        db.add(TaxLedgerEntry(user_id=uid, gross_payout=Decimal("25000.00"), stake=Decimal("10000.00"), net_winnings=Decimal("15000.00"),
                              tds_rate=0.30, tds_amount=Decimal("4500.00"), net_post_tax_payout=Decimal("20500.00"), financial_year="2026-27"))
        db.add(CurrencyExchangeRate(base_currency="USD", quote_currency="INR", rate=Decimal("84.10"), provider="frankfurter:ecb", last_updated_at=now))
        await db.commit()
    async with sessions() as db:
        entry = (await db.execute(sa.select(TaxLedgerEntry))).scalar_one()
        assert (entry.mode, entry.paper_bet_id, entry.tds_amount, entry.section) == ("LIVE", None, Decimal("4500.00"), "115BBJ")
        rate = (await db.execute(sa.select(CurrencyExchangeRate))).scalar_one()
        assert (rate.base_currency, rate.quote_currency, rate.rate) == ("USD", "INR", Decimal("84.10"))


@pytest.mark.asyncio
async def test_ashvatthama_counts_placed_real_tickets(sessions: async_sessionmaker[AsyncSession]) -> None:
    ctx = ProbeContext(redis=None, vault_configured=True, ws_clients=lambda: 3)
    async with sessions() as db:
        status, metrics = await PROBES[LegendaryBot.ASHVATTHAMA](db, ctx)
        assert (status, metrics["open_real_tickets"], metrics["activity_summary"]) == (BotStatus.ONLINE, 0, "0 verdicts signed in 24h · 0 open real tickets · 3 live sockets")

        uid = await _user(db)
        common: dict[str, Any] = {
            "user_id": uid, "sport": "soccer", "league": "EPL", "match_id": "m1", "event_name": "Arsenal v Chelsea", "market_type": "h2h",
            "selection": "HOME", "recommended_stake": Decimal("1000.00"), "target_odds": Decimal("2.10"), "minimum_profitable_odds": Decimal("1.95"),
            "true_probability": 0.52, "model_edge_pct": 9.2, "deep_link_url": "https://example.test", "clipboard_payload": "slip",
        }
        db.add_all([RealBetTicket(ticket_reference="T-PLACED", status=RealTicketStatus.PLACED.value, **common),
                    RealBetTicket(ticket_reference="T-BUILT", status=RealTicketStatus.PENDING_PLACEMENT.value, **common)])
        await db.commit()
        status, metrics = await PROBES[LegendaryBot.ASHVATTHAMA](db, ctx)
        assert (status, metrics["open_real_tickets"]) == (BotStatus.WORKING, 1)  # only a placed ticket is open money


# --------------------------------------------------------------------------- the migration
def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("retire_arjuna", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kumbha_reported", [True, False])
def test_migration_hands_arjunas_history_to_kumbha(kumbha_reported: bool) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    md = sa.MetaData()
    profiles = sa.Table("hive_bot_profiles", md, sa.Column("bot_name", sa.String(32), primary_key=True),
                        sa.Column("tasks_completed", sa.Integer), sa.Column("error_count", sa.Integer))
    tasks = sa.Table("hive_tasks", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("assignee_name", sa.String(32)))
    logs = sa.Table("hive_self_learning_logs", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("bot_name", sa.String(32)))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(profiles.insert(), [{"bot_name": "ARJUNA", "tasks_completed": 4, "error_count": 1}]
                     + ([{"bot_name": "KUMBHA", "tasks_completed": 2, "error_count": 0}] if kumbha_reported else []))
        conn.execute(tasks.insert(), [{"id": 1, "assignee_name": "ARJUNA"}, {"id": 2, "assignee_name": "DRONA"}])
        conn.execute(logs.insert(), [{"id": 1, "bot_name": "ARJUNA"}])
        with Operations.context(MigrationContext.configure(conn)):
            migration._retire_commander()
        assert {r.bot_name: (r.tasks_completed, r.error_count) for r in conn.execute(sa.select(profiles))} == {"KUMBHA": (6, 1) if kumbha_reported else (4, 1)}
        assert [r.assignee_name for r in conn.execute(sa.select(tasks).order_by(tasks.c.id))] == ["KUMBHA", "DRONA"]
        assert [r.bot_name for r in conn.execute(sa.select(logs))] == ["KUMBHA"]
    assert migration.down_revision == "f96a2b7c4d10"
