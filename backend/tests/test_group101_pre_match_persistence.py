"""Group 101: pre-match rate persistence (point b.13a): the 3-tier cadence, the board kept per fixture, the store's reads
and the /pre-match contract. Developed for Amit Ashok Kumar Patnaik.

SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set). The fleet side (a run keeps its board, the beat's tier gate,
forced refreshes against the breaker, reserve and shield) is in ``tests/omni_fleet/test_pre_match_cadence.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_db
from app.api.v1 import pre_match as pre_match_api
from app.core.config import Settings, get_settings
from app.domain.pre_match.cadence import due_sports, freshness, sport_is_due, tier_for
from app.models import Base
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.services import pre_match_odds_service as service

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
TABLES = [PreMatchOddsSnapshotModel.__table__]
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
H = 3600.0


# Group 101's cadence (6h / 60 min / 60 s, tier 2 from 2h); Group 112 lengthened the defaults and pins them in its own tests
GROUP_101_TIERS = {"PRE_MATCH_TIER2_KICKOFF_SECONDS": 2 * H, "PRE_MATCH_TIER2_MAX_AGE_SECONDS": H, "PRE_MATCH_TIER3_MAX_AGE_SECONDS": 60.0}


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update={**GROUP_101_TIERS, **overrides})


S = _settings()


def _event(match_id: str = "evt-1", *, kickoff: datetime = NOW + timedelta(hours=48), h2h: dict[str, tuple[float, float, float]] | None = None,
           totals: bool = True) -> dict[str, Any]:
    """An Odds API v4 event, shaped like a live /sports/{sport}/odds response."""
    stamp = "2026-10-22T11:59:30Z"
    books = []
    for key, (home, draw, away) in (h2h or {"pinnacle": (2.0, 3.6, 3.9), "williamhill": (1.95, 3.5, 3.8)}).items():
        markets = [{"key": "h2h", "last_update": stamp, "outcomes": [
            {"name": "Arsenal", "price": home}, {"name": "Chelsea", "price": away}, {"name": "Draw", "price": draw}]}]
        if totals:
            markets.append({"key": "totals", "last_update": stamp, "outcomes": [
                {"name": "Over", "price": 1.91, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}]})
        books.append({"key": key, "title": key.title(), "last_update": stamp, "markets": markets})
    return {"id": match_id, "sport_key": "soccer_epl", "commence_time": kickoff.isoformat().replace("+00:00", "Z"),
            "home_team": "Arsenal", "away_team": "Chelsea", "bookmakers": books}


# ================================================================ the cadence (pure)
@pytest.mark.parametrize(("kickoff_h", "tier"), [(48.0, 1), (24.01, 1), (24.0, 2), (6.0, 2), (2.01, 2), (2.0, 3), (0.5, 3), (-1.0, 3)])
def test_tier_follows_time_to_kickoff(kickoff_h: float, tier: int) -> None:
    assert tier_for(kickoff_h * H, S) == tier


@pytest.mark.parametrize(("kickoff_h", "fresh_age_h", "stale_age_h"), [
    (48.0, 2.0, 6.5),  # tier 1: 6 hours
    (6.0, 0.5, 1.25),  # tier 2: 60 minutes
    (0.5, 30 / H, 90 / H),  # tier 3: 60 seconds
])
def test_each_tier_has_its_own_max_age(kickoff_h: float, fresh_age_h: float, stale_age_h: float) -> None:
    kickoff = NOW + timedelta(hours=kickoff_h)
    assert not freshness(kickoff, NOW - timedelta(hours=fresh_age_h), NOW, S).is_stale
    assert freshness(kickoff, NOW - timedelta(hours=stale_age_h), NOW, S).is_stale


def test_the_tier_is_judged_now_not_when_the_board_was_bought() -> None:
    """Bought 3h ago when the match was 27h away (tier 1, fine for 6h); it is 24h away now: tier 2, and 3h > 60 min."""
    fresh = freshness(NOW + timedelta(hours=24), NOW - timedelta(hours=3), NOW, S)
    assert (fresh.tier, fresh.is_stale) == (2, True)
    assert fresh.next_refresh_at == NOW - timedelta(hours=2)


def test_the_shield_stretch_lengthens_every_tier_and_never_shortens() -> None:
    kickoff = NOW + timedelta(hours=48)
    assert freshness(kickoff, NOW - timedelta(hours=7), NOW, S).is_stale
    assert not freshness(kickoff, NOW - timedelta(hours=7), NOW, S, stretch=4.0).is_stale
    assert freshness(kickoff, NOW - timedelta(hours=7), NOW, S, stretch=0.25).is_stale  # under 1 is treated as 1


def test_naive_timestamps_are_read_as_utc() -> None:
    fresh = freshness((NOW + timedelta(hours=48)).replace(tzinfo=None), (NOW - timedelta(hours=1)).replace(tzinfo=None), NOW, S)
    assert (fresh.tier, round(fresh.age_seconds)) == (1, 3600)


def test_a_sport_is_due_by_its_most_urgent_fixture() -> None:
    assert sport_is_due(None, None, NOW, S)  # never bought
    assert not sport_is_due(None, NOW - timedelta(hours=5), NOW, S)  # nothing stored (off season): tier 1, 6h
    assert sport_is_due(None, NOW - timedelta(hours=6), NOW, S)
    nearest = {"soccer_epl": NOW + timedelta(minutes=90), "tennis_atp": NOW + timedelta(hours=40)}
    polled = {"soccer_epl": NOW - timedelta(seconds=61), "tennis_atp": NOW - timedelta(hours=1), "mma_mixed_martial_arts": NOW - timedelta(hours=7)}
    assert due_sports(["tennis_atp", "soccer_epl", "mma_mixed_martial_arts", "cricket_ipl"], nearest, polled, NOW, S) == [
        "soccer_epl", "mma_mixed_martial_arts", "cricket_ipl"]


def test_settings_refuse_tiers_that_loosen_near_kickoff() -> None:
    with pytest.raises(ValidationError, match="must not grow"):
        Settings(PRE_MATCH_TIER2_MAX_AGE_SECONDS=30.0)
    with pytest.raises(ValidationError, match="TIER2_KICKOFF"):
        Settings(PRE_MATCH_TIER2_KICKOFF_SECONDS=30 * H)


# ================================================================ one board -> rows (pure)
def test_event_row_keeps_every_market_as_quoted_and_devigs_h2h() -> None:
    row = service.event_row(_event(), "soccer_epl", NOW)
    assert row is not None
    assert (row["match_id"], row["home_team"], row["away_team"], row["commence_time"]) == ("evt-1", "Arsenal", "Chelsea", NOW + timedelta(hours=48))
    assert row["bookmaker_odds"] == {"pinnacle": {"home": 2.0, "draw": 3.6, "away": 3.9}, "williamhill": {"home": 1.95, "draw": 3.5, "away": 3.8}}
    assert row["markets_json"]["totals"]["books"]["pinnacle"]["outcomes"] == [{"name": "Over", "price": 1.91, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}]
    assert row["markets_json"]["h2h"]["polled_at"] == NOW.isoformat()

    def devig(h: float, d: float, a: float) -> list[float]:
        implied = [1 / h, 1 / d, 1 / a]
        return [p / sum(implied) for p in implied]

    expected = [(x + y) / 2 for x, y in zip(devig(2.0, 3.6, 3.9), devig(1.95, 3.5, 3.8), strict=True)]
    consensus = row["consensus_fair_odds"]
    assert consensus["books"] == 2 and consensus["market"] == "h2h"
    for side, p in zip(("home", "draw", "away"), expected, strict=True):
        assert consensus["outcomes"][side]["probability"] == pytest.approx(p, abs=1e-6)
        assert consensus["outcomes"][side]["fair_odds"] == pytest.approx(1 / p, abs=1e-4)
    assert consensus["sharp"]["book"] == "pinnacle" and set(consensus["sharp"]["fair_odds"]) == {"home", "draw", "away"}


def test_two_way_markets_and_incomplete_books() -> None:
    raw = _event(totals=False)
    for book in raw["bookmakers"]:
        book["markets"][0]["outcomes"] = [o for o in book["markets"][0]["outcomes"] if o["name"] != "Draw"]
    raw["bookmakers"][1]["markets"][0]["outcomes"] = raw["bookmakers"][1]["markets"][0]["outcomes"][:1]  # one side only
    row = service.event_row(raw, "soccer_epl", NOW)
    assert row is not None and set(row["consensus_fair_odds"]["outcomes"]) == {"home", "away"}
    assert row["consensus_fair_odds"]["books"] == 1  # an incomplete book is shown, never part of the consensus
    assert row["bookmaker_odds"]["williamhill"] == {"home": 1.95}


@pytest.mark.parametrize("broken", [{"id": ""}, {"home_team": None}, {"commence_time": "soon"}, {"commence_time": None}, {"id": "x" * 129}])
def test_an_unusable_event_is_skipped(broken: dict[str, Any]) -> None:
    assert service.event_row({**_event(), **broken}, "soccer_epl", NOW) is None


def test_junk_prices_are_dropped_never_repaired() -> None:
    raw = _event(totals=False)
    raw["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = "n/a"
    raw["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = 1.0  # not a price: 1.0 returns only the stake
    row = service.event_row(raw, "soccer_epl", NOW)
    assert row is not None and "home" not in row["bookmaker_odds"]["pinnacle"] and "home" not in row["bookmaker_odds"]["williamhill"]
    assert row["consensus_fair_odds"] == {}  # no complete book: no consensus, nothing filled in


def test_a_board_without_prices_still_records_the_fixture() -> None:
    row = service.event_row({**_event(), "bookmakers": []}, "soccer_epl", NOW)
    assert row is not None and (row["markets_json"], row["bookmaker_odds"], row["consensus_fair_odds"]) == ({}, {}, {})


def test_merge_replaces_the_markets_a_fetch_carried_and_keeps_the_rest() -> None:
    old = service.event_row(_event(), "soccer_epl", NOW - timedelta(hours=1))
    h2h_only = service.event_row(_event(h2h={"pinnacle": (2.1, 3.5, 3.7)}, totals=False), "soccer_epl", NOW)  # quiet hours: h2h only
    merged = service.merge(old, h2h_only)
    assert merged["markets_json"]["totals"] == old["markets_json"]["totals"]  # kept, with its own older polled_at
    assert merged["markets_json"]["h2h"]["polled_at"] == NOW.isoformat()
    assert merged["bookmaker_odds"] == {"pinnacle": {"home": 2.1, "draw": 3.5, "away": 3.7}}

    totals_only = service.event_row(_event(), "soccer_epl", NOW)
    del totals_only["markets_json"]["h2h"]
    kept = service.merge(old, totals_only)
    assert kept["bookmaker_odds"] == old["bookmaker_odds"] and kept["consensus_fair_odds"] == old["consensus_fair_odds"]
    assert service.merge(None, h2h_only) is h2h_only


# ================================================================ the store (SQLite and PostgreSQL)
@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    else:
        if not TEST_POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=TABLES))
        await engine.dispose()


async def _store(sessions: async_sessionmaker[AsyncSession], events: list[dict[str, Any]], polled_at: datetime, sport: str = "soccer_epl") -> int:
    async with sessions() as db:
        n = await service.store_board(db, events, sport, polled_at, S)
        await db.commit()
    return n


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[PreMatchOddsSnapshotModel]:
    async with sessions() as db:
        return list((await db.execute(select(PreMatchOddsSnapshotModel).order_by(PreMatchOddsSnapshotModel.match_id))).scalars().all())


async def test_store_upserts_one_row_per_fixture(sessions) -> None:
    assert await _store(sessions, [_event("a"), _event("b", kickoff=NOW + timedelta(hours=5)), {"junk": True}], NOW - timedelta(hours=1)) == 2
    first = {r.match_id: r for r in await _rows(sessions)}
    assert await _store(sessions, [_event("a", h2h={"pinnacle": (2.2, 3.4, 3.5)}, totals=False)], NOW) == 1
    rows = {r.match_id: r for r in await _rows(sessions)}
    assert set(rows) == {"a", "b"}
    a = rows["a"]
    assert a.id == first["a"].id  # updated in place, never duplicated
    assert a.bookmaker_odds == {"pinnacle": {"home": 2.2, "draw": 3.4, "away": 3.5}}
    assert "totals" in a.markets_json  # the h2h-only refresh kept the stored totals
    assert a.last_polled_at.replace(tzinfo=UTC) == NOW and rows["b"].last_polled_at.replace(tzinfo=UTC) == NOW - timedelta(hours=1)


async def test_store_prunes_fixtures_long_past_kickoff(sessions) -> None:
    await _store(sessions, [_event("old", kickoff=NOW - timedelta(hours=60)), _event("recent", kickoff=NOW - timedelta(hours=10))], NOW - timedelta(hours=70))
    await _store(sessions, [_event("new")], NOW)
    assert [r.match_id for r in await _rows(sessions)] == ["new", "recent"]  # 60h > PRE_MATCH_RETENTION_HOURS (48h)


async def test_reads_serve_the_window_soonest_first(sessions) -> None:
    await _store(sessions, [_event("in-play", kickoff=NOW - timedelta(hours=1)), _event("finished", kickoff=NOW - timedelta(hours=5)),
                            _event("t2h", kickoff=NOW + timedelta(hours=2)), _event("t20h", kickoff=NOW + timedelta(hours=20)),
                            _event("t70h", kickoff=NOW + timedelta(hours=70)), _event("t100h", kickoff=NOW + timedelta(hours=100))], NOW)
    await _store(sessions, [_event("ipl", kickoff=NOW + timedelta(hours=1))], NOW, sport="cricket_ipl")
    async with sessions() as db:
        async def ids(sport: str | None, window: str, limit: int = 100) -> list[str]:
            return [r.match_id for r in await service.list_fixtures(db, S, sport=sport, window=window, now=NOW, limit=limit)]

        assert await ids(None, "3h") == ["in-play", "ipl", "t2h"]
        assert await ids("soccer_epl", "24h") == ["in-play", "t2h", "t20h"]
        assert await ids("soccer_epl", "all") == ["in-play", "t2h", "t20h", "t70h"]  # PRE_MATCH_HORIZON_HOURS: 72
        assert await ids(None, "72h", limit=2) == ["in-play", "ipl"]
        assert (await service.get_fixture(db, "t20h")).home_team == "Arsenal"
        assert await service.get_fixture(db, "nope") is None


async def test_cadence_inputs_find_each_sports_most_urgent_fixture(sessions) -> None:
    await _store(sessions, [_event("next", kickoff=NOW + timedelta(hours=30)), _event("later", kickoff=NOW + timedelta(hours=50))], NOW - timedelta(minutes=20))
    await _store(sessions, [_event("ipl-live", kickoff=NOW - timedelta(hours=1)), _event("ipl-next", kickoff=NOW + timedelta(hours=26))],
                 NOW - timedelta(minutes=2), sport="cricket_ipl")
    async with sessions() as db:
        inputs = await service.cadence_inputs(db, None, S, ["soccer_epl", "cricket_ipl", "tennis_atp"], NOW)
    assert inputs.nearest_kickoff == {"soccer_epl": NOW + timedelta(hours=30), "cricket_ipl": NOW - timedelta(hours=1)}  # in play: tier 3
    assert inputs.last_polled == {"soccer_epl": NOW - timedelta(minutes=20), "cricket_ipl": NOW - timedelta(minutes=2)}
    assert due_sports(["soccer_epl", "cricket_ipl", "tennis_atp"], inputs.nearest_kickoff, inputs.last_polled, NOW, S) == ["cricket_ipl", "tennis_atp"]


async def test_a_match_dropped_from_the_latest_board_no_longer_holds_tier_3(sessions) -> None:
    await _store(sessions, [_event("done", kickoff=NOW - timedelta(hours=2)), _event("next", kickoff=NOW + timedelta(hours=30))], NOW - timedelta(minutes=30))
    await _store(sessions, [_event("next", kickoff=NOW + timedelta(hours=30))], NOW - timedelta(minutes=5))  # "done" is off the board
    async with sessions() as db:
        inputs = await service.cadence_inputs(db, None, S, ["soccer_epl"], NOW)
    assert inputs.nearest_kickoff == {"soccer_epl": NOW + timedelta(hours=30)}
    assert due_sports(["soccer_epl"], inputs.nearest_kickoff, inputs.last_polled, NOW, S) == []


async def test_due_scope_fails_open_without_the_table() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        assert await service.due_scope(sessions, None, S, ["soccer_epl", "cricket_ipl"], NOW) == ["soccer_epl", "cricket_ipl"]
        assert await service.due_scope(sessions, None, _settings(PRE_MATCH_TIERED_POLLING_ENABLED=False), ["x"], NOW) == ["x"]
    finally:
        await engine.dispose()


async def test_persisting_a_fleet_batch_never_raises(sessions) -> None:
    from app.adapters.ingestion.base import SourcePayload  # noqa: PLC0415

    payloads = [SourcePayload(key="soccer_epl", data=[_event("a")]), SourcePayload(key="cricket_ipl", data={"message": "not a list"})]
    assert await service.persist_fleet_batch(sessions, None, S, payloads, NOW) == 1
    broken = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        assert await service.persist_fleet_batch(async_sessionmaker(broken), None, S, payloads, NOW) == 0  # no table: logged, not raised
    finally:
        await broken.dispose()


# ================================================================ the /pre-match contract
@pytest_asyncio.fixture
async def client(sessions) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(pre_match_api.router, prefix="/api/v1/pre-match")

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[pre_match_api.fleet_deps] = lambda: object()
    app.dependency_overrides[get_settings] = lambda: S
    now = datetime.now(UTC)
    await _store(sessions, [_event("soon", kickoff=now + timedelta(hours=1)), _event("far", kickoff=now + timedelta(hours=40))], now - timedelta(minutes=10))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_list_and_detail_report_each_fixtures_tier(client: AsyncClient) -> None:
    res = await client.get("/api/v1/pre-match/fixtures", params={"sport": "soccer_epl", "window": "72h"})
    assert res.status_code == 200
    body = res.json()
    assert [(f["match_id"], f["tier"], f["is_stale"]) for f in body] == [("soon", 3, True), ("far", 1, False)]
    assert body[1]["max_age_seconds"] == S.PRE_MATCH_TIER1_MAX_AGE_SECONDS and 590 <= body[1]["age_seconds"] <= 700
    assert "markets_json" not in body[0] and body[0]["consensus_fair_odds"]["books"] == 2
    assert [f["match_id"] for f in (await client.get("/api/v1/pre-match/fixtures", params={"window": "3h", "sport": "all"})).json()] == ["soon"]

    detail = (await client.get("/api/v1/pre-match/fixtures/far")).json()
    assert set(detail["markets_json"]) == {"h2h", "totals"} and detail["bookmaker_odds"]["pinnacle"]["home"] == 2.0
    assert (await client.get("/api/v1/pre-match/fixtures/nope")).status_code == 404


@pytest.mark.parametrize("params", [{"window": "1w"}, {"sport": "EPL; drop"}, {"limit": 0}, {"limit": 5000}])
async def test_list_rejects_bad_queries(client: AsyncClient, params: dict[str, Any]) -> None:
    assert (await client.get("/api/v1/pre-match/fixtures", params=params)).status_code == 422


async def test_force_refresh_maps_every_outcome(client: AsyncClient, sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    async with sessions() as db:
        row = await service.get_fixture(db, "far")
    results: list[Any] = [
        service.RefreshOutcome("SYNCED", row, "the_odds_api", "bought"),
        service.RefreshRefused("the operator has forced the quota cache shield"),
        service.RefreshFailed("HTTPStatusError: 401"),
        None,
    ]

    async def fake(_deps: Any, match_id: str, _now: datetime) -> service.RefreshOutcome | None:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(service, "force_refresh", fake)
    url = "/api/v1/pre-match/fixtures/far/force-refresh"
    ok = await client.post(url)
    assert ok.status_code == 200
    assert {k: ok.json()[k] for k in ("match_id", "status", "source")} == {"match_id": "far", "status": "SYNCED", "source": "the_odds_api"}
    assert ok.json()["fixture"]["markets_json"]["h2h"]
    refused = await client.post(url)
    assert (refused.status_code, refused.json()["detail"]) == (409, "the operator has forced the quota cache shield")
    assert (await client.post(url)).status_code == 502
    assert (await client.post(url)).status_code == 404


def test_pre_match_is_mounted_for_signed_in_users_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    assert {"/api/v1/pre-match/fixtures", "/api/v1/pre-match/fixtures/{match_id}", "/api/v1/pre-match/fixtures/{match_id}/force-refresh"} <= set(paths)
    for path, verb in (("/api/v1/pre-match/fixtures", "get"), ("/api/v1/pre-match/fixtures/{match_id}/force-refresh", "post")):
        assert paths[path][verb].get("security"), path  # the mount's get_current_user (OAuth2 bearer)


async def test_table_shape_matches_the_contract(sessions) -> None:
    await _store(sessions, [_event("a")], NOW)
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(PreMatchOddsSnapshotModel))).scalar_one() == 1
        row = (await db.execute(select(PreMatchOddsSnapshotModel))).scalar_one()
    assert isinstance(row.id, uuid.UUID) and row.created_at is not None and row.updated_at is not None
