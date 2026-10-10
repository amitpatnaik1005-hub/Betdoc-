"""Group 78: The Wire as the fortress's feed. Developed for Amit Ashok Kumar Patnaik.

The brief's proofs on the real code paths:

* the venue registry (seed, club suffixes, a fuzzy match that cannot cross clubs, tennis tournaments, indoor sports);
* the friction factor Pi (temperature bands, the wind floor, rain and snow, altitude, cricket's evening dew, domes at 1,
  retractable roofs flagged) and the match window's worst hour;
* Open-Meteo's forecast parsing and the geocoder's country/state choice; ESPN's scoreboard and summary parsing and the
  pairing of its events with the feed's fixtures;
* sports sentiment (phrases, negation), tactical impact by kickoff proximity, credibility tiers, the side named first;
* the lineup delta (the brief's striker and centre-back), unrated absences, the renormalised probabilities, the
  fortress's impact scale; referee profiles shrunk to the league; the news-to-steam catalyst rule;
* end to end through Redis and the API: the weather scan writing snapshots and the weather section (indoor arenas
  too, nothing for a venue nobody knows), the ESPN sync (team sheets, injuries, officiating records, scores with the
  clock), rating an absence writing the injury section, news ingested, paged and turned into a catalyst when the
  market follows, referee profiles reaching the referee section, the dashboard, and the live socket.

SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index; ESPN and Open-Meteo
answered by an in-process transport (no network).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
import redis as sync_redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.wire import open_meteo
from app.api.deps import get_current_admin, get_current_user, get_ws_user
from app.api.v1 import the_wire as wire_api
from app.api.v1 import wire_ws
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.core.live_odds import publish_board_ticks
from app.domain.the_wire import NEWS_CACHE, SCORE_CACHE, WEATHER_CACHE
from app.domain.the_wire import espn_parse as ep
from app.domain.the_wire import lineup_impact as li
from app.domain.the_wire import referee_bias as rb
from app.domain.the_wire import sentiment_engine as se
from app.domain.the_wire import steam_catalyst as sc
from app.domain.the_wire import venue_geocoder as geo
from app.domain.the_wire import weather_impact as wi
from app.models import User
from app.models.control_panel import SystemSettingsModel
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment, OfficiatingRecord, RefereeProfile, VenueLocation, WeatherSnapshot
from app.schemas.market import MarketTick
from app.schemas.the_wire import NewsItem
from app.services.sentinel_bus import AlertKind
from app.services.the_wire import espn_sync, live, news, weather
from app.services.the_wire.fixtures import tracked
from app.services.the_wire.venues import place
from app.services.twin.intel import read_intel
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, seed, stream

D = Decimal
TABLES = [User.__table__, SystemSettingsModel.__table__, VenueLocation.__table__, WeatherSnapshot.__table__, InjuryRosterReport.__table__,
          NewsArticleSentiment.__table__, OfficiatingRecord.__table__, RefereeProfile.__table__]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-wire"
DEVELOPER = "Amit Ashok Kumar Patnaik"


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
    for cache in (NEWS_CACHE, SCORE_CACHE, WEATHER_CACHE):
        cache.clear()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    # no network: no Odds API scores, no RSS, no newsapi.org (the scans are handed their inputs or a mock transport)
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_wire", "WIRE_PREFIX": "test_wire",
                                             "ODDS_API_KEY": None, "NEWSAPI_ORG_API_KEY": None, "WIRE_NEWS_FEEDS": []})


async def make_user(sessions: async_sessionmaker[AsyncSession], role: str = "ADMIN") -> User:
    async with sessions() as session:
        user = User(id=uuid.uuid4(), username=f"vidur_{uuid.uuid4().hex[:6]}", hashed_password="x", role=role)
        session.add(user)
        await session.commit()
        return user


def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(wire_api.router, prefix="/api/v1/the-wire")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


async def board(redis: Redis, fixture: str, home: str, away: str, sport_key: str, kickoff: datetime) -> None:
    await publish_board_ticks(redis, [MarketTick(match_id=fixture, home_team=home, away_team=away, market_type="Match Odds", selection="HOME", odds=D("2"),
                                                 true_probability=D("0.5"), is_suspended=False, sport_key=sport_key, commence_time=kickoff)])


def transport(routes: dict[str, Callable[[httpx.Request], Any]]) -> httpx.AsyncClient:
    """An HTTP client answered in-process: the first route whose key is in the URL answers (JSON)."""
    def handler(request: httpx.Request) -> httpx.Response:
        for key, answer in routes.items():
            if key in str(request.url):
                return httpx.Response(200, json=answer(request))
        return httpx.Response(404, json={"code": 404})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def meteo(now: datetime, *, offset_hours: int = 1, wind: float = 30.0, rain_hour: int = 2, rain: float = 3.0, temp: float = 8.0, humidity: float = 80.0,
          elevation: float = 34.0) -> Callable[[httpx.Request], dict[str, Any]]:
    """Open-Meteo's shape: local wall-clock hours (offset from UTC), one wet hour ``rain_hour`` hours from now."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    stamps = [(start + timedelta(hours=i + offset_hours)).strftime("%Y-%m-%dT%H:%M") for i in range(72)]
    rains = [rain if i == rain_hour + 2 else 0.0 for i in range(72)]

    def answer(_: httpx.Request) -> dict[str, Any]:
        return {"latitude": 51.55, "longitude": -0.11, "elevation": elevation, "utc_offset_seconds": offset_hours * 3600, "timezone": "Europe/London",
                "hourly": {"time": stamps, "temperature_2m": [temp] * 72, "relative_humidity_2m": [humidity] * 72, "precipitation": rains, "snowfall": [0.0] * 72,
                           "wind_speed_10m": [wind] * 72, "wind_direction_10m": [270.0] * 72, "weather_code": [63 if r else 3 for r in rains]}}
    return answer


