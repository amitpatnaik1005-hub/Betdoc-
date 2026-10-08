"""Universal Ingestion Matrix: JSONPath mappings, ProviderSpec validation, SpecMapper fidelity, config
providers running end to end, and Fleet Command's provider API (create, preview, delete)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.adapters.ingestion import OddsApiIngestor, PolymarketIngestor
from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.adapters.ingestion.factory import ProviderSpec, SpecMapper, UniversalDataIngestor
from app.adapters.ingestion.jsonpath import JsonPathError, compile_path, first, select
from app.api.deps import get_current_user, get_db
from app.api.v1 import omni_fleet as fleet_api
from app.api.v1 import ws as ws_api
from app.core.live_odds import publish_board_ticks
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import OmniFleetSource
from app.schemas.market import MarketTick
from app.services import omni_fleet
from app.services.omni_fleet import FleetDeps, run_source
from app.services.omni_normalizer import OmniNormalizer, default_alias_dictionary

from .conftest import KICKOFF, ProviderStub, odds_api_epl
from .test_resilience import PARTNER_SPEC


@pytest.fixture(autouse=True)
def _fresh_process_state() -> None:
    PolymarketIngestor._sports_cache = None
    OddsApiIngestor._last_quota = None
    omni_fleet._entities_synced = False
    omni_fleet._local_breakers.clear()
    omni_fleet._local_claims.clear()


def _batch(source_id: str, key: str, data) -> IngestionBatch:
    return IngestionBatch(source_id=source_id, payloads=[SourcePayload(key, data)], fetched_at=datetime.now(UTC), latency_ms=1, requests=1, retries=0)


def _spec(**changes) -> ProviderSpec:
    raw = copy.deepcopy(PARTNER_SPEC)
    for dotted, value in changes.items():
        node = raw
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return ProviderSpec.model_validate(raw)


# =============================================================== JSONPath
DOC = {"data": {"events": [{"id": 7, "teams": [{"side": "home", "name": "Arsenal"}, {"side": "away", "name": "Leeds"}], "m": {"1x2": {"1": 1.5}}}]}}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.data.events[*].id", [7]),
        ("data.events[0].id", [7]),
        ("$.data.events[-1].teams[1].name", ["Leeds"]),
        ("$.data.events[*].teams[?(@.side == 'HOME')].name", ["Arsenal"]),
        ("$.data.events[*].teams[?(@.side != 'home')].name", ["Leeds"]),
        ("$.data.events[0].m['1x2'].*", [1.5]),
        ("$.data.missing[*]", []),
    ],
)
def test_jsonpath_subset(path, expected) -> None:
    assert select(DOC, path) == expected


@pytest.mark.parametrize("bad", ["", "a..b", "x[?(@.a ~ 1)]", "x[foo]", "x[1"])
def test_jsonpath_rejects_unsupported_syntax(bad) -> None:
    with pytest.raises(JsonPathError):
        compile_path(bad)


def test_jsonpath_first_default() -> None:
    assert first(DOC, "$.nope", "fallback") == "fallback"


# =============================================================== spec validation
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"base_url": "http://plain.example"}, "https"),
        ({"requests": [{"path": "https://evil.example/x"}]}, "relative"),
        ({"coverage": {"Soccer EPL": "x"}}, "canonical"),
        ({"auth": {"type": "query"}}, "auth.param"),
        ({"mapping__events": "$..bad"}, "mapping.events"),
        ({"mapping__price": {"path": "@value"}}, "@value"),
        ({"surprise": True}, "Extra inputs"),
    ],
)
def test_spec_validation_explains_what_is_wrong(change, message) -> None:
    raw = copy.deepcopy(PARTNER_SPEC)
    for key, value in change.items():
        if key == "mapping__events":
            raw["mapping"]["events"] = value
        elif key == "mapping__price":
            raw["mapping"]["price"] = value
        else:
            raw[key] = value
    with pytest.raises(ValidationError, match=message):
        ProviderSpec.model_validate(raw)


def test_template_spec_is_valid() -> None:
    ProviderSpec.model_validate(fleet_api.TEMPLATE_SPEC)


# =============================================================== mapping fidelity
def test_config_mapping_reproduces_the_hand_written_adapter_exactly() -> None:
    """The Odds API's format described as config yields the very same ticks as its Python adapter."""
    normalizer = OmniNormalizer()
    builtin = normalizer.normalize(_batch("odds_api", "soccer_epl", odds_api_epl()))
    spec = _spec()
    config = normalizer.normalize(_batch("partner", "soccer_epl", odds_api_epl()), mapper=SpecMapper(spec, normalizer.aliases), devig_method=spec.devig)
    shape = lambda report: [(t.match_id, t.selection, t.odds, t.true_probability, t.home_team_id) for t in report.ticks]  # noqa: E731
    assert shape(config) == shape(builtin) and len(config.ticks) == 3


