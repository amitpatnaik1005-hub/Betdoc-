"""Group 103: the Sports Engine unified into The Oracle (ARYABHATA retired into ASHOKA) and the match board (points b.4
and b.11). Developed for Amit Ashok Kumar Patnaik.

The roster and ASHOKA's absorbed self-test; the data migration on stand-in tables; the market-implied projection;
the board's windows, countries and sport order on synthetic cards; and end to end, a fixture written to the live
market in Redis showing on GET /oracle/board with its projection and country (Redis-gated, like the workbench tests).
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.v1 import oracle as oracle_api
from app.core.config import Settings, get_settings
from app.domain.oracle.match_model import dixon_coles_matrix, likeliest_score
from app.models import Base, User
from app.models.sports_config import LeagueRegistry, SportConfiguration
from app.models.the_hive import BotStatus, LegendaryBot
from app.services import commander_supervisor as supervisor
from app.services import match_board_service as mb
from app.services.sports_catalog import ensure_seeded

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "backend" / "alembic" / "versions" / "c103a7b9e2d4_retire_aryabhata_into_ashoka.py"
FOURTEEN = ["ASHOKA", "KAUTILYA", "BAJIRAO", "VIDUR", "KUMBHA", "PANINI", "PRATAP", "TODAR MAL", "CHANAKYA", "SHIVAJI", "DRONA",
            "BHEESHMA", "KARNA", "ASHVATTHAMA"]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]
_MARK = "betdoc:test-group103"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


# ================================================================ the roster
def test_the_roster_is_fourteen_with_aryabhata_retired_into_ashoka() -> None:
    assert [b.value for b in LegendaryBot] == FOURTEEN
    assert set(supervisor.PROBES) == set(LegendaryBot)


def test_the_frontend_roster_and_routes_follow() -> None:
    config = (ROOT / "frontend" / "src" / "config" / "commanders.config.ts").read_text(encoding="utf-8")
    ids = re.findall(r'"([^"]+)"', re.search(r"COMMANDER_IDS\s*=\s*\[(.*?)\]\s*as const", config, re.DOTALL).group(1))  # type: ignore[union-attr]
    assert ids == FOURTEEN and "ARYABHATA:" not in config
    ashoka = re.search(r"ASHOKA: \{(.*?)\n  \},", config, re.DOTALL).group(1)  # type: ignore[union-attr]
    assert '"/oracle", "/sports"' in ashoka
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert '<Route path="/sports" element={<Navigate replace to="/oracle?tab=engine" />} />' in app
    assert "pages/SportsEngine" not in app and "'Sports Engine'" not in app and "ARYABHATA" not in app
    assert not (ROOT / "frontend" / "src" / "pages" / "SportsEngine.tsx").exists()
    assert (ROOT / "frontend" / "src" / "components" / "oracle" / "SportsEngineDesk.tsx").exists()  # the desk lives on The Oracle


@pytest.mark.asyncio
@pytest.mark.parametrize(("passed", "recent", "status"), [(True, 3, BotStatus.WORKING), (True, 0, BotStatus.ONLINE), (False, 3, BotStatus.DEGRADED)])
async def test_ashoka_runs_aryabhatas_self_test(monkeypatch: pytest.MonkeyPatch, passed: bool, recent: int, status: BotStatus) -> None:
    async def count(_db: Any, _stmt: Any) -> int:
        return recent

    monkeypatch.setattr(supervisor, "_count", count)
    monkeypatch.setattr(supervisor, "_probability_self_test", lambda: (passed, 0.42))
    got, detail = await supervisor._ashoka(None, None)  # type: ignore[arg-type]
    assert got == status and detail["self_test_passed"] is passed and detail["self_test_ms"] == 0.42
    assert ("FAILED" in detail["activity_summary"]) is not passed


def test_the_real_self_test_passes() -> None:
    passed, ms = supervisor._probability_self_test()
    assert passed and ms >= 0


@pytest.mark.parametrize("ashoka_reported", [True, False])
def test_migration_hands_aryabhatas_history_to_ashoka(ashoka_reported: bool) -> None:
    spec = importlib.util.spec_from_file_location("retire_aryabhata", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    md = sa.MetaData()
    profiles = sa.Table("hive_bot_profiles", md, sa.Column("bot_name", sa.String(32), primary_key=True),
                        sa.Column("tasks_completed", sa.Integer), sa.Column("error_count", sa.Integer))
    tasks = sa.Table("hive_tasks", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("assignee_name", sa.String(32)))
    logs = sa.Table("hive_self_learning_logs", md, sa.Column("id", sa.Integer, primary_key=True), sa.Column("bot_name", sa.String(32)))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(profiles.insert(), [{"bot_name": "ARYABHATA", "tasks_completed": 5, "error_count": 2}]
                     + ([{"bot_name": "ASHOKA", "tasks_completed": 7, "error_count": 0}] if ashoka_reported else []))
        conn.execute(tasks.insert(), [{"id": 1, "assignee_name": "ARYABHATA"}, {"id": 2, "assignee_name": "DRONA"}])
        conn.execute(logs.insert(), [{"id": 1, "bot_name": "ARYABHATA"}, {"id": 2, "bot_name": "KARNA"}])
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert {r.bot_name: (r.tasks_completed, r.error_count) for r in conn.execute(sa.select(profiles))} == {"ASHOKA": (12, 2) if ashoka_reported else (5, 2)}
        assert [r.assignee_name for r in conn.execute(sa.select(tasks).order_by(tasks.c.id))] == ["ASHOKA", "DRONA"]
        assert [r.bot_name for r in conn.execute(sa.select(logs).order_by(logs.c.id))] == ["ASHOKA", "KARNA"]
    assert migration.down_revision == "b1013c5d7e9f" and migration.revision == "c103a7b9e2d4"


# ================================================================ the projection
def test_likeliest_score_is_the_matrix_argmax_and_never_the_folded_tail() -> None:
    m = dixon_coles_matrix(1.6, 1.1)
    got = likeliest_score(m)
    h, a = np.unravel_index(np.argmax(m[:10, :10]), (10, 10))
    assert got["score"] == f"{h}-{a}" and got["probability"] == pytest.approx(m[h, a], abs=1e-6)
    assert got["expected_goals"] == pytest.approx([float(m.sum(axis=1) @ np.arange(11)), float(m.sum(axis=0) @ np.arange(11))], abs=1e-3)
    lopsided = np.zeros((11, 11))
    lopsided[10, 0], lopsided[2, 0] = 0.6, 0.4  # the tail cell is the heaviest: it is not a scoreline
    assert likeliest_score(lopsided)["score"] == "2-0"


# ================================================================ the board (synthetic cards)
def _card(fid: str, sport: str | None, sport_key: str, *, in_play: bool = False, kickoff_in: float = 2.0, evs: tuple[float | None, ...] = (0.03, -0.02)) -> dict[str, Any]:
    return {"fixture_id": fid, "home": "H", "away": "A", "sport": sport, "sport_key": sport_key, "league": f"feed {sport_key}",
            "kickoff": (NOW + timedelta(hours=kickoff_in)).isoformat(), "in_play": in_play, "minutes_since_kickoff": 30 if in_play else None,
            "max_stake_inr": None, "projection": None,
            "markets": [{"market": "Match Odds", "kind": "MATCH_ODDS", "margins": {}, "selections": [{"leg_id": f"{fid}|Match Odds|{i}", "ev": ev} for i, ev in enumerate(evs)]}]}


@pytest_asyncio.fixture
async def sqlite_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: Base.metadata.create_all(s, tables=[SportConfiguration.__table__, LeagueRegistry.__table__, User.__table__]))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await ensure_seeded(db)
        await db.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_board_windows_countries_and_sport_order(sqlite_sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [
        _card("epl-live", "Football", "soccer_epl", in_play=True, kickoff_in=-0.5),
        _card("epl-2h", "Football", "soccer_epl", kickoff_in=2),
        _card("ipl-10h", "Cricket", "cricket_ipl", kickoff_in=10, evs=(-0.01, None)),
        _card("x-30h", "Tennis", "tennis_unmapped_cup", kickoff_in=30),
    ]

    async def board(_redis: Any, _settings: Settings, _now: datetime, *, sport: str | None = None) -> dict[str, Any]:
        assert sport is None  # the service filters, so every sport's count is known
        return {"fixtures": cards, "as_of": NOW.isoformat(), "steam_readable": True, "stats": {}}

    monkeypatch.setattr(mb.manual_parlay, "board", board)
    settings = get_settings()
    async with sqlite_sessions() as db:
        async def ids(window: mb.Window, sport: str | None = None) -> list[str]:
            return [c["fixture_id"] for c in (await mb.match_board(None, db, settings, NOW, window=window, sport=sport))["fixtures"]]  # type: ignore[arg-type]

        assert await ids("all") == ["epl-live", "epl-2h", "ipl-10h", "x-30h"]
        assert await ids("live") == ["epl-live"]
        assert await ids("3h") == ["epl-2h"]
        assert await ids("12h") == ["epl-2h", "ipl-10h"]
        assert await ids("upcoming") == ["epl-2h", "ipl-10h", "x-30h"]
        assert await ids("all", "Cricket") == ["ipl-10h"]
        full = await mb.match_board(None, db, settings, NOW, window="all")  # type: ignore[arg-type]
    assert [s["sport"] for s in full["sports"]][:2] == ["Cricket", "Football"]  # MATCH_BOARD_PRIORITY_SPORTS first
    counts = {s["sport"]: (s["count"], s["live"]) for s in full["sports"]}
    assert counts["Football"] == (2, 1) and counts["Cricket"] == (1, 0) and counts["Tennis"] == (1, 0) and counts["Esports"] == (0, 0)
    by_id = {c["fixture_id"]: c for c in full["fixtures"]}
    assert (by_id["epl-2h"]["country"], by_id["epl-2h"]["league_name"]) == ("England", "Premier League")
    assert (by_id["ipl-10h"]["country"], by_id["ipl-10h"]["league_name"]) == ("India", "Indian Premier League")
    assert by_id["x-30h"]["country"] is None and by_id["x-30h"]["league_name"] == "feed tennis_unmapped_cup"  # never given a country
    assert by_id["epl-2h"]["best_ev"] == 0.03 and by_id["ipl-10h"]["best_ev"] == -0.01 and by_id["epl-2h"]["market_count"] == 1
    assert full["countries"][0] == {"country": "England", "count": 2, "leagues": [{"league": "Premier League", "count": 2}]}
    assert full["countries"][-1]["country"] is None  # unmapped feeds last


# ================================================================ end to end through Redis
@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(oracle_api.router, prefix="/api/v1/oracle")
    app.state.redis = redis

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: User(id=uuid.uuid4(), username="board", hashed_password="x", role="USER")
    return app


@pytest.mark.asyncio
async def test_a_live_market_fixture_on_the_board(sqlite_sessions, redis: Redis) -> None:
    from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, seed  # noqa: PLC0415

    settings = get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya_g103", "TWIN_PREFIX": "test_twin_g103"})
    await seed(redis, settings, "fx-ars-che", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)  # soccer_epl, kicking off in 2h
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sqlite_sessions, redis, settings)), base_url="http://test") as client:
        body = (await client.get("/api/v1/oracle/board")).json()
        card = body["fixtures"][0]
        assert (card["fixture_id"], card["country"], card["league_name"], card["sport"]) == ("fx-ars-che", "England", "Premier League", "Football")
        assert re.fullmatch(r"\d-\d", card["projection"]["score"]) and 0 < card["projection"]["probability"] < 1
        odds = next(m for m in card["markets"] if m["market"] == "Match Odds")
        assert {s["selection"] for s in odds["selections"]} == {"HOME", "DRAW", "AWAY"} and card["market_count"] == len(card["markets"])
        assert body["countries"][0]["country"] == "England" and body["sports"][1] == {"sport": "Football", "count": 1, "live": 0, "priority": True}
        assert (await client.get("/api/v1/oracle/board", params={"window": "live"})).json()["fixtures"] == []
        assert len((await client.get("/api/v1/oracle/board", params={"window": "3h"})).json()["fixtures"]) == 1
        assert (await client.get("/api/v1/oracle/board", params={"sport": "Cricket"})).json()["fixtures"] == []
        assert (await client.get("/api/v1/oracle/board", params={"sport": "Curling"})).status_code == 422
        assert (await client.get("/api/v1/oracle/board", params={"window": "1w"})).status_code == 422
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sqlite_sessions, None, settings)), base_url="http://test") as client:
        assert (await client.get("/api/v1/oracle/board")).status_code == 503  # no live market: refused, never a stand-in board


@pytest.mark.asyncio
async def test_a_two_way_match_winner_is_priced_not_crashed(sqlite_sessions, redis: Redis) -> None:
    """Before Group 103 a two-way market (no book offers a draw: cricket, tennis, MMA) had no consensus, and the board
    raised on its first leg; now it is de-vigged over home and away, with no scoreline fit (a football model)."""
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    from app.core.live_odds import publish_board_ticks  # noqa: PLC0415
    from app.schemas.market import MarketTick  # noqa: PLC0415
    from app.services.aryabhata_pipeline import AryabhataKeys  # noqa: PLC0415

    settings = get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya_g103b", "TWIN_PREFIX": "test_twin_g103b"})
    await publish_board_ticks(redis, [MarketTick(match_id="fx-csk-mi", home_team="Chennai Super Kings", away_team="Mumbai Indians", market_type="Match Odds",
                                                 selection="HOME", odds=Decimal("2"), true_probability=Decimal("0.5"), is_suspended=False, sport_key="cricket_ipl",
                                                 commence_time=datetime.now(UTC) + timedelta(hours=5))])
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    await redis.sadd(keys.markets("fx-csk-mi"), "Match Odds")
    books = {"pinnacle": {"HOME": 1.90, "AWAY": 1.95}, "onexbet": {"HOME": 1.98, "AWAY": 1.88}, "parimatch": {"HOME": 1.95, "AWAY": 1.90}}
    for book, prices in books.items():
        await redis.hset(keys.books("fx-csk-mi|Match Odds"), f"odds_api|{book}", json.dumps({"s": "odds_api", "b": book, "p": prices, "t": time.time() - 3, "x": False}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sqlite_sessions, redis, settings)), base_url="http://test") as client:
        res = await client.get("/api/v1/oracle/board", params={"sport": "Cricket"})
    assert res.status_code == 200
    card = res.json()["fixtures"][0]
    assert (card["country"], card["league_name"], card["projection"]) == ("India", "Indian Premier League", None)
    odds = next(m for m in card["markets"] if m["market"] == "Match Odds")
    fair = {s["selection"]: s["fair_probability"] for s in odds["selections"]}
    assert set(fair) == {"HOME", "AWAY"} and sum(fair.values()) == pytest.approx(1.0, abs=1e-3)  # no draw leg is offered


def test_the_board_route_is_signed_in_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    assert app.openapi()["paths"]["/api/v1/oracle/board"]["get"].get("security")