# ================================================================ venues
def test_the_seed_resolves_home_sides_without_crossing_clubs() -> None:
    cutoff = get_settings().WIRE_GEOCODER_FUZZY_CUTOFF
    ars = geo.resolve_seed("Arsenal FC", "soccer_epl", cutoff)
    assert ars is not None and ars.name == "Emirates Stadium" and ars.roof is geo.Roof.OPEN_AIR
    assert geo.resolve_seed("Manchester City", "soccer_epl", cutoff).name == "Etihad Stadium"  # type: ignore[union-attr]
    assert geo.resolve_seed("Manchester United", "soccer_epl", cutoff).name == "Old Trafford"  # type: ignore[union-attr]
    assert geo.resolve_seed("AFC Bournemouth", "soccer_epl", cutoff).name == "Vitality Stadium"  # type: ignore[union-attr]
    assert geo.resolve_seed("Detroit Lions", "americanfootball_nfl", cutoff).roof is geo.Roof.FIXED_DOME  # type: ignore[union-attr]
    assert geo.resolve_seed("Dallas Cowboys", "americanfootball_nfl", cutoff).roof is geo.Roof.RETRACTABLE  # type: ignore[union-attr]
    assert geo.resolve_seed("Arsenal", "americanfootball_nfl", cutoff) is None  # a sport's seed only
    assert geo.resolve_seed("Nowhere Rovers", "soccer_epl", cutoff) is None
    assert geo.resolve_tournament("tennis_atp_aus_open_singles").name == "Melbourne Park"  # type: ignore[union-attr]  # not "us open"
    assert geo.resolve_tournament("tennis_atp_us_open").name.startswith("USTA")  # type: ignore[union-attr]
    assert geo.resolve_tournament("tennis_wta_wimbledon").surface is geo.Surface.NATURAL_GRASS  # type: ignore[union-attr]
    assert geo.is_indoor_sport("basketball_nba", ["basketball", "icehockey"]) and not geo.is_indoor_sport("soccer_epl", ["basketball"])
    assert all(-90 <= v.latitude <= 90 and -180 <= v.longitude <= 180 and v.teams for v in geo.SEED_VENUES)


# ================================================================ weather friction
def _hours(local: datetime, n: int, **kw: Any) -> list[wi.Hour]:
    base = {"temperature_c": 15.0, "humidity_pct": 50.0, "precipitation_mm": 0.0, "snowfall_cm": 0.0, "wind_kmh": 10.0, "wind_direction_deg": 0.0, "weather_code": 1}
    return [wi.Hour(local=local + timedelta(hours=i), **{**base, **kw}) for i in range(n)]


def test_the_friction_factor_follows_the_brief() -> None:
    p = wi.WeatherPolicy.from_settings(get_settings())
    assert [wi.f_temp(t, p) for t in (-5, 0, 5, 10, 25, 28, 32, 35)] == [0.92, 0.96, 0.96, 1.0, 1.0, 1.0, 1.0, 0.95]  # 25-32 C: optimal (the brief left it open)
    assert wi.f_wind(15, p) == 1.0 and wi.f_wind(25, p) == pytest.approx(0.92) and wi.f_wind(100, p) == 0.70
    assert (wi.f_precip(0, False, p), wi.f_precip(2, False, p), wi.f_precip(6, False, p), wi.f_precip(0, True, p)) == (1.0, 0.90, 0.78, 0.78)
    assert wi.f_alt(1609, p) == pytest.approx(1.09654) and wi.f_alt(4000, p) == pytest.approx(1.15)
    lambeau = wi.summarize(_hours(datetime(2026, 12, 20, 12), 4, temperature_c=-10.0, wind_kmh=25.0, precipitation_mm=8.0), p)
    frozen = wi.impact(lambeau, geo.Roof.OPEN_AIR, 206.0, "americanfootball_nfl", p)
    assert frozen.factor == pytest.approx(0.92 * 0.92 * 0.78 * (1 + 0.00006 * 206), abs=1e-4) and frozen.factor < 0.85
    dome = wi.impact(lambeau, geo.Roof.FIXED_DOME, 1609.0, "basketball_nba", p)
    assert dome.factor == 1.0 and set(dome.factors.values()) == {1.0}
    retractable = wi.impact(lambeau, geo.Roof.RETRACTABLE, 0.0, "americanfootball_nfl", p)
    assert retractable.roof_may_close and retractable.factor < 1 and "retractable" in retractable.advisory


def test_the_window_reports_its_worst_hour_and_evening_dew_only_for_cricket() -> None:
    p = wi.WeatherPolicy.from_settings(get_settings())
    hours = _hours(datetime(2026, 4, 10, 18, 0), 4, humidity_pct=85.0)
    hours[1] = wi.Hour(hours[1].local, 15.0, 85.0, 4.0, 0.0, 40.0, 225.0, 63)
    c = wi.summarize(hours, p)
    assert c is not None and (c.wind_kmh, c.precipitation_mmh, c.wind_direction_deg, c.weather_code) == (40.0, 4.0, 225.0, 63)
    assert c.dew_hours == 2 and wi.cardinal(c.wind_direction_deg) == "SW"  # 20:00 and 21:00 are at or after 19:30
    cricket = wi.impact(c, geo.Roof.OPEN_AIR, 0.0, "cricket_ipl", p)
    assert cricket.dew_expected and cricket.factors["dew"] == 1.15
    assert wi.impact(c, geo.Roof.OPEN_AIR, 0.0, "soccer_epl", p).factors["dew"] == 1.0
    afternoon = wi.summarize(_hours(datetime(2026, 4, 10, 14, 0), 3, humidity_pct=90.0), p)
    assert not wi.impact(afternoon, geo.Roof.OPEN_AIR, 0.0, "cricket_ipl", p).dew_expected
    assert wi.summarize([], p) is None


