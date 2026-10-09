"""Group 68: The Sentinel. Alerting, liveness and remote command.

The brief's three proofs come first:

* the spam debouncer, verified mathematically: over thousands of seeded random alert storms, on every
  channel any two CRITICAL messages are at least one window (30s) apart, every CRITICAL alert goes out
  exactly once (alone or inside exactly one digest), nothing waits longer than one window, and FATAL
  is never held; then the same thing end to end through Redis, the dispatcher and the delivery trail;
* the dead man's switch on a mocked clock: 59.9s of silence is fine, 60s is one FATAL however many
  monitors notice it, the heartbeat coming back resolves it, and a heartbeat that never came counts;
* Telegram's ``/halt``: only with the webhook's secret token, only from the configured admin chat and
  user, and then the kill switch is really engaged (Redis flag, database emergency stop, the risk
  guard refusing orders); ``/resume`` needs its one-time code; ``/status`` reports P&L and bot health.

Then the dispatchers' wire formats (Telegram, Discord, Twilio, PagerDuty) with retries and secret
scrubbing, dependency health, the producers (flash crash, whale orders, margin calls, a broken hash
chain), the 08:00 hype engine and its schedule, routing, and the test suite's own Redis isolation.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import random
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.notifications.base import DispatcherConfigurationError, OutboundMessage
from app.adapters.notifications.discord import DiscordDispatcher
from app.adapters.notifications.pagerduty import PagerDutyDispatcher
from app.adapters.notifications.telegram import TelegramDispatcher
from app.adapters.notifications.twilio import TwilioDispatcher
from app.api.v1 import sentinel as sentinel_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.models import User
from app.models.cfo_vault import AuditEvent, AuditLog
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.hive_bots import BotStatus, TradingBot
from app.models.sentinel import ChannelName, SentinelAlertLog, SentinelChannel, SentinelCommandLog, SentinelDelivery, SentinelRouting, Severity
from app.services.risk_guard import RiskGuard, RiskGuardViolation, kill_switch_engaged
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert, recent_alerts
from app.services.sentinel_debouncer import Decision, SpamDebouncer
from app.services.sentinel_health import DependencyCheck, HealthMonitor, LivenessMonitor, beat, judge_source
from app.services.sentinel_hype import HYPE_LINES, MarketForecast, Tier, classify, is_due, market_forecast_hype, pick_line, render
from app.services.sentinel_routing import DEFAULT_MATRIX, channels_for, normalise, validate
from app.services.sentinel_watch import watch_drawdown_block, watch_execution, watch_ledger
from app.workers.sentinel_dispatcher import SentinelDispatcher
from tests.test_hive_automation import TABLES as HIVE_TABLES
from tests.test_hive_automation import engine_for, make_bot, owner  # noqa: F401 - owner is a fixture, used by name

D = Decimal
TABLES = [*HIVE_TABLES, SentinelChannel.__table__, SentinelRouting.__table__, SentinelAlertLog.__table__, SentinelDelivery.__table__, SentinelCommandLog.__table__]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-sentinel"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
WINDOW = 30.0
ADMIN_CHAT, ADMIN_USER, BOT_TOKEN, WEBHOOK_SECRET = 111_222, 42, "123456:AAtest-token-never-printed", "s3cret-webhook-token"


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
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "starting_bankroll": 1_000_000.0,
            "CFO_KILL_SWITCH_KEY": "test:kill_switch",
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak",
            "CFO_IDEMPOTENCY_KEY_PREFIX": "test:idempotency",
            "ARYABHATA_PREFIX": "test_arya",
            "SNIPER_PREFIX": "test_sniper",
            "PORTFOLIO_CHANNEL_PREFIX": "test:live_portfolio",
            "LIVE_ODDS_CHANNEL": "test:live_odds",
            "HIVE_PREFIX": "test_hive",
            "CFO_EXECUTION_MODE": "paper",
            "SENTINEL_PREFIX": "test_sentinel",
            "SENTINEL_STREAM": "test_sentinel_alerts",
        }
    )


@pytest.fixture
def vault() -> VaultCrypto:
    return VaultCrypto(Fernet.generate_key().decode())


async def configure(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, channel: ChannelName, secrets: dict[str, str], config: dict[str, Any], *, enabled: bool = True) -> None:
    async with sessions() as session:
        row = await session.get(SentinelChannel, channel.value) or SentinelChannel(channel=channel.value)
        row.enabled, row.config, row.encrypted_credentials = enabled, config, vault.encrypt_key(json.dumps(secrets))
        session.add(row)
        await session.commit()


async def telegram(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto) -> None:
    await configure(sessions, vault, ChannelName.TELEGRAM, {"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET}, {"chat_ids": [ADMIN_CHAT], "admin_user_ids": [ADMIN_USER]})


async def stream_alerts(redis: Redis, settings: Settings) -> list[SentinelAlert]:
    return list(reversed(await recent_alerts(redis, settings, 500)))


def alert(severity: Severity = Severity.CRITICAL, at: datetime = T0, kind: AlertKind = AlertKind.TEST, title: str = "test") -> SentinelAlert:
    return SentinelAlert(kind=kind, severity=severity, title=title, source="test", occurred_at=at)


class Wire:
    """An httpx transport that records every request and answers from a script."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer or (lambda request: httpx.Response(200, json={"ok": True, "result": {}}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ================================================================ 1. the spam debouncer, mathematically
def _storm(rng: random.Random, channels: tuple[str, ...]) -> list[tuple[float, str, Severity]]:
    """A ten-minute storm: bursts of CRITICALs (cascading failures), a few FATALs and WARNINGs."""
    arrivals: list[tuple[float, str, Severity]] = []
    t = 0.0
    while t < 600:
        t += rng.expovariate(1 / 20)  # a burst roughly every 20s
        for _ in range(rng.randint(1, 12)):
            severity = rng.choices([Severity.CRITICAL, Severity.FATAL, Severity.WARNING], weights=[85, 5, 10])[0]
            arrivals.append((t + rng.uniform(0, 4), rng.choice(channels), severity))
    return sorted(arrivals, key=lambda a: a[0])