FLAT_1X2 = {
    "display_name": "Flat 1X2",
    "base_url": "https://flat.feed.example",
    "requests": [{"path": "/events/{sport}"}],
    "coverage": {"soccer_epl": "premier-league"},
    "mapping": {
        "events": "$.events[*]", "event_id": "id", "home": "home", "away": "away", "commence_time": "start", "commence_format": "epoch_ms",
        "markets": "markets.*", "market_key": "type", "market_values": ["1x2", "match_winner"],
        "outcomes": "prices", "outcome_name": "@key", "price": {"path": "@value", "format": "fractional"},
    },
}


def test_flat_feed_with_fractional_member_prices_and_nicknames() -> None:
    data = {"events": [
        {"id": "e1", "home": "Man Utd", "away": "Spurs", "start": int(KICKOFF.timestamp() * 1000),
         "markets": {"a": {"type": "totals", "prices": {"over": "5/6"}}, "b": {"type": "MATCH_WINNER", "prices": {"1": "6/5", "X": "12/5", "2": "9/4"}}}},
        {"id": "broken", "home": "A"},  # missing fields: isolated, the rest of the batch lands
    ]}
    spec = ProviderSpec.model_validate(FLAT_1X2)
    report = OmniNormalizer().normalize(_batch("flat", "soccer_epl", data), mapper=SpecMapper(spec, default_alias_dictionary()))
    assert report.events_seen == 2 and report.events_normalized == 1 and report.malformed == 1 and not report.unmapped
    ticks = {t.selection: t for t in report.ticks}
    assert ticks["HOME"].home_team == "Manchester United" and ticks["HOME"].away_team == "Tottenham Hotspur"
    assert float(ticks["HOME"].odds) == pytest.approx(2.2) and float(ticks["DRAW"].odds) == pytest.approx(3.4)
    assert report.devig_methods == {"shin": 1}


AMERICAN = {
    "display_name": "US feed",
    "base_url": "https://us.feed.example",
    "requests": [{"path": "/games"}],
    "coverage": {"americanfootball_nfl": "nfl"},
    "mapping": {
        "events": "$.games[*]", "event_id": "gameId",
        "home": "competitors[?(@.homeAway == 'home')].team", "away": "competitors[?(@.homeAway == 'away')].team",
        "commence_time": "kickoff", "markets": "lines[*]", "market_key": "kind", "market_values": ["moneyline"],
        "outcomes": "sides[*]", "outcome_name": "team", "price": {"path": "american", "format": "american"},
        "selections": {"home": ["{home}"], "draw": [], "away": ["{away}"]},
    },
}


def test_american_odds_and_filtered_participants() -> None:
    data = {"games": [{
        "gameId": 99, "kickoff": KICKOFF.isoformat(),
        "competitors": [{"homeAway": "away", "team": "Buccaneers"}, {"homeAway": "home", "team": "Dallas Cowboys"}],
        "lines": [{"kind": "spread", "sides": []}, {"kind": "moneyline", "sides": [{"team": "Dallas Cowboys", "american": -400}, {"team": "Buccaneers", "american": 320}]}],
    }]}
    spec = ProviderSpec.model_validate(AMERICAN)
    report = OmniNormalizer().normalize(_batch("us", "americanfootball_nfl", data), mapper=SpecMapper(spec, default_alias_dictionary()))
    ticks = {t.selection: t for t in report.ticks}
    assert set(ticks) == {"HOME", "AWAY"} and ticks["HOME"].home_team == "Dallas Cowboys"
    assert float(ticks["HOME"].odds) == pytest.approx(1.25) and float(ticks["AWAY"].odds) == pytest.approx(4.2)
    assert 0.75 < float(ticks["HOME"].true_probability) < 0.8


