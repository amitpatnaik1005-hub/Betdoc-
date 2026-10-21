"""Group 101 in the ingestion fleet: an Odds API run keeps its board per fixture, the beat buys a sport again only when
its nearest fixture's tier says so, and a forced refresh spends a credit only when the fleet itself would.

Real Redis on the isolated test index (skipped when unreachable), SQLite, The Odds API stubbed (``conftest.ProviderStub``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, update

from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.services import omni_fleet
from app.services import pre_match_odds_service as service
from app.services.omni_fleet import FleetDeps, fleet_tick, run_source

from .conftest import ProviderStub, odds_api_epl

MATCH = "e912304de2b2964b5aed6d8e6b2a4e5c"  # conftest.odds_api_epl: Arsenal v Leeds United, two days out


@pytest.fixture(autouse=True)
def _fresh_process_state() -> None:
    OddsApiIngestor._last_quota = None
    omni_fleet._entities_synced = False
    omni_fleet._local_breakers.clear()
    omni_fleet._local_claims.clear()


async def _no_wait(_: float) -> None:
    return None


@pytest.fixture
def make_deps(redis, session_factory, http, fleet_settings):
    def build(**overrides) -> FleetDeps:
        return FleetDeps(redis=redis, session_factory=session_factory, http=http, vault=VaultCrypto(Fernet.generate_key().decode()),
                         settings=fleet_settings(**overrides), sleep=_no_wait)

    return build


def _odds_calls(stub: ProviderStub) -> int:
    return sum(1 for r in stub.requests if r.url.path.endswith("/sports/soccer_epl/odds"))


async def _row(session_factory) -> PreMatchOddsSnapshotModel:
    async with session_factory() as session:
        return (await session.execute(select(PreMatchOddsSnapshotModel).where(PreMatchOddsSnapshotModel.match_id == MATCH))).scalar_one()


async def _age(session_factory, redis, deps, seconds: float, *, kickoff_in: timedelta | None = None) -> None:
    """Pretend the soccer_epl board was bought ``seconds`` ago (optionally moving the kickoff)."""
    polled = datetime.now(UTC) - timedelta(seconds=seconds)
    values: dict[str, object] = {"last_polled_at": polled}
    if kickoff_in is not None:
        values["commence_time"] = datetime.now(UTC) + kickoff_in
    async with session_factory() as session:
        await session.execute(update(PreMatchOddsSnapshotModel).values(**values))
        await session.commit()
    await redis.hset(f"{deps.settings.PRE_MATCH_REDIS_PREFIX}:sport_polled", mapping={"soccer_epl": polled.timestamp()})


async def _tick(deps, redis) -> list[tuple[str, list[str]]]:
    """One beat tick with odds_api due on the fleet's own interval; the Odds API ingests it dispatched."""
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    omni_fleet._local_claims.clear()
    await redis.delete(keys.fleet_claim("odds_api"))
    await redis.hset(keys.fleet_metrics("odds_api"), mapping={"quota_fraction": "0.9", "last_attempt_at": time.time() - 3600})
    calls: list[tuple[str, list[str], str]] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:
        calls.append((sid, scope, action))

    await fleet_tick(deps, record)
    return [(sid, scope) for sid, scope, action in calls if sid == "odds_api" and action == "ingest"]


async def test_a_fleet_run_keeps_the_board_per_fixture(make_deps, redis, session_factory) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    row = await _row(session_factory)
    assert (row.sport_key, row.home_team, row.away_team) == ("soccer_epl", "Arsenal", "Leeds United")
    assert row.bookmaker_odds["pinnacle"] == {"home": 1.45, "draw": 4.6, "away": 7.5}  # as quoted, never netted or filled
    assert set(row.markets_json) == {"h2h"} and set(row.markets_json["h2h"]["books"]) == {"pinnacle", "betfair_ex_uk", "williamhill"}
    consensus = row.consensus_fair_odds
    assert consensus["books"] == 3 and set(consensus["outcomes"]) == {"home", "draw", "away"}
    assert sum(o["probability"] for o in consensus["outcomes"].values()) == pytest.approx(1.0, abs=1e-5)
    assert consensus["sharp"]["book"] == "pinnacle"
    polled = await redis.hget(f"{deps.settings.PRE_MATCH_REDIS_PREFIX}:sport_polled", "soccer_epl")
    assert polled is not None and abs(float(polled) - row.last_polled_at.replace(tzinfo=UTC).timestamp()) < 1.0


