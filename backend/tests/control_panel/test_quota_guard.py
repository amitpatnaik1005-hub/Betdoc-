"""Group 92: BHEESHMA's quota guard, the cache shield's effect on the fleet cadence, and its API.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin
from app.core.config import get_settings
from app.core.omni_keys import OmniRedisKeys
from app.domain.control_panel.quota_guard_math import bucket_tokens_now, evaluate_source, parse_shield_mode, shield_engaged
from app.models.omni_vault import OmniAuthStrategy, OmniFleetSource, OmniProviderConfig
from app.services.omni_fleet import fleet_bucket_key, read_shield_mode, tick_interval

from .conftest import app

BASE = "/api/v1/control-panel"
DEVELOPER = "Amit Ashok Kumar Patnaik"
SETTINGS = get_settings()
KEYS = OmniRedisKeys(SETTINGS.omni_redis_prefix)


class StubRedis:
    """The Redis calls the quota guard and the shield read make, in memory."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.published: list[str] = []

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, *keys: str) -> int:
        hit = 0
        for key in keys:
            hit += int(key in self.kv or key in self.hashes or key in self.ttls)
            self.kv.pop(key, None)
            self.hashes.pop(key, None)
            self.ttls.pop(key, None)
        return hit

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})
        return len(mapping)

    async def pttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.kv or k in self.hashes or k in self.ttls)

    async def publish(self, _channel: str, message: str) -> int:
        self.published.append(message)
        return 1


def _eval(**overrides: Any):  # noqa: ANN202
    args: dict[str, Any] = {
        "source_id": "odds_api", "kind": "fleet", "cost": "metered", "schedule": "enabled", "breaker_remaining_seconds": None,
        "quota_fraction": 0.65, "tokens_available": 8.0, "capacity": 10.0, "base_interval_seconds": 60.0,
        "reserve_fraction": 0.05, "warn_fraction": 0.20, "shield_multiplier": 4.0, "mode": "AUTO",
    }
    return evaluate_source(**(args | overrides))


# --------------------------------------------------------------------------- math
def test_guard_states_and_the_polling_the_fleet_will_do() -> None:
    healthy = _eval()
    assert (healthy.guard_state, healthy.polling_mode, healthy.effective_interval_seconds) == ("HEALTHY", "LIVE_FULL", 60.0)
    assert healthy.remaining_pct == 65.0 and healthy.used_pct == 35.0 and healthy.bucket_utilization_pct == 20.0

    warn = _eval(quota_fraction=0.15)
    assert (warn.guard_state, warn.polling_mode, warn.shield_engaged) == ("WARNING_CACHE_SHIELD", "CACHE_SHIELD", True)
    assert warn.effective_interval_seconds == 240.0 and "polling every 240s" in warn.verdict_summary

    reserve = _eval(quota_fraction=0.04)
    assert (reserve.guard_state, reserve.polling_mode) == ("CRITICAL_LOCKED", "RESERVE_FAILOVER")

    unmetered = _eval(quota_fraction=None)
    assert (unmetered.guard_state, unmetered.polling_mode, unmetered.remaining_pct) == ("UNMETERED", "LIVE_FULL", None)
    assert _eval(quota_fraction=1.7).guard_state == "UNMETERED"  # an impossible reading is not a measurement

    breaker = _eval(breaker_remaining_seconds=45.0)
    assert (breaker.guard_state, breaker.polling_mode) == ("CRITICAL_LOCKED", "BREAKER_HOLD")
    assert _eval(schedule="needs_key").polling_mode == "NOT_POLLING"


def test_operator_modes_never_override_the_reserve_or_touch_free_sources() -> None:
    overridden = _eval(quota_fraction=0.15, mode="LIVE_UNRESTRICTED")
    assert (overridden.polling_mode, overridden.shield_engaged, overridden.effective_interval_seconds) == ("LIVE_FULL", False, 60.0)
    assert "overridden" in overridden.verdict_summary
    assert _eval(quota_fraction=0.03, mode="LIVE_UNRESTRICTED").polling_mode == "RESERVE_FAILOVER"

    forced = _eval(mode="FORCE_CACHE_SHIELD")
    assert forced.polling_mode == "CACHE_SHIELD" and "forced by operator" in forced.verdict_summary
    assert _eval(cost="free", quota_fraction=None, mode="FORCE_CACHE_SHIELD").polling_mode == "LIVE_FULL"
    assert _eval(kind="router", cost=None, quota_fraction=None, mode="FORCE_CACHE_SHIELD", base_interval_seconds=None).shield_engaged is False
    assert shield_engaged(cost="metered", quota_fraction=None, mode="AUTO", warn_fraction=0.2) is False  # unmetered: nothing to protect yet