# ================================================================ Open-Meteo and ESPN parsing
def test_open_meteo_hours_map_to_utc_and_the_geocoder_never_guesses_a_country() -> None:
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    fc = open_meteo.parse_forecast(meteo(now)(httpx.Request("GET", "http://x")))
    assert fc.elevation_m == 34.0 and fc.utc_offset_seconds == 3600
    first_utc, first = fc.hours[0]
    assert first_utc == now - timedelta(hours=2) and first.local == datetime(2026, 10, 11, 11, 0)
    window = open_meteo.window(fc, now + timedelta(minutes=30), now + timedelta(hours=2, minutes=30))
    assert len(window) == 3 and max(h.precipitation_mm for h in window) == 3.0  # 12:00, 13:00, 14:00 UTC; the wet hour is 14:00
    results = [{"name": "Arlington", "latitude": 38.88, "longitude": -77.1, "country": "United States", "country_code": "US", "admin1": "Virginia"},
               {"name": "Arlington", "latitude": 32.74, "longitude": -97.11, "country": "United States", "country_code": "US", "admin1": "Texas", "elevation": 184.0}]
    assert open_meteo.pick_place(results, "USA", "TX").latitude == 32.74  # type: ignore[union-attr]
    assert open_meteo.pick_place(results, "USA", None).latitude == 38.88  # type: ignore[union-attr]
    assert open_meteo.pick_place(results, "England", None) is None
    assert open_meteo.pick_place(results, None, None) is None


def espn_event(event_id: str, start: datetime, home: tuple[str, str], away: tuple[str, str], *, state: str = "pre", score: tuple[int, int] = (0, 0),
               venue: dict[str, Any] | None = None, details: list[dict[str, Any]] | None = None, clock: str | None = None) -> dict[str, Any]:
    """ESPN's scoreboard event, in the shape the live API returns (the fields the parser reads)."""
    return {"id": event_id, "date": start.strftime("%Y-%m-%dT%H:%MZ"), "status": {"displayClock": clock or "0'", "period": 1 if state == "in" else 0,
                                                                           "type": {"state": state, "shortDetail": {"pre": "Sat", "in": clock, "post": "FT"}[state]}},
            "competitions": [{"venue": venue or {}, "details": details or [], "competitors": [
                {"homeAway": "home", "score": str(score[0]), "team": {"id": home[0], "displayName": home[1]}},
                {"homeAway": "away", "score": str(score[1]), "team": {"id": away[0], "displayName": away[1]}}]}]}


def card(team: str, *, red: bool = False, penalty: bool = False) -> dict[str, Any]:
    return {"type": {"text": "Card"}, "team": {"id": team}, "yellowCard": not red and not penalty, "redCard": red, "penaltyKick": penalty, "shootout": False}


def test_espn_parsing_and_pairing() -> None:
    start = datetime(2026, 10, 10, 11, 30, tzinfo=UTC)
    events = ep.parse_scoreboard({"events": [
        espn_event("1", start, ("357", "Leeds United"), ("359", "Arsenal"), state="post", score=(1, 2),
                   venue={"id": "1", "fullName": "Elland Road", "address": {"city": "Leeds", "country": "England"}},
                   details=[card("357"), card("357"), card("359"), card("359", red=True), card("359", penalty=True)]),
        {"id": "broken"},
    ]}, "soccer/eng.1")
    assert len(events) == 1
    e = events[0]
    assert (e.home, e.away, e.home_score, e.away_score, e.status, e.completed) == ("Leeds United", "Arsenal", 1, 2, "FT", True)
    assert e.venue is not None and e.venue.city == "Leeds" and e.venue.indoor is None
    assert e.discipline["HOME"] == ep.TeamDiscipline(2, 0, 0) and e.discipline["AWAY"] == ep.TeamDiscipline(1, 1, 1)
    summary = ep.parse_summary({
        "rosters": [{"team": {"id": "357"}, "roster": [{"starter": True}]}, {"team": {"id": "359"}, "roster": [{"starter": False}]}],
        "injuries": [{"team": {"id": "357"}, "injuries": [
            {"type": {"description": "out"}, "athlete": {"displayName": "Player One", "position": {"abbreviation": "CB"}}, "details": {"type": "Knee", "returnDate": "2026-11-01"}},
            {"type": {"description": "probable"}, "athlete": {"displayName": "Player Two"}}]}],
        "gameInfo": {"officials": [{"displayName": "Second Official", "order": 2}, {"displayName": "Lead Referee", "order": 1}]},
    }, "1", "357", "359")
    assert summary.sheets_listed and summary.sheets == {"HOME": True, "AWAY": False}
    assert summary.injuries_listed and [(a.side, a.player, a.position, a.status) for a in summary.absences] == [("HOME", "Player One", "CB", "OUT")]
    assert summary.officials == ("Lead Referee", "Second Official")
    bare = ep.parse_summary({}, "2", "a", "b")
    assert not bare.sheets_listed and not bare.injuries_listed and bare.absences == ()
    # pairing: names as the two feeds write them, kickoffs within the tolerance
    assert ep.name_similarity("AFC Bournemouth", "Bournemouth") == 1.0 and ep.name_similarity("Leeds", "Leeds United") == 1.0
    assert ep.name_similarity("Brighton & Hove Albion", "Brighton and Hove Albion") >= 0.8
    assert ep.name_similarity("Inter Milan", "AC Milan") < 0.8 and ep.name_similarity("Manchester City", "Manchester United") < 0.8
    fixtures = [("fx-1", "Leeds", "Arsenal", start + timedelta(minutes=30)), ("fx-2", "Leeds", "Arsenal", start + timedelta(days=7))]
    assert ep.match_fixture(e, fixtures, cutoff=0.8, tolerance=timedelta(minutes=90)) == "fx-1"
    assert ep.match_fixture(e, fixtures[1:], cutoff=0.8, tolerance=timedelta(minutes=90)) is None


