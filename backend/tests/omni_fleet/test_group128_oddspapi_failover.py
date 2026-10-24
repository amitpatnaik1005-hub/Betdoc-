"""Group 128: OddsPapi, The Odds API's failover for 1xBet and Pinnacle. Developed for Amit Ashok Kumar Patnaik.

The adapter reads OddsPapi as it answers (shapes copied from live responses on 2026-10-24: 1xBet's open pre-match 1X2
arrives ``marketActive: false`` with every outcome active; every Asian handicap line is its own market) and hands the
fleet The Odds API's event shape, so the normaliser de-vigs it unchanged: the full-time 1X2, the totals lines, one
handicap line per book (its main one) and BTTS; half-time markets, closed outcomes and fixtures whose teams it cannot
name are left out. A run costs one request per bookmaker per five tournaments and reads the month's budget from the
free account call. The router keeps OddsPapi on standby while The Odds API is available and hands it The Odds API's
groups when that one is not. The key is read under the name the .env file uses.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.ingestion import INGESTORS
from app.adapters.ingestion.oddspapi_adapter import OddsPapiIngestor, main_handicap, parse_markets, to_event
from app.core.config import Settings, get_settings
from app.services import omni_fleet
from app.services.omni_normalizer import OmniNormalizer, default_alias_dictionary
from app.services.omni_router import BUILTIN_PRIORITY, builtin_descriptors, compute_plan
from app.services.omni_fleet import ENV_KEY_FALLBACK, run_source
from tests.omni_fleet.conftest import ProviderStub
from tests.omni_fleet.test_fleet_runtime import _fresh_process_state, make_deps  # noqa: F401 - fixtures

KICKOFF = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
STAMP = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
PARTICIPANTS = {"38": "Chelsea FC", "33": "Tottenham Hotspur", "2817": "Real Madrid"}
MARKETS = [
    {"marketId": 101, "marketType": "1x2", "period": "fulltime", "playerProp": False, "handicap": 0.0,
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 103, "outcomeName": "2"}, {"outcomeId": 102, "outcomeName": "X"}]},
    {"marketId": 1010, "marketType": "totals", "period": "fulltime", "playerProp": False, "handicap": 2.5,
     "outcomes": [{"outcomeId": 1011, "outcomeName": "Under"}, {"outcomeId": 1010, "outcomeName": "Over"}]},
    {"marketId": 1068, "marketType": "spreads", "period": "fulltime", "playerProp": False, "handicap": -0.5,
     "outcomes": [{"outcomeId": 1068, "outcomeName": "1"}, {"outcomeId": 1069, "outcomeName": "2"}]},
    {"marketId": 1070, "marketType": "spreads", "period": "fulltime", "playerProp": False, "handicap": -0.25,
     "outcomes": [{"outcomeId": 1070, "outcomeName": "1"}, {"outcomeId": 1071, "outcomeName": "2"}]},
    {"marketId": 104, "marketType": "bothteamsscore", "period": "fulltime", "playerProp": False, "handicap": 0.0,
     "outcomes": [{"outcomeId": 104, "outcomeName": "Yes"}, {"outcomeId": 105, "outcomeName": "No"}]},
    {"marketId": 10676, "marketType": "1x2", "period": "1sthalf", "playerProp": False, "handicap": 0.0,
     "outcomes": [{"outcomeId": 10677, "outcomeName": "1"}]},
    {"marketId": 10797, "marketType": "totals-corners", "period": "fulltime", "playerProp": False, "handicap": 8.0,
     "outcomes": [{"outcomeId": 10798, "outcomeName": "Under"}]},
]


def _outcome(price: float, *, active: bool = True, main: bool = False) -> dict[str, Any]:
    return {"players": {"0": {"active": active, "price": price, "changedAt": STAMP, "mainLine": main, "playerName": None}}}


def _row(fixture: str, tournament: int, home: int, away: int, book: str, markets: dict[str, Any]) -> dict[str, Any]:
    return {"fixtureId": fixture, "participant1Id": home, "participant2Id": away, "sportId": 10, "tournamentId": tournament, "statusId": 0, "hasOdds": True,
            "startTime": KICKOFF.isoformat().replace("+00:00", "Z"),
            "bookmakerOdds": {book: {"bookmakerIsActive": True, "suspended": False, "markets": copy.deepcopy(markets)}}}


ONEXBET = {
    "101": {"marketActive": False, "outcomes": {"103": _outcome(4.11), "102": _outcome(3.98), "101": _outcome(1.919)}},  # open, despite marketActive
    "1010": {"marketActive": True, "outcomes": {"1010": _outcome(1.85), "1011": _outcome(1.97)}},
    "1068": {"marketActive": True, "outcomes": {"1068": _outcome(1.93, main=True), "1069": _outcome(1.95, main=True)}},
    "1070": {"marketActive": True, "outcomes": {"1070": _outcome(1.70), "1071": _outcome(2.20)}},
    "104": {"marketActive": True, "outcomes": {"104": _outcome(1.72), "105": _outcome(2.05, active=False)}},  # one side closed: no BTTS
    "10676": {"marketActive": True, "outcomes": {"10677": _outcome(2.6)}},  # half time: not Ashoka's
}
PINNACLE = {
    "101": {"marketActive": True, "outcomes": {"101": _outcome(1.925), "102": _outcome(3.90), "103": _outcome(3.94)}},
    "1070": {"marketActive": True, "outcomes": {"1070": _outcome(1.71, main=True), "1071": _outcome(2.21, main=True)}},
    "104": {"marketActive": True, "outcomes": {"104": _outcome(1.74), "105": _outcome(2.08)}},
}


@pytest.fixture(autouse=True)
def _clean_catalogues() -> None:
    OddsPapiIngestor._participants.clear()  # noqa: SLF001
    OddsPapiIngestor._markets.clear()  # noqa: SLF001


def _merged() -> dict[str, Any]:
    row = _row("id1", 17, 38, 33, "1xbet", ONEXBET)
    row["bookmakerOdds"]["pinnacle"] = _row("id1", 17, 38, 33, "pinnacle", PINNACLE)["bookmakerOdds"]["pinnacle"]
    return row


# ================================================================ translation (pure)
def test_a_fixture_becomes_the_odds_apis_event() -> None:
    catalogue = parse_markets(MARKETS)
    assert set(catalogue) == {"101", "1010", "1068", "1070", "104"}  # half time and corners are not Ashoka's markets
    event = to_event(_merged(), "soccer_epl", PARTICIPANTS, catalogue)
    assert event is not None and (event["home_team"], event["away_team"], event["sport_key"]) == ("Chelsea FC", "Tottenham Hotspur", "soccer_epl")
    books = {b["key"]: {m["key"]: m["outcomes"] for m in b["markets"]} for b in event["bookmakers"]}
    assert {o["name"]: o["price"] for o in books["1xbet"]["h2h"]} == {"Chelsea FC": 1.919, "Draw": 3.98, "Tottenham Hotspur": 4.11}
    assert books["1xbet"]["spreads"] == [{"name": "Chelsea FC", "price": 1.93, "point": -0.5}, {"name": "Tottenham Hotspur", "price": 1.95, "point": 0.5}]
    assert books["pinnacle"]["spreads"][0]["point"] == -0.25  # each book's own main line
    assert "btts" not in books["1xbet"] and {o["name"] for o in books["pinnacle"]["btts"]} == {"Yes", "No"}  # one side closed: no market
    partial = _merged()
    partial["bookmakerOdds"]["1xbet"]["markets"]["101"]["outcomes"]["102"] = _outcome(3.98, active=False)
    assert "h2h" not in {m["key"] for b in to_event(partial, "soccer_epl", PARTICIPANTS, parse_markets(MARKETS))["bookmakers"] if b["key"] == "1xbet" for m in b["markets"]}
    assert {(o["name"], o["point"]) for o in books["1xbet"]["totals"]} == {("Over", 2.5), ("Under", 2.5)}
    assert to_event(_row("id2", 8, 2817, 999, "1xbet", ONEXBET), "soccer_spain_la_liga", PARTICIPANTS, catalogue) is None  # a team it cannot name


def test_the_main_handicap_line() -> None:
    assert main_handicap({-0.5: {"1": (1.9, False), "2": (1.9, False)}, -0.25: {"1": (1.7, True), "2": (2.2, True)}}) == -0.25  # the book's flag
    assert main_handicap({-1.0: {"1": (2.6, False), "2": (1.5, False)}, -0.5: {"1": (1.95, False), "2": (1.93, False)}}) == -0.5  # else the most balanced
    assert main_handicap({-0.5: {"1": (1.9, False)}}) is None  # one side priced is no line


def test_the_normaliser_de_vigs_it_like_the_odds_api() -> None:
    from app.adapters.ingestion.base import IngestionBatch, SourcePayload  # noqa: PLC0415

    event = to_event(_merged(), "soccer_epl", PARTICIPANTS, parse_markets(MARKETS))
    batch = IngestionBatch(source_id="oddspapi", payloads=[SourcePayload(key="soccer_epl", data=[event])], fetched_at=datetime.now(UTC), latency_ms=0, requests=0, retries=0)
    report = OmniNormalizer(default_alias_dictionary()).normalize(batch, devig_method="multiplicative")
    assert report.events_normalized == 1 and report.malformed == 0 and len(report.ticks) == 3
    quotes = {q.market_type: {b.bookmaker_id for b in q.books} for q in report.quotes}
    assert quotes["Totals 2.5"] == {"1xbet"} and quotes["BTTS"] == {"pinnacle"}
    assert quotes["Asian Handicap -0.5"] == {"1xbet"} and quotes["Asian Handicap -0.25"] == {"pinnacle"}
    aliases = default_alias_dictionary()
    assert aliases.resolve("soccer_epl", "Chelsea FC").id == aliases.resolve("soccer_epl", "Chelsea").id  # pairs with The Odds API's fixture


# ================================================================ registration, key, router
def test_oddspapi_is_registered_behind_the_odds_api() -> None:
    assert INGESTORS["oddspapi"] is OddsPapiIngestor and BUILTIN_PRIORITY["odds_api"] < BUILTIN_PRIORITY["oddspapi"]
    assert ENV_KEY_FALLBACK["oddspapi"] == ("ODDSPAPI_API_KEY",)
    s = get_settings().model_copy(update={"ODDS_SPORT_KEYS": "soccer_epl,cricket_odi,soccer_spain_la_liga"})
    assert OddsPapiIngestor.coverage(s) == {"soccer_epl": "17", "soccer_spain_la_liga": "8"}  # only polled sports it has a tournament for


def test_the_key_is_read_under_the_env_files_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDSPAPI_IO_API_KEY", "from-the-env-file")
    monkeypatch.delenv("ODDSPAPI_API_KEY", raising=False)
    assert Settings().ODDSPAPI_API_KEY.get_secret_value() == "from-the-env-file"  # type: ignore[union-attr]


def test_the_router_keeps_oddspapi_on_standby_until_the_odds_api_is_down() -> None:
    s = get_settings().model_copy(update={"ODDS_SPORT_KEYS": "soccer_epl,soccer_spain_la_liga,cricket_odi"})
    registry = {k: v for k, v in builtin_descriptors(s, default_alias_dictionary(), {}).items() if k in ("odds_api", "oddspapi")}
    plan, _ = compute_plan(registry, {"odds_api": "available", "oddspapi": "available"})
    assert plan["odds_api"].role == "primary" and plan["oddspapi"].role == "standby" and plan["oddspapi"].native_scope == []  # spends nothing
    plan, groups = compute_plan(registry, {"odds_api": "quota_reserve", "oddspapi": "available"})
    assert plan["oddspapi"].role == "failover" and sorted(plan["oddspapi"].native_scope) == ["17", "8"]
    assert groups["soccer_epl"].failover and groups["cricket_odi"].uncovered  # cricket has no OddsPapi tournament: said, not guessed


# ================================================================ a fleet run
class OddsPapiStub(ProviderStub):
    def __init__(self) -> None:
        super().__init__()
        self.count = 41

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path.endswith("/account"):
            return httpx.Response(200, json={"subscriptions": [{"is_active": True, "request_limit": 250, "request_count": self.count, "plan": "free"}]})
        self.count += 1
        if path.endswith("/participants"):
            return httpx.Response(200, json=PARTICIPANTS)
        if path.endswith("/markets"):
            return httpx.Response(200, json=MARKETS)
        if path.endswith("/odds-by-tournaments"):
            book = params["bookmaker"]
            rows = [_row("id1", 17, 38, 33, book, ONEXBET if book == "1xbet" else PINNACLE)] if "17" in params["tournamentIds"].split(",") else []
            return httpx.Response(200, json=rows)
        return httpx.Response(404, json={"message": f"no stub for {path}"})


@pytest.fixture
def stub() -> OddsPapiStub:  # the fleet suite's http fixture serves this one
    return OddsPapiStub()


async def test_a_fleet_run_prices_the_board_and_reads_the_budget(make_deps, redis, stub: OddsPapiStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OddsPapiIngestor, "burst", 10)  # the suite's sleep is a no-op, so tokens never refill between a run's calls
    deps = make_deps(ODDSPAPI_API_KEY=SecretStr("papi-key"), ODDS_SPORT_KEYS="soccer_epl,soccer_spain_la_liga")
    summary = await run_source("oddspapi", deps, force=True)
    assert summary.status == "ok" and summary.ticks == 3, (summary.status, summary.reason)
    odds_calls = [r for r in stub.requests if r.url.path.endswith("/odds-by-tournaments")]
    assert sorted(r.url.params["bookmaker"] for r in odds_calls) == ["1xbet", "pinnacle"]  # one per book: both tournaments in one call
    assert all(r.url.params["apiKey"] == "papi-key" for r in stub.requests)
    metrics = await redis.hgetall(omni_fleet.fleet_keys(deps.settings).fleet_metrics("oddspapi"))
    assert float(metrics["quota_remaining"]) == 250 - stub.count and metrics["key_in_use"] == "ODDSPAPI_API_KEY"
    again = await run_source("oddspapi", deps, force=True)
    assert again.status == "ok" and len([r for r in stub.requests if r.url.path.endswith(("/participants", "/markets"))]) == 2  # catalogues read once