# =============================================================== config providers at runtime
@pytest.fixture
def make_deps(redis, session_factory, http, fleet_settings):
    async def no_wait(_: float) -> None:
        return None

    def build(**overrides) -> FleetDeps:
        return FleetDeps(redis=redis, session_factory=session_factory, http=http, vault=VaultCrypto(Fernet.generate_key().decode()),
                         settings=fleet_settings(**overrides), sleep=no_wait)

    return build


async def _add(session_factory, source_id: str, spec: dict, enabled: bool = True) -> None:
    async with session_factory() as session:
        session.add(OmniFleetSource(source_id=source_id, spec=spec, is_enabled=enabled, consecutive_failures=0))
        await session.commit()


async def test_config_provider_runs_end_to_end_with_its_env_key(make_deps, session_factory, stub: ProviderStub, monkeypatch) -> None:
    spec = copy.deepcopy(PARTNER_SPEC)
    spec["auth"] = {"type": "header", "param": "X-Partner-Key"}  # secret_env defaults to OMNI_PARTNER_API_KEY
    monkeypatch.setenv("OMNI_PARTNER_API_KEY", "partner-secret-123")
    await _add(session_factory, "partner", spec)
    summary = await run_source("partner", make_deps(omni_allow_private_networks=True), force=True)
    assert summary.status == "ok" and summary.ticks == 3
    assert stub.requests[-1].headers["X-Partner-Key"] == "partner-secret-123"
    assert stub.requests[-1].url.path == "/v1/sports/soccer_epl/odds"


async def test_config_provider_without_its_key_waits(make_deps, session_factory) -> None:
    spec = copy.deepcopy(PARTNER_SPEC)
    spec["auth"] = {"type": "bearer", "secret_env": "OMNI_NOT_SET_ANYWHERE_KEY"}
    await _add(session_factory, "partner", spec)
    assert (await run_source("partner", make_deps(omni_allow_private_networks=True), force=True)).reason == "needs_key"


async def test_format_change_is_schema_drift_and_trips_the_breaker(make_deps, session_factory, redis, stub: ProviderStub) -> None:
    await _add(session_factory, "partner", PARTNER_SPEC)
    events = odds_api_epl()
    for event in events:
        event.pop("home_team")  # the provider renamed a field
    stub.responses["/sports/soccer_epl/odds"] = events
    deps = make_deps(omni_allow_private_networks=True)
    summary = await run_source("partner", deps, force=True)
    assert summary.status == "failed" and "format may have changed" in (summary.reason or "")
    assert await redis.exists(omni_fleet.fleet_keys(deps.settings).breaker_open("partner"))


async def test_private_targets_are_refused(make_deps, session_factory, stub: ProviderStub) -> None:
    spec = copy.deepcopy(PARTNER_SPEC)
    spec["base_url"] = "https://127.0.0.1"
    await _add(session_factory, "sneaky", spec)
    summary = await run_source("sneaky", make_deps(omni_allow_private_networks=False), force=True)
    assert summary.status == "failed" and "unsafe target" in (summary.reason or "")
    assert stub.requests == []


async def test_universal_ingestor_reads_quota_headers(http, fleet_settings, stub: ProviderStub) -> None:
    spec = _spec(quota={"remaining_header": "x-requests-remaining", "used_header": None, "limit": 600})
    batch = await UniversalDataIngestor("partner", spec, http, fleet_settings(omni_allow_private_networks=True)).fetch()
    assert batch.quota_remaining == 480 and batch.quota_fraction == pytest.approx(0.8)


