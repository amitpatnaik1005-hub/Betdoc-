"""Group 127: the live feed keeps flowing and grows only where the user chooses. Developed for Amit Ashok Kumar Patnaik.

A key The Odds API rejects (401) no longer silences the fleet while another configured key works: the run moves to
the next key at once (a rejected request costs nothing), records which key is in use, and pauses only when every key
is refused. The Vault lists the sports in season from the provider's free index, with what each costs a day, and
switches one on or off; nothing outside the board's sports or out of season is switched on. A board page that finds
the board thin starts the fleet's own guarded refresh, never forced and at most once per cooldown; a paused source
stays paused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from app.api.deps import get_current_admin, get_db
from app.api.v1 import vault_admin
from app.core.config import get_settings
from app.models.omni_vault import OmniFleetSource, VaultBookmakerAccount, VaultFleetConfig
from app.services import live_sports_ingestion_service as live
from app.services import omni_fleet
from app.services.omni_fleet import candidate_api_keys, run_source
from tests.omni_fleet.conftest import ProviderStub, odds_api_epl
from tests.omni_fleet.test_fleet_runtime import _fresh_process_state, make_deps  # noqa: F401 - fixtures

pytestmark = pytest.mark.usefixtures("_fresh_process_state")

VAULT_KEY = "revoked" * 4
ENV_KEY = "working" * 4
DESCRIPTOR = SimpleNamespace(source_id="odds_api", spec=None)
SPORTS_INDEX = [
    {"key": "soccer_epl", "group": "Soccer", "title": "EPL", "active": True, "has_outrights": False},
    {"key": "basketball_nba", "group": "Basketball", "title": "NBA", "active": True, "has_outrights": False},
    {"key": "tennis_atp_paris_masters", "group": "Tennis", "title": "ATP Paris Masters", "active": True, "has_outrights": False},
    {"key": "basketball_nba_championship_winner", "group": "Basketball", "title": "NBA Championship Winner", "active": True, "has_outrights": True},
    {"key": "baseball_mlb", "group": "Baseball", "title": "MLB", "active": False, "has_outrights": False},
    {"key": "aussierules_afl", "group": "Aussie Rules", "title": "AFL", "active": True, "has_outrights": False},
]


class KeyedStub(ProviderStub):
    """The Odds API as it answers a revoked key (401, nothing charged) and a working one."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("apiKey") == VAULT_KEY:
            self.requests.append(request)
            return httpx.Response(401, json={"message": "API key is not valid or out of usage credits"})
        return super().__call__(request)


@pytest.fixture
def stub() -> KeyedStub:  # overrides the fleet suite's stub: the keyed one, with The Odds API's own sport index
    provider = KeyedStub()
    provider.responses = {"/sports/soccer_epl/odds": odds_api_epl(), "/sports": SPORTS_INDEX}
    provider.headers = {"x-requests-remaining": "404"}
    return provider


async def _source(session_factory, deps, **fields: Any) -> None:
    async with session_factory() as session:
        session.add(OmniFleetSource(source_id="odds_api", is_enabled=True, consecutive_failures=0, encrypted_api_key=deps.vault.encrypt_key(VAULT_KEY), **fields))
        await session.commit()


# ================================================================ the key fallback
def test_the_keys_in_precedence_order_each_once() -> None:
    settings = get_settings().model_copy(update={"THE_ODDS_API_KEY": SecretStr(ENV_KEY),
                                                 "ODDS_API_KEY": SecretStr(ENV_KEY)})
    vault = SimpleNamespace(decrypt_key=lambda _: VAULT_KEY)
    row = SimpleNamespace(encrypted_api_key="ciphertext")
    assert candidate_api_keys(row, DESCRIPTOR, settings, vault) == [("fleet_command_vault", VAULT_KEY), ("THE_ODDS_API_KEY", ENV_KEY)]  # type: ignore[arg-type]
    assert candidate_api_keys(None, DESCRIPTOR, settings, None) == [("THE_ODDS_API_KEY", ENV_KEY)]  # type: ignore[arg-type]


async def test_a_rejected_vault_key_falls_through_to_the_working_environment_key(make_deps, session_factory, redis, stub: KeyedStub) -> None:
    deps = make_deps(THE_ODDS_API_KEY=SecretStr(ENV_KEY), ODDS_API_KEY=None)
    await _source(session_factory, deps)
    summary = await run_source("odds_api", deps, force=True)
    assert summary.status == "ok" and summary.ticks == 3
    keys_tried = [r.url.params["apiKey"] for r in stub.requests if r.url.path.endswith("/odds")]
    assert keys_tried == [VAULT_KEY, ENV_KEY]  # the revoked key once, then the working one
    async with session_factory() as session:
        row = await session.get(OmniFleetSource, "odds_api")
        assert row.consecutive_failures == 0 and row.paused_at is None and "401" not in (row.last_error or "")
    metrics = await redis.hgetall(omni_fleet.fleet_keys(deps.settings).fleet_metrics("odds_api"))
    assert metrics["key_in_use"] == "THE_ODDS_API_KEY" and metrics["rejected_keys"] == "fleet_command_vault"
    status = await live.status(deps)
    assert status["source"]["key_in_use"] == "THE_ODDS_API_KEY" and status["source"]["rejected_keys"] == ["fleet_command_vault"]


async def test_when_every_key_is_rejected_the_run_fails_and_counts(make_deps, session_factory, stub: KeyedStub) -> None:
    deps = make_deps(THE_ODDS_API_KEY=None, ODDS_API_KEY=None)
    await _source(session_factory, deps)
    summary = await run_source("odds_api", deps, force=True)
    assert summary.status == "failed" and "401" in (summary.reason or "") and VAULT_KEY not in (summary.reason or "")
    async with session_factory() as session:
        assert (await session.get(OmniFleetSource, "odds_api")).consecutive_failures == 1