# ================================================================ sentiment
def test_sports_sentiment_impact_and_credibility() -> None:
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    window, medium = timedelta(hours=2), 0.35
    assert se.polarity("Star striker ruled out with torn ACL") < -0.5
    assert se.polarity("Captain returns to starting XI fit and dominant") > 0.4
    assert se.polarity("No injury concerns for Chelsea") > 0  # negated
    fixtures = [("fx-1", "Arsenal", "Chelsea", now + timedelta(hours=1)), ("fx-2", "Leeds United", "Everton", now + timedelta(hours=30))]
    near = se.read("Arsenal striker ruled out with torn ACL", "Blow before Chelsea", fixtures, now, critical_window=window, medium=medium)
    assert near.impact is se.Impact.CRITICAL and se.Event.ABSENCE in near.events and near.subject is not None and near.subject.team == "Arsenal"
    assert [(m.fixture_id, m.side) for m in near.mentions] == [("fx-1", "HOME"), ("fx-1", "AWAY")]
    far = se.read("Leeds defender ruled out", "", fixtures, now, critical_window=window, medium=medium)
    assert far.impact is se.Impact.HIGH and far.subject is not None and far.subject.fixture_id == "fx-2" and far.subject.team == "Leeds United"  # "Leeds" names Leeds United
    assert se.read("Everton boss faces fitness test doubts", "", fixtures, now, critical_window=window, medium=medium).impact is se.Impact.HIGH
    assert se.read("Club statement", "", (), now, critical_window=window, medium=medium).impact is se.Impact.LOW
    table, default = get_settings().WIRE_SOURCE_CREDIBILITY, get_settings().WIRE_SOURCE_DEFAULT_CREDIBILITY
    assert se.credibility("https://www.bbc.co.uk/sport/1", None, table, default) == 1.0
    assert se.credibility("https://www.skysports.com/x", None, table, default) == 0.9
    assert se.credibility("https://www.thesun.co.uk/x", None, table, default) == 0.4
    assert se.credibility("https://randomblog.example/x", None, table, default) == default


# ================================================================ lineups, referees, catalysts
def test_the_lineup_delta_matches_the_brief_and_renormalises() -> None:
    p = li.LineupPolicy.from_settings(get_settings())
    striker = li.Absence("HOME", "Star Striker", "ST", "OUT", 8.5)
    back = li.Absence("HOME", "Center Back", "CB", "OUT", 6.0)
    delta, unrated = li.deltas([striker, back, li.Absence("AWAY", "Unknown", "GK", "OUT", None)], "soccer", p)
    assert delta["HOME"] == pytest.approx(-(0.85 * 1.3 * 0.1 + 0.6 * 1.2 * 0.1), abs=1e-4) and delta["HOME"] < -0.05  # the brief's -5% and more
    assert delta["AWAY"] == 0.0 and [a.player for a in unrated] == ["Unknown"]  # unrated: counted in nothing
    doubtful = li.cost(li.Absence("HOME", "x", "ST", "DOUBTFUL", 8.5), "soccer", p)
    assert doubtful == pytest.approx(li.cost(striker, "soccer", p) * 0.5)  # type: ignore[operator]
    qb = li.Absence("HOME", "QB1", "QB", "OUT", 9.0)
    assert li.deltas([qb], "americanfootball", p)[0]["HOME"] == -0.25  # capped
    assert li.fortress_impact(qb, "americanfootball", p) == 0.9 and li.fortress_impact(li.Absence("HOME", "x", "CB", "OUT", 9.0), "americanfootball", p) == pytest.approx(0.9 / 3.5, abs=1e-4)
    adjusted = li.adjust({"HOME": 0.5, "DRAW": 0.25, "AWAY": 0.25}, {"HOME": -0.1, "AWAY": 0.0})
    assert sum(adjusted.values()) == pytest.approx(1.0, abs=1e-3) and adjusted["HOME"] == pytest.approx(0.4 / 0.9, abs=1e-4)