# =============================================================== Fleet Command provider API
@dataclass
class FakeUser:
    username: str
    role: str
    is_active: bool = True


@pytest_asyncio.fixture
async def client(redis, session_factory, http, fleet_settings):
    settings = fleet_settings(ODDS_API_KEY=None, omni_allow_private_networks=True)
    app = FastAPI()
    app.include_router(fleet_api.router, prefix="/api/v1")
    app.include_router(ws_api.router, prefix="/api/v1/ws")
    vault = VaultCrypto(Fernet.generate_key().decode())
    app.state.settings, app.state.redis, app.state.vault = settings, redis, vault
    app.state.fleet_deps = FleetDeps(redis=redis, session_factory=session_factory, http=http, vault=vault, settings=settings)

    async def db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: FakeUser("ops", "ADMIN")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_preview_maps_a_pasted_sample_without_network(client, stub: ProviderStub) -> None:
    body = {"spec": FLAT_1X2, "sport": "soccer_epl", "sample": {"events": [
        {"id": "e1", "home": "Arsenal FC", "away": "Leeds", "start": 1791900000000, "markets": {"m": {"type": "1x2", "prices": {"1": "2/5", "X": "4/1", "2": "7/1"}}}}
    ]}}
    preview = (await client.post("/api/v1/omni/fleet/providers/preview", json=body)).json()
    assert preview["events_normalized"] == 1 and len(preview["ticks"]) == 3
    assert all(t["home_canonical"] and t["away_canonical"] for t in preview["ticks"])
    assert stub.requests == []


async def test_preview_rejects_a_sport_outside_coverage(client) -> None:
    response = await client.post("/api/v1/omni/fleet/providers/preview", json={"spec": FLAT_1X2, "sport": "basketball_nba", "sample": {}})
    assert response.status_code == 422


async def test_create_list_and_delete_a_config_provider(client) -> None:
    created = await client.post("/api/v1/omni/fleet/providers", json={"source_id": "flat_feed", "spec": FLAT_1X2})
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "config" and body["is_enabled"] is False and body["status"] == "DISABLED"
    assert body["secret_env"] == "OMNI_FLAT_FEED_API_KEY" and body["spec"]["display_name"] == "Flat 1X2"

    overview = (await client.get("/api/v1/omni/fleet")).json()
    ids = [s["source_id"] for s in overview["sources"]]
    assert ids[:2] == ["odds_api", "polymarket"] and "flat_feed" in ids
    assert {"group": "soccer_epl"}.items() <= next(g for g in overview["groups"] if g["group"] == "soccer_epl").items()

    assert (await client.post("/api/v1/omni/fleet/providers", json={"source_id": "flat_feed", "spec": FLAT_1X2})).status_code == 409
    assert (await client.post("/api/v1/omni/fleet/providers", json={"source_id": "odds_api", "spec": FLAT_1X2})).status_code == 409
    assert (await client.delete("/api/v1/omni/fleet/providers/flat_feed")).status_code == 204
    assert (await client.delete("/api/v1/omni/fleet/providers/odds_api")).status_code == 404  # built-ins: disable only


async def test_overview_shows_failover_roles(client, session_factory, redis) -> None:
    await _add(session_factory, "partner", PARTNER_SPEC)
    sources = {s["source_id"]: s for s in (await client.get("/api/v1/omni/fleet")).json()["sources"]}
    assert sources["odds_api"]["role"] == "unavailable" and sources["odds_api"]["availability"] == "needs_key"
    assert sources["partner"]["role"] == "failover" and sources["partner"]["covering"][0]["replacing"] == "odds_api"
    assert sources["polymarket"]["role"] == "always_on"


async def test_live_odds_snapshot_endpoint_serves_the_board(client, redis) -> None:
    tick = MarketTick(match_id="m", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=2.0, true_probability=0.5, is_suspended=False)
    await publish_board_ticks(redis, [tick])
    response = await client.get("/api/v1/ws/live-odds/snapshot")
    assert response.status_code == 200 and response.json()[0]["matchId"] == "m"
    assert response.headers["cache-control"] == "no-store"
