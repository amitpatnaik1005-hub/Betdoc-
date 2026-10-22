"""Group 108: VIDUR's four-column intelligence board (point b.14b) and the neural event bus (point b.9). Developed for
Amit Ashok Kumar Patnaik.

Pure: the categories follow the events the Wire read (a catalyst is ODDS_STEAM), the tiers follow the host and the
credibility table, the summary is the source's own first two sentences, a probability move needs both readings, an
EV shift needs the same leg; a chain counts its steps and a slip is the same slip in any order.

The transport: a frame goes through Redis or, without it, to this process's listeners, never both; the history is
trimmed and expires; a claim is a set-if-absent lock.

End to end, on SQLite and PostgreSQL with the live market in Redis: a story ingested by the Wire's scan reaches the
board against its fixture with the consensus when it landed and now, and the market moving after it shows as a
measured move; rated absences, a forecast that matters and a fixture's best leg above fair are on it; the filters and
the counts agree. The scan's CRITICAL story starts a system chain. A user's chain runs the five steps on the real
engines (the Dixon-Coles fit, a fresh Ashoka read, the Slip Score trial, the smallcases), emits its frames in order to
its own audience only, remembers the best leg and the Slip Score so the next run reports the change, and says SKIPPED
with the reason when an item names no fixture. The socket forwards the system's frames and the viewer's own only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import redis as sync_redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_ws_user
from app.api.v1 import neural_bus as bus_api
from app.api.v1 import the_wire as wire_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.neural_bus.chain import MODULES, Chain, Module, Status, Step, slip_signature
from app.domain.the_wire import intel_board as ib
from app.models import User
from app.models.cfo_vault import BankrollAccount
from app.models.market_signals import MarketTickModel
from app.models.tax_and_fx import CurrencyExchangeRate
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment, WeatherSnapshot
from app.schemas.market_signals import SteamAlert
from app.schemas.the_wire import NewsItem
from app.services import neural_bus as bus
from app.services import neural_chain
from app.services.the_wire import board as wire_board
from app.services.the_wire import news
from app.services.the_wire.fixtures import Fixture
from tests.test_backtest_and_parlay import TABLES, _sqlite_metadata, app_for, make_user, redis  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market
from tests.test_ultra_vetting import seed

S = get_settings()
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
EXTRA = [CurrencyExchangeRate.__table__, NewsArticleSentiment.__table__, InjuryRosterReport.__table__, WeatherSnapshot.__table__, MarketTickModel.__table__]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
DEVELOPER = "Amit Ashok Kumar Patnaik"


@pytest.fixture
def settings() -> Settings:
    # the Wire's own prefixes; no network; HIGH stories start a chain too (the seeded fixtures kick off beyond the critical window)
    return get_settings().model_copy(update={
        "ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_wb", "WIRE_PREFIX": "test_wire_g108", "NEURAL_BUS_PREFIX": "test_neural",
        "ASHOKA_MC_PATHS": 4_000, "ORACLE_TIMEZONE": "Asia/Kolkata", "ODDS_API_KEY": None, "NEWSAPI_ORG_API_KEY": None, "WIRE_NEWS_FEEDS": [],
        "NEURAL_BUS_AUTO_IMPACTS": ["CRITICAL", "HIGH"],
    })


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [*TABLES, *EXTRA]
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        md = _sqlite_metadata()
        for table in EXTRA:
            table.to_metadata(md)
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=tables))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(tables))))
        await engine.dispose()


# ================================================================ the board's rules (pure)
def test_categories_tiers_and_summaries_follow_what_was_read() -> None:
    assert ib.category_of(["ABSENCE", "WEATHER"], catalyst=False) is ib.Category.LINEUP_INJURY  # the first category its events name
    assert ib.category_of(["DOUBT"], catalyst=False) is ib.Category.LINEUP_INJURY and ib.category_of(["RETURN"], catalyst=False) is ib.Category.LINEUP_INJURY
    assert ib.category_of(["WEATHER"], catalyst=False) is ib.Category.WEATHER_PITCH
    assert ib.category_of(["ROTATION"], catalyst=False) is ib.Category.TACTICAL_ROTATION and ib.category_of(["SACKING"], catalyst=False) is ib.Category.TACTICAL_ROTATION
    assert ib.category_of(["ABSENCE"], catalyst=True) is ib.Category.ODDS_STEAM  # the market followed it
    assert ib.category_of([], catalyst=False) is ib.Category.GENERAL

    official, press_min = S.WIRE_OFFICIAL_SOURCE_HOSTS, S.WIRE_PRESS_MIN_CREDIBILITY
    assert ib.tier_of("https://www.premierleague.com/news/1", 0.4, official_hosts=official, press_min=press_min) is ib.Tier.TIER_1_OFFICIAL
    assert ib.tier_of("https://www.iplt20.com/news/9", 0.4, official_hosts=official, press_min=press_min) is ib.Tier.TIER_1_OFFICIAL
    assert ib.tier_of("https://www.bbc.co.uk/sport/1", 1.0, official_hosts=official, press_min=press_min) is ib.Tier.TIER_2_PRESS  # the press, not an official
    assert ib.tier_of("https://www.thesun.co.uk/sport/1", 0.4, official_hosts=official, press_min=press_min) is ib.Tier.TIER_3_WIRE
    assert ib.tier_of("https://notpremierleague.com/x", 0.4, official_hosts=official, press_min=press_min) is ib.Tier.TIER_3_WIRE  # a suffix, not a substring

    text = "<p>Saka passed fit.</p> Arteta confirmed it on Friday! He starts on the right. A fourth line."
    assert ib.two_sentences(text) == "Saka passed fit. Arteta confirmed it on Friday!"
    assert ib.two_sentences("") == "" and ib.two_sentences("x" * 400).endswith("…") and len(ib.two_sentences("x" * 400)) == 320
    assert ib.sport_label("soccer", S.WIRE_BOARD_SPORTS) == "FOOTBALL" and ib.sport_label("cricket", S.WIRE_BOARD_SPORTS) == "CRICKET"
    assert ib.sport_label("icehockey", S.WIRE_BOARD_SPORTS) is None and ib.sport_label(None, S.WIRE_BOARD_SPORTS) is None


def test_moves_and_shifts_need_both_readings() -> None:
    before, now = {"HOME": 0.45, "DRAW": 0.27, "AWAY": 0.28}, {"HOME": 0.40, "DRAW": 0.28, "AWAY": 0.32}
    assert ib.probability_move(before, now, "HOME") == {"selection": "HOME", "before": 0.45, "now": 0.4, "delta_pts": -5.0}
    assert ib.probability_move(before, None, "HOME") is None and ib.probability_move(None, now, "HOME") is None
    assert ib.probability_move(before, now, None) is None  # no side named: no move is claimed
    assert ib.edge_shift(0.05, {"leg_id": "a", "ev": 0.03}, "a") == 2.0
    assert ib.edge_shift(0.05, {"leg_id": "b", "ev": 0.03}, "a") is None  # another leg: not a shift
    assert ib.edge_shift(None, {"leg_id": "a", "ev": 0.03}, "a") is None and ib.edge_shift(0.05, None, "a") is None
    assert ib.weather_is_news(0.94, False, 0.05) and ib.weather_is_news(1.0, True, 0.05) and not ib.weather_is_news(0.97, False, 0.05)
    item = {"fixture": {"home": "Arsenal", "away": "Chelsea"}, "sport": "FOOTBALL", "category": "LINEUP_INJURY", "headline": "Saka fit", "summary": "",
            "source": {"name": "BBC Sport"}}
    assert ib.matches(item, sport="FOOTBALL", category="ALL", query="chelsea", linked_only=True)
    assert not ib.matches(item, sport="CRICKET", category=None, query=None, linked_only=False)
    assert not ib.matches({**item, "fixture": None}, sport=None, category=None, query=None, linked_only=True)


def test_a_steam_move_is_reported_as_measured() -> None:
    fixture = Fixture("fx-1", "India", "Australia", "cricket_international_t20", datetime.now(UTC) + timedelta(hours=3))
    alert = SteamAlert(match_id="fx-1", selection_id="AWAY", market_type="h2h", opening_odds=2.40, current_odds=2.10, opening_line=None, current_line=None,
                       implied_prob_delta_pct=5.95, triggering_bookmakers=["pinnacle", "1xbet"], detected_at=datetime.now(UTC))
    item = wire_board._steam_item(alert, fixture, {}, {}, S, datetime.now(UTC))  # noqa: SLF001
    assert item["category"] == "ODDS_STEAM" and item["sport"] == "CRICKET" and item["source"]["tier"] == "MEASURED" and "shortened 2.40 → 2.10" in item["headline"]
    assert item["market"]["steam"]["delta_pts"] == 5.95 and item["market"]["edge"] is None and item["fixture"]["oracle_link"] == "/oracle?tab=board&fixture=fx-1"


def test_a_chain_counts_its_steps_and_a_slip_is_the_same_in_any_order() -> None:
    assert [m for m, _, _ in MODULES] == [Module.WIRE, Module.SCORE_PREDICTOR, Module.ORACLE, Module.CONSULTANT, Module.VAULT]
    assert [name for _, name, _ in MODULES] == ["VIDUR / THE WIRE", "SCORE PREDICTOR", "ORACLE & BET BUILDER", "BETDOC CONSULTANT", "THE VAULT & SMALLCASES"]
    chain = Chain("c1", "u1", {"item_id": "news:x"}, "2026-10-22T10:00:00+00:00")
    chain.steps = [Step(1, Module.WIRE, Status.COMPLETED, "ok"), Step(2, Module.SCORE_PREDICTOR, Status.SKIPPED, "no fixture"),
                   Step(3, Module.ORACLE, Status.FAILED, "refused")]
    assert chain.counts == {"COMPLETED": 1, "SKIPPED": 1, "FAILED": 1} and chain.outcome() == "3 steps: 1 completed, 1 skipped, 1 failed"
    out = chain.as_dict(viewer="u1")
    assert out["mine"] and out["origin"] == "USER" and out["steps"][0]["module_name"] == "VIDUR / THE WIRE" and not chain.as_dict(viewer="u2")["mine"]
    assert Chain("c2", "system", {}, "t").as_dict(viewer="u1")["origin"] == "SYSTEM"
    assert slip_signature(["b", "a", "a"], "betdoc") == slip_signature(["a", "b"], "betdoc") != slip_signature(["a", "b"], "one_xbet")


# ================================================================ the transport
@pytest.mark.asyncio
async def test_a_frame_goes_through_redis_or_in_process_never_both(redis: Redis, settings: Settings) -> None:  # noqa: F811
    queue = bus.HUB.listen()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(bus.channel(settings))
    try:
        assert bus.channel(settings) == "test_neural:pipeline" and get_settings().NEURAL_BUS_PREFIX == "betdoc:neural"  # betdoc:neural:pipeline
        assert await bus.publish(redis, settings, {"type": "step", "audience": "system", "n": 1}) == "redis"
        message = await pubsub.get_message(timeout=2.0)
        while message is None or message.get("type") != "message":
            message = await pubsub.get_message(timeout=2.0)
        assert json.loads(message["data"])["n"] == 1 and queue.empty()
        assert await bus.publish(None, settings, {"type": "step", "audience": "system", "n": 2}) == "local"
        assert json.loads(queue.get_nowait())["n"] == 2
    finally:
        bus.HUB.drop(queue)
        await pubsub.unsubscribe()
        await pubsub.aclose()

    small = settings.model_copy(update={"NEURAL_BUS_HISTORY": 3})
    for i in range(5):
        await bus.remember(redis, small, "system", {"chain_id": f"c{i}", "started_at": f"2026-10-22T10:0{i}:00+00:00"})
    await bus.remember(redis, small, "u1", {"chain_id": "mine", "started_at": "2026-10-22T10:02:30+00:00"})
    assert [c["chain_id"] for c in await bus.recent(redis, small, ["system", "u1"], 10)] == ["c4", "c3", "mine", "c2"]  # trimmed, merged, newest first
    assert 0 < await redis.ttl(bus.history_key(small, "system")) <= int(small.NEURAL_BUS_HISTORY_HOURS * 3600)
    await bus.remember(None, small, "u9", {"chain_id": "kept-here", "started_at": "t"})
    assert [c["chain_id"] for c in await bus.recent(None, small, ["u9"], 10)] == ["kept-here"]
    assert await bus.claim(redis, settings, "x", 30) and not await bus.claim(redis, settings, "x", 30)
    await bus.release(redis, settings, "x")
    assert await bus.claim(redis, settings, "x", 30)


# ================================================================ end to end
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:  # noqa: F811
    app = app_for(sessions, redis, settings, user)
    app.include_router(wire_api.router, prefix="/api/v1/the-wire")
    app.include_router(bus_api.router, prefix="/api/v1/neural-bus")
    return app


async def _wire(sessions: async_sessionmaker[AsyncSession], fixture_ids: dict[str, str], now: datetime) -> None:
    """A rated absence on Arsenal (fx-fair), a forecast that matters there and one that does not at Everton (fx-bad)."""
    async with sessions() as session:
        session.add(InjuryRosterReport(id=uuid.uuid4(), fixture_id=fixture_ids["fair"], sport="soccer", side="HOME", team_name="Arsenal", player_name="Bukayo Saka",
                                       position="F", status="OUT", rating=9.0, source_name="espn", is_active=True, reported_at=now, updated_at=now))
        for fid, home, away, factor in ((fixture_ids["fair"], "Arsenal", "Chelsea", 0.84), (fixture_ids["bad"], "Everton", "Fulham", 0.99)):
            session.add(WeatherSnapshot(id=uuid.uuid4(), fixture_id=fid, sport_key="soccer_epl", home_team=home, away_team=away, kickoff_at=now + timedelta(hours=20),
                                        venue_name=f"{home} ground", roof_type="OPEN_AIR", window_hours=2.0, temp_c=6.0, wind_speed_kmh=38.0, precipitation_mmh=6.0,
                                        condition="Heavy rain", pitch_impact_score=factor, factors={}, tactical_advisory="Wind and rain slow the ball.",
                                        source="open-meteo", fetched_at=now))
        await session.commit()


@pytest.mark.asyncio
async def test_the_board_measures_what_it_shows_and_the_chain_runs_on_real_engines(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with sessions() as session:
        session.add(BankrollAccount(user_id=user.id, bot_id=None, available_balance=Decimal("100000"), exposure_balance=Decimal("0")))
        await session.commit()
    now = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)  # fx-fair Arsenal v Chelsea, fx-bad Everton v Fulham, fx-good Brighton v Wolves
        await _wire(sessions, {"fair": "fx-fair", "bad": "fx-bad"}, now)
        items = [
            NewsItem(id="a", source="BBC Sport", title="Brighton captain ruled out of Wolves clash with a hamstring injury",
                     summary="The skipper limped off in training. He will miss the weekend. Further scans are due.",
                     url="https://www.bbc.co.uk/sport/football/brighton-1", published_at=now - timedelta(seconds=60)),
            NewsItem(id="b", source="BBC Sport", title="Golf: weekend round-up", summary="", url="https://www.bbc.co.uk/sport/golf/2", published_at=now - timedelta(minutes=5)),
        ]
        scanned = await news.scan(sessions, redis, settings, now, items=items)
        assert scanned["ingested"] == 2 and scanned["chains"] == 1  # the HIGH story on a tracked fixture started a system chain

        # ---------------------------------------------------------- the board
        board = (await client.get("/api/v1/the-wire/board")).json()
        assert board["developer_credit"] == DEVELOPER and board["tracked_fixtures"] == 3
        by = {i["id"]: i for i in board["items"]}
        story = next(i for i in board["items"] if i["kind"] == "NEWS")
        assert story["fixture"]["fixture_id"] == "fx-good" and story["fixture"]["side"] == "HOME" and story["fixture"]["oracle_link"] == "/oracle?tab=board&fixture=fx-good"
        assert story["category"] == "LINEUP_INJURY" and story["sport"] == "FOOTBALL" and story["impact_level"] in ("CRITICAL", "HIGH")
        assert story["source"] == {"name": "BBC Sport", "url": items[0].url, "tier": "TIER_2_PRESS", "credibility": 1.0, "basis": "the article"}
        assert story["summary"] == "The skipper limped off in training. He will miss the weekend."  # the source's first two sentences
        move = story["market"]["move"]
        assert move["selection"] == "HOME" and move["delta_pts"] == pytest.approx(0.0, abs=0.01)  # the market has not moved since it landed
        edge = story["market"]["edge"]
        assert edge["leg_id"] == "fx-good|Match Odds|HOME" and edge["ev"] > 0 and edge["book"] in ("1xbet", "parimatch") and story["market"]["legs_above_fair"] >= 1
        assert not any(i["headline"].startswith("Golf") or i["fixture"] is None for i in board["items"])  # linked only by default
        absences = by["absences:fx-fair"]
        assert absences["category"] == "LINEUP_INJURY" and absences["source"]["tier"] == "MEASURED" and absences["source"]["basis"] == "ESPN injury list"
        assert absences["market"]["lineup"]["delta_pts"]["HOME"] < 0 and absences["market"]["lineup"]["adjusted"]["HOME"] < absences["market"]["lineup"]["probabilities"]["HOME"]
        assert by["weather:fx-fair"]["market"]["weather"]["friction_factor"] == 0.84 and by["weather:fx-fair"]["impact_level"] == "HIGH"
        assert "weather:fx-bad" not in by  # 0.99: not news
        assert all(i["market"]["edge"] is None or i["market"]["edge"]["ev"] > 0 for i in board["items"])

        wide = (await client.get("/api/v1/the-wire/board", params={"linked_only": "false"})).json()
        golf = next(i for i in wide["items"] if i["headline"].startswith("Golf"))
        assert golf["fixture"] is None and golf["category"] == "GENERAL" and golf["market"]["edge"] is None and wide["counts"]["ALL"] == board["counts"]["ALL"] + 1
        assert {i["category"] for i in (await client.get("/api/v1/the-wire/board", params={"category": "weather_pitch"})).json()["items"]} == {"WEATHER_PITCH"}
        assert [i["fixture"]["fixture_id"] for i in (await client.get("/api/v1/the-wire/board", params={"q": "brighton"})).json()["items"]] == ["fx-good"]
        assert (await client.get("/api/v1/the-wire/board", params={"sport": "cricket"})).json()["items"] == [] and board["counts"]["by_sport"]["FOOTBALL"] == board["total"]
        assert (await client.get("/api/v1/the-wire/board", params={"sport": "badminton"})).status_code == 422
        assert (await client.get("/api/v1/the-wire/board", params={"category": "gossip"})).status_code == 422
        assert (await client.get("/api/v1/the-wire/board")).json()["as_of"] == board["as_of"]  # one shared board
        assert (await client.get(f"/api/v1/the-wire/board/{story['id']}")).json()["id"] == story["id"]
        assert (await client.get("/api/v1/the-wire/board/news:nope")).status_code == 404

        # the market follows the story: Brighton drift everywhere, and the board shows the measured move
        await seed(redis, settings, "fx-good", "Brighton", "Wolves", sharp={"HOME": 2.70, "DRAW": 3.30, "AWAY": 2.70},
                   soft={b: {"HOME": 2.75, "DRAW": 3.25, "AWAY": 2.65} for b in ("williamhill", "betway", "onexbet", "parimatch")})
        moved = await wire_board.build(sessions, redis, settings, datetime.now(UTC), fresh=True)
        assert wire_board.find(moved, story["id"])["market"]["move"]["delta_pts"] < -3.0

        # ---------------------------------------------------------- the system chain the scan started
        system = await bus.recent(redis, settings, ["system"], 5)
        assert len(system) == 1 and system[0]["origin"] == "SYSTEM" and system[0]["trigger"]["item_id"] == story["id"]
        steps = {s["module"]: s for s in system[0]["steps"]}
        assert steps["CONSULTANT"]["status"] == "SKIPPED" and "carries no slip" in steps["CONSULTANT"]["summary"]
        assert steps["ORACLE"]["status"] == "COMPLETED" and steps["VAULT"]["status"] == "COMPLETED" and "KUMBHA sizes each account" in steps["VAULT"]["summary"]

        # ---------------------------------------------------------- a user's chain, with the slip
        await seed(redis, settings, "fx-good", "Brighton", "Wolves", sharp={"HOME": 2.20, "DRAW": 3.30, "AWAY": 3.30},  # back to value at the retail books
                   soft={"williamhill": {"HOME": 2.10, "DRAW": 3.40, "AWAY": 3.50}, "betway": {"HOME": 2.12, "DRAW": 3.40, "AWAY": 3.45},
                         "onexbet": {"HOME": 2.62, "DRAW": 3.10, "AWAY": 3.00}, "parimatch": {"HOME": 2.58, "DRAW": 3.10, "AWAY": 3.00}})
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(bus.channel(settings))
        slip = {"item_id": story["id"], "leg_ids": ["fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"], "skin": "one_xbet"}
        first = await client.post("/api/v1/neural-bus/chains", json=slip)
        assert first.status_code == 201, first.text
        chain = first.json()
        assert chain["mine"] and chain["origin"] == "USER" and chain["developer_credit"] == DEVELOPER and chain["counts"] == {"COMPLETED": 5, "SKIPPED": 0, "FAILED": 0}
        assert [s["module"] for s in chain["steps"]] == ["WIRE", "SCORE_PREDICTOR", "ORACLE", "CONSULTANT", "VAULT"]
        st = {s["module"]: s for s in chain["steps"]}
        assert "Brighton v Wolves" in st["WIRE"]["summary"] and st["WIRE"]["detail"]["source"]["tier"] == "TIER_2_PRESS"
        fit = st["SCORE_PREDICTOR"]["detail"]
        assert set(fit["consensus"]) == {"HOME", "DRAW", "AWAY"} and len(fit["market_fit"]["expected_goals"]) == 2 and "xG" in st["SCORE_PREDICTOR"]["summary"]
        assert fit["market_fit"]["top_scorelines"][0]["probability"] > 0
        oracle = st["ORACLE"]["detail"]
        assert oracle["top"][0]["leg_id"] == "fx-good|Match Odds|HOME" and oracle["legs_above_fair"] >= 1
        assert oracle["edge_recorded"]["leg_id"] == "fx-good|Match Odds|HOME" and oracle["edge_shift_pts"] == pytest.approx(0.0, abs=0.01)  # the system chain's record, same prices
        assert json.loads(await redis.get(bus.edge_key(settings, "fx-good")))["leg_id"] == "fx-good|Match Odds|HOME"
        consult = st["CONSULTANT"]["detail"]
        assert 0 <= consult["score"] <= 100 and consult["tier"] and consult["on_fixture"] == 1 and consult["previous"] is None and "Slip Score" in st["CONSULTANT"]["summary"]
        vault = st["VAULT"]["detail"]
        assert vault["bankroll_inr"] is not None and any(h["basket_id"] == "EUROPEAN_FOOTBALL_FORTRESS" for h in vault["baskets"])
        held = next(h for h in vault["baskets"] if h["basket_id"] == "EUROPEAN_FOOTBALL_FORTRESS")
        assert Decimal(held["stake_inr"]) > 0 and Decimal(held["stop_floor_inr"]) < Decimal(held["stake_inr"]) and "₹" in st["VAULT"]["summary"]

        frames = []
        while len(frames) < 12:
            message = await pubsub.get_message(timeout=2.0)
            if message and message.get("type") == "message":
                frames.append(json.loads(message["data"]))
        await pubsub.unsubscribe()
        await pubsub.aclose()
        assert [f["type"] for f in frames] == ["chain_started", *["step"] * 10, "chain"] and {f["audience"] for f in frames} == {str(user.id)}
        assert [f["step"]["status"] for f in frames[1:11:2]] == ["RUNNING"] * 5 and [f["step"]["module"] for f in frames[2:11:2]] == [m.value for m, _, _ in MODULES]

        again = (await client.post("/api/v1/neural-bus/chains", json=slip)).json()
        st2 = {s["module"]: s for s in again["steps"]}
        assert st2["ORACLE"]["detail"]["edge_shift_pts"] == pytest.approx(0.0, abs=0.01) and "pts since" in st2["ORACLE"]["summary"]  # the same leg, remembered
        assert st2["CONSULTANT"]["detail"]["previous"]["score"] == consult["score"] and "(was " in st2["CONSULTANT"]["summary"]

        listed = (await client.get("/api/v1/neural-bus/chains", params={"limit": 10})).json()["chains"]
        assert [c["origin"] for c in listed].count("USER") == 2 and [c["origin"] for c in listed].count("SYSTEM") == 1
        assert all(c["mine"] == (c["origin"] == "USER") for c in listed) and listed[0]["chain_id"] == again["chain_id"]
        modules = (await client.get("/api/v1/neural-bus/modules")).json()
        assert [m["name"] for m in modules["modules"]][0] == "VIDUR / THE WIRE" and modules["channel"] == "test_neural:pipeline" and modules["transport"] == "redis"

        # ---------------------------------------------------------- an item with no fixture, one gone, one already running
        golf_chain = await neural_chain.run(sessions, redis, settings, item=golf, user_id=user.id, leg_ids=slip["leg_ids"])
        assert [s["status"] for s in golf_chain["steps"]] == ["COMPLETED", "SKIPPED", "SKIPPED", "SKIPPED", "SKIPPED"]
        assert "names no fixture" in golf_chain["steps"][0]["summary"]
        gone = await client.post("/api/v1/neural-bus/chains", json={"item_id": "news:nope"})
        assert gone.status_code == 404 and gone.json()["detail"]["reason"] == "NOT_ON_BOARD"
        assert await bus.claim(redis, settings, f"user:{user.id}", 60)
        busy = await client.post("/api/v1/neural-bus/chains", json=slip)
        assert busy.status_code == 429 and busy.json()["detail"]["reason"] == "CHAIN_RUNNING"
        await bus.release(redis, settings, f"user:{user.id}")
        assert (await client.post("/api/v1/neural-bus/chains", json={**slip, "skin": "betway"})).status_code == 422


@pytest.mark.asyncio
async def test_without_redis_the_chain_still_reports_and_reaches_this_process(sessions, settings: Settings) -> None:
    user = await make_user(sessions)
    queue = bus.HUB.listen()
    try:
        item = {"id": "news:x", "kind": "NEWS", "category": "GENERAL", "headline": "A story", "impact_level": "LOW", "published_at": datetime.now(UTC).isoformat(),
                "source": {"name": "BBC Sport", "tier": "TIER_2_PRESS"}, "sport": None,
                "fixture": {"fixture_id": "fx-x", "home": "A", "away": "B", "sport_key": "soccer_epl", "kickoff": (datetime.now(UTC) + timedelta(hours=5)).isoformat(),
                            "oracle_link": "/oracle?tab=board&fixture=fx-x"}}
        chain = await neural_chain.run(sessions, None, settings, item=item, user_id=user.id)
        steps = {s["module"]: s for s in chain["steps"]}
        assert steps["SCORE_PREDICTOR"]["status"] == "SKIPPED" and "no Match Odds consensus" in steps["SCORE_PREDICTOR"]["summary"]
        assert steps["ORACLE"]["status"] == "FAILED" and "without Redis" in steps["ORACLE"]["summary"]
        assert steps["CONSULTANT"]["status"] == "SKIPPED" and steps["VAULT"]["status"] == "FAILED"
        frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
        assert [f["type"] for f in frames] == ["chain_started", *["step"] * 10, "chain"]
        assert (await bus.recent(None, settings, [str(user.id)], 5))[0]["chain_id"] == chain["chain_id"]
    finally:
        bus.HUB.drop(queue)


def test_the_socket_forwards_the_systems_frames_and_the_viewers_own() -> None:
    probe = sync_redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        probe.ping()
    except (RedisError, OSError):
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    settings = get_settings().model_copy(update={"NEURAL_BUS_PREFIX": "test_neural_ws"})
    viewer = User(id=uuid.uuid4(), username="viewer", hashed_password="x")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        yield
        await app.state.redis.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(bus_api.ws_router, prefix="/api/v1/ws")
    app.dependency_overrides[get_ws_user] = lambda: viewer
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client, client.websocket_connect("/api/v1/ws/neural-bus?token=x") as ws:
            ack = ws.receive_json()
            assert ack == {"type": "connection_ack", "channel": "test_neural_ws:pipeline", "transport": "redis", "status": "LISTENING_TO_THE_NEURAL_BUS"}
            probe.publish(bus.channel(settings), json.dumps({"type": "step", "audience": str(uuid.uuid4()), "n": 0}))  # another user's
            probe.publish(bus.channel(settings), json.dumps({"type": "step", "audience": "system", "n": 1}))
            probe.publish(bus.channel(settings), json.dumps({"type": "step", "audience": str(viewer.id), "n": 2}))
            assert ws.receive_json() == {"type": "step", "n": 1, "mine": False}
            assert ws.receive_json() == {"type": "step", "n": 2, "mine": True}
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}
    finally:
        probe.close()


# ================================================================ routes and the front end
def test_the_routes_are_signed_in_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path, method in (("/api/v1/the-wire/board", "get"), ("/api/v1/the-wire/board/{item_id}", "get"), ("/api/v1/neural-bus/chains", "post"),
                         ("/api/v1/neural-bus/chains", "get"), ("/api/v1/neural-bus/modules", "get")):
        assert paths[path][method].get("security"), path


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_board_and_the_pipeline_bar_are_wired() -> None:
    wire = (FRONTEND / "pages" / "TheWire.tsx").read_text(encoding="utf-8")
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    board = (FRONTEND / "components" / "wire" / "WireIntelligenceBoard.tsx").read_text(encoding="utf-8")
    card = (FRONTEND / "components" / "wire" / "WireIntelligenceCard.tsx").read_text(encoding="utf-8")
    bar = (FRONTEND / "components" / "common" / "NeuralPipelineBar.tsx").read_text(encoding="utf-8")
    lib = (FRONTEND / "lib" / "neural_bus.ts").read_text(encoding="utf-8")
    store = (FRONTEND / "store" / "useNeuralBusStore.ts").read_text(encoding="utf-8")
    match_board = (FRONTEND / "components" / "oracle" / "board" / "MatchBoard.tsx").read_text(encoding="utf-8")
    assert "<WireIntelligenceBoard" in wire and "<NeuralPipelineBar" in app and "<WireIntelligenceCard" in board
    assert "/the-wire/board" in lib and "/neural-bus/chains" in lib and "/ws/neural-bus" in store
    assert "Open in Oracle" in card and "Audit my slip" in card and "fixture" in match_board and "useSearchParams" in match_board
    for source in (wire, board, card, bar, lib, store):
        assert "lucide-react" not in source and not re.search(r"Bumrah|Saka|Alcaraz|Rodri", source)  # no stock stories, no icon dependency
    assert "material-symbols-outlined" in bar and "material-symbols-outlined" in card
    # the page opens with a guide under the hero, held to Group 99's contract: full width, exactly three steps, the EV it shows
    block = wire.split("<ComponentGuideBanner", 1)[1].split("/>", 1)[0]
    steps = block[block.index("steps={["):block.index("]}")]
    assert 'className="lg:col-span-12"' in block and len(re.findall(r'^\s+"(?:[^"\\]|\\.)*",?$', steps, re.M)) == 3 and '"EV"' in block
    assert wire.index("<CommanderHero") < wire.index("<ComponentGuideBanner") < wire.index("<WireIntelligenceBoard")