# ================================================================ the sport catalogue and the Vault's switch
async def test_the_catalogue_lists_the_boards_sports_in_season_with_their_cost(make_deps, session_factory, stub: KeyedStub) -> None:
    deps = make_deps(THE_ODDS_API_KEY=SecretStr(ENV_KEY), ODDS_API_KEY=None, ODDS_SPORT_KEYS="soccer_epl")
    await _source(session_factory, deps)
    catalog = await live.sport_catalog(deps, vault_sports=["basketball_nba"], now=datetime.now(UTC))
    rows = {s["key"]: s for s in catalog["sports"]}
    assert set(rows) == {"soccer_epl", "basketball_nba", "tennis_atp_paris_masters"}  # outrights, out of season, AFL (not on the board): left out
    assert rows["soccer_epl"]["source"] == "environment" and rows["basketball_nba"]["source"] == "vault" and rows["tennis_atp_paris_masters"]["polling"] is False
    assert rows["tennis_atp_paris_masters"]["suggested"] and rows["tennis_atp_paris_masters"]["category"] == "Tennis" and not rows["soccer_epl"]["suggested"]
    cost = rows["basketball_nba"]["credits_per_day"]
    assert 0 < cost["far"] < cost["near"]  # cheapest days out, dearest near kickoff
    sports_calls = [r for r in stub.requests if r.url.path.endswith("/sports")]
    assert [r.url.params["apiKey"] for r in sports_calls] == [VAULT_KEY, ENV_KEY]  # the index is read with the first key accepted
    await live.sport_catalog(deps, vault_sports=[], now=datetime.now(UTC))
    assert len([r for r in stub.requests if r.url.path.endswith("/sports")]) == 2  # then served from Redis


async def test_the_vault_switches_a_sport_on_and_off(make_deps, session_factory, stub: KeyedStub, engine) -> None:  # noqa: ARG001
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: VaultFleetConfig.__table__.create(sync, checkfirst=True))
        await conn.run_sync(lambda sync: VaultBookmakerAccount.__table__.create(sync, checkfirst=True))
    deps = make_deps(THE_ODDS_API_KEY=SecretStr(ENV_KEY), ODDS_API_KEY=None, ODDS_SPORT_KEYS="soccer_epl")
    await _source(session_factory, deps)
    admin = SimpleNamespace(id=None, username="admin", role="ADMIN")
    app = FastAPI()
    app.include_router(vault_admin.router, prefix="/api/v1")
    app.state.redis, app.state.fleet_deps = deps.redis, deps

    async def db() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_admin] = lambda: admin
    app.dependency_overrides[vault_admin.get_settings] = lambda: deps.settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        on = await client.put("/api/v1/vault/sports/basketball_nba", json={"polling": True})
        assert on.status_code == 200 and on.json()["changed"] and on.json()["config"]["sports"] == ["basketball_nba"]
        again = await client.put("/api/v1/vault/sports/basketball_nba", json={"polling": True})
        assert again.json()["changed"] is False
        assert (await client.put("/api/v1/vault/sports/baseball_mlb", json={"polling": True})).status_code == 422  # out of season
        assert (await client.put("/api/v1/vault/sports/aussierules_afl", json={"polling": True})).status_code == 422  # not a board sport
        assert (await client.put("/api/v1/vault/sports/soccer_epl", json={"polling": False})).status_code == 409  # the environment's
        catalog = (await client.get("/api/v1/vault/sports/catalog")).json()
        assert next(s for s in catalog["sports"] if s["key"] == "basketball_nba")["polling"] is True
        off = await client.put("/api/v1/vault/sports/basketball_nba", json={"polling": False})
        assert off.json()["config"]["sports"] == []
    async with session_factory() as session:
        assert (await session.execute(select(VaultFleetConfig))).scalar_one().sports == []


# ================================================================ the board-load nudge
async def test_a_thin_board_nudges_the_fleet_once_and_a_paused_source_stays_paused(make_deps, session_factory, stub: KeyedStub) -> None:
    deps = make_deps(THE_ODDS_API_KEY=SecretStr(ENV_KEY), ODDS_API_KEY=None, LIVE_ODDS_NUDGE_MIN_FIXTURES=5)
    await _source(session_factory, deps, paused_at=datetime.now(UTC))
    assert await live.nudge(deps, fixtures_on_board=12) == "board_full"
    assert await live.nudge(deps, fixtures_on_board=0) == "SKIPPED"  # paused in Fleet Command: said, nothing bought
    assert await live.nudge(deps, fixtures_on_board=0) == "cooling_down"
    assert not [r for r in stub.requests if r.url.path.endswith("/odds")]
    assert await live.nudge(None, fixtures_on_board=0) == "disabled"


async def test_a_thin_board_buys_only_what_the_cadence_says_is_due(make_deps, session_factory, stub: KeyedStub) -> None:
    deps = make_deps(THE_ODDS_API_KEY=SecretStr(ENV_KEY), ODDS_API_KEY=None)
    await _source(session_factory, deps)
    outcome = await live.nudge(deps, fixtures_on_board=0)
    assert outcome == "BOUGHT"  # nothing stored yet: the sport is due
    odds_calls = [r for r in stub.requests if r.url.path.endswith("/odds")]
    assert odds_calls and odds_calls[-1].url.params["apiKey"] == ENV_KEY
    await deps.redis.delete(live._nudge_key(deps.settings))  # noqa: SLF001 - past the cooldown
    assert await live.nudge(deps, fixtures_on_board=0) == "UP_TO_DATE"  # the board just bought is within its tier: no credit spent
    assert len([r for r in stub.requests if r.url.path.endswith("/odds")]) == len(odds_calls)
