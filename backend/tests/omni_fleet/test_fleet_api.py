"""Phase 5 backend: Fleet Command's API (reads for users, writes for admins, write-only keys)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user, get_db
from app.api.v1 import omni_fleet as fleet_api
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import OmniFleetSource
from app.services import omni_fleet
from app.services.omni_fleet import FleetDeps


@dataclass
class FakeUser:
    username: str
    role: str
    is_active: bool = True


@pytest_asyncio.fixture
async def make_client(redis, session_factory, http, fleet_settings):
    clients: list[AsyncClient] = []

    async def build(role: str = "ADMIN", **overrides) -> tuple[AsyncClient, FastAPI]:
        settings = fleet_settings(**overrides)
        app = FastAPI()
        app.include_router(fleet_api.router, prefix="/api/v1")
        vault = VaultCrypto(Fernet.generate_key().decode())
        app.state.settings, app.state.redis, app.state.vault = settings, redis, vault
        app.state.fleet_deps = FleetDeps(redis=redis, session_factory=session_factory, http=http, vault=vault, settings=settings)

        async def db():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_current_user] = lambda: FakeUser("ops", role)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
        clients.append(client)
        return client, app

    yield build
    for client in clients:
        await client.aclose()


async def test_overview_lists_every_source_with_its_health(make_client) -> None:
    client, _ = await make_client(ODDS_API_KEY=None)
    body = (await client.get("/api/v1/omni/fleet")).json()
    sources = {s["source_id"]: s for s in body["sources"]}
    assert set(sources) == {"polymarket", "odds_api"}
    assert sources["polymarket"]["status"] == "IDLE" and sources["polymarket"]["requires_api_key"] is False
    assert sources["odds_api"]["status"] == "NEEDS_KEY" and sources["odds_api"]["has_api_key"] is False
    assert body["mode"] == "inprocess" and body["redis_available"] is True and body["vault_configured"] is True


async def test_overview_reflects_a_successful_run(make_client) -> None:
    client, _ = await make_client()
    assert (await client.post("/api/v1/omni/fleet/polymarket/run")).json()["dispatched_to"] == "manual"
    # Let the in-process run finish first: the in-memory SQLite engine shares one connection, so a
    # concurrent request's rollback would interleave with the run's transaction (Postgres doesn't).
    await asyncio.wait_for(asyncio.gather(*list(omni_fleet._background)), timeout=10)
    status = next(s for s in (await client.get("/api/v1/omni/fleet")).json()["sources"] if s["source_id"] == "polymarket")
    assert status["status"] == "HEALTHY"
    assert status["success_rate"] == 1.0 and status["ticks_last_run"] == 3 and status["ping_ms"] is not None
    assert status["last_success_at"] is not None


async def test_api_key_is_write_only_and_encrypted(make_client, session_factory) -> None:
    client, _ = await make_client(ODDS_API_KEY=None)
    secret = "0123456789abcdef0123456789abcdef"
    response = await client.put("/api/v1/omni/fleet/odds_api/api-key", json={"api_key": secret})
    assert response.status_code == 200
    body = response.json()
    assert secret not in response.text
    assert body["has_api_key"] and body["key_origin"] == "vault" and body["api_key_hint"].endswith("cdef")
    assert body["status"] == "IDLE"  # no longer waiting for a key
    async with session_factory() as session:
        row = await session.get(OmniFleetSource, "odds_api")
        assert row.encrypted_api_key and secret not in row.encrypted_api_key

    cleared = (await client.delete("/api/v1/omni/fleet/odds_api/api-key")).json()
    assert not cleared["has_api_key"] and cleared["status"] == "NEEDS_KEY"


async def test_short_keys_are_rejected(make_client) -> None:
    client, _ = await make_client()
    assert (await client.put("/api/v1/omni/fleet/odds_api/api-key", json={"api_key": "short"})).status_code == 422


async def test_toggle_disables_and_reenabling_clears_a_fatal_pause(make_client, redis, session_factory) -> None:
    client, app = await make_client()
    off = (await client.put("/api/v1/omni/fleet/polymarket", json={"is_enabled": False})).json()
    assert off["status"] == "DISABLED"
    keys = OmniRedisKeys(app.state.settings.omni_redis_prefix)
    assert await redis.hget(keys.fleet_metrics("polymarket"), "state") == "disabled"  # the beat tick sees it at once

    async with session_factory() as session:
        row = await session.get(OmniFleetSource, "polymarket")
        row.is_enabled, row.paused_at, row.consecutive_failures, row.last_error = True, datetime.now(UTC), 3, "boom"
        await session.commit()
    assert next(s for s in (await client.get("/api/v1/omni/fleet")).json()["sources"] if s["source_id"] == "polymarket")["status"] == "FATAL"

    on = (await client.put("/api/v1/omni/fleet/polymarket", json={"is_enabled": True})).json()
    assert on["status"] != "FATAL" and on["consecutive_failures"] == 0 and on["paused_at"] is None


async def test_interval_override_and_reset(make_client) -> None:
    client, _ = await make_client()
    assert (await client.put("/api/v1/omni/fleet/polymarket", json={"interval_seconds": 120})).json()["interval_seconds"] == 120
    reset = (await client.put("/api/v1/omni/fleet/polymarket", json={"interval_seconds": None})).json()
    assert reset["interval_seconds"] == reset["default_interval_seconds"]
    assert (await client.put("/api/v1/omni/fleet/polymarket", json={"interval_seconds": 1})).status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/api/v1/omni/fleet/polymarket", {"is_enabled": False}),
        ("put", "/api/v1/omni/fleet/odds_api/api-key", {"api_key": "0123456789abcdef"}),
        ("delete", "/api/v1/omni/fleet/odds_api/api-key", None),
        ("post", "/api/v1/omni/fleet/polymarket/run", None),
    ],
)
async def test_writes_need_an_admin(make_client, method, path, body) -> None:
    client, _ = await make_client(role="QUANT")
    response = await client.request(method, path, json=body)
    assert response.status_code == 403
    assert (await client.get("/api/v1/omni/fleet")).status_code == 200  # reads stay open to every user


async def test_unknown_source_is_404(make_client) -> None:
    client, _ = await make_client()
    assert (await client.put("/api/v1/omni/fleet/scraper", json={"is_enabled": True})).status_code == 404


async def test_dead_letter_listing(make_client, redis) -> None:
    client, app = await make_client()
    keys = OmniRedisKeys(app.state.settings.omni_redis_prefix)
    await redis.lpush(keys.fleet_deadletter(), '{"source_id":"polymarket","status":"FATAL","failures":3,"error":"HTTP 503","at":"2026-10-08T10:00:00+00:00"}')
    entries = (await client.get("/api/v1/omni/fleet/deadletter")).json()
    assert entries[0]["source_id"] == "polymarket" and entries[0]["failures"] == 3