async def test_the_beat_buys_a_sport_only_when_its_tier_says_so(make_deps, redis, session_factory) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert await _tick(deps, redis) == [("odds_api", ["soccer_epl"])]  # nothing stored yet: due
    assert (await run_source("odds_api", deps, force=True)).status == "ok"

    assert await _tick(deps, redis) == []  # two days out, just bought: tier 1 holds it for 6h
    await _age(session_factory, redis, deps, 5 * 3600)
    assert await _tick(deps, redis) == []
    await _age(session_factory, redis, deps, 6 * 3600 + 5)
    assert await _tick(deps, redis) == [("odds_api", ["soccer_epl"])]

    await _age(session_factory, redis, deps, 30 * 60, kickoff_in=timedelta(hours=10))  # tier 2: 60 min
    assert await _tick(deps, redis) == []
    await _age(session_factory, redis, deps, 61 * 60, kickoff_in=timedelta(hours=10))
    assert await _tick(deps, redis) == [("odds_api", ["soccer_epl"])]

    await _age(session_factory, redis, deps, 30, kickoff_in=timedelta(minutes=45))  # tier 3: the fleet's 60 s
    assert await _tick(deps, redis) == []
    await _age(session_factory, redis, deps, 65, kickoff_in=timedelta(minutes=45))
    assert await _tick(deps, redis) == [("odds_api", ["soccer_epl"])]


async def test_an_engaged_shield_stretches_every_tier(make_deps, redis, session_factory) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    await redis.hset(deps.settings.QUOTA_GUARD_SHIELD_MODE_KEY, mapping={"mode": "FORCE_CACHE_SHIELD"})
    stretch = deps.settings.QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER
    await _age(session_factory, redis, deps, 6 * 3600 + 5)  # due unshielded, not at 6h x the multiplier
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    await redis.hset(keys.fleet_metrics("odds_api"), mapping={"last_attempt_at": time.time() - 3600 * stretch})
    calls: list[str] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:  # noqa: ARG001
        calls.append(sid)

    await fleet_tick(deps, record)
    assert "odds_api" not in calls


async def test_tiering_off_restores_the_fleet_cadence(make_deps, redis) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32), PRE_MATCH_TIERED_POLLING_ENABLED=False)
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    assert await _tick(deps, redis) == [("odds_api", ["soccer_epl"])]


# ---------------------------------------------------------------- force refresh
async def test_force_refresh_inside_the_cooldown_spends_nothing(make_deps, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    calls = _odds_calls(stub)
    outcome = await service.force_refresh(deps, MATCH, datetime.now(UTC))
    assert outcome is not None and (outcome.status, outcome.source) == ("FRESH", "cache") and _odds_calls(stub) == calls
    assert await service.force_refresh(deps, "no-such-fixture", datetime.now(UTC)) is None


async def test_force_refresh_buys_the_sport_board(make_deps, redis, session_factory, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    await _age(session_factory, redis, deps, 300)
    before = (await _row(session_factory)).last_polled_at
    stub.responses["/sports/soccer_epl/odds"] = odds_api_epl(home=1.40)
    calls = _odds_calls(stub)
    outcome = await service.force_refresh(deps, MATCH, datetime.now(UTC))
    assert outcome is not None and (outcome.status, outcome.source) == ("SYNCED", "the_odds_api")
    assert _odds_calls(stub) == calls + 1
    row = await _row(session_factory)
    assert row.last_polled_at > before and row.bookmaker_odds["pinnacle"]["home"] == 1.40

    await _age(session_factory, redis, deps, 300)
    stub.responses["/sports/soccer_epl/odds"] = []  # the provider no longer lists it (finished or withdrawn)
    outcome = await service.force_refresh(deps, MATCH, datetime.now(UTC))
    assert outcome is not None and outcome.status == "NOT_OFFERED" and outcome.snapshot.bookmaker_odds["pinnacle"]["home"] == 1.40


@pytest.mark.parametrize("gate", ["shield", "reserve", "breaker"])
async def test_force_refresh_never_spends_what_the_fleet_would_not(make_deps, redis, session_factory, stub: ProviderStub, gate: str) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    await _age(session_factory, redis, deps, 300)
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    if gate == "shield":
        await redis.hset(deps.settings.QUOTA_GUARD_SHIELD_MODE_KEY, mapping={"mode": "FORCE_CACHE_SHIELD"})
    elif gate == "reserve":
        await redis.hset(keys.fleet_metrics("odds_api"), mapping={"quota_fraction": "0.01"})
    else:
        await redis.set(keys.breaker_open("odds_api"), "1", ex=120)
    calls = _odds_calls(stub)
    with pytest.raises(service.RefreshRefused):
        await service.force_refresh(deps, MATCH, datetime.now(UTC))
    assert _odds_calls(stub) == calls