def test_a_referee_profile_shrinks_to_the_league() -> None:
    league = "soccer/eng.1"
    records = [rb.Record("Test Referee", league, 2, 3, 0, 1, 1, 0, 2, 1), rb.Record("test referee ", league, 1, 1, 0, 0, 0, 0, 0, 0),
               rb.Record(None, league, 4, 4, 0, 0, 0, 1, 3, 2), rb.Record(None, league, 2, 2, 0, 0, 0, 0, 1, 1), rb.Record("Other", "soccer/esp.1", 9, 9, 2, 2, 2, 2, 5, 5)]
    prof = rb.profile(records, "Test Referee", league, prior=10.0, line=2.5, min_matches=5)
    assert prof is not None and prof.matches == 2 and prof.baseline_matches == 4 and not prof.fortress_ready
    assert prof.avg_yellow_cards == pytest.approx((7 + 10 * 4.75) / 12, abs=1e-3) and prof.avg_red_cards == pytest.approx((1 + 10 * 0.25) / 12, abs=1e-3)
    assert prof.penalties_per_90 == pytest.approx(0.5) and prof.over_totals_pct == 50.0 and prof.home_bias_ratio == 1.5
    assert rb.profile(records, "Nobody", league, prior=10.0, line=2.5, min_matches=5) is None


def test_the_catalyst_rule() -> None:
    t0 = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)
    kw: dict[str, Any] = {"published_at": t0, "observed_at": t0 + timedelta(seconds=150), "window_seconds": 900.0, "min_shift": 0.035, "impacts": ("CRITICAL", "HIGH")}
    before, after = {"HOME": 0.46, "DRAW": 0.26, "AWAY": 0.28}, {"HOME": 0.40, "DRAW": 0.27, "AWAY": 0.33}
    hit = sc.evaluate(impact="CRITICAL", sentiment=-0.7, side="HOME", before=before, after=after, **kw)
    assert hit is not None and hit.selection == "HOME" and hit.shift == -0.06 and hit.latency_seconds == 150.0
    assert sc.evaluate(impact="CRITICAL", sentiment=0.7, side="HOME", before=before, after=after, **kw) is None  # the market went the other way
    assert sc.evaluate(impact="LOW", sentiment=-0.7, side="HOME", before=before, after=after, **kw) is None
    assert sc.evaluate(impact="HIGH", sentiment=-0.7, side="HOME", before=before, after=after, **{**kw, "observed_at": t0 + timedelta(seconds=901)}) is None
    small = {"HOME": 0.44, "DRAW": 0.26, "AWAY": 0.31}
    assert sc.evaluate(impact="HIGH", sentiment=-0.7, side="HOME", before=before, after=small, **kw) is None  # 2 and 3 points: under 3.5
    away = sc.evaluate(impact="HIGH", sentiment=-0.7, side="HOME", before=before, after={"HOME": 0.44, "DRAW": 0.22, "AWAY": 0.34}, **kw)
    assert away is not None and away.selection == "AWAY" and away.shift == 0.06  # the opponent shortened