def test_bucket_level_is_refilled_to_now_and_capped() -> None:
    assert bucket_tokens_now(stored_tokens=0.5, stamp=100.0, now=102.0, refill_per_second=0.5, capacity=10) == 1.5
    assert bucket_tokens_now(stored_tokens=9.0, stamp=0.0, now=1_000.0, refill_per_second=1.0, capacity=10) == 10.0
    assert bucket_tokens_now(stored_tokens=None, stamp=None, now=1.0, refill_per_second=1.0, capacity=10) is None
    saturated = _eval(tokens_available=0.4)
    assert saturated.is_throttled and saturated.verdict_summary.endswith("rate limiter saturated")
    assert parse_shield_mode(b"FORCE_CACHE_SHIELD") == "FORCE_CACHE_SHIELD" and parse_shield_mode("nonsense") == "AUTO"


@pytest.mark.asyncio
async def test_the_fleet_beat_applies_the_stored_shield_mode() -> None:
    redis = StubRedis()
    metered = SimpleNamespace(cost="metered", interval_seconds=60.0)
    free = SimpleNamespace(cost="free", interval_seconds=30.0)
    multiplier = SETTINGS.QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER

    assert await read_shield_mode(redis, SETTINGS) == "AUTO" and await read_shield_mode(None, SETTINGS) == "AUTO"
    assert tick_interval(metered, 0.10, "AUTO", SETTINGS) == 60.0 * multiplier  # type: ignore[arg-type]
    assert tick_interval(metered, 0.90, "AUTO", SETTINGS) == 60.0  # type: ignore[arg-type]

    await redis.hset(SETTINGS.QUOTA_GUARD_SHIELD_MODE_KEY, mapping={"mode": "FORCE_CACHE_SHIELD"})
    mode = await read_shield_mode(redis, SETTINGS)
    assert mode == "FORCE_CACHE_SHIELD"
    assert tick_interval(metered, 0.90, mode, SETTINGS) == 60.0 * multiplier  # type: ignore[arg-type]
    assert tick_interval(free, None, mode, SETTINGS) == 30.0  # type: ignore[arg-type]
    assert tick_interval(metered, 0.10, "LIVE_UNRESTRICTED", SETTINGS) == 60.0  # type: ignore[arg-type]


# --------------------------------------------------------------------------- API
@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> StubRedis:
    stub = StubRedis()
    monkeypatch.setattr(app.state, "redis", stub, raising=False)
    return stub


