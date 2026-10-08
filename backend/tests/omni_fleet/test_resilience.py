"""Omni-resilience: Shin/multiplicative de-vig, IQR outlier rejection, circuit breakers, token-bucket
throttling, quota-aware failover routing, the Redis-outage spool and the shared beat tick."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from app.adapters.base_adapter import StandardizedEvent
from app.adapters.ingestion import OddsApiIngestor, PolymarketIngestor
from app.adapters.ingestion.base import ThrottledError
from app.core.live_odds import read_snapshot
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import OmniFleetSource, OmniQuarantineLog
from app.schemas.market import MarketTick
from app.services import omni_fleet
from app.services.omni_fleet import FleetDeps, fleet_tick, run_source
from app.services.omni_normalizer import (
    QuorumConsensusEngine,
    QuorumPolicy,
    devig,
    implied_probability,
    iqr_inliers,
    remove_vig,
    shin_devig,
)
from app.services.omni_quorum_buffer import buffer_events
from app.services.omni_router import SourceDescriptor, compute_plan
from app.services.omni_spool import SPOOL
from app.services.omni_throttle import RateLimit, TokenBucket
from app.workers.omni_quorum import resolve_quorums

from .conftest import ProviderStub


@pytest.fixture(autouse=True)
def _fresh_process_state() -> None:
    PolymarketIngestor._sports_cache = None
    OddsApiIngestor._last_quota = None
    omni_fleet._entities_synced = False
    omni_fleet._local_breakers.clear()
    omni_fleet._local_claims.clear()
    SPOOL._drain()


async def _no_wait(_: float) -> None:
    return None


@pytest.fixture
def make_deps(redis, session_factory, http, fleet_settings):
    def build(redis_client: Redis | None = None, local_sink=None, **overrides) -> FleetDeps:
        return FleetDeps(
            redis=redis_client or redis,
            session_factory=session_factory,
            http=http,
            vault=VaultCrypto(Fernet.generate_key().decode()),
            settings=fleet_settings(**overrides),
            sleep=_no_wait,
            local_sink=local_sink,
        )

    return build


# =============================================================== de-vig
def test_shin_matches_multiplicative_for_a_symmetric_book() -> None:
    implied = [implied_probability(1.9), implied_probability(1.9)]
    assert shin_devig(implied) == pytest.approx([0.5, 0.5])


def test_shin_takes_more_margin_from_the_longshot() -> None:
    implied = [implied_probability(p) for p in (1.40, 4.6, 8.0)]
    shin, mult = shin_devig(implied), remove_vig(implied)
    assert math.fsum(shin) == pytest.approx(1.0)
    assert shin[2] < mult[2] and shin[0] > mult[0]  # favourite-longshot bias corrected


@pytest.mark.parametrize("implied", [[0.5, 0.45], [0.48, 0.48], [0.9]])
def test_devig_degrades_to_multiplicative_instead_of_raising(implied) -> None:
    fair, method = devig(implied, "shin")
    assert method == "multiplicative" and math.fsum(fair) == pytest.approx(1.0)


def test_devig_honours_an_explicit_multiplicative_choice() -> None:
    assert devig([0.55, 0.5], "multiplicative")[1] == "multiplicative"


# =============================================================== IQR outlier rejection
def _event(provider: int, value: float, age: float = 0.0) -> StandardizedEvent:
    from uuid import UUID

    return StandardizedEvent(
        entity_id="m|Match Odds|HOME",
        event_type="market.true_probability",
        normalized_value=value,
        confidence_score=0.8,
        source_timestamp=datetime.now(UTC) - timedelta(seconds=age),
        provider_id=UUID(int=provider),
    )


def _engine(threshold: float = 0.05) -> QuorumConsensusEngine:
    return QuorumConsensusEngine(QuorumPolicy(variance_threshold=threshold, half_life_seconds=60, max_age_seconds=300, zero_tolerance=1e-9))


def test_iqr_flags_the_stale_fourth_provider() -> None:
    assert iqr_inliers([0.50, 0.51, 0.505, 0.80], 0.05) == [True, True, True, False]


def test_iqr_never_ejects_near_identical_values() -> None:
    assert all(iqr_inliers([0.5, 0.5, 0.5, 0.501], 0.05))


def test_iqr_needs_four_values() -> None:
    assert all(iqr_inliers([0.5, 0.51, 0.9], 0.05))


def test_consensus_survives_one_wild_provider() -> None:
    result = _engine().resolve_detailed("t", [_event(1, 0.50), _event(2, 0.51), _event(3, 0.505), _event(4, 0.80)])
    assert len(result.rejected) == 1 and result.rejected[0].normalized_value == 0.80
    assert 0.50 <= float(result.event.normalized_value) <= 0.51  # type: ignore[arg-type]


def test_without_rejection_the_same_set_would_quarantine() -> None:
    from app.services.omni_normalizer import QuorumVarianceException

    with pytest.raises(QuorumVarianceException):
        _engine().resolve_conflict("t", [_event(1, 0.50), _event(2, 0.51), _event(3, 0.80)])  # only 3: no IQR


async def test_scheduled_quorum_logs_rejected_outliers(redis, session_factory, fleet_settings) -> None:
    settings = fleet_settings()
    keys = OmniRedisKeys(settings.omni_redis_prefix)
    await buffer_events(redis, keys, [("q|Match Odds|HOME", _event(i, v)) for i, v in enumerate((0.50, 0.51, 0.505, 0.80), start=1)], 300)
    summary = await resolve_quorums(redis, session_factory, settings)
    assert summary["resolved"] == 1 and summary["outliers"] == 1 and summary["quarantined"] == 0
    async with session_factory() as session:
        row = (await session.execute(select(OmniQuarantineLog))).scalar_one()
        assert row.reason == "outlier_rejected" and len(row.events) == 1


# =============================================================== circuit breaker
async def test_failure_trips_the_breaker_and_the_scheduler_respects_it(make_deps, redis, stub: ProviderStub) -> None:
    deps = make_deps()
    stub.status_override = 503
    assert (await run_source("polymarket", deps, force=True)).status == "failed"
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    assert 0 < await redis.pttl(keys.breaker_open("polymarket")) <= 30_000  # base cooldown
    stub.status_override = None
    assert (await run_source("polymarket", deps)).reason == "circuit_open"  # no request spent
    assert (await run_source("polymarket", deps, force=True)).status == "ok"  # operator trial closes it
    assert not await redis.exists(keys.breaker_open("polymarket"), keys.breaker_half_open("polymarket"))


async def test_breaker_cooldown_doubles_per_failure(make_deps, redis, stub: ProviderStub) -> None:
    deps = make_deps(OMNI_FLEET_FAILURE_THRESHOLD=5)
    stub.status_override = 500
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    ttls = []
    for _ in range(3):
        await run_source("polymarket", deps, force=True)
        ttls.append(await redis.pttl(keys.breaker_open("polymarket")))
    assert ttls[0] <= 30_000 < ttls[1] <= 60_000 < ttls[2] <= 120_000


async def test_one_tripped_provider_does_not_stop_the_others(make_deps, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    stub.failing = {"/sports/soccer_epl/odds": 502}  # The Odds API is down, Polymarket is fine
    assert (await run_source("odds_api", deps, force=True)).status == "failed"
    assert (await run_source("polymarket", deps, force=True)).status == "ok"
    assert (await run_source("odds_api", deps)).reason == "circuit_open"  # paused, not spamming


# =============================================================== token bucket
async def test_bucket_allows_the_burst_then_waits(redis) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(min(seconds, 0.05))

    bucket = TokenBucket(redis, "test", max_wait_seconds=30, sleep=record)
    limit = RateLimit(requests_per_minute=60, burst=3)  # one token per second
    for _ in range(3):
        assert await bucket.acquire("p", limit) == 0.0
    with pytest.raises(ThrottledError):
        await TokenBucket(redis, "test", max_wait_seconds=0.01, sleep=record).acquire("p", limit)
    assert waits == []  # the over-budget wait was refused, not slept


async def test_bucket_is_shared_across_instances(redis) -> None:
    limit = RateLimit(requests_per_minute=6, burst=1)
    assert await TokenBucket(redis, "shared", max_wait_seconds=0.01).acquire("p", limit) == 0.0
    with pytest.raises(ThrottledError):  # a second "worker" sees the bucket empty
        await TokenBucket(redis, "shared", max_wait_seconds=0.01).acquire("p", limit)


async def test_bucket_falls_back_to_process_memory_without_redis() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.1)
    bucket = TokenBucket(dead, "local", max_wait_seconds=0.01)
    limit = RateLimit(requests_per_minute=60, burst=2)
    assert await bucket.acquire("q", limit) == 0.0 and await bucket.acquire("q", limit) == 0.0
    with pytest.raises(ThrottledError):
        await bucket.acquire("q", limit)
    await dead.aclose()


async def test_throttled_run_is_deferred_not_failed(make_deps, redis, session_factory) -> None:
    deps = make_deps(OMNI_FLEET_THROTTLE_MAX_WAIT_SECONDS=0.01)
    bucket_key = TokenBucket(None, f"{deps.settings.omni_redis_prefix}:fleet").key("polymarket")
    await redis.hset(bucket_key, mapping={"tokens": "0", "ts": "9999999999"})  # drained, no refill yet
    summary = await run_source("polymarket", deps, force=True)
    assert summary.status == "skipped" and summary.reason == "throttled"
    async with session_factory() as session:
        assert (await session.get(OmniFleetSource, "polymarket")).consecutive_failures == 0


# =============================================================== failover routing
def _descriptor(sid: str, cost: str, priority: int, groups: tuple[str, ...]) -> SourceDescriptor:
    return SourceDescriptor(
        source_id=sid, display_name=sid, description="", docs_url=None, kind="config", cost=cost, priority=priority,  # type: ignore[arg-type]
        coverage={g: g for g in groups}, interval_seconds=60, default_interval_seconds=60, requires_api_key=False,
        rate_limit=RateLimit(60, 5), devig="shin",
    )


REGISTRY = {
    "primary": _descriptor("primary", "metered", 10, ("soccer_epl", "basketball_nba")),
    "backup": _descriptor("backup", "metered", 20, ("soccer_epl",)),
    "free": _descriptor("free", "free", 50, ("soccer_epl",)),
}


def test_healthy_primary_runs_and_the_backup_stands_by() -> None:
    plan, groups = compute_plan(REGISTRY, {sid: "available" for sid in REGISTRY})
    assert plan["primary"].role == "primary" and plan["primary"].scope == ["soccer_epl", "basketball_nba"]
    assert plan["backup"].role == "standby" and plan["backup"].scope == []
    assert plan["free"].role == "always_on"
    assert not groups["soccer_epl"].failover


@pytest.mark.parametrize("reason", ["quota_reserve", "circuit_open", "disabled", "paused", "needs_key"])
def test_unavailable_primary_fails_over_per_group(reason) -> None:
    plan, groups = compute_plan(REGISTRY, {"primary": reason, "backup": "available", "free": "available"})
    assert plan["primary"].role == "unavailable" and plan["primary"].scope == []
    assert plan["backup"].role == "failover" and plan["backup"].scope == ["soccer_epl"]
    assert plan["backup"].covering[0].replacing == "primary" and plan["backup"].covering[0].reason == reason
    assert groups["soccer_epl"].failover and groups["basketball_nba"].uncovered  # nobody else covers the NBA


def test_redundancy_runs_two_metered_sources() -> None:
    plan, _ = compute_plan(REGISTRY, {sid: "available" for sid in REGISTRY}, redundancy=2)
    assert plan["backup"].role == "primary" and plan["backup"].scope == ["soccer_epl"]


# =============================================================== quota-aware failover, end to end
PARTNER_SPEC = {
    "display_name": "Partner aggregator",
    "base_url": "https://partner.feed.example",
    "requests": [{"path": "/v1/sports/{sport}/odds"}],
    "coverage": {"soccer_epl": "soccer_epl"},
    "rate_limit": {"requests_per_minute": 600, "burst": 50},
    "cost": "metered",
    "priority": 20,
    "mapping": {
        "events": "$[*]", "event_id": "id", "home": "home_team", "away": "away_team", "commence_time": "commence_time",
        "books": "bookmakers[*]", "markets": "markets[*]", "market_key": "key", "market_values": ["h2h"],
        "outcomes": "outcomes[*]", "outcome_name": "name", "price": {"path": "price"},
    },
}


async def test_quota_under_reserve_hands_the_market_to_the_backup(make_deps, session_factory, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32), omni_allow_private_networks=True)
    async with session_factory() as session:
        session.add(OmniFleetSource(source_id="partner", spec=PARTNER_SPEC, is_enabled=True, consecutive_failures=0))
        await session.commit()
    stub.headers = {"x-requests-remaining": "12", "x-requests-used": "488"}  # 2.4% left
    assert (await run_source("odds_api", deps, force=True)).status == "ok"

    dispatched: list[tuple[str, list[str], str]] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:
        dispatched.append((sid, scope, action))

    result = await fleet_tick(deps, record)
    assert ("partner", ["soccer_epl"], "ingest") in dispatched  # the backup took the EPL
    assert not any(sid == "odds_api" and action == "ingest" for sid, _, action in dispatched)
    assert result["failover_groups"] == ["soccer_epl"]


async def test_reserve_is_rechecked_with_a_free_probe(make_deps, redis, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32), OMNI_FLEET_QUOTA_RECHECK_SECONDS=1)
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    await redis.hset(keys.fleet_metrics("odds_api"), mapping={"quota_fraction": "0.01", "quota_checked_at": "0"})
    dispatched: list[tuple[str, str]] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:
        dispatched.append((sid, action))

    await fleet_tick(deps, record)
    assert ("odds_api", "probe") in dispatched
    stub.responses["/sports"] = []  # Odds API /sports: costs nothing, still reports quota
    stub.headers = {"x-requests-remaining": "500", "x-requests-used": "0"}  # the month reset
    assert (await run_source("odds_api", deps, probe=True)).reason == "probe"
    assert stub.requests[-1].url.path.endswith("/sports")
    assert float(await redis.hget(keys.fleet_metrics("odds_api"), "quota_fraction")) == 1.0


# =============================================================== Redis outage
class Sink:
    def __init__(self) -> None:
        self.ticks: list[MarketTick] = []

    async def broadcast_market_ticks(self, ticks) -> None:
        self.ticks.extend(ticks)


async def test_ingestion_survives_redis_loss_and_flushes_on_recovery(make_deps, redis, session_factory) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.1, decode_responses=True)
    sink = Sink()
    summary = await run_source("polymarket", make_deps(redis_client=dead, local_sink=sink), force=True)
    await dead.aclose()
    assert summary.status == "ok" and summary.ticks == 3  # no crash
    assert len(sink.ticks) == 3  # this worker's sockets still got the prices
    assert SPOOL.pending > 0

    flushed = await SPOOL.flush(redis, make_deps().settings)
    assert flushed > 0 and SPOOL.pending == 0
    assert len(await read_snapshot(redis) or []) == 3  # the board caught up once Redis returned


async def test_tick_without_redis_still_plans_and_dispatches(make_deps) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.1, decode_responses=True)
    seen: list[str] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:
        seen.append(sid)

    result = await fleet_tick(make_deps(redis_client=dead, ODDS_API_KEY=None), record)
    await dead.aclose()
    assert "polymarket" in seen and result["dispatched"]


async def test_tick_scopes_polymarket_to_its_leagues(make_deps) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def record(sid: str, scope: list[str], action: str, _: float) -> None:
        calls.append((sid, scope))

    await fleet_tick(make_deps(ODDS_API_KEY=None, POLYMARKET_LEAGUES="epl,nfl"), record)
    assert ("polymarket", ["epl", "nfl"]) in calls
    # A second tick inside the interval dispatches nothing (claim + interval gate)
    calls.clear()
    await fleet_tick(make_deps(ODDS_API_KEY=None, POLYMARKET_LEAGUES="epl,nfl"), record)
    assert calls == []
