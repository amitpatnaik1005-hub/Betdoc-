"""Group 112: live sports feeds, quota-safe PostgreSQL caching and honest hydration.

* Odds are bought only through the ingestion fleet's Odds API source; the runner and the admin endpoint go through it
  (switch, key, breaker, reserve, floor, shield) and report HYDRATED_FROM_DATABASE when nothing is bought.
* THE_ODDS_API_KEY is read before ODDS_API_KEY (Fleet Command's vault key still wins); sports out of season are never
  bought (the free /sports list); the cadence is 6h / 2h / 30 min; the multi-sport boards and the Wire's dashboard
  spend no credit of their own.
* A stored board replayed into Redis keeps the time it was bought: nothing older than Redis would hold is replayed, and
  the match board lists stored prices aged, with no EV, never slipped.
* Football-Data.org and Sportmonks are paired to the stored fixtures by names and kickoff (precision first) and give the
  board each fixture's kickoff check, status, score and table positions.
* The header chip on /consultant and /bet-slip reads "Ticket Desk".
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.adapters.ingestion.base import IngestionBatch, ProviderAuthError, SourcePayload
from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor
from app.adapters.sports_data import SportsDataAuthError
from app.adapters.sports_data.football_data import parse_matches, parse_table
from app.adapters.sports_data.sportsmonks import parse_fixtures
from app.api.v1 import omni_admin
from app.api.v1 import the_wire as wire_api
from app.core.config import Settings, get_settings
from app.domain.live_context import ProviderFixture, StoredFixture, TableRow, context, pair
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.services import live_sports_ingestion_service as live
from app.services import match_board_service, multi_sport_odds_sync, omni_fleet
from app.services import pre_match_odds_service as store
from app.services.omni_fleet import FleetDeps, RunSummary, env_key_field, resolve_api_key
from app.services.omni_normalizer import OmniNormalizer, default_alias_dictionary
from app.workers import live_ingest_runner
from tests.test_backtest_and_parlay import redis  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ODDS_DESCRIPTOR = SimpleNamespace(source_id="odds_api", spec=None)  # what the key helpers read of a SourceDescriptor


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def _event(match_id: str, home: str, away: str, kickoff: datetime, books: dict[str, tuple[float, ...]], updated: datetime) -> dict[str, Any]:
    stamp = updated.isoformat().replace("+00:00", "Z")
    def outcomes(prices: tuple[float, ...]) -> list[dict[str, Any]]:
        names = [home, "Draw", away] if len(prices) == 3 else [home, away]
        return [{"name": n, "price": p} for n, p in zip(names, prices, strict=True)]
    return {"id": match_id, "sport_key": "soccer_epl", "commence_time": kickoff.isoformat().replace("+00:00", "Z"), "home_team": home, "away_team": away,
            "bookmakers": [{"key": b, "title": b.title(), "last_update": stamp, "markets": [{"key": "h2h", "last_update": stamp, "outcomes": outcomes(p)}]}
                           for b, p in books.items()]}


BOOKS = {"pinnacle": (2.10, 3.45, 3.60), "onexbet": (2.18, 3.40, 3.50), "parimatch": (2.15, 3.35, 3.55), "williamhill": (2.05, 3.30, 3.40)}


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(PreMatchOddsSnapshotModel.__table__.create)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _store(sessions: async_sessionmaker[AsyncSession], events: list[dict[str, Any]], bought: datetime, sport: str = "soccer_epl") -> None:
    async with sessions() as db:
        await store.store_board(db, events, sport, bought, _settings())
        await db.commit()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[PreMatchOddsSnapshotModel]:
    async with sessions() as db:
        boards = await store.stored_boards(db, ["soccer_epl", "soccer_spain_la_liga"], NOW, _settings())
    return [r for rows in boards.values() for r in rows]


def _deps(sessions: Any, settings: Settings | None = None, redis: Any = None, http: Any = None) -> FleetDeps:
    return FleetDeps(redis=redis, session_factory=sessions, http=http, vault=None, settings=settings or _settings())


# ================================================================ Option B and the keys
def test_the_header_chip_reads_ticket_desk() -> None:
    config = (FRONTEND / "config" / "commanders.config.ts").read_text(encoding="utf-8")
    block = config[config.index("  ASHVATTHAMA: {"):]
    assert re.search(r'name: "Ticket Desk"', block[:block.index("\n  },")]) and "Ashvatthama" not in config


def test_the_dashboard_key_is_read_first_and_the_vault_still_wins() -> None:
    both = _settings(THE_ODDS_API_KEY=SecretStr("dashboard"), ODDS_API_KEY=SecretStr("legacy"))
    assert both.odds_api_env_key().get_secret_value() == "dashboard"
    assert env_key_field(ODDS_DESCRIPTOR, both) == "THE_ODDS_API_KEY" and resolve_api_key(None, ODDS_DESCRIPTOR, both, None) == "dashboard"
    legacy = _settings(THE_ODDS_API_KEY=SecretStr(""), ODDS_API_KEY=SecretStr("legacy"))
    assert env_key_field(ODDS_DESCRIPTOR, legacy) == "ODDS_API_KEY" and legacy.odds_api_env_key().get_secret_value() == "legacy"
    vault = SimpleNamespace(decrypt_key=lambda _: "fleet-command")
    row = SimpleNamespace(encrypted_api_key="ciphertext")
    assert resolve_api_key(row, ODDS_DESCRIPTOR, both, vault) == "fleet-command"  # type: ignore[arg-type]


def test_the_suite_never_borrows_the_developers_provider_keys() -> None:
    s = get_settings()
    for key in (s.THE_ODDS_API_KEY, s.FOOTBALL_DATA_ORG_API_KEY, s.SPORTSMONKS_API_KEY):
        assert key is None or key.get_secret_value() == ""


def test_coverage_and_cadence_defaults() -> None:
    s = get_settings()
    assert [k for k in s.ODDS_SPORT_KEYS.split(",")] == [
        "cricket_international_t20", "cricket_odi", "cricket_test_match", "cricket_ipl", "cricket_t20_blast",
        "soccer_epl", "soccer_uefa_champs_league", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga"]
    assert (s.PRE_MATCH_TIER1_KICKOFF_SECONDS, s.PRE_MATCH_TIER1_MAX_AGE_SECONDS) == (24 * 3600, 6 * 3600)
    assert (s.PRE_MATCH_TIER2_KICKOFF_SECONDS, s.PRE_MATCH_TIER2_MAX_AGE_SECONDS, s.PRE_MATCH_TIER3_MAX_AGE_SECONDS) == (6 * 3600, 2 * 3600, 30 * 60)
    assert s.ODDS_API_SKIP_INACTIVE_SPORTS and s.ODDS_QUOTA_FLOOR >= 2  # the floor is stricter than "two left"
    assert s.SPORTS_ODDS_SOURCE == "store" and s.WIRE_ODDS_API_SCORES_ENABLED is False  # no second buyer of the quota


def test_a_day_at_todays_fixtures_is_projected_from_the_tiers() -> None:
    s = _settings(ODDS_API_REGIONS="uk,eu", ODDS_API_MARKETS="h2h", ODDS_POLLING_INTERVAL_SEC=60)
    per_day = live.projected_credits_per_day({"soccer_epl": NOW + timedelta(hours=3), "cricket_odi": NOW + timedelta(hours=12), "soccer_spain_la_liga": None}, s, NOW)
    assert per_day == {"soccer_epl": 48 * 2, "cricket_odi": 12 * 2, "soccer_spain_la_liga": 4 * 2}  # 30 min, 2 h, 6 h; two regions each


# ================================================================ the free in-season filter
async def test_sports_out_of_season_are_never_bought() -> None:
    OddsApiIngestor._season = None
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/v4/sports"):
            return httpx.Response(200, json=[{"key": "soccer_epl", "active": True}, {"key": "cricket_ipl", "active": False}],
                                  headers={"x-requests-remaining": "480"})
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "478"})

    s = _settings(ODDS_SPORT_KEYS="soccer_epl,cricket_ipl", OMNI_FLEET_MAX_ATTEMPTS=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        batch = await OddsApiIngestor(http, s, api_key="k" * 32).fetch()
        again = await OddsApiIngestor(http, s, api_key="k" * 32).fetch()
    assert seen == ["/v4/sports", "/v4/sports/soccer_epl/odds", "/v4/sports/soccer_epl/odds"]  # the list is read once per TTL
    assert batch.meta["out_of_season"] == ["cricket_ipl"] and {(p.key, tuple(p.data)) for p in batch.payloads} == {("cricket_ipl", ()), ("soccer_epl", ())}
    assert again.meta["sports"] == ["soccer_epl"]
    OddsApiIngestor._season = None


async def test_an_unreadable_season_list_drops_nothing_but_a_rejected_key_raises() -> None:
    OddsApiIngestor._season = None
    s = _settings(ODDS_SPORT_KEYS="soccer_epl,cricket_ipl", OMNI_FLEET_MAX_ATTEMPTS=1)
    paths: list[str] = []

    def down(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500) if request.url.path.endswith("/v4/sports") else httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        batch = await OddsApiIngestor(http, s, api_key="k" * 32).fetch()
    assert batch.meta["sports"] == ["soccer_epl", "cricket_ipl"] and paths.count("/v4/sports") == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as http:
        with pytest.raises(ProviderAuthError):
            await OddsApiIngestor(http, s, api_key="k" * 32).fetch()
    OddsApiIngestor._season = None


# ================================================================ the store replays exactly what was bought
async def test_a_stored_board_replays_as_the_event_it_was_bought_as(sessions) -> None:
    bought = NOW - timedelta(minutes=4)
    event = _event("ev-1", "Arsenal", "Chelsea", NOW + timedelta(hours=20), BOOKS, bought)
    await _store(sessions, [event], bought)
    (row,) = await _rows(sessions)
    assert store.price_time(row) == bought
    replay = store.snapshot_event(row, polled_at=bought)
    normalizer = OmniNormalizer(default_alias_dictionary())

    def run(events: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
        report = normalizer.normalize(IngestionBatch(source_id="odds_api", payloads=[SourcePayload("soccer_epl", events)], fetched_at=bought,
                                                     latency_ms=0, requests=0, retries=0))
        return [t.model_dump() for t in report.ticks], [q.model_dump() for q in report.quotes]

    assert run([replay]) == run([event]) and run([event])[0]  # the same ticks and frames as the original purchase
    assert store.snapshot_event(row, polled_at=bought - timedelta(hours=1))["bookmakers"] == []  # another fetch's markets are not this one's


async def test_hydration_keeps_the_purchase_time_and_skips_what_redis_would_not_hold(sessions, monkeypatch) -> None:
    fresh, old = NOW - timedelta(minutes=5), NOW - timedelta(hours=2)
    await _store(sessions, [_event("ev-fresh", "Arsenal", "Chelsea", NOW + timedelta(hours=8), BOOKS, fresh)], fresh)
    await _store(sessions, [_event("ev-old", "Everton", "Fulham", NOW + timedelta(hours=30), BOOKS, old)], old)
    published: dict[str, list[Any]] = {}

    async def ticks(_deps: Any, items: list[Any]) -> list[Any]:
        published["ticks"] = list(items)
        return list(items)

    async def quotes(_redis: Any, items: list[Any], _settings: Any) -> bool:
        published["quotes"] = list(items)
        return True

    async def source(_deps: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(live, "publish_fleet_ticks", ticks)
    monkeypatch.setattr(live, "publish_market_quotes", quotes)
    monkeypatch.setattr(live, "_odds_source", source)
    report = await live.hydrate(_deps(sessions, _settings(LIVE_ODDS_SNAPSHOT_TTL_SECONDS=900)), sports=["soccer_epl"], now=NOW)
    assert (report["fixtures_stored"], report["fixtures_hydrated"], report["skipped_stale"]) == (2, 1, 1)
    assert published["ticks"] and {t.home_team for t in published["ticks"]} == {"Arsenal"}
    assert all(q.fetched_at == fresh for q in published["quotes"])  # never re-stamped as live
    assert all(t.observed_at is None or t.observed_at <= fresh for t in published["ticks"])


# ================================================================ ingest goes through the fleet, never around it
@pytest.fixture
def fleet_doubles(monkeypatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"runs": [], "hydrated": 0, "metrics": {}, "refusal": None, "due": None, "row": None}

    async def source(_deps: Any) -> tuple[Any, Any]:
        return SimpleNamespace(source_id="odds_api", devig=None, interval_seconds=60.0, cost="metered", spec=None, requires_api_key=True), calls["row"]

    async def metrics(_deps: Any) -> dict[str, str]:
        return calls["metrics"]

    async def stretch(_deps: Any, _descriptor: Any) -> float:
        return 1.0

    async def refusal(_deps: Any, _source: str) -> str | None:
        return calls["refusal"]

    async def shield(_redis: Any, _settings: Any) -> str:
        return "AUTO"

    async def due(_sf: Any, _redis: Any, _settings: Any, scope: list[str], _now: Any, stretch: float = 1.0) -> list[str]:  # noqa: ARG001
        return list(scope) if calls["due"] is None else calls["due"]

    async def run(source_id: str, _deps: Any, **kwargs: Any) -> RunSummary:
        calls["runs"].append((source_id, kwargs))
        return calls.get("summary") or RunSummary(source_id=source_id, status="ok", runner="manual", fixtures=12, ticks=36)

    async def hydrate(_deps: Any, **_: Any) -> dict[str, Any]:
        calls["hydrated"] += 1
        return {"fixtures_hydrated": 0}

    async def nothing(*_: Any, **__: Any) -> dict[str, Any]:
        return {"fixtures": 0, "sports": {}}

    for name, fn in {"_odds_source": source, "_metrics": metrics, "_stretch": stretch, "refusal_reason": refusal, "read_shield_mode": shield,
                     "run_source": run, "hydrate": hydrate, "refresh_context": nothing, "store_summary": nothing}.items():
        monkeypatch.setattr(live, name, fn)
    monkeypatch.setattr(live.store, "due_scope", due)
    return calls


async def test_scope_is_the_fleets_coverage_by_sport_group() -> None:
    keys, missing = live.resolve_scope(["cricket", "Football", "soccer_epl", "darts"], get_settings())
    assert keys[:3] == ["cricket_international_t20", "cricket_odi", "cricket_test_match"] and "soccer_germany_bundesliga" in keys
    assert keys.count("soccer_epl") == 1 and missing == ["darts"]


async def test_a_due_sport_is_bought_through_the_fleet(fleet_doubles) -> None:
    fleet_doubles["due"] = ["soccer_epl"]
    result = await live.ingest(_deps(None, _settings(ODDS_API_KEY=SecretStr("test-key"))), sports=["football"], now=NOW)
    assert result["status"] == "COMPLETED" and result["odds_api"]["outcome"] == "BOUGHT" and fleet_doubles["hydrated"] == 0
    assert fleet_doubles["runs"] == [("odds_api", {"runner": "manual", "force": False, "scope": ["soccer_epl"]})]


@pytest.mark.parametrize(("setup", "force", "outcome"), [
    ({"due": []}, False, "UP_TO_DATE"),  # the cadence: every stored board within its tier's age
    ({"refusal": "the odds_api circuit breaker is open for 40s more"}, True, "REFUSED"),
    ({"metrics": {"quota_remaining": "8"}}, True, "REFUSED"),  # under ODDS_QUOTA_FLOOR, even forced
    ({"summary": RunSummary(source_id="odds_api", status="skipped", runner="manual", reason="locked")}, True, "SKIPPED"),
    ({"row": SimpleNamespace(is_enabled=False, paused_at=None, encrypted_api_key="x")}, False, "SKIPPED"),  # switched off in Fleet Command
    ({"summary": RunSummary(source_id="odds_api", status="error", runner="manual", reason="The Odds API: HTTP 401")}, False, "FAILED"),
])
async def test_nothing_bought_is_said_and_served_from_the_store(fleet_doubles, setup: dict[str, Any], force: bool, outcome: str) -> None:
    fleet_doubles.update(setup)
    result = await live.ingest(_deps(None, _settings(ODDS_QUOTA_FLOOR=10, ODDS_API_KEY=SecretStr("test-key"))), sports=["cricket", "football"], force=force, now=NOW)
    assert result["status"] == "HYDRATED_FROM_DATABASE" and result["odds_api"]["outcome"] == outcome and fleet_doubles["hydrated"] == 1
    assert result["odds_api"]["reason"] and result["developer_credit"] == "Amit Ashok Kumar Patnaik"
    if outcome in ("UP_TO_DATE", "REFUSED"):
        assert fleet_doubles["runs"] == []
    if setup.get("row") is not None:
        assert "switched off in Fleet Command" in result["odds_api"]["reason"] and fleet_doubles["runs"] == []


# ================================================================ the sports-data feeds
FD_MATCHES = {"matches": [
    {"id": 1, "utcDate": "2026-10-24T14:00:00Z", "status": "TIMED", "lastUpdated": "2026-10-23T08:00:00Z", "competition": {"code": "PL", "name": "Premier League"},
     "homeTeam": {"name": "Arsenal FC", "shortName": "Arsenal"}, "awayTeam": {"name": "Chelsea FC", "shortName": "Chelsea"}, "score": {"fullTime": {"home": None, "away": None}}},
    {"id": 2, "utcDate": "2026-10-24T18:30:00Z", "status": "TIMED", "competition": {"code": "PL", "name": "Premier League"},
     "homeTeam": {"name": "Everton FC", "shortName": "Everton"}, "awayTeam": {"name": "Fulham FC", "shortName": "Fulham"}, "score": {"fullTime": {}}},
    {"id": 3, "utcDate": "2026-10-23T11:00:00Z", "status": "IN_PLAY", "competition": {"code": "PD", "name": "Primera Division"},
     "homeTeam": {"name": "FC Bayern München", "shortName": "Bayern"}, "awayTeam": {"name": "Real Madrid CF", "shortName": "Real Madrid"},
     "score": {"fullTime": {"home": 1, "away": 0}}},
    {"id": 4, "utcDate": "2026-10-25T14:00:00Z", "status": "POSTPONED", "competition": {"code": "XX"}, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}},
]}
FD_TABLE = {"standings": [{"stage": "REGULAR_SEASON", "type": "TOTAL", "table": [
    {"position": 1, "team": {"name": "Arsenal FC", "shortName": "Arsenal"}, "points": 21, "playedGames": 8},
    {"position": 9, "team": {"name": "Chelsea FC", "shortName": "Chelsea"}, "points": 12, "playedGames": 8},
    {"position": 14, "team": {"name": "Everton FC", "shortName": "Everton"}, "points": 8, "playedGames": 8},
]}]}
SM_FIXTURES = {"data": [{
    "id": 77, "league_id": 271, "starting_at": "2026-10-23 17:00:00", "starting_at_timestamp": int(datetime(2026, 10, 23, 17, tzinfo=UTC).timestamp()),
    "state": {"developer_name": "FT"}, "league": {"name": "Superliga"},
    "participants": [{"name": "Brøndby IF", "meta": {"location": "away", "position": 5}}, {"name": "Randers FC", "meta": {"location": "home", "position": 9}}],
    "scores": [{"description": "CURRENT", "score": {"goals": 2, "participant": "home"}}, {"description": "CURRENT", "score": {"goals": 1, "participant": "away"}},
               {"description": "1ST_HALF", "score": {"goals": 1, "participant": "home"}}],
}], "pagination": {"has_more": False}}
COMPETITIONS = {"PL": "soccer_epl", "PD": "soccer_spain_la_liga"}


def test_the_feeds_parse_as_they_answer() -> None:
    fixtures = parse_matches(FD_MATCHES, COMPETITIONS)
    assert [(f.provider_id, f.status, f.home_score, f.away_score) for f in fixtures] == [("1", "SCHEDULED", None, None), ("2", "SCHEDULED", None, None), ("3", "IN_PLAY", 1, 0)]
    assert fixtures[0].home == ("Arsenal FC", "Arsenal") and fixtures[0].sport_key == "soccer_epl"  # a competition not configured is left out
    assert [(r.position, r.points) for r in parse_table(FD_TABLE)] == [(1, 21), (9, 12), (14, 8)]
    assert parse_table({"standings": [{"type": "TOTAL", "table": []}, {"type": "TOTAL", "table": []}]}) == []  # group tables are not one table
    (sm,) = parse_fixtures(SM_FIXTURES, {271: "soccer_denmark_superliga"})
    assert (sm.home, sm.away, sm.status, sm.home_score, sm.away_score, sm.home_position, sm.away_position) == (("Randers FC",), ("Brøndby IF",), "FINISHED", 2, 1, 9, 5)
    assert parse_fixtures(SM_FIXTURES, {501: "soccer_spl"}) == []


def _stored(fid: str, home: str, away: str, kickoff: datetime, sport: str = "soccer_epl") -> StoredFixture:
    return StoredFixture(fid, sport, home, away, kickoff)


def test_pairing_is_precision_first() -> None:
    k = datetime(2026, 10, 24, 14, tzinfo=UTC)
    provided = [ProviderFixture("football-data.org", "1", "soccer_epl", "PL", k, ("Arsenal FC", "Arsenal"), ("Chelsea FC", "Chelsea"), "SCHEDULED"),
                ProviderFixture("football-data.org", "2", "soccer_epl", "PL", k + timedelta(hours=2), ("Manchester United FC", "Man United"), ("Leeds United FC",), "SCHEDULED"),
                ProviderFixture("football-data.org", "3", "soccer_spain_la_liga", "PD", k, ("FC Bayern München", "Bayern"), ("Real Madrid CF", "Real Madrid"), "SCHEDULED")]
    stored = [_stored("a", "Arsenal", "Chelsea", k + timedelta(hours=2)),  # moved by two hours: still the same match
              _stored("m", "Manchester City", "Leeds United", k + timedelta(hours=2)),  # City is not United
              _stored("b", "Bayern Munich", "Real Madrid", k, "soccer_spain_la_liga"),  # no shared word, same kickoff, the other side certain
              _stored("b2", "Bayern Munich", "Real Madrid", k + timedelta(hours=3), "soccer_spain_la_liga")]  # same names, kickoff off: not loose
    result = pair(stored, provided, cutoff=0.85, window=timedelta(hours=72), tolerance=timedelta(minutes=5))
    assert {fid: p.provider_id for fid, p in result.pairs.items()} == {"a": "1", "b": "3"} and result.unpaired == 2


def test_context_says_what_the_feed_says() -> None:
    k = datetime(2026, 10, 24, 14, tzinfo=UTC)
    fixture = ProviderFixture("football-data.org", "1", "soccer_epl", "Premier League", k + timedelta(hours=2), ("Arsenal FC", "Arsenal"), ("Chelsea FC", "Chelsea"), "SCHEDULED")
    tables = {"soccer_epl": [TableRow(("Arsenal FC", "Arsenal"), 1, 21, 8), TableRow(("Chelsea FC", "Chelsea"), 9, 12, 8)]}
    ctx = context(_stored("a", "Arsenal", "Chelsea", k), fixture, tolerance=timedelta(minutes=5), tables=tables, cutoff=0.85, checked_at=NOW)
    assert ctx["kickoff"] == {"check": "MOVED", "provider_kickoff": (k + timedelta(hours=2)).isoformat(), "difference_minutes": 120}
    assert ctx["score"] is None and ctx["standings"] == {"home": {"position": 1, "points": 21, "played": 8}, "away": {"position": 9, "points": 12, "played": 8}}
    finished = ProviderFixture("sportmonks", "77", "soccer_epl", None, k, ("Arsenal",), ("Chelsea",), "FINISHED", 2, 1, home_position=3, away_position=None)
    ctx = context(_stored("a", "Arsenal", "Chelsea", k), finished, tolerance=timedelta(minutes=5), tables={}, cutoff=0.85, checked_at=NOW)
    assert ctx["kickoff"]["check"] == "CONFIRMED" and ctx["score"] == {"home": 2, "away": 1}
    assert ctx["standings"] == {"home": {"position": 3, "points": None, "played": None}, "away": None}


async def test_context_is_refreshed_paired_and_read_back(sessions, redis: Redis) -> None:  # noqa: F811 - fixture
    bought = NOW - timedelta(minutes=10)
    await _store(sessions, [_event("ev-a", "Arsenal", "Chelsea", datetime(2026, 10, 24, 14, tzinfo=UTC), BOOKS, bought),
                            _event("ev-e", "Everton", "Fulham", datetime(2026, 10, 24, 16, 30, tzinfo=UTC), BOOKS, bought)], bought)
    secret = "sm-token-never-shown"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "football-data" in request.url.host:
            assert request.headers["X-Auth-Token"] == "fd-token" and "fd-token" not in str(request.url)
            if path.endswith("/matches"):
                return httpx.Response(200, json=FD_MATCHES, headers={"X-Requests-Available-Minute": "8"})
            if path.endswith("/competitions/PL/standings"):
                return httpx.Response(200, json=FD_TABLE)
            return httpx.Response(403)
        return httpx.Response(401, json={"message": "no plan"})

    settings = _settings(FOOTBALL_DATA_ORG_API_KEY=SecretStr("fd-token"), SPORTSMONKS_API_KEY=SecretStr(secret), FOOTBALL_DATA_COMPETITIONS=COMPETITIONS,
                         LIVE_CONTEXT_REDIS_PREFIX="g112:test:live_context")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await live.refresh_context(_deps(sessions, settings, redis, http), now=NOW)
    fd = report["providers"]["football-data.org"]
    assert (fd["fixtures"], fd["paired"], fd["requests_available_minute"], fd["tables"]) == (3, 2, 8, 1)
    assert "401" in report["providers"]["sportmonks"]["error"] and secret not in json.dumps(report)
    rows = await _rows(sessions)
    ids = {r.home_team: live.canonical_fixture_id(r, default_alias_dictionary()) for r in rows}
    contexts = await live.read_context(redis, settings, list(ids.values()))
    arsenal, everton = contexts[ids["Arsenal"]], contexts[ids["Everton"]]
    assert arsenal["kickoff"]["check"] == "CONFIRMED" and arsenal["standings"]["home"]["position"] == 1 and arsenal["standings"]["away"]["position"] == 9
    assert everton["kickoff"] == {"check": "MOVED", "provider_kickoff": "2026-10-24T18:30:00+00:00", "difference_minutes": 120}
    assert everton["standings"]["away"] is None  # Fulham is not in the table it was given


async def test_a_feed_that_rejects_its_key_raises_without_the_key() -> None:
    from app.adapters.sports_data.sportsmonks import SportmonksClient  # noqa: PLC0415

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as http:
        with pytest.raises(SportsDataAuthError) as info:
            await SportmonksClient(http, "secret-token", "https://api.sportmonks.com/v3", 5).fixtures({271: "x"}, NOW.date(), NOW.date())
    assert "secret-token" not in str(info.value) and "api_token" not in str(info.value)


# ================================================================ the match board: stored prices and context
async def test_the_board_lists_stored_prices_aged_and_never_offered(sessions, monkeypatch) -> None:
    bought = NOW - timedelta(minutes=47)
    await _store(sessions, [_event("ev-live", "Arsenal", "Chelsea", NOW + timedelta(hours=5), BOOKS, bought),
                            _event("ev-store", "Everton", "Fulham", NOW + timedelta(hours=26), BOOKS, bought)], bought)
    rows = {r.home_team: r for r in await _rows(sessions)}
    aliases = default_alias_dictionary()
    live_id = live.canonical_fixture_id(rows["Arsenal"], aliases)
    stored_id = live.canonical_fixture_id(rows["Everton"], aliases)
    live_card = {"fixture_id": live_id, "home": "Arsenal", "away": "Chelsea", "sport": "Football", "sport_key": "soccer_epl", "league": "EPL",
                 "kickoff": (NOW + timedelta(hours=5)).isoformat(), "in_play": False, "minutes_since_kickoff": None, "max_stake_inr": None, "projection": None,
                 "markets": [{"market": "Match Odds", "kind": "MATCH_ODDS", "margins": {}, "selections": [{"leg_id": f"{live_id}|Match Odds|HOME", "ev": 0.03}]}]}

    async def board(*_: Any, **__: Any) -> dict[str, Any]:
        return {"fixtures": [live_card], "steam_readable": True, "stats": {"fixtures": 1}, "as_of": NOW.isoformat()}

    async def contexts(_redis: Any, _settings: Any, ids: list[str]) -> dict[str, Any]:
        return {stored_id: {"status": "POSTPONED"}} if stored_id in ids else {}

    monkeypatch.setattr(match_board_service.manual_parlay, "board", board)
    monkeypatch.setattr(match_board_service.live_ingest, "read_context", contexts)
    async with sessions() as db:
        result = await match_board_service.match_board(None, db, _settings(), NOW)  # type: ignore[arg-type]
    assert [c["fixture_id"] for c in result["fixtures"]] == [live_id, stored_id]  # the live card wins its fixture; stored after
    live_row, stored_row = result["fixtures"]
    assert live_row["priced_from"] == "live" and stored_row["priced_from"] == "stored" and stored_row["prices_age_minutes"] == 47
    assert stored_row["context"] == {"status": "POSTPONED"} and live_row["context"] is None and result["stats"]["stored_fixtures"] == 1
    (market,) = stored_row["markets"]
    assert [s["leg_id"] for s in market["selections"]] == [f"{stored_id}|Match Odds|{x}" for x in ("HOME", "DRAW", "AWAY")]
    home = market["selections"][0]
    assert home["ev"] is None and home["best"] is None and stored_row["best_ev"] is None
    assert home["prices"] == {"pinnacle": 2.10, "1xbet": 2.18, "parimatch": 2.15}  # the account books by their own names; William Hill is not one
    assert 0.40 < home["fair_probability"] < 0.50  # the consensus de-vigged when it was bought


def test_a_stored_board_without_two_priced_sides_is_not_listed() -> None:
    row = SimpleNamespace(markets_json={}, last_polled_at=NOW, consensus_fair_odds={"outcomes": {"home": {"probability": 0.5}}},
                          bookmaker_odds={"parimatch": {"home": 1.9}}, commence_time=NOW + timedelta(hours=3), home_team="A", away_team="B", sport_key="soccer_epl")
    assert match_board_service.stored_card(row, "fx", NOW, _settings()) is None  # type: ignore[arg-type]


# ================================================================ no second buyer of the quota
async def test_the_multi_sport_boards_read_the_store_with_its_purchase_time(sessions, redis: Redis) -> None:  # noqa: F811 - fixture
    bought = NOW - timedelta(minutes=20)
    await _store(sessions, [_event("ev-1", "Arsenal", "Chelsea", NOW + timedelta(hours=20), BOOKS, bought)], bought)
    service = multi_sport_odds_sync.MultiSportOddsSyncService(settings=_settings(SPORTS_PREFIX="g112:test:sports", SPORTS_VALUE_LINE_MAX_AGE_SECONDS=900.0))
    report = await service._poll_from_store(sessions, redis, ["soccer_epl", "basketball_nba"])
    assert report["fetched"] == ["soccer_epl"] and report["missing"] == ["basketball_nba"] and "ODDS_SPORT_KEYS" in report["reason"]
    board = json.loads(await redis.get("g112:test:sports:board:soccer_epl"))
    assert datetime.fromisoformat(board["fetched_at"]) == bought and board["source"] == "store" and len(board["events"]) == 1
    screened = await service.screen_value_lines(redis, NOW)
    assert screened["stale_boards"] == 1 and screened["edges"] == 0  # twenty minutes old: no value line from it
    assert (await service.screen_value_lines(redis, bought + timedelta(minutes=1)))["stale_boards"] == 0


def test_the_wire_dashboard_spends_no_credit_unless_asked() -> None:
    off = _settings(THE_ODDS_API_KEY=SecretStr("dash"), WIRE_ODDS_API_SCORES_ENABLED=False)
    assert wire_api._odds(off)._api_key is None
    on = _settings(THE_ODDS_API_KEY=SecretStr("dash"), WIRE_ODDS_API_SCORES_ENABLED=True)
    assert wire_api._odds(on)._api_key == "dash" and wire_api._odds(on) is wire_api._odds(on)  # one cache across dashboard polls


# ================================================================ the runner, the endpoint, the beat
def test_the_runner_parses_and_reports_without_keys() -> None:
    args = live_ingest_runner.parse_args(["--status", "--probe"])
    assert args.status and args.probe and args.sports == "cricket,football"
    with pytest.raises(SystemExit):
        live_ingest_runner.parse_args(["--hydrate-only", "--status"])
    text = live_ingest_runner.render("hydrate", {"fixtures_stored": 9, "fixtures_hydrated": 2, "skipped_stale": 7, "max_age_seconds": 900.0,
                                                 "ticks": 6, "quotes": 2, "elapsed_ms": 4.2})
    assert "replayed into Redis" in text and "7 (over 900s" in text and "Amit Ashok Kumar Patnaik" in text


async def test_the_admin_trigger_is_token_protected(monkeypatch) -> None:
    calls: list[Any] = []

    async def ingest(deps: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "HYDRATED_FROM_DATABASE"}

    async def hydrate(deps: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("hydrate", kwargs))
        return {"fixtures_hydrated": 0}

    monkeypatch.setattr(omni_admin.live_ingest, "ingest", ingest)
    monkeypatch.setattr(omni_admin.live_ingest, "hydrate", hydrate)
    settings = get_settings()
    app = FastAPI()
    app.include_router(omni_admin.router, prefix="/api/v1")
    app.state.settings = settings
    app.state.fleet_deps = SimpleNamespace(settings=settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/v1/admin/omni/hydrate-live")).status_code in (401, 503)
        headers = {settings.omni_admin_header: settings.omni_admin_token.get_secret_value()}  # type: ignore[union-attr]
        res = await client.post("/api/v1/admin/omni/hydrate-live", params={"force_refresh": "true", "sports": "cricket"}, headers=headers)
        assert res.status_code == 200 and calls[-1] == {"sports": ["cricket"], "force": True, "with_context": True}
        res = await client.post("/api/v1/admin/omni/hydrate-live", params={"hydrate_only": "true"}, headers=headers)
        assert res.json()["status"] == "HYDRATED_FROM_DATABASE" and calls[-1][0] == "hydrate"


def test_the_context_refresh_is_scheduled() -> None:
    from app.core.celery_app import celery_app  # noqa: PLC0415

    entry = celery_app.conf.beat_schedule["live-context-refresh"]
    assert entry["task"] == "live.context_refresh" and entry["schedule"] == get_settings().LIVE_CONTEXT_REFRESH_SECONDS
    assert "app.workers.live_context_tasks" in celery_app.conf.include


# ================================================================ honest sources
def test_no_invented_probability_and_no_re_stamped_price() -> None:
    source = (ROOT / "backend" / "app" / "services" / "live_sports_ingestion_service.py").read_text(encoding="utf-8")
    assert not re.search(r"0\.33|0\.50|0\.5\b", source)  # a missing consensus is a missing price, never a coin flip
    assert "fetched_at=bought" in source and "datetime.now" not in source.split("async def hydrate", 1)[1].split("return report", 1)[0].replace("datetime.now(UTC))", "")
    assert omni_fleet.ENV_KEY_FALLBACK["odds_api"] == ("THE_ODDS_API_KEY", "ODDS_API_KEY")


def test_the_board_shows_stored_prices_and_context() -> None:
    lib = (FRONTEND / "lib" / "match_board.ts").read_text(encoding="utf-8")
    row = (FRONTEND / "components" / "oracle" / "board" / "MatchRow.tsx").read_text(encoding="utf-8")
    box = (FRONTEND / "components" / "oracle" / "board" / "OddsBox.tsx").read_text(encoding="utf-8")
    board = (FRONTEND / "components" / "oracle" / "board" / "MatchBoard.tsx").read_text(encoding="utf-8")
    chips = (FRONTEND / "components" / "oracle" / "board" / "FixtureContextChips.tsx").read_text(encoding="utf-8")
    assert 'priced_from: "live" | "stored"' in lib and "context: FixtureContext | null" in lib
    assert "<StoredPricesBadge" in row and "<FixtureContextChips" in row and 'stored={card.priced_from === "stored"}' in row
    assert "disabled={price === undefined || stored}" in box and 'card.priced_from !== "stored"' in board
    assert "material-symbols-outlined" in chips and "lucide-react" not in chips and "POSTPONED" in chips
