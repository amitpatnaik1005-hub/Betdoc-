"""Phases 3-4: locked, health-tracked runs that dead-letter after repeated failure and push
canonical ticks through Redis pub/sub to every API worker's sockets. Needs a Redis server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from sqlalchemy import func, select

from app.adapters.ingestion import OddsApiIngestor, PolymarketIngestor
from app.core.live_odds import live_odds_keys, publish_board_ticks, read_snapshot, run_live_odds_relay
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.core.websockets import ConnectionManager
from app.models.canonical import CanonicalEntity
from app.models.odds import OddsSnapshot
from app.models.omni_vault import OmniFleetSource, OmniQuarantineLog
from app.schemas.market import MarketTick
from app.services import omni_fleet
from app.services.omni_fleet import FleetDeps, run_source
from app.services.omni_normalizer import tick_to_event
from app.services.omni_quorum_buffer import buffer_events
from app.workers.omni_quorum import resolve_quorums

from .conftest import ProviderStub, odds_api_epl


@pytest.fixture(autouse=True)
def _fresh_process_state() -> None:
    PolymarketIngestor._sports_cache = None
    OddsApiIngestor._last_quota = None
    omni_fleet._entities_synced = False


@pytest.fixture
def make_deps(redis, session_factory, http, fleet_settings):
    def build(**overrides) -> FleetDeps:
        async def no_wait(_: float) -> None:
            return None

        return FleetDeps(
            redis=redis,
            session_factory=session_factory,
            http=http,
            vault=VaultCrypto(Fernet.generate_key().decode()),
            settings=fleet_settings(**overrides),
            sleep=no_wait,
        )

    return build


class Sink:
    def __init__(self) -> None:
        self.batches: list[list[MarketTick]] = []
        self.got = asyncio.Event()

    async def broadcast_market_ticks(self, ticks: Sequence[MarketTick]) -> None:
        self.batches.append(list(ticks))
        self.got.set()


async def _wait_subscribed(redis: Redis, channel: str, count: int) -> None:
    for _ in range(100):
        subs = dict(await redis.pubsub_numsub(channel))
        if subs.get(channel, 0) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("relay never subscribed")


# ---------------------------------------------------------------- end-to-end runs
async def test_polymarket_run_flows_to_the_board_buffer_and_health(make_deps, redis, session_factory) -> None:
    deps = make_deps()
    pubsub = redis.pubsub()
    await pubsub.subscribe(live_odds_keys().channel)

    summary = await run_source("polymarket", deps, runner="celery")
    assert summary.status == "ok" and summary.ticks == 3 and summary.fixtures == 1

    message = None
    for _ in range(50):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            break
    await pubsub.aclose()
    assert message is not None
    published = json.loads(message["data"])
    assert {t["selection"] for t in published} == {"HOME", "DRAW", "AWAY"}
    assert published[0]["homeTeamId"] and published[0]["source"] == "polymarket"

    snapshot = await read_snapshot(redis)
    assert snapshot is not None and len(snapshot) == 3

    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    assert await redis.zcard(keys.quorum_topics()) == 3
    metrics = await redis.hgetall(keys.fleet_metrics("polymarket"))
    assert metrics["last_status"] == "ok" and metrics["ticks"] == "3" and metrics["state"] == "enabled"
    assert await redis.lrange(keys.fleet_runs("polymarket"), 0, -1) == ["1"]
    assert await redis.exists(keys.fleet_heartbeat())  # a celery run marks the worker alive

    async with session_factory() as session:
        row = await session.get(OmniFleetSource, "polymarket")
        assert row is not None and row.consecutive_failures == 0 and row.last_success_at is not None
        assert (await session.scalar(select(func.count()).select_from(CanonicalEntity))) >= 90


async def test_interval_gate_skips_a_run_that_is_not_due(make_deps) -> None:
    deps = make_deps()
    assert (await run_source("polymarket", deps)).status == "ok"
    assert (await run_source("polymarket", deps)).reason == "not_due"
    assert (await run_source("polymarket", deps, force=True)).status == "ok"


async def test_distributed_lock_prevents_overlapping_runs(make_deps, redis) -> None:
    deps = make_deps()
    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    holder = Lock(redis, keys.fleet_lock("polymarket"), timeout=30)
    assert await holder.acquire(blocking=False)
    try:
        summary = await run_source("polymarket", deps, force=True)
    finally:
        await holder.release()
    assert summary.status == "skipped" and summary.reason == "locked"


async def test_concurrent_runs_of_one_source_execute_once(make_deps, stub: ProviderStub) -> None:
    deps = make_deps()
    results = await asyncio.gather(*(run_source("polymarket", deps, force=True) for _ in range(4)))
    assert sorted(r.status for r in results).count("ok") == 1
    assert {r.reason for r in results if r.status == "skipped"} == {"locked"}


async def test_odds_api_without_a_key_waits_without_failing(make_deps, session_factory) -> None:
    deps = make_deps(ODDS_API_KEY=None)
    summary = await run_source("odds_api", deps, force=True)
    assert summary.status == "skipped" and summary.reason == "needs_key"
    async with session_factory() as session:
        assert (await session.get(OmniFleetSource, "odds_api")).consecutive_failures == 0


async def test_odds_api_run_uses_the_vault_key_and_stores_snapshots(make_deps, session_factory, stub: ProviderStub) -> None:
    deps = make_deps(ODDS_API_KEY=None)
    async with session_factory() as session:
        session.add(OmniFleetSource(source_id="odds_api", is_enabled=True, consecutive_failures=0, encrypted_api_key=deps.vault.encrypt_key("vaultkey" * 4)))
        await session.commit()
    summary = await run_source("odds_api", deps, force=True)
    assert summary.status == "ok" and summary.ticks == 3
    assert stub.requests[-1].url.params["apiKey"] == "vaultkey" * 4
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(OddsSnapshot)) == 9  # 3 books x 3 selections


async def test_two_sources_merge_into_one_consensus_board_cell(make_deps, redis, stub: ProviderStub) -> None:
    stub.responses["/sports/soccer_epl/odds"] = odds_api_epl(home=1.42, draw=4.9, away=9.5)
    deps = make_deps(ODDS_API_KEY=SecretStr("k" * 32))
    assert (await run_source("polymarket", deps, force=True)).status == "ok"
    assert (await run_source("odds_api", deps, force=True)).status == "ok"
    board = {t.selection: t for t in await read_snapshot(redis) or []}
    home = board["HOME"]
    assert home.sources == ("odds_api", "polymarket")
    assert float(home.odds) == pytest.approx(1.42) and home.source == "odds_api"  # beats Polymarket 1/0.71=1.408
    assert home.quorum == "consensus"  # ~0.697 vs ~0.709: inside the 5% variance threshold


# ---------------------------------------------------------------- dead-letter handling
async def test_three_consecutive_failures_mark_fatal_and_pause(make_deps, redis, session_factory, stub: ProviderStub, caplog) -> None:
    stub.status_override = 503
    deps = make_deps()
    statuses = [(await run_source("polymarket", deps, force=True)).status for _ in range(3)]
    assert statuses == ["failed", "failed", "fatal"]
    assert any(r.levelname == "CRITICAL" and "FATAL" in r.getMessage() for r in caplog.records)

    keys = OmniRedisKeys(deps.settings.omni_redis_prefix)
    dead = [json.loads(x) for x in await redis.lrange(keys.fleet_deadletter(), 0, -1)]
    assert dead[0]["source_id"] == "polymarket" and dead[0]["status"] == "FATAL" and dead[0]["failures"] == 3
    assert (await redis.hget(keys.fleet_metrics("polymarket"), "state")) == "paused"
    async with session_factory() as session:
        row = await session.get(OmniFleetSource, "polymarket")
        assert row.paused_at is not None and "503" in row.last_error

    # Paused: even a forced run does nothing until an operator re-enables it
    stub.status_override = None
    assert (await run_source("polymarket", deps, force=True)).reason == "paused"


async def test_a_success_resets_the_failure_streak(make_deps, session_factory, stub: ProviderStub) -> None:
    deps = make_deps()
    stub.status_override = 500
    await run_source("polymarket", deps, force=True)
    await run_source("polymarket", deps, force=True)
    stub.status_override = None
    assert (await run_source("polymarket", deps, force=True)).status == "ok"
    stub.status_override = 500
    assert (await run_source("polymarket", deps, force=True)).status == "failed"  # streak restarted at 1
    async with session_factory() as session:
        assert (await session.get(OmniFleetSource, "polymarket")).consecutive_failures == 1


async def test_disabled_source_never_runs(make_deps, session_factory, stub: ProviderStub) -> None:
    async with session_factory() as session:
        session.add(OmniFleetSource(source_id="polymarket", is_enabled=False, consecutive_failures=0))
        await session.commit()
    assert (await run_source("polymarket", make_deps(), force=True)).reason == "disabled"
    assert stub.requests == []


# ---------------------------------------------------------------- Gap 1: cross-worker fan-out
async def test_one_publish_reaches_every_workers_relay(redis) -> None:
    """Two relays = two gunicorn workers. A tick published once reaches both."""
    sinks = [Sink(), Sink()]
    relays = [asyncio.create_task(run_live_odds_relay(redis, sink)) for sink in sinks]
    try:
        await _wait_subscribed(redis, live_odds_keys().channel, 2)
        tick = MarketTick(match_id="m-9", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=2.1, true_probability=0.45, is_suspended=False)
        assert await publish_board_ticks(redis, [tick])
        await asyncio.wait_for(asyncio.gather(*(s.got.wait() for s in sinks)), timeout=3)
    finally:
        for relay in relays:
            relay.cancel()
        await asyncio.gather(*relays, return_exceptions=True)
    assert [s.batches[0][0].match_id for s in sinks] == ["m-9", "m-9"]


async def test_new_socket_gets_the_redis_snapshot_and_stale_cells_are_dropped(redis) -> None:
    fresh = MarketTick(match_id="fresh", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=2.0, true_probability=0.5, is_suspended=False)
    stale = fresh.model_copy(update={"match_id": "stale"})
    await publish_board_ticks(redis, [fresh, stale])
    keys = live_odds_keys()
    await redis.zadd(keys.board_ts, {stale.board_key: 1.0})  # last updated in 1970
    assert [t.match_id for t in await read_snapshot(redis) or []] == ["fresh"]

    class Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def accept(self) -> None:
            return None

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    socket = Socket()
    await ConnectionManager().connect(socket, snapshot=await read_snapshot(redis))  # type: ignore[arg-type]
    assert [t["matchId"] for t in json.loads(socket.sent[0])] == ["fresh"]


async def test_publish_reports_failure_when_redis_is_unreachable() -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    tick = MarketTick(match_id="x", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=2.0, true_probability=0.5, is_suspended=False)
    assert await publish_board_ticks(dead, [tick]) is False
    await dead.aclose()


# ---------------------------------------------------------------- Gap 3: scheduled quorum
async def _seed(redis: Redis, settings, *ticks: MarketTick) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await buffer_events(redis, OmniRedisKeys(settings.omni_redis_prefix), [(t.board_key, tick_to_event(t, now)) for t in ticks], 300)


def _board_tick(source: str, probability: float) -> MarketTick:
    from datetime import UTC, datetime

    return MarketTick(
        match_id="q-1", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=2.0,
        true_probability=probability, is_suspended=False, source=source, confidence=0.8, observed_at=datetime.now(UTC),
    )


async def test_quorum_resolves_agreeing_providers_and_skips_unchanged_sets(redis, session_factory, fleet_settings) -> None:
    settings = fleet_settings()
    await _seed(redis, settings, _board_tick("polymarket", 0.50), _board_tick("odds_api", 0.505))
    first = await resolve_quorums(redis, session_factory, settings)
    assert first["resolved"] == 1 and first["quarantined"] == 0
    consensus = json.loads(await redis.get(OmniRedisKeys(settings.omni_redis_prefix).quorum_consensus("q-1|Match Odds|HOME")))
    assert 0.50 <= consensus["normalized_value"] <= 0.505
    second = await resolve_quorums(redis, session_factory, settings)
    assert second["unchanged"] == 1 and second["resolved"] == 0


async def test_quorum_quarantines_disagreement_in_the_database(redis, session_factory, fleet_settings) -> None:
    settings = fleet_settings()
    await _seed(redis, settings, _board_tick("polymarket", 0.70), _board_tick("odds_api", 0.40))
    summary = await resolve_quorums(redis, session_factory, settings)
    assert summary["quarantined"] == 1
    async with session_factory() as session:
        row = (await session.execute(select(OmniQuarantineLog))).scalar_one()
        assert row.reason == "variance_exceeded" and row.topic == "q-1|Match Odds|HOME" and len(row.events) == 2


async def test_quorum_needs_two_providers(redis, session_factory, fleet_settings) -> None:
    settings = fleet_settings()
    await _seed(redis, settings, _board_tick("polymarket", 0.70))
    assert (await resolve_quorums(redis, session_factory, settings))["insufficient"] == 1