@pytest_asyncio.fixture
async def admin_client(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(username="desk-admin", role="admin")
    yield client  # the conftest client clears every override on teardown


async def _seed(db: AsyncSession, redis: StubRedis) -> OmniProviderConfig:
    # A stored (never decrypted) key schedules odds_api whatever the local .env holds
    db.add(OmniFleetSource(source_id="odds_api", is_enabled=True, encrypted_api_key="sealed", consecutive_failures=3))
    router = OmniProviderConfig(
        provider_name="sportmonks", category_code="A", base_url="https://api.example.test", auth_strategy=OmniAuthStrategy.HEADER,
        requests_per_minute=60, is_active=True,
    )
    db.add_all([router, OmniProviderConfig(
        provider_name="coingecko", category_code="H", base_url="https://api.example.test", auth_strategy=OmniAuthStrategy.HEADER,
        requests_per_minute=30, is_active=True,
    )])
    await db.commit()
    await db.refresh(router)

    redis.hashes[KEYS.fleet_metrics("odds_api")] = {
        "quota_fraction": "0.14", "quota_remaining": "70", "quota_used": "430", "quota_limit": "500",
        "latency_ms": "142", "consecutive_failures": "3",
    }
    # Stamped ahead of now so no refill accrues during the test: 0.2 tokens stays 0.2
    redis.hashes[fleet_bucket_key(SETTINGS, "odds_api")] = {"tokens": "0.2", "ts": str(time.time() + 600)}
    redis.ttls[KEYS.breaker_open("odds_api")] = 45_000
    redis.kv[KEYS.breaker_half_open("odds_api")] = "1"
    minute = int(time.time() // 60)
    for window in (minute, minute + 1):  # the request may cross a minute boundary
        redis.kv[KEYS.rate_window(router.id, window)] = "45"
    return router


def _provider(body: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(p for p in body["providers"] if p["source_id"] == source_id)


@pytest.mark.asyncio
async def test_dashboard_reports_measured_state(admin_client: AsyncClient, db_session: AsyncSession, redis: StubRedis) -> None:
    await _seed(db_session, redis)
    resp = await admin_client.get(f"{BASE}/quota-guard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["architect"] == DEVELOPER and body["redis_limiter_enforcing"] is True
    assert body["shield"]["mode"] == "AUTO" and body["global_rpm_limit"] == SETTINGS.RATE_LIMIT_GLOBAL_RPM
    assert body["excluded_sources"] == ["coingecko"]
    assert {p["source_id"] for p in body["providers"]} >= {"odds_api", "polymarket", "sportmonks"}

    odds = _provider(body, "odds_api")
    assert (odds["guard_state"], odds["polling_mode"], odds["breaker_state"]) == ("CRITICAL_LOCKED", "BREAKER_HOLD", "open")
    assert odds["breaker_remaining_seconds"] == 45.0 and odds["is_throttled"] is True and odds["tokens_available"] == 0.2
    assert (odds["remaining_pct"], odds["quota_remaining"], odds["quota_limit"]) == (14.0, 70.0, 500.0)
    assert odds["consecutive_failures"] == 3 and odds["last_latency_ms"] == 142 and odds["kind"] == "fleet"

    poly = _provider(body, "polymarket")  # free, no quota headers, bucket never drawn: full
    assert poly["guard_state"] == "UNMETERED" and poly["tokens_available"] == float(poly["burst_capacity"])

    router = _provider(body, "sportmonks")
    assert router["kind"] == "router" and router["category"] == "A" and router["tokens_available"] == 15.0 and router["bucket_utilization_pct"] == 75.0
    assert body["critical_sources"] == 1 and body["throttled_sources"] == 1 and body["cache_shield_active"] is False


@pytest.mark.asyncio
async def test_reset_then_shield_modes_drive_the_cadence(admin_client: AsyncClient, db_session: AsyncSession, redis: StubRedis) -> None:
    await _seed(db_session, redis)
    reset = await admin_client.post(f"{BASE}/quota-guard/reset-bucket", json={"source_id": "odds_api"})
    assert reset.status_code == 200
    odds = _provider(reset.json(), "odds_api")
    assert odds["breaker_state"] == "closed" and odds["consecutive_failures"] == 0 and odds["is_throttled"] is False
    assert odds["tokens_available"] == float(odds["burst_capacity"])  # refilled
    assert (odds["guard_state"], odds["polling_mode"], odds["shield_engaged"]) == ("WARNING_CACHE_SHIELD", "CACHE_SHIELD", True)
    assert odds["effective_interval_seconds"] == odds["base_interval_seconds"] * SETTINGS.QUOTA_GUARD_SHIELD_INTERVAL_MULTIPLIER
    assert reset.json()["cache_shield_active"] is True and reset.json()["shielded_sources"] == 1
    row = await db_session.get(OmniFleetSource, "odds_api")
    await db_session.refresh(row)
    assert row is not None and row.consecutive_failures == 0 and row.paused_at is None

    live = await admin_client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "LIVE_UNRESTRICTED", "reason": "Derby day: full cadence."})
    assert live.status_code == 200
    body = live.json()
    assert body["shield"]["mode"] == "LIVE_UNRESTRICTED" and body["shield"]["set_by"] == "desk-admin"
    assert body["shield"]["reason"] == "Derby day: full cadence." and body["shield"]["set_at"]
    assert _provider(body, "odds_api")["polling_mode"] == "LIVE_FULL" and body["cache_shield_active"] is False
    assert await read_shield_mode(redis, SETTINGS) == "LIVE_UNRESTRICTED"  # what the fleet beat will apply

    redis.hashes[KEYS.fleet_metrics("odds_api")]["quota_fraction"] = "0.03"
    reserve = _provider((await admin_client.get(f"{BASE}/quota-guard")).json(), "odds_api")
    assert reserve["polling_mode"] == "RESERVE_FAILOVER"  # the reserve holds even in live unrestricted

    redis.hashes[KEYS.fleet_metrics("odds_api")]["quota_fraction"] = "0.9"
    forced = (await admin_client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "FORCE_CACHE_SHIELD"})).json()
    assert _provider(forced, "odds_api")["polling_mode"] == "CACHE_SHIELD"
    assert _provider(forced, "polymarket")["polling_mode"] == "LIVE_FULL"  # free: nothing to protect
    assert any('"quota-guard/mode"' in m or "/quota-guard/mode" in m for m in redis.published)


@pytest.mark.asyncio
async def test_writes_need_an_admin_and_a_known_source(client: AsyncClient, db_session: AsyncSession, redis: StubRedis) -> None:
    await _seed(db_session, redis)
    assert (await client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "AUTO"})).status_code == 401
    assert (await client.post(f"{BASE}/quota-guard/reset-bucket", json={"source_id": "odds_api"})).status_code == 401

    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(username="desk-admin", role="admin")
    assert (await client.post(f"{BASE}/quota-guard/reset-bucket", json={"source_id": "nope"})).status_code == 404
    assert (await client.post(f"{BASE}/quota-guard/reset-bucket", json={"source_id": "coingecko"})).status_code == 404
    assert (await client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "TURBO"})).status_code == 422
    assert (await client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "AUTO", "triggered_by": "x"})).status_code == 422


@pytest.mark.asyncio
async def test_without_redis_nothing_is_guessed_and_the_mode_is_refused(admin_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    db_session.add(OmniFleetSource(source_id="odds_api", is_enabled=True, encrypted_api_key="sealed"))
    await db_session.commit()

    body = (await admin_client.get(f"{BASE}/quota-guard")).json()
    assert body["redis_limiter_enforcing"] is False and body["shield"]["mode"] == "AUTO"
    odds = _provider(body, "odds_api")
    assert odds["tokens_available"] is None and odds["guard_state"] == "UNMETERED" and odds["remaining_pct"] is None

    refused = await admin_client.post(f"{BASE}/quota-guard/mode", json={"shield_mode": "FORCE_CACHE_SHIELD"})
    assert refused.status_code == 503 and "would not take effect" in refused.json()["detail"]
    assert (await admin_client.post(f"{BASE}/quota-guard/reset-bucket", json={"source_id": "odds_api"})).status_code == 503