def _simulate(arrivals: list[tuple[float, str, Severity]], window: float) -> tuple[dict[str, list[tuple[datetime, str, list[uuid.UUID]]]], dict[uuid.UUID, tuple[str, datetime]], list[tuple[datetime, datetime]]]:
    """Drive a debouncer like the dispatcher does: offer each arrival; release digests the moment they
    are due. Returns (CRITICAL messages per channel, every CRITICAL offered, FATAL/WARNING send delays)."""
    debouncer = SpamDebouncer(window)
    messages: dict[str, list[tuple[datetime, str, list[uuid.UUID]]]] = defaultdict(list)
    offered: dict[uuid.UUID, tuple[str, datetime]] = {}
    immediate: list[tuple[datetime, datetime]] = []
    index = 0
    while index < len(arrivals) or debouncer.next_due() is not None:
        upcoming = T0 + timedelta(seconds=arrivals[index][0]) if index < len(arrivals) else None
        due = debouncer.next_due()
        if due is not None and (upcoming is None or due <= upcoming):
            for digest in debouncer.due(due):
                assert digest.released_at == digest.opened_at + timedelta(seconds=window)  # released the instant the window closes
                messages[digest.channel].append((due, "digest", [a.id for a in digest.alerts]))
            continue
        _, channel, severity = arrivals[index]
        index += 1
        item = alert(severity, upcoming)  # type: ignore[arg-type]
        decision = debouncer.offer(channel, item, upcoming)  # type: ignore[arg-type]
        if severity is Severity.CRITICAL:
            offered[item.id] = (channel, upcoming)  # type: ignore[assignment]
            if decision is Decision.SEND:
                messages[channel].append((upcoming, "single", [item.id]))  # type: ignore[arg-type]
        else:
            assert decision is Decision.SEND  # FATAL and WARNING are never held
            immediate.append((upcoming, upcoming))  # type: ignore[arg-type]
    return messages, offered, immediate


def test_the_debouncer_sends_at_most_one_critical_per_channel_per_window_and_loses_nothing() -> None:
    """200 seeded ten-minute storms over three channels (thousands of alerts). For every channel:
    (1) consecutive CRITICAL messages are >= 30s apart, so any 30s window holds at most one;
    (2) every CRITICAL offered appears in exactly one message, alone or in exactly one digest;
    (3) no alert waits more than one window; (4) the message count is at most span/30 + 1;
    (5) FATAL and WARNING always go at once. And the debouncer actually works: most alerts are batched."""
    window = timedelta(seconds=WINDOW)
    total = batched = 0
    for seed in range(200):
        arrivals = _storm(random.Random(seed), ("TELEGRAM", "DISCORD", "PAGERDUTY"))
        messages, offered, immediate = _simulate(arrivals, WINDOW)
        assert all(sent == arrived for arrived, sent in immediate)
        for channel, sent in messages.items():
            times = [at for at, _, _ in sent]
            assert all(later - earlier >= window for earlier, later in itertools.pairwise(times)), (seed, channel)  # (1)
            carried = [i for _, _, ids in sent for i in ids]
            mine = {i for i, (c, _) in offered.items() if c == channel}
            assert len(carried) == len(set(carried)) and set(carried) == mine, (seed, channel)  # (2)
            for at, _, ids in sent:
                assert all(at - offered[i][1] <= window for i in ids)  # (3)
            assert len(times) <= (times[-1] - times[0]) / window + 1  # (4)
            batched += sum(len(ids) for _, kind, ids in sent if kind == "digest")
        total += len(offered)
    assert total > 10_000 and batched > total * 0.6  # thousands of alerts, and the storms really were damped


def test_a_burst_of_forty_criticals_is_one_message_now_and_one_digest_thirty_seconds_later() -> None:
    debouncer = SpamDebouncer(WINDOW)
    burst = [alert(at=T0 + timedelta(seconds=i * 0.1), kind=AlertKind.DEPENDENCY_DOWN, title=f"source {i} down") for i in range(40)]
    decisions = [debouncer.offer("TELEGRAM", a, a.occurred_at) for a in burst]
    assert decisions[0] is Decision.SEND and decisions[1:] == [Decision.HOLD] * 39
    assert debouncer.due(T0 + timedelta(seconds=29.999)) == []
    (digest,) = debouncer.due(T0 + timedelta(seconds=30))
    assert [a.id for a in digest.alerts] == [a.id for a in burst[1:]]
    message = digest.as_alert()
    assert message.kind is AlertKind.DIGEST and message.severity is Severity.CRITICAL
    assert message.title.startswith("39 critical alerts held by the debouncer (39x DEPENDENCY_DOWN)")
    assert len(message.detail["alert_ids"]) == 39 and "source 1 down" in message.body
    # the digest opened the next window: a CRITICAL a second later waits for t+60
    late = alert(at=T0 + timedelta(seconds=31))
    assert debouncer.offer("TELEGRAM", late, late.occurred_at) is Decision.HOLD
    assert debouncer.next_due() == T0 + timedelta(seconds=60)
    # other channels and other severities are untouched
    assert debouncer.offer("DISCORD", late, late.occurred_at) is Decision.SEND
    assert debouncer.offer("TELEGRAM", alert(Severity.FATAL), T0 + timedelta(seconds=32)) is Decision.SEND


