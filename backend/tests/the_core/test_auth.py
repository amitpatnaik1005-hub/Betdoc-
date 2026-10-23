"""Auth on the real /api/v1/core mount: every REST route needs a signed-in user, the writes (bootstrap, toggle,
stress-test, test, test-compare, backtest) an administrator; /core/live keeps its own handshake token check."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_current_user, get_db, get_ws_user
from app.api.v1 import the_core as core_api
from app.api.v1.router import api_router
from app.models import User
from app.models.the_core import BacktestJobModel, SmallcaseRegistryModel, SmallcaseStatus, TestBenchRunModel

BASE = "/api/v1/core"
READS = ("/status", "/smallcases", "/smallcases/{smallcase_id}", "/backtests")
WRITES = ("/bootstrap", "/smallcases/{smallcase_id}/toggle", "/smallcases/{smallcase_id}/stress-test", "/test", "/test-compare", "/backtest")


def _app(session_factory, orch, broadcaster) -> FastAPI:
    """The real api_router (its mount-level auth included) on The Crucible's sqlite tables and orchestrator."""
    app = FastAPI()
    app.include_router(api_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[core_api.get_session_factory] = lambda: session_factory
    app.dependency_overrides[core_api.get_orchestrator] = lambda: orch
    app.dependency_overrides[core_api.get_ws_manager] = lambda: broadcaster
    return app


def _sign_in(app: FastAPI, role: str) -> None:
    user = User(id=uuid.uuid4(), username=f"pratap_{uuid.uuid4().hex[:6]}", hashed_password="x", role=role)
    app.dependency_overrides[get_current_user] = lambda: user  # get_current_admin's role check still runs


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine-room.test")


def _reads(smallcase_id: str) -> list[str]:
    return ["/status", "/smallcases", f"/smallcases/{smallcase_id}", "/backtests"]


def _writes(smallcase_id: str, match_context: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """Valid bodies: without the gates every one of these would be accepted."""
    return [
        ("/bootstrap", None),
        (f"/smallcases/{smallcase_id}/toggle", {"expected_status": "ACTIVE", "target_status": "STANDBY"}),
        (f"/smallcases/{smallcase_id}/stress-test", None),
        ("/test", {"smallcase_id": smallcase_id, "match_context": match_context}),
        ("/test-compare", {"smallcase_ids": [smallcase_id], "match_context": match_context}),
        ("/backtest", {"smallcase_id": smallcase_id, "start_date": "2026-01-01", "end_date": "2026-01-31"}),
    ]


def _active(smallcases: list[SmallcaseRegistryModel]) -> SmallcaseRegistryModel:
    return next(s for s in smallcases if s.status == SmallcaseStatus.ACTIVE)


async def _rows(session_factory, model: type[Any]) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def test_every_core_rest_route_is_signed_in_only_in_openapi() -> None:
    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    core = {(path.removeprefix(BASE), method) for path, ops in paths.items() if path.startswith(f"{BASE}/") for method in ops}
    assert core == {(p, "get") for p in READS} | {(p, "post") for p in WRITES}  # a new Core route must be added here
    for path, method in sorted(core):
        assert paths[f"{BASE}{path}"][method].get("security"), path


@pytest.mark.asyncio
async def test_without_a_token_every_rest_route_answers_401(session_factory, orch, broadcaster, smallcases, match_context):
    smallcase_id = str(_active(smallcases).id)
    async with _client(_app(session_factory, orch, broadcaster)) as http:
        for path in _reads(smallcase_id):
            assert (await http.get(f"{BASE}{path}")).status_code == 401, path
        for path, body in _writes(smallcase_id, match_context):
            assert (await http.post(f"{BASE}{path}", json=body)).status_code == 401, path
    assert await _rows(session_factory, TestBenchRunModel) == 0
    assert await _rows(session_factory, BacktestJobModel) == 0


@pytest.mark.asyncio
async def test_a_signed_in_non_admin_reads_but_every_write_is_refused_403(session_factory, orch, broadcaster, smallcases, match_context, reload):
    active = _active(smallcases)
    smallcase_id = str(active.id)
    app = _app(session_factory, orch, broadcaster)
    _sign_in(app, role="QUANT")
    async with _client(app) as http:
        for path in _reads(smallcase_id):
            assert (await http.get(f"{BASE}{path}")).status_code == 200, path
        for path, body in _writes(smallcase_id, match_context):
            refused = await http.post(f"{BASE}{path}", json=body)
            assert refused.status_code == 403, path
            assert refused.json()["detail"] == "Admin privileges required"
    assert (await reload(SmallcaseRegistryModel, active.id)).status == SmallcaseStatus.ACTIVE  # the toggle never ran
    assert await _rows(session_factory, TestBenchRunModel) == 0  # no stress test, bench or compare run was queued
    assert await _rows(session_factory, BacktestJobModel) == 0
    assert broadcaster.messages == []


@pytest.mark.asyncio
async def test_an_admin_passes_the_gate(session_factory, orch, broadcaster):
    app = _app(session_factory, orch, broadcaster)
    _sign_in(app, role="ADMIN")
    async with _client(app) as http:
        booted = await http.post(f"{BASE}/bootstrap")
        assert booted.status_code == 200 and len(booted.json()) == 3
        active = next(s for s in booted.json() if s["status"] == "ACTIVE")
        toggled = await http.post(f"{BASE}/smallcases/{active['id']}/toggle", json={"expected_status": "ACTIVE", "target_status": "STANDBY"})
        assert toggled.status_code == 200 and toggled.json()["status"] == "STANDBY"


def test_the_live_websocket_keeps_its_handshake_token_check_on_the_real_mount():
    app = FastAPI()
    app.include_router(api_router)
    manager = core_api.ConnectionManager()
    app.dependency_overrides[core_api.get_ws_manager] = lambda: manager
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"{BASE}/live"):
            pass
    assert refused.value.code == 1008  # no ?token=: get_ws_user refuses the handshake

    app.dependency_overrides[get_ws_user] = lambda: None  # a valid ?token= stand-in; the REST mount's Depends must not run here
    with TestClient(app) as client, client.websocket_connect(f"{BASE}/live") as ws:
        assert ws.receive_json()["event"] == "engine.connected"