# ================================================================ end to end: weather
@pytest.mark.asyncio
async def test_the_weather_scan_writes_snapshots_and_the_weather_section(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    now = datetime.now(UTC)
    kickoff = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    await board(redis, "fx-ars", "Arsenal", "Chelsea", "soccer_epl", kickoff)
    await board(redis, "fx-nba", "Los Angeles Lakers", "Boston Celtics", "basketball_nba", kickoff + timedelta(hours=4))
    await board(redis, "fx-nowhere", "Nowhere Rovers", "Elsewhere Town", "soccer_epl", kickoff)
    async with transport({"api.open-meteo.com": meteo(now)}) as http:
        report = await weather.scan(sessions, redis, settings, now, http=http)
    assert report == {"fixtures": 3, "indoor": 1, "forecast": 1, "no_venue": 1, "no_forecast": 0}
    intel = await read_intel(redis, settings, ["fx-ars", "fx-nba", "fx-nowhere"])
    assert "fx-nowhere" not in intel  # no venue: no weather, never a default climate
    w = intel["fx-ars"].weather
    assert w is not None and not w.indoor and w.wind_kmh == 30.0 and w.precipitation_mmh == 3.0 and w.altitude_m == 34.0 and w.source == "open-meteo"
    nba = intel["fx-nba"].weather
    assert nba is not None and nba.indoor and nba.wind_kmh is None
    async with sessions() as session:
        snaps = {s.fixture_id: s for s in (await session.execute(select(WeatherSnapshot))).scalars()}
    ars = snaps["fx-ars"]
    p = wi.WeatherPolicy.from_settings(settings)
    assert ars.venue_name == "Emirates Stadium" and ars.venue_source == "seed" and ars.wind_cardinal == "W" and ars.condition == "Rain"
    assert ars.pitch_impact_score == pytest.approx(round(0.96 * wi.f_wind(30.0, p) * 0.90 * wi.f_alt(34.0, p), 4))
    assert snaps["fx-nba"].pitch_impact_score == 1.0 and snaps["fx-nba"].is_indoor_dome and snaps["fx-nba"].roof_type == "INDOOR"
    frames = [json.loads(f) for f in await live.recent(redis, settings)]
    assert {f["match_id"] for f in frames if f["type"] == "weather"} == {"fx-ars", "fx-nba"}


@pytest.mark.asyncio
async def test_an_entered_venue_wins_and_an_espn_venue_is_geocoded_once(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    now = datetime.now(UTC)
    await board(redis, "fx-x", "Arsenal", "Chelsea", "soccer_epl", now + timedelta(hours=3))
    await board(redis, "fx-y", "Hometown FC", "Away FC", "soccer_epl", now + timedelta(hours=3))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        made = await client.post("/api/v1/the-wire/venues", json={"sport": "soccer", "team_name": "Arsenal", "venue_name": "Temporary Ground", "city": "London",
                                                                    "latitude": 51.6, "longitude": -0.2, "roof_type": "OPEN_AIR"})
        assert made.status_code == 201 and made.json()["venue"]["source"] == f"admin:{user.username}" and made.json()["developer_credit"] == DEVELOPER
        listed = (await client.get("/api/v1/the-wire/venues", params={"sport": "soccer"})).json()
        assert [v["venue"] for v in listed["stored"]] == ["Temporary Ground"] and any(v["name"] == "Emirates Stadium" for v in listed["seed"])
    fixtures = {f.fixture_id: f for f in await tracked(redis, settings, now)}
    calls: list[str] = []

    def geocoder(request: httpx.Request) -> dict[str, Any]:
        calls.append(str(request.url))
        return {"results": [{"name": "Hometown", "latitude": 52.123456, "longitude": -1.5, "elevation": 80.0, "country": "United Kingdom", "country_code": "GB", "admin1": "England"}]}

    espn_venue = {"id": "77", "name": "Hometown Park", "city": "Hometown", "state": None, "country": "England", "indoor": False, "grass": True}
    async with transport({"geocoding-api": geocoder}) as http, sessions() as session:
        entered = await place(session, settings, fixtures["fx-x"], None, http, now)
        assert entered is not None and entered.venue_name == "Temporary Ground" and entered.source.startswith("admin:")
        learnt = await place(session, settings, fixtures["fx-y"], espn_venue, http, now)
        again = await place(session, settings, fixtures["fx-y"], espn_venue, http, now)
        await session.commit()
    assert learnt is not None and again is not None and learnt.venue_name == "Hometown Park" and learnt.latitude == 52.1235 and learnt.roof is geo.Roof.OPEN_AIR
    assert len(calls) == 1 and again.source == "espn+open-meteo"


# ================================================================ end to end: ESPN, absences, referees
@pytest.mark.asyncio
async def test_the_espn_sync_feeds_sheets_injuries_records_and_scores(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    now = datetime.now(UTC)
    soon, later = now + timedelta(hours=1), now + timedelta(hours=26)
    await seed(redis, settings, "fx-lee-ars", "Leeds United", "Arsenal", sharp=PINNACLE_NO_ARB, soft=SOFT, kickoff=soon)
    await board(redis, "fx-nfl", "Dallas Cowboys", "Green Bay Packers", "americanfootball_nfl", later)
    soccer = {"events": [
        espn_event("e1", soon, ("357", "Leeds United"), ("359", "Arsenal"), venue={"id": "9", "fullName": "Elland Road", "address": {"city": "Leeds", "country": "England"}}),
        espn_event("e0", now - timedelta(hours=20), ("363", "Chelsea"), ("370", "Fulham"), state="post", score=(2, 1),
                   details=[card("363"), card("370"), card("370"), card("370", red=True), card("363", penalty=True)]),
    ]}
    nfl = {"events": [espn_event("n1", later, ("6", "Dallas Cowboys"), ("9", "Green Bay Packers"),
                                 venue={"id": "3687", "fullName": "AT&T Stadium", "address": {"city": "Arlington", "state": "TX", "country": "USA"}, "indoor": True})]}
    summaries = {
        "e1": {"rosters": [{"team": {"id": "357"}, "roster": [{"starter": True}]}, {"team": {"id": "359"}, "roster": [{"starter": True}]}]},
        "n1": {"injuries": [{"team": {"id": "6"}, "injuries": [{"type": {"description": "out"}, "athlete": {"displayName": "Player One", "position": {"abbreviation": "QB"}}}]},
                            {"team": {"id": "9"}, "injuries": [{"type": {"description": "questionable"}, "athlete": {"displayName": "Player Two", "position": {"abbreviation": "CB"}}}]}],
               "gameInfo": {"officials": [{"displayName": "Back Judge", "order": 1}]}},
    }
    routes = {"/soccer/eng.1/scoreboard": lambda r: soccer, "/football/nfl/scoreboard": lambda r: nfl,
              "/summary": lambda r: summaries[r.url.params["event"]]}
    async with transport(routes) as http:
        report = await espn_sync.sync(sessions, redis, settings, now, http=http)
        repeat = await espn_sync.sync(sessions, redis, settings, now, http=http)  # the summaries are gated; the record exists once
    assert report["paired"] == 2 and report["records"] == 1 and report["lineups"] == 1 and report["injuries"] == 0 and report["referees"] == 0  # NFL is not refereed
    assert repeat["lineups"] == 0 and repeat["records"] == 1
    intel = await read_intel(redis, settings, ["fx-lee-ars", "fx-nfl"])
    lu = intel["fx-lee-ars"].lineups
    assert lu is not None and lu.home_confirmed and lu.away_confirmed and lu.source == "espn"
    assert intel.get("fx-nfl") is None  # no sheets listed for the NFL, the absence unrated: nothing written
    links = await espn_sync.read_links(redis, settings, ["fx-lee-ars", "fx-nfl"])
    assert links["fx-nfl"]["venue"]["indoor"] is True and links["fx-lee-ars"]["event_id"] == "e1"
    async with sessions() as session:
        records = list((await session.execute(select(OfficiatingRecord))).scalars())
        absences = {a.player_name: a for a in (await session.execute(select(InjuryRosterReport))).scalars()}
    assert [(r.espn_event_id, r.status, r.home_yellow, r.away_yellow, r.away_red, r.home_penalties, r.home_goals, r.league) for r in records] == [
        ("e0", "FINAL", 1, 2, 1, 1, 2, "soccer/eng.1")]
    assert absences["Player One"].status == "OUT" and absences["Player One"].rating is None and absences["Player Two"].status == "QUESTIONABLE"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        before = (await client.get("/api/v1/the-wire/absences/fx-nfl")).json()
        assert sorted(before["unrated"]) == ["Player One", "Player Two"] and before["delta"] == {"HOME": 0.0, "AWAY": 0.0}
        refused = await client.post("/api/v1/the-wire/absences/fx-nfl/publish")
        assert refused.status_code == 409 and refused.json()["detail"]["unrated"] == ["Player One"]
        assert (await client.patch(f"/api/v1/the-wire/absences/{absences['Player One'].id}", json={"rating": 11})).status_code == 422
        rated = (await client.patch(f"/api/v1/the-wire/absences/{absences['Player One'].id}", json={"rating": 9})).json()
        assert rated["injury_section"] == {"written": True, "absences": 1}
        after = (await client.get("/api/v1/the-wire/absences/fx-nfl")).json()
        assert after["delta"]["HOME"] == -0.25 and after["unrated"] == ["Player Two"]
        qb = next(a for a in after["absences"] if a["player"] == "Player One")
        assert qb["fortress_impact"] == 0.9 and qb["position_weight"] == 3.5
        lee = (await client.get("/api/v1/the-wire/absences/fx-lee-ars")).json()
        total = sum(lee["probabilities"].values())
        assert lee["adjusted"] == pytest.approx({k: v / total for k, v in lee["probabilities"].items()}, abs=1e-3)  # no absences: only renormalised
        assert (await client.get("/api/v1/the-wire/absences/fx-nowhere")).status_code == 404
        board_ = (await client.get("/api/v1/the-wire/dashboard", params={"match_ids": "fx-lee-ars,fx-nfl"})).json()
        scores = {s["match_id"]: s for s in board_["scores"]}
        assert scores["fx-lee-ars"]["source"] == "espn" and scores["fx-lee-ars"]["status"] == "SCHEDULED" and board_["developer_credit"] == DEVELOPER
        fixtures = {f["fixture_id"]: f for f in (await client.get("/api/v1/the-wire/fixtures")).json()["fixtures"]}
        assert fixtures["fx-nfl"]["absences"] == 2 and "injuries" in fixtures["fx-nfl"]["intel_sections"] and fixtures["fx-lee-ars"]["espn"]["event_id"] == "e1"
    injuries = (await read_intel(redis, settings, ["fx-nfl"]))["fx-nfl"].injuries
    assert injuries is not None and [(a.side, a.player, a.status, a.impact) for a in injuries.absences] == [("HOME", "Player One", "OUT", 0.9)]  # QUESTIONABLE is no absence


@pytest.mark.asyncio
async def test_referee_records_reach_the_referee_section(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    now = datetime.now(UTC)
    await board(redis, "fx-ref", "Liverpool", "Everton", "soccer_epl", now + timedelta(hours=20))
    rows = [{"referee_name": "Test Referee", "league": "soccer/eng.1", "home_team": f"H{i}", "away_team": f"A{i}", "played_at": (now - timedelta(days=7 * (i + 1))).isoformat(),
             "home_yellow": 2, "away_yellow": 3, "home_red": 0, "away_red": int(i == 0), "home_penalties": int(i < 2), "away_penalties": 0, "home_goals": 2, "away_goals": i % 2}
            for i in range(5)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        imported = (await client.post("/api/v1/the-wire/referees/records", json={"source": "match reports", "records": rows[:4]})).json()
        assert imported["imported"] == 4 and imported["profiles"][0]["matches"] == 4 and not imported["profiles"][0]["fortress_ready"]
        early = (await client.post("/api/v1/the-wire/referees/assign", json={"fixture_id": "fx-ref", "referee_name": "Test  Referee"})).json()
        assert early["record"]["referee"] == "Test Referee" and early["referee_section_written"] is False  # 4 matches: under WIRE_REFEREE_MIN_MATCHES
        await client.post("/api/v1/the-wire/referees/records", json={"source": "match reports", "records": rows[4:]})
        ready = (await client.post("/api/v1/the-wire/referees/assign", json={"fixture_id": "fx-ref", "referee_name": "Test Referee"})).json()
        assert ready["referee_section_written"] is True and ready["profile"]["matches"] == 5
        listed = (await client.get("/api/v1/the-wire/referees")).json()
        assert listed["profiles"][0]["fortress_ready"] and listed["records_per_league"] == {"soccer/eng.1": 5}
        one = (await client.get("/api/v1/the-wire/referees/test referee")).json()
        assert one["referee"] == "Test Referee" and len(one["records"]) == 6 and one["developer_credit"] == DEVELOPER  # 5 records and the assignment
        assert (await client.get("/api/v1/the-wire/referees/nobody")).status_code == 404
    ref = (await read_intel(redis, settings, ["fx-ref"]))["fx-ref"].referee
    # baseline = the referee's own 5 matches (no others): the shrinkage returns the raw rates
    assert ref is not None and ref.name == "Test Referee" and ref.matches == 5 and ref.cards_per_game == pytest.approx(5.2) and ref.penalties_per_90 == pytest.approx(0.4)


# ================================================================ end to end: news and catalysts
@pytest.mark.asyncio
async def test_breaking_news_is_paged_and_becomes_a_catalyst_when_the_market_follows(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    now = datetime.now(UTC)
    await seed(redis, settings, "fx-ars-che", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT, kickoff=now + timedelta(hours=1))
    items = [
        NewsItem(id="a", source="BBC Sport", title="Arsenal captain ruled out of Chelsea clash with hamstring injury", summary="A late blow.",
                 url="https://www.bbc.co.uk/sport/football/1", published_at=now - timedelta(seconds=60)),
        NewsItem(id="b", source="BBC Sport", title="Golf: weekend round-up", summary="", url="https://www.bbc.co.uk/sport/golf/2", published_at=now - timedelta(minutes=5)),
        NewsItem(id="c", source="BBC Sport", title="Old story", summary="", url="https://www.bbc.co.uk/sport/3", published_at=now - timedelta(days=3)),
    ]
    first = await news.scan(sessions, redis, settings, now, items=items)
    assert first == {"fetched": 3, "ingested": 2, "alerts": 1, "catalysts": 0, "fixtures": 1}
    assert (await news.scan(sessions, redis, settings, now, items=items))["ingested"] == 0  # one row per URL
    async with sessions() as session:
        story = (await session.execute(select(NewsArticleSentiment).where(NewsArticleSentiment.url == items[0].url))).scalar_one()
    assert story.tactical_impact == "CRITICAL" and story.sentiment_score < 0 and story.source_credibility == 1.0 and story.alerted
    assert story.subject == {"fixture_id": "fx-ars-che", "team": "Arsenal", "side": "HOME"} and set(story.baseline_probabilities["fx-ars-che"]) == {"HOME", "DRAW", "AWAY"}
    paged = [a for a in await stream(redis, settings) if a.kind is AlertKind.WIRE_BREAKING_NEWS]
    assert len(paged) == 1 and DEVELOPER in paged[0].body and paged[0].detail["fixtures"] == ["fx-ars-che"]

    # the market follows: Arsenal drift everywhere
    await seed(redis, settings, "fx-ars-che", "Arsenal", "Chelsea", sharp={"HOME": 2.70, "DRAW": 3.30, "AWAY": 2.70},
               soft={b: {"HOME": 2.75, "DRAW": 3.25, "AWAY": 2.65} for b in SOFT}, kickoff=now + timedelta(hours=1))
    second = await news.scan(sessions, redis, settings, now + timedelta(seconds=120), items=[])
    assert second["catalysts"] == 1
    async with sessions() as session:
        story = (await session.execute(select(NewsArticleSentiment).where(NewsArticleSentiment.url == items[0].url))).scalar_one()
    assert story.latency_seconds == 180.0 and story.catalyst is not None and story.catalyst["fixture_id"] == "fx-ars-che"
    assert story.associated_steam_move_id == f"fx-ars-che|Match Odds|{story.catalyst['selection']}" and abs(story.catalyst["shift"]) >= settings.WIRE_CATALYST_MIN_PROB_SHIFT
    assert (await news.scan(sessions, redis, settings, now + timedelta(seconds=240), items=[]))["catalysts"] == 0  # recorded once
    assert [a.kind for a in await stream(redis, settings) if a.kind is AlertKind.WIRE_STEAM_CATALYST] == [AlertKind.WIRE_STEAM_CATALYST]
    frames = [json.loads(f) for f in await live.recent(redis, settings)]
    assert [f["type"] for f in frames].count("news") == 2 and [f["type"] for f in frames].count("catalyst") == 1

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        dash = (await client.get("/api/v1/the-wire/dashboard", params={"match_ids": "fx-ars-che"})).json()
        assert dash["news"][0]["tactical_impact"] in ("CRITICAL", "LOW") and {n["title"] for n in dash["news"]} == {items[0].title, items[1].title}
        assert [c["match_id"] for c in dash["catalysts"]] == ["fx-ars-che"] and dash["catalysts"][0]["latency_seconds"] == 180.0
        critical = (await client.get("/api/v1/the-wire/news", params={"impact": "CRITICAL"})).json()["articles"]
        assert [a["title"] for a in critical] == [items[0].title]
        by_fixture = (await client.get("/api/v1/the-wire/news", params={"fixture_id": "fx-ars-che"})).json()["articles"]
        assert len(by_fixture) == 1
        assert len((await client.get("/api/v1/the-wire/catalysts")).json()["catalysts"]) == 1


# ================================================================ the live socket
def test_the_wire_socket_greets_replays_and_relays() -> None:
    probe = sync_redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        probe.ping()
    except (RedisError, OSError):
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    if probe.dbsize() and not probe.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
        pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    settings = get_settings().model_copy(update={"WIRE_PREFIX": "test_wire_ws"})
    probe.flushdb()
    probe.lpush(live.recent_key(settings), json.dumps({"type": "news", "title": "older"}))
    probe.lpush(live.recent_key(settings), json.dumps({"type": "news", "title": "newer"}))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        app.state.redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        yield
        await app.state.redis.aclose()
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(wire_ws.router, prefix="/api/v1/ws")
    app.dependency_overrides[get_ws_user] = lambda: User(id=uuid.uuid4(), username="viewer", hashed_password="x")
    app.dependency_overrides[get_session_factory] = lambda: async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client, client.websocket_connect("/api/v1/ws/the-wire?token=x") as ws:
            ack = ws.receive_json()
            assert ack["type"] == "connection_ack" and ack["developer"] == DEVELOPER and ack["status"] == "CONNECTED_TO_VIDUR_TACTICAL_WIRE"
            assert [ws.receive_json()["title"], ws.receive_json()["title"]] == ["older", "newer"]  # the backlog, oldest first
            probe.publish(live.channel(settings), json.dumps({"type": "catalyst", "match_id": "fx-1"}))
            assert ws.receive_json() == {"type": "catalyst", "match_id": "fx-1"}
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}
    finally:
        probe.flushdb()
        probe.close()
