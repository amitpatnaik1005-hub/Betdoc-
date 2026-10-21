"""Group 85: live competitive market intelligence (Commanders KARNA & PANINI). Developed for Amit Ashok Kumar Patnaik.

* the margin, RLM, trap, verdict and edge-score mathematics (thresholds from settings; an unmeasured share fires nothing);
* end to end through Redis and the API: a two-way cricket market and a 1X2 football market priced by Parimatch, 1xBet
  and Pinnacle on the live board, public splits measured by a named feed, the USD rate from TODAR MAL's cache: the
  sync, Cricket and Football first, the trap filter, the measured margins and price shares, the derived public-trap
  grades, an administrator's recorded pick surviving a re-sync, and the first read syncing on its own.

The FA-7 routes keep their own suite (``test_api.py``, ``test_manager.py``) unchanged.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user, get_db
from app.api.v1.competitive_intel import router as competitive_intel_router
from app.core.config import Settings, get_settings
from app.core.live_odds import publish_board_ticks
from app.domain.competitive_intel import live_consensus_math as lcm
from app.domain.oracle.match_model import consensus_two_way
from app.models import User
from app.models.competitive_intel_live import BookmakerMarginBenchmark, CompetitorPublicConsensusSnapshot, CompetitorSignalFeedItem
from app.models.control_panel import SystemSettingsModel
from app.models.paper_trading import CurrencyExchangeRate
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.schemas.market import MarketTick
from app.schemas.twin import FixtureIntel, PublicSplitsIntel, SelectionSplit
from app.services.aryabhata_pipeline import AryabhataKeys
from app.services.twin.intel import write_intel

D = Decimal
TABLES = [User.__table__, SystemSettingsModel.__table__, UserPlacedBet.__table__, UserPlacedLeg.__table__, CurrencyExchangeRate.__table__,
          CompetitorPublicConsensusSnapshot.__table__, BookmakerMarginBenchmark.__table__, CompetitorSignalFeedItem.__table__]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-karna"
BASE = "/api/v1/rnd/competitive-intel"
DEVELOPER = "Amit Ashok Kumar Patnaik"
USD_INR = D("84.10")


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
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


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_karna", "CURRENCY_CACHE_KEY": "test:fx:rates:latest"})


def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(competitive_intel_router, prefix=BASE)
    app.state.redis = redis

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


async def make_admin(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        user = User(id=uuid.uuid4(), username=f"karna_{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
        session.add(user)
        await session.commit()
        return user


async def market(redis: Redis, settings: Settings, fixture: str, home: str, away: str, sport_key: str, books: dict[str, dict[str, float]]) -> None:
    """The live board and one Aryabhata hash entry per book: what Garuda and Aryabhata write."""
    kickoff = datetime.now(UTC) + timedelta(hours=3)
    await publish_board_ticks(redis, [MarketTick(match_id=fixture, home_team=home, away_team=away, market_type="Match Odds", selection="HOME", odds=D("2"),
                                                 true_probability=D("0.5"), is_suspended=False, sport_key=sport_key, commence_time=kickoff)])
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    await redis.sadd(keys.markets(fixture), "Match Odds")
    for book, prices in books.items():
        await redis.hset(keys.books(f"{fixture}|Match Odds"), f"odds_api|{book}", json.dumps({"s": "odds_api", "b": book, "p": prices, "t": time.time() - 3, "x": False}))


async def splits(redis: Redis, settings: Settings, fixture: str, shares: dict[str, tuple[float, float | None]], opening: dict[str, float]) -> None:
    await write_intel(redis, settings, fixture, FixtureIntel(public_splits=PublicSplitsIntel(
        source="Splits Feed", observed_at=datetime.now(UTC),
        splits={"Match Odds": {s: SelectionSplit(tickets_pct=t, money_pct=m) for s, (t, m) in shares.items()}},
        opening_odds={"Match Odds": opening})))


async def usd_rate(redis: Redis, settings: Settings) -> None:
    await redis.hset(settings.CURRENCY_CACHE_KEY, "USD_INR", json.dumps({"rate": str(USD_INR), "as_of": datetime.now(UTC).isoformat(), "provider": "test"}))


CRICKET = {"pinnacle": {"HOME": 1.95, "AWAY": 1.95}, "parimatch": {"HOME": 1.82, "AWAY": 2.02}, "onexbet": {"HOME": 1.80, "AWAY": 2.06}}
FOOTBALL = {"pinnacle": {"HOME": 2.78, "DRAW": 3.10, "AWAY": 2.75}, "parimatch": {"HOME": 3.60, "DRAW": 2.95, "AWAY": 2.60},
            "onexbet": {"HOME": 3.02, "DRAW": 2.98, "AWAY": 2.64}}
TENNIS = {"pinnacle": {"HOME": 1.80, "AWAY": 2.05}, "parimatch": {"HOME": 1.75, "AWAY": 2.10}, "onexbet": {"HOME": 1.78, "AWAY": 2.04}}


async def seed_board(redis: Redis, settings: Settings) -> None:
    await market(redis, settings, "fx-mi-csk", "Mumbai Indians", "Chennai Super Kings", "cricket_ipl", CRICKET)
    await market(redis, settings, "fx-get-ray", "Getafe", "Rayo Vallecano", "soccer_spain_la_liga", FOOTBALL)
    await market(redis, settings, "fx-alc-sin", "Carlos Alcaraz", "Jannik Sinner", "tennis_atp_paris", TENNIS)
    await splits(redis, settings, "fx-mi-csk", {"HOME": (0.74, 0.32), "AWAY": (0.26, 0.68)}, {"HOME": 1.74})
    await splits(redis, settings, "fx-get-ray", {"HOME": (0.18, 0.55), "DRAW": (0.12, 0.15), "AWAY": (0.70, 0.30)}, {"AWAY": 2.50})
    await usd_rate(redis, settings)


# ================================================================ the mathematics
def test_margins_rlm_traps_and_the_edge_score() -> None:
    pm = lcm.compute_bookmaker_margin([D("1.82"), D("2.02")])
    ox = lcm.compute_bookmaker_margin([1.80, 2.06])
    assert pm.overround_pct == D("4.45") and ox.overround_pct == D("4.10") and ox.payout_efficiency_pct > pm.payout_efficiency_pct
    assert pm.payout_efficiency_pct == D("95.74") and abs(sum(pm.fair_probabilities) - 1) <= D("0.0002")
    with pytest.raises(ValueError):
        lcm.compute_bookmaker_margin([1.0, 2.0])
    policy = lcm.ConsensusPolicy.from_settings(get_settings())
    assert lcm.detect_reverse_line_movement(74.0, 32.0, 1.74, 1.82, policy)  # the crowd on it, the price drifting out
    assert lcm.detect_reverse_line_movement(26.5, 64.0, 2.42, 2.28, policy)  # the crowd off it, the money cutting it
    assert not lcm.detect_reverse_line_movement(70.0, 72.0, 1.80, 1.72, policy)  # a normal move with the public
    assert not lcm.detect_reverse_line_movement(74.0, 32.0, None, 1.82, policy) and not lcm.detect_reverse_line_movement(None, 64.0, 2.42, 2.28, policy)
    trap = lcm.evaluate_consensus_signal(public_ticket_pct=82.0, money_pct=35.0, opening_odds=1.44, best_odds=1.53, cheapest_margin_pct=4.12, betdoc_true_prob=0.6098, policy=policy)
    assert trap.public_trap_flag and trap.verdict == "PUBLIC_TRAP" and trap.rlm_detected and trap.divergence_pct == D("-47.00")
    w = policy.weights
    ev = (0.6098 * 1.53 - 1) * 100
    expected = w["base"] + max(w["ev_min"], min(w["ev_max"], ev * w["ev_weight"])) + max(w["divergence_min"], -47 * w["divergence_weight"]) - w["trap_penalty"] + w["tight_margin_bonus"]
    assert trap.contrarian_edge_score == D(str(round(max(0.0, expected), 2))).quantize(D("0.01"))
    sharp = lcm.evaluate_consensus_signal(public_ticket_pct=26.5, money_pct=64.0, opening_odds=2.42, best_odds=2.32, cheapest_margin_pct=2.99, betdoc_true_prob=0.4762, policy=policy)
    assert not sharp.public_trap_flag and sharp.rlm_detected and sharp.verdict == "SHARP_CONTRARIAN" and sharp.betdoc_ev_pct == D("10.48")
    blind = lcm.evaluate_consensus_signal(public_ticket_pct=None, money_pct=None, opening_odds=None, best_odds=2.32, cheapest_margin_pct=None, betdoc_true_prob=0.4762, policy=policy)
    assert blind.divergence_pct is None and not blind.rlm_detected and not blind.public_trap_flag and blind.verdict == "NEUTRAL"  # nothing measured: nothing fires
    assert blind.contrarian_edge_score == D(str(w["base"] + min(w["ev_max"], 10.48 * w["ev_weight"]))).quantize(D("0.01"))


# ================================================================ end to end
@pytest.mark.asyncio
async def test_the_live_board_sync_ranks_measures_and_grades(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    admin = await make_admin(sessions)
    await seed_board(redis, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, admin)), base_url="http://test") as client:
        synced = (await client.post(f"{BASE}/live-consensus/sync")).json()
        assert synced == {"status": "synced", "snapshots_synced": 7, "benchmarks_synced": 9, "signals_synced": 2, "fixtures_read": 3,
                          "usd_rate": float(USD_INR), "architect": DEVELOPER}
        rows = (await client.get(f"{BASE}/live-consensus")).json()
        assert [r["sport"] for r in rows] == ["Cricket"] * 2 + ["Football"] * 3 + ["Tennis"] * 2  # Cricket and Football first
        mi = {r["selection"]: r for r in rows if r["match_id"] == "fx-mi-csk"}
        home, away = mi["HOME"], mi["AWAY"]
        assert (home["parimatch_odds"], home["one_xbet_odds"], home["parimatch_margin_pct"], home["one_xbet_margin_pct"]) == (1.82, 1.8, 4.45, 4.1)
        fair = consensus_two_way([CRICKET["pinnacle"], CRICKET["parimatch"], CRICKET["onexbet"]], ["HOME", "AWAY"])
        assert fair is not None and home["betdoc_true_prob"] == pytest.approx(fair["HOME"], abs=1e-4) and home["sharp_fair_odds"] == pytest.approx(1 / fair["HOME"], abs=1e-3)
        assert (home["public_ticket_pct"], home["sharp_money_pct"], home["public_source"], home["divergence_pct"]) == (74.0, 32.0, "Splits Feed", -42.0)
        assert home["rlm_detected"] and home["public_trap_flag"] and home["verdict"] == "PUBLIC_TRAP" and home["recommended_stake_inr"] == 0.0
        assert home["best_bookmaker"] == "Parimatch" and away["best_bookmaker"] == "1xBet" and "PUBLIC TRAP" in home["plain_english_summary"]
        assert not away["rlm_detected"] and away["opening_odds"] is None and away["divergence_pct"] == 42.0
        getafe = {r["selection"]: r for r in rows if r["match_id"] == "fx-get-ray"}
        assert getafe["HOME"]["betdoc_ev_pct"] > 0 and getafe["HOME"]["recommended_stake_inr"] > 0 and getafe["AWAY"]["public_trap_flag"]
        stake = D(str(getafe["HOME"]["recommended_stake_inr"]))
        assert D(str(getafe["HOME"]["recommended_stake_usd"])) == (stake / USD_INR).quantize(D("0.01"))
        assert stake % settings.TWIN_STAKE_STEP_INR == 0 and stake <= settings.COMPETITIVE_LIVE_REFERENCE_BANKROLL_INR * D(str(settings.CFO_MAX_SINGLE_STAKE_PCT))  # KUMBHA's cap
        tennis = [r for r in rows if r["sport"] == "Tennis"]
        assert all(r["public_ticket_pct"] is None and r["verdict"] != "PUBLIC_TRAP" and not r["rlm_detected"] for r in tennis)
        assert all("No public split measured" in r["plain_english_summary"] for r in tennis)

        traps = (await client.get(f"{BASE}/live-consensus", params={"public_trap_only": True})).json()
        assert sorted((r["match_id"], r["selection"]) for r in traps) == [("fx-get-ray", "AWAY"), ("fx-mi-csk", "HOME")]
        assert {r["sport"] for r in (await client.get(f"{BASE}/live-consensus", params={"sport": "Cricket"})).json()} == {"Cricket"}

        bench = (await client.get(f"{BASE}/bookmaker-benchmarks", params={"sport": "Cricket"})).json()
        by_key = {b["bookmaker_key"]: b for b in bench}
        assert set(by_key) == {"parimatch", "1xbet", "sharp_consensus"} and [b["bookmaker_key"] for b in bench][0] == "sharp_consensus"
        assert by_key["parimatch"]["measured_overround_pct"] == 4.45 and by_key["1xbet"]["best_price_share_pct"] == 50.0 and by_key["parimatch"]["sample_size"] == 1
        assert by_key["sharp_consensus"]["measured_overround_pct"] == 0.0 and by_key["sharp_consensus"]["best_price_share_pct"] is None

        feed = {s["match_id"]: s for s in (await client.get(f"{BASE}/public-traps")).json()}
        assert set(feed) == {"fx-mi-csk", "fx-get-ray"}  # tennis: no crowd measured, no pick to grade
        assert feed["fx-mi-csk"]["betdoc_verdict"] == "PUBLIC_TRAP" and feed["fx-mi-csk"]["public_backing_pct"] == 74.0 and feed["fx-mi-csk"]["origin"] == "DERIVED"
        assert feed["fx-mi-csk"]["betdoc_recommended_pick"] == "Skip this market" and feed["fx-mi-csk"]["stake_inr"] == 0.0  # no side is worth backing
        assert feed["fx-get-ray"]["betdoc_verdict"] == "PUBLIC_TRAP" and feed["fx-get-ray"]["public_selection"] == "AWAY"
        assert feed["fx-get-ray"]["betdoc_recommended_pick"].endswith("Getafe @ 3.60") and feed["fx-get-ray"]["best_bookmaker"] == "Parimatch" and feed["fx-get-ray"]["stake_inr"] > 0

        # an administrator records a named source's pick; a re-sync rebuilds the derived picks and re-grades it, never drops it
        recorded = await client.post(f"{BASE}/public-traps", json={"source_site": "Covers  Consensus", "match_id": "fx-mi-csk", "selection": "AWAY", "public_backing_pct": 63})
        assert recorded.status_code == 201 and recorded.json()["origin"] == "RECORDED" and recorded.json()["source_site"] == "Covers Consensus"
        assert recorded.json()["recorded_by"] == admin.username and recorded.json()["betdoc_verdict"] == "NEUTRAL"
        missing = await client.post(f"{BASE}/public-traps", json={"source_site": "Covers", "match_id": "fx-nowhere", "selection": "HOME", "public_backing_pct": 80})
        assert missing.status_code == 404 and missing.json()["detail"]["reason"] == "NOT_ON_BOARD"
        assert (await client.post(f"{BASE}/live-consensus/sync")).json()["signals_synced"] == 3
        feed = (await client.get(f"{BASE}/public-traps")).json()
        assert sorted((s["origin"], s["match_id"]) for s in feed) == [("DERIVED", "fx-get-ray"), ("DERIVED", "fx-mi-csk"), ("RECORDED", "fx-mi-csk")]


@pytest.mark.asyncio
async def test_the_first_read_syncs_itself_and_no_redis_refuses_a_sync(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    admin = await make_admin(sessions)
    await market(redis, settings, "fx-ind-aus", "India", "Australia", "cricket_test_match", CRICKET)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, admin)), base_url="http://test") as client:
        rows = (await client.get(f"{BASE}/live-consensus")).json()
        assert len(rows) == 2 and all(r["recommended_stake_usd"] is None for r in rows)  # no fresh USD rate: no USD figure, never a guessed one
    async with sessions() as session:
        assert len(list((await session.execute(select(CompetitorPublicConsensusSnapshot))).scalars())) == 2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, admin)), base_url="http://test") as client:
        refused = await client.post(f"{BASE}/live-consensus/sync")
        assert refused.status_code == 503 and refused.json()["detail"]["reason"] == "NO_REDIS"
        assert len((await client.get(f"{BASE}/live-consensus")).json()) == 2  # the stored board still reads