@pytest.mark.asyncio
async def test_the_dispatcher_debounces_end_to_end_and_acknowledges_only_after_the_digest(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto
) -> None:
    """Five CRITICALs and then a FATAL through the real stream, consumer group and dispatcher. Telegram
    gets the first CRITICAL at once, the FATAL at once, and the other four as one digest when the window
    closes; until then those four stay pending on the stream (a crash would not lose them)."""
    await telegram(sessions, vault)
    wire = Wire()
    clock = {"now": T0}
    dispatcher = SentinelDispatcher(redis, sessions, settings, vault, http=wire.client(), clock=lambda: clock["now"])
    await dispatcher.ensure_group()
    keys = SentinelKeys(settings)
    burst = [alert(title=f"bookmaker {i} down", kind=AlertKind.DEPENDENCY_DOWN) for i in range(5)]
    for item in burst:
        assert await emit_alert(redis, settings, item)
    assert await dispatcher.step() == 5
    assert len(wire.requests) == 1 and "bookmaker 0 down" in json.loads(wire.requests[0].content)["text"]
    assert (await redis.xpending(keys.stream, keys.group))["pending"] == 4  # held, so not acknowledged

    clock["now"] = T0 + timedelta(seconds=10)
    fatal = alert(Severity.FATAL, kind=AlertKind.GARUDA_SILENT, title="Garuda silent")
    await emit_alert(redis, settings, fatal)
    await dispatcher.step()
    assert len(wire.requests) == 2 and "Garuda silent" in json.loads(wire.requests[1].content)["text"]  # FATAL never waits

    clock["now"] = T0 + timedelta(seconds=30)
    await dispatcher.step()
    assert len(wire.requests) == 3
    digest = json.loads(wire.requests[2].content)
    assert digest["chat_id"] == ADMIN_CHAT and "4 critical alerts held by the debouncer" in digest["text"]
    assert (await redis.xpending(keys.stream, keys.group))["pending"] == 0
    async with sessions() as session:
        rows = list((await session.execute(select(SentinelDelivery).where(SentinelDelivery.channel == "TELEGRAM"))).scalars())
        logged = list((await session.execute(select(SentinelAlertLog))).scalars())
    statuses = sorted(r.status for r in rows)
    assert statuses == ["BATCHED"] * 4 + ["DIGEST", "SENT", "SENT"]
    assert next(r for r in rows if r.status == "DIGEST").batched == 4
    assert {r.id for r in logged} == {a.id for a in [*burst, fatal]}  # every alert recorded once
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_a_routing_edit_applies_to_the_very_next_alert(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    """The dispatcher caches the routing for 10s, but an edit bumps a version it checks every step."""
    from app.api.deps import get_current_admin  # noqa: PLC0415

    await telegram(sessions, vault)
    wire = Wire()
    dispatcher = SentinelDispatcher(redis, sessions, settings, vault, http=wire.client())
    await dispatcher.ensure_group()
    await emit_alert(redis, settings, alert(Severity.WARNING))  # WARNING goes to Discord only by default: nothing on Telegram
    await dispatcher.step()
    assert wire.requests == []
    async with sessions() as session:
        admin = User(username=f"admin_{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
        session.add(admin)
        await session.commit()
    app = app_for(sessions, redis, settings, vault)
    app.dependency_overrides[get_current_admin] = lambda: admin
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put("/api/v1/sentinel/routing", json={"matrix": {"WARNING": ["TELEGRAM"]}})).status_code == 200
    await emit_alert(redis, settings, alert(Severity.WARNING, title="after the edit"))
    await dispatcher.step()  # inside the 10s cache, yet the edit is already in force
    assert len(wire.requests) == 1 and "after the edit" in json.loads(wire.requests[0].content)["text"]
    await dispatcher.aclose()


# ================================================================ 2. the dead man's switch
@pytest.mark.asyncio
async def test_the_dead_mans_switch_fires_one_fatal_after_sixty_seconds_of_silence(redis: Redis, settings: Settings) -> None:
    clock = {"t": 1_000_000.0}
    monitor = LivenessMonitor(redis, settings, clock=lambda: clock["t"])
    second = LivenessMonitor(redis, settings, clock=lambda: clock["t"])  # Celery beat and an API worker, both watching
    assert await beat(redis, settings, runner="celery", at=clock["t"])
    for later in (5.0, 30.0, 59.9):
        clock["t"] = 1_000_000.0 + later
        report = await monitor.check()
        assert report.status == "ALIVE" and report.fired is None
    clock["t"] = 1_000_060.0
    report = await monitor.check()
    assert report.status == "SILENT" and report.fired is not None and report.fired.severity is Severity.FATAL
    assert report.fired.kind is AlertKind.GARUDA_SILENT and "silent for 60s" in report.fired.title and "celery" in report.fired.body
    for later in (60.5, 75.0, 300.0):
        clock["t"] = 1_000_000.0 + later
        assert (await monitor.check()).fired is None and (await second.check()).fired is None  # one FATAL per silence
    fired = [a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.GARUDA_SILENT]
    assert len(fired) == 1

    clock["t"] = 1_000_310.0
    await beat(redis, settings, runner="inprocess", at=clock["t"])
    report = await second.check()
    assert report.status == "ALIVE" and report.fired is not None and report.fired.kind is AlertKind.GARUDA_RECOVERED
    assert report.fired.resolves and report.fired.dedupe_key == "liveness:garuda"
    assert (await monitor.check()).fired is None

    clock["t"] = 1_000_370.0  # silent again: a new episode, a new FATAL
    assert (await monitor.check()).fired is not None
    assert len([a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.GARUDA_SILENT]) == 2


@pytest.mark.asyncio
async def test_a_heartbeat_that_never_came_counts_from_when_the_watch_began(redis: Redis, settings: Settings) -> None:
    clock = {"t": 2_000_000.0}
    monitor = LivenessMonitor(redis, settings, clock=lambda: clock["t"])
    assert (await monitor.check()).fired is None
    clock["t"] += 59.0
    assert (await monitor.check()).status == "ALIVE"
    clock["t"] += 1.0
    report = await monitor.check()
    assert report.status == "SILENT" and report.fired is not None and "No heartbeat has been seen" in report.fired.body


@pytest.mark.asyncio
async def test_the_in_process_ingestion_scheduler_beats_garudas_heartbeat(redis: Redis, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The API's fallback scheduler beats on every tick it runs (Celery's 5-second tick beats too)."""
    import asyncio  # noqa: PLC0415

    from app.services import omni_fleet  # noqa: PLC0415

    ticks: list[float] = []

    async def fake_tick(deps: Any, dispatch: Any) -> dict[str, Any]:  # noqa: ARG001
        ticks.append(1.0)
        if len(ticks) >= 2:
            raise asyncio.CancelledError
        return {}

    monkeypatch.setattr(omni_fleet, "fleet_tick", fake_tick)
    deps = omni_fleet.FleetDeps(redis=redis, session_factory=None, http=None, vault=None, settings=settings.model_copy(update={"OMNI_FLEET_FALLBACK_TICK_SECONDS": 0.01}))  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await omni_fleet.run_inprocess_fallback(deps)
    raw = await redis.get(SentinelKeys(settings).heartbeat_last("garuda"))
    assert raw is not None and json.loads(raw)["runner"] == "inprocess"
    assert await redis.ttl(SentinelKeys(settings).heartbeat("garuda")) > 0


# ================================================================ 3. Telegram: /halt, /resume, /status
def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> FastAPI:
    app = FastAPI()
    app.include_router(sentinel_api.router, prefix="/api/v1")
    app.state.redis, app.state.vault = redis, vault
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def update(text: str, *, chat: int = ADMIN_CHAT, user: int = ADMIN_USER, update_id: int | None = None) -> dict[str, Any]:
    return {
        "update_id": update_id if update_id is not None else random.randint(1, 10**9),
        "message": {"message_id": 1, "date": 0, "text": text, "chat": {"id": chat, "type": "private"}, "from": {"id": user, "is_bot": False, "username": "amit_admin"}},
    }


async def send(client: httpx.AsyncClient, body: dict[str, Any], secret: str | None = WEBHOOK_SECRET) -> httpx.Response:
    headers = {sentinel_api.TELEGRAM_SECRET_HEADER: secret} if secret is not None else {}
    return await client.post("/api/v1/sentinel/webhook/telegram", json=body, headers=headers)


async def controls(sessions: async_sessionmaker[AsyncSession], **fields: Any) -> SystemSettingsModel:
    async with sessions() as session:
        row = await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
        if row is None:
            row = SystemSettingsModel(id=SETTINGS_SINGLETON_ID)
            session.add(row)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.mark.asyncio
async def test_telegram_halt_engages_the_kill_switch_and_only_for_the_admin(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto
) -> None:
    await telegram(sessions, vault)
    await controls(sessions, max_daily_exposure=500.0, bots_enabled=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, vault)), base_url="http://test") as client:
        assert (await send(client, update("/halt"), secret=None)).status_code == 401
        assert (await send(client, update("/halt"), secret="guess")).status_code == 401
        assert (await send(client, update("/halt", chat=999_999))).json() == {}  # a stranger's chat: silence
        assert (await send(client, update("/halt", user=7))).json() == {}  # the admin chat, not an admin user
        assert await kill_switch_engaged(redis, settings) is False

        halt = update("/halt@BetDocSentinelBot", update_id=4242)
        reply = (await send(client, halt)).json()
        assert reply["method"] == "sendMessage" and reply["chat_id"] == ADMIN_CHAT and "Kill switch engaged" in reply["text"]
        assert (await send(client, halt)).json() == {}  # Telegram's retry of the same update: answered once

    assert await kill_switch_engaged(redis, settings) is True
    row = await controls(sessions)
    assert row.max_daily_exposure == 0 and row.bots_enabled is False and row.last_emergency_stop_at is not None
    async with sessions() as session:
        with pytest.raises(RiskGuardViolation) as refused:
            await RiskGuard(redis, settings).kill_switch(session)  # every execution checks this first
        assert refused.value.reason == "BLOCKED_BY_KILL_SWITCH"
        log = list((await session.execute(select(SentinelCommandLog).order_by(SentinelCommandLog.id))).scalars())
    assert [(r.authorised, r.outcome) for r in log] == [(False, "IGNORED"), (False, "IGNORED"), (True, "HALTED")]
    assert log[-1].sender == "@amit_admin" and log[-1].detail["snapshot"]["max_daily_exposure"] == 500.0
    (kill,) = [a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.KILL_SWITCH]
    assert kill.severity is Severity.CRITICAL and "@amit_admin" in kill.title


@pytest.mark.asyncio
async def test_telegram_resume_restores_the_limits_only_with_its_one_time_code(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto
) -> None:
    await telegram(sessions, vault)
    await controls(sessions, max_daily_exposure=750.0, bots_enabled=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, vault)), base_url="http://test") as client:
        assert "not halted" in (await send(client, update("/resume"))).json()["text"]
        await send(client, update("/halt"))
        ask = (await send(client, update("/resume"))).json()["text"]
        code = re.search(r"/resume ([0-9A-F]{6})", ask).group(1)  # type: ignore[union-attr]
        assert "₹750.00" in ask and "bots enabled" in ask
        assert "wrong or has expired" in (await send(client, update("/resume 000000"))).json()["text"]
        assert (await send(client, update(f"/resume {code}", chat=555))).json() == {}  # the code is useless from any other chat
        assert await kill_switch_engaged(redis, settings) is True
        done = (await send(client, update(f"/resume {code.lower()}"))).json()["text"]
        assert "Trading resumed" in done and "₹750.00" in done
        assert "not halted" in (await send(client, update(f"/resume {code}"))).json()["text"]  # the code was single use
    row = await controls(sessions)
    assert row.max_daily_exposure == 750.0 and row.bots_enabled is True
    assert await kill_switch_engaged(redis, settings) is False
    lifted = [a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.KILL_SWITCH_LIFTED]
    assert len(lifted) == 1 and lifted[0].resolves and lifted[0].dedupe_key == "kill-switch"


@pytest.mark.asyncio
async def test_a_halt_on_a_fresh_install_saves_the_default_limits_for_resume(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto
) -> None:
    """No settings row yet: the defaults are the limits in force, so they are what /resume puts back."""
    await telegram(sessions, vault)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, vault)), base_url="http://test") as client:
        await send(client, update("/halt"))
        ask = (await send(client, update("/resume"))).json()["text"]
        code = re.search(r"/resume ([0-9A-F]{6})", ask)
        assert code is not None and "₹500.00" in ask
        assert "Trading resumed" in (await send(client, update(f"/resume {code.group(1)}"))).json()["text"]
    assert (await controls(sessions)).max_daily_exposure == 500.0


@pytest.mark.asyncio
async def test_resume_will_not_lift_a_stop_engaged_from_the_control_panel(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto
) -> None:
    from app.domain.control_panel.manager import ControlPanelManager  # noqa: PLC0415

    await telegram(sessions, vault)
    await controls(sessions, max_daily_exposure=500.0)
    async with sessions() as session:
        await ControlPanelManager().emergency_stop(session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, vault)), base_url="http://test") as client:
        reply = (await send(client, update("/resume"))).json()["text"]
    assert "engaged from the Control Panel" in reply
    assert (await controls(sessions)).max_daily_exposure == 0


@pytest.mark.asyncio
async def test_telegram_status_reports_pnl_and_bot_health(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, owner: uuid.UUID  # noqa: F811
) -> None:
    await telegram(sessions, vault)
    await controls(sessions, max_daily_exposure=500.0)
    now = datetime.now(UTC)
    async with sessions() as session:
        for pnl, ago in ((D("150.00"), 1), (D("-50.00"), 5), (D("999.00"), 30)):  # the 30h-old one is outside the window
            session.add(AuditLog(user_id=owner, event=AuditEvent.SETTLED, reason="WON", pnl_inr=pnl, detail={}, created_at=now - timedelta(hours=ago)))
        await session.commit()
    await make_bot(sessions, settings, owner, name="Alpha")
    await make_bot(sessions, settings, owner, name="Beta")
    suspended = await make_bot(sessions, settings, owner, name="Gamma")
    async with sessions() as session:
        bot = await session.get(TradingBot, suspended.id)
        bot.status = BotStatus.SUSPENDED  # type: ignore[union-attr]
        await session.commit()
    await beat(redis, settings, runner="celery")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, vault)), base_url="http://test") as client:
        text = (await send(client, update("/status"))).json()["text"]
    assert "P&amp;L (24h, realised): <b>₹100.00</b>" in text
    assert "2 active" in text and "1 suspended" in text
    assert "Kill switch: ✅ off" in text and "Garuda (odds feed): 💓" in text and "Hive: ▶️ running" in text


# ================================================================ dispatchers: the wire formats
def message(severity: Severity = Severity.CRITICAL, **fields: Any) -> OutboundMessage:
    base = {"severity": severity, "kind": "DEPENDENCY_DOWN", "title": "Redis is down", "body": "PING timed out <script>", "occurred_at": T0, "source": "sentinel.health", "dedupe_key": "dependency:redis"}
    return OutboundMessage(**{**base, **fields})


@pytest.mark.asyncio
async def test_telegram_escapes_html_and_never_resends_to_a_chat_that_already_got_it() -> None:
    calls = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if json.loads(request.content)["chat_id"] == 2 and calls["n"] == 2:
            return httpx.Response(502)
        return httpx.Response(200, json={"ok": True})

    wire, sleeps = Wire(answer), Sleeps()
    result = await TelegramDispatcher(wire.client(), bot_token=BOT_TOKEN, chat_ids=[1, 2], sleep=sleeps).send(message())
    assert result.ok and result.deliveries == 2 and result.attempts == 2 and sleeps.calls == [0.5]
    assert [json.loads(r.content)["chat_id"] for r in wire.requests] == [1, 2, 2]  # chat 1 was not sent twice
    body = json.loads(wire.requests[0].content)
    assert str(wire.requests[0].url) == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    assert body["parse_mode"] == "HTML" and "&lt;script&gt;" in body["text"] and "<script>" not in body["text"]


@pytest.mark.asyncio
async def test_a_bot_token_never_appears_in_a_delivery_error() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    result = await TelegramDispatcher(Wire(answer).client(), bot_token=BOT_TOKEN, chat_ids=[1], sleep=Sleeps()).send(message())
    assert not result.ok and result.attempts == 3 and BOT_TOKEN not in (result.error or "") and "***" in (result.error or "")


@pytest.mark.asyncio
async def test_discord_embeds_honour_retry_after_and_only_real_webhooks_are_accepted() -> None:
    url = "https://discord.com/api/webhooks/1234/abcDEFsecret"
    answers = iter([httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(200, json={"id": "1"})])
    wire, sleeps = Wire(lambda request: next(answers)), Sleeps()
    result = await DiscordDispatcher(wire.client(), webhook_url=url, mention_on_fatal=True, sleep=sleeps).send(message(Severity.FATAL))
    assert result.ok and sleeps.calls == [1.5]
    body = json.loads(wire.requests[-1].content)
    embed = body["embeds"][0]
    assert embed["color"] == 0x7F1D1D and embed["footer"]["text"] == "DEPENDENCY_DOWN" and body["content"] == "@here"
    quiet = DiscordDispatcher(Wire().client(), webhook_url=url).payload(message(Severity.CRITICAL))
    assert quiet["allowed_mentions"] == {"parse": []} and "content" not in quiet
    for bad in ("http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/x", "https://discord.com/channels/1"):
        with pytest.raises(DispatcherConfigurationError):
            DiscordDispatcher(Wire().client(), webhook_url=bad)
    failing = await DiscordDispatcher(Wire(lambda request: httpx.Response(400, text=f"bad {url}")).client(), webhook_url=url).send(message())
    assert not failing.ok and "abcDEFsecret" not in (failing.error or "")


@pytest.mark.asyncio
async def test_twilio_texts_every_number_and_rings_them_for_fatal() -> None:
    sid, token = "AC" + "0" * 32, "twilio-auth-token"
    wire = Wire(lambda request: httpx.Response(201, json={"sid": "SM1"}))
    dispatcher = TwilioDispatcher(wire.client(), account_sid=sid, auth_token=token, from_number="+15005550006", to_numbers=["+919876543210"], voice_on_fatal=True)
    assert (await dispatcher.send(message(Severity.CRITICAL))).deliveries == 1
    assert (await dispatcher.send(message(Severity.FATAL, title="Garuda silent"))).deliveries == 2
    sms, _, call = wire.requests
    assert str(sms.url).endswith(f"/Accounts/{sid}/Messages.json") and str(call.url).endswith(f"/Accounts/{sid}/Calls.json")
    form = dict(httpx.QueryParams(sms.content.decode()))
    assert form["To"] == "+919876543210" and form["From"] == "+15005550006" and len(form["Body"]) <= 320
    assert sms.headers["Authorization"] == "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()
    assert "<Say" in dict(httpx.QueryParams(call.content.decode()))["Twiml"]
    with pytest.raises(DispatcherConfigurationError):
        TwilioDispatcher(wire.client(), account_sid=sid, auth_token=token, from_number="+15005550006", to_numbers=["98765 43210"])


@pytest.mark.asyncio
async def test_pagerduty_triggers_with_a_dedup_key_and_resolves_with_the_same_one() -> None:
    wire = Wire(lambda request: httpx.Response(202, json={"status": "success"}))
    dispatcher = PagerDutyDispatcher(wire.client(), routing_key="R" * 32)
    await dispatcher.send(message(Severity.FATAL))
    await dispatcher.send(message(Severity.INFO, resolves=True))
    trigger, resolve = (json.loads(r.content) for r in wire.requests)
    assert trigger["event_action"] == "trigger" and trigger["payload"]["severity"] == "critical" and trigger["dedup_key"] == "dependency:redis"
    assert resolve == {"routing_key": "R" * 32, "event_action": "resolve", "dedup_key": "dependency:redis"}


# ================================================================ dependency health
@pytest.mark.asyncio
async def test_a_dependency_going_down_is_one_critical_and_coming_back_is_one_resolve(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> None:
    script: list[list[DependencyCheck]] = []

    def checks(postgres_ok: bool, source_ok: bool) -> list[DependencyCheck]:
        return [
            DependencyCheck("postgres", "postgres", postgres_ok, 1.0, "SELECT 1"),
            DependencyCheck("redis", "redis", True, 0.5, "PING"),
            DependencyCheck("bookmaker:odds_api", "bookmaker", source_ok, None, "circuit open"),
        ]

    monitors = [HealthMonitor(redis, sessions, settings), HealthMonitor(redis, sessions, settings)]
    for monitor in monitors:
        async def probe(script: list[list[DependencyCheck]] = script) -> list[DependencyCheck]:
            return script[-1]

        monitor.probe = probe  # type: ignore[method-assign]
    script.append(checks(True, True))
    assert all([(await m.check()).fired == [] for m in monitors])
    script.append(checks(False, False))
    first, second = await monitors[0].check(), await monitors[1].check()
    assert sorted(a.title for a in first.fired) == ["Bookmaker API odds_api is down", "PostgreSQL is down"] and second.fired == []  # compare-and-set: once
    assert all(a.severity is Severity.CRITICAL and a.kind is AlertKind.DEPENDENCY_DOWN for a in first.fired)
    assert (await monitors[0].check()).fired == []  # still down, inside the reminder interval
    script.append(checks(True, False))
    (back,) = (await monitors[1].check()).fired
    assert back.kind is AlertKind.DEPENDENCY_RECOVERED and back.resolves and back.dedupe_key == "dependency:postgres"
    stored = {c["name"]: c["ok"] for c in [json.loads(v) for v in (await redis.hgetall(SentinelKeys(settings).health)).values()]}
    assert stored == {"postgres": True, "redis": True, "bookmaker:odds_api": False}


@pytest.mark.asyncio
async def test_redis_being_down_is_delivered_without_redis(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    delivered: list[SentinelAlert] = []

    async def direct(item: SentinelAlert) -> None:
        delivered.append(item)

    report = await HealthMonitor(dead, sessions, settings, deliver_without_redis=direct).check()
    assert not report.redis_ok and [a.title for a in delivered] == ["Redis is down"]
    assert next(c for c in report.checks if c.name == "postgres").ok
    assert (await HealthMonitor(dead, sessions, settings, deliver_without_redis=direct).check()).fired  # a new process has no memory: it reports too
    await dead.aclose()


def test_bookmaker_health_is_read_from_the_fleets_own_record(settings: Settings) -> None:
    """No request, no quota: the breaker, the last success and the failure streak decide."""
    now = 10_000.0
    assert not judge_source("odds_api", 60, {"consecutive_failures": "4", "last_error": "HTTP 503"}, 45_000, settings, now).ok  # breaker open
    stale = judge_source("odds_api", 60, {"consecutive_failures": "2", "last_success_at": str(now - 700)}, -2, settings, now)
    assert not stale.ok and "no success for 12 min" in stale.detail  # 10 intervals of 60s without a success
    assert judge_source("odds_api", 60, {"consecutive_failures": "0", "last_success_at": str(now - 700)}, -2, settings, now).ok  # quiet, not failing
    assert not judge_source("odds_api", 60, {"consecutive_failures": "3"}, -2, settings, now).ok  # never succeeded
    assert judge_source("odds_api", 60, {}, -2, settings, now).detail == "not polled yet"


# ================================================================ producers
@pytest.mark.asyncio
async def test_a_flash_crash_reaches_the_sentinel(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    from tests.test_e2e_flash_crash import board  # noqa: PLC0415

    await board(redis, settings, "fx-sen-tin|Match Odds|HOME", [(90, 0.40), (60, 0.43), (30, 0.47)])
    flag = await engine_for(sessions, redis, settings).flash_crash_scan()
    assert flag is not None
    (crash,) = [a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.FLASH_CRASH]
    assert crash.severity is Severity.CRITICAL and "17.5% swing on fx-sen-tin" in crash.title


@pytest.mark.asyncio
async def test_whale_orders_and_margin_calls(redis: Redis, settings: Settings) -> None:
    user = uuid.uuid4()
    common = {"user_id": user, "bot_id": None, "fixture_id": "fx-1", "market": "Match Odds", "selection": "HOME", "bookmaker_id": "pinnacle", "odds": D("2.10"), "mode": "paper"}
    small = await watch_execution(redis, settings, idempotency_key=uuid.uuid4(), stake_inr=D("49999.99"), available=D("900000"), exposure=D("49999.99"), **common)
    assert small == []
    (whale,) = await watch_execution(redis, settings, idempotency_key=uuid.uuid4(), stake_inr=D("60000"), available=D("800000"), exposure=D("60000"), **common)
    assert whale.kind is AlertKind.WHALE_ORDER and whale.severity is Severity.WARNING and "₹60,000.00" in whale.title
    (big,) = await watch_execution(redis, settings, idempotency_key=uuid.uuid4(), stake_inr=D("250000"), available=D("500000"), exposure=D("250000"), **common)
    assert big.severity is Severity.CRITICAL  # five times the whale line
    margin = await watch_execution(redis, settings, idempotency_key=uuid.uuid4(), stake_inr=D("1000"), available=D("5000"), exposure=D("95000"), **common)
    assert [a.kind for a in margin] == [AlertKind.MARGIN_CALL] and "95%" in margin[0].title
    again = await watch_execution(redis, settings, idempotency_key=uuid.uuid4(), stake_inr=D("1000"), available=D("4000"), exposure=D("96000"), **common)
    assert again == []  # once an hour per account
    drawdown = await watch_drawdown_block(redis, settings, user_id=user, bot_id=None, message="24h loss 5100 exceeds 5% of peak bankroll (5000.00)", detail={"pnl_24h": "-5100"})
    assert drawdown is not None and drawdown.kind is AlertKind.MARGIN_CALL and "daily drawdown limit" in drawdown.title
    assert await watch_drawdown_block(redis, settings, user_id=user, bot_id=None, message="again", detail={}) is None


@pytest.mark.asyncio
async def test_a_broken_hash_chain_is_fatal_once(redis: Redis, settings: Settings) -> None:
    ok = {"ok": True, "rows": 10, "failures": [], "failures_total": 0, "head_seq": 10}
    broken = {"ok": False, "rows": 10, "head_seq": 10, "failures_total": 2, "failures": [{"seq": 4, "problem": "hash_mismatch", "detail": "row 4 was edited"}, {"seq": 5, "problem": "broken_link", "detail": ""}]}
    assert await watch_ledger(redis, settings, ok) is None
    fatal = await watch_ledger(redis, settings, broken)
    assert fatal is not None and fatal.severity is Severity.FATAL and fatal.kind is AlertKind.HASH_CHAIN_BROKEN and "#4: hash_mismatch" in fatal.body
    assert await watch_ledger(redis, settings, broken) is None  # the hourly re-verification does not page again


# ================================================================ the 08:00 hype engine
def forecast(fixtures: int, ev: str, steam: int = 0) -> MarketForecast:
    return MarketForecast(T0.date(), "Asia/Kolkata", fixtures, 9, D(ev), steam)


def test_the_hype_tiers_and_lines(settings: Settings) -> None:
    assert classify(forecast(6, "16"), settings) is Tier.BUSSIN
    assert classify(forecast(6, "8"), settings) is Tier.PRIMED
    assert classify(forecast(5, "40"), settings) is Tier.QUIET  # plenty of EV, too few fixtures (and no steam)
    assert classify(forecast(3, "1", steam=3), settings) is Tier.VOLATILE
    assert classify(forecast(12, "2"), settings) is Tier.QUIET
    assert "No cap, the EV today is bussin. Let's secure the bag. 💰" in HYPE_LINES[Tier.BUSSIN]
    assert "Market volatility is spiking. Good day to make profits, let's go make some! ⚡" in HYPE_LINES[Tier.VOLATILE]
    day = T0.date()
    first = pick_line(Tier.PRIMED, day, [])
    assert pick_line(Tier.PRIMED, day, []) == first  # deterministic for a day
    assert pick_line(Tier.PRIMED, day, [first[0]])[0] != first[0]  # but never yesterday's line again
    for tier, pool in HYPE_LINES.items():
        for template in pool:
            text = render(template, forecast(14, "23.44", steam=4))
            assert "{" not in text and len(text) <= 200, (tier, text)


@pytest.mark.asyncio
async def test_the_morning_forecast_reads_the_math_engine_and_sends_once_a_day(redis: Redis, settings: Settings) -> None:
    from app.core.live_odds import publish_board_ticks  # noqa: PLC0415
    from app.schemas.aryabhata import EdgeSignal  # noqa: PLC0415
    from app.schemas.market import MarketTick  # noqa: PLC0415
    from app.services.aryabhata_pipeline import AryabhataKeys  # noqa: PLC0415

    now = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)  # 08:00 in Kolkata
    kickoff = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)  # 19:30 IST, today
    ticks = [
        MarketTick(match_id=f"fx-{i}", home_team=f"Home {i}", away_team=f"Away {i}", market_type="Match Odds", selection="HOME", odds=D("2.0"), true_probability=D("0.5"),
                   is_suspended=False, sport_key="soccer_epl", commence_time=kickoff)
        for i in range(8)
    ] + [MarketTick(match_id="fx-tomorrow", home_team="A", away_team="B", market_type="Match Odds", selection="HOME", odds=D("2.0"), true_probability=D("0.5"), is_suspended=False, commence_time=kickoff + timedelta(days=1))]
    assert await publish_board_ticks(redis, ticks)
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    for i, ev in enumerate(["6.5", "5.0", "4.2", "2.0"]):
        signal = EdgeSignal(
            signal_id=uuid.uuid4(), fixture_id=f"fx-{i}", market_id=uuid.uuid4(), market_type="Match Odds", selection="HOME", home_team=f"Home {i}", away_team=f"Away {i}",
            commence_time=kickoff, bookmaker_id="pinnacle", source="odds_api", odds=D("2.30"), true_prob=D("0.47"), ev=D(ev) / 100, ev_percent=D(ev), full_kelly=D("0.05"),
            devig_method="shin", overround=D("0.05"), books=3, timestamp=now, expires_at=now + timedelta(minutes=5), is_steam_move=i == 0,
        )
        await redis.hset(keys.active, signal.key, signal.model_dump_json())
    preview = await market_forecast_hype(redis, settings, now=now, dry_run=True)
    assert preview["tier"] == Tier.BUSSIN and not preview["sent"]  # 8 fixtures, +17.7% EV: twice the bar
    assert preview["forecast"]["fixtures_today"] == 8 and preview["forecast"]["total_ev_pct"] == "17.70" and preview["forecast"]["steam_moves"] == 1
    assert "Home 0 v Away 0, HOME @ 2.30 (pinnacle, +6.5% EV)" in preview["body"]
    sent = await market_forecast_hype(redis, settings, now=now)
    assert sent["sent"] is True
    (hype,) = [a for a in await stream_alerts(redis, settings) if a.kind is AlertKind.MARKET_HYPE]
    assert hype.severity is Severity.INFO and hype.title == sent["title"] and channels_for(hype, DEFAULT_MATRIX) == [ChannelName.TELEGRAM, ChannelName.TWILIO]
    again = await market_forecast_hype(redis, settings, now=now + timedelta(minutes=1))  # the edges are still live
    assert again["sent"] is False and again["already_sent_today"]


def test_the_forecast_runs_at_eight_in_the_morning_local_time(settings: Settings) -> None:
    from app.core.celery_app import ZonedCrontab, celery_app  # noqa: PLC0415

    schedule = celery_app.conf.beat_schedule
    assert schedule["sentinel-dependency-health"]["schedule"] == 30.0 and schedule["sentinel-liveness-check"]["schedule"] == 5.0
    hype = schedule["sentinel-market-forecast-hype"]["schedule"]
    assert isinstance(hype, ZonedCrontab) and hype.zone == "Asia/Kolkata"
    start, delta, _ = hype.remaining_delta(datetime(2026, 10, 8, 2, 30, tzinfo=UTC))  # yesterday's 08:00 IST run
    assert start + delta == datetime(2026, 10, 9, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # next: 08:00 IST (02:30 UTC), not 08:00 UTC
    assert is_due(datetime(2026, 10, 9, 2, 30, tzinfo=UTC), settings) and not is_due(datetime(2026, 10, 9, 2, 29, tzinfo=UTC), settings)


# ================================================================ routing, the bus, the test suite's isolation
def test_the_routing_matrix() -> None:
    assert normalise(None) == {k: list(v) for k, v in DEFAULT_MATRIX.items()}
    custom = normalise({"CRITICAL": ["telegram"], "INFO": ["DISCORD", "BROWSER"]})
    assert custom["CRITICAL"] == ["TELEGRAM"] and custom["INFO"] == ["DISCORD", "BROWSER"] and custom["FATAL"] == DEFAULT_MATRIX["FATAL"]
    assert channels_for(alert(Severity.INFO), custom) == [ChannelName.DISCORD]  # the browser column is the frontend's
    assert validate({"CRITICAL": ["SLACK"], "LOUD": []}) == ["unknown row 'LOUD'", "unknown channel 'SLACK' in row 'CRITICAL'"]


@pytest.mark.asyncio
async def test_the_bus_fans_out_live_and_never_raises(redis: Redis, settings: Settings) -> None:
    pubsub = redis.pubsub()
    await pubsub.subscribe(SentinelKeys(settings).live)
    await pubsub.get_message(timeout=1)  # the subscribe confirmation
    item = alert(Severity.FATAL, kind=AlertKind.GARUDA_SILENT)
    assert await emit_alert(redis, settings, item)
    frame = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    assert frame is not None and json.loads(frame["data"])["alert"]["id"] == str(item.id)
    await pubsub.aclose()
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    assert await emit_alert(dead, settings, item) is False
    await dead.aclose()


def test_the_test_suite_can_only_reach_its_own_redis_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Group 68 gap patch: every Redis URL the app can read is forced onto the isolated index."""
    from tests import conftest  # noqa: PLC0415

    settings = get_settings()
    assert settings.REDIS_URL.get_secret_value() == TEST_REDIS_URL == settings.celery_broker_url.get_secret_value()
    assert conftest._redis_index(TEST_REDIS_URL) != 0  # noqa: SLF001
    assert os.environ.get(conftest.CLAIMED_ENV) == "1"  # the session claimed the index: leftovers of this run never skip a test
    monkeypatch.setenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    with pytest.raises(pytest.UsageError):
        conftest.isolated_redis_url()
    monkeypatch.setenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/?db=0")
    with pytest.raises(pytest.UsageError):
        conftest.isolated_redis_url()
