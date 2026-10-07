import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_ws_user
from app.api.v1 import the_hive as hive_api
from app.api.v1.the_hive import ConnectionManager
from app.models.the_hive import LegendaryBot, TaskStatus
from app.schemas.the_hive import TaskEvent, TaskEventType


class FakeWebSocket:
    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay, self.fail = delay, fail
        self.accepted = False
        self.closed_code: int | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("socket gone")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(data)


def _ws(fake: FakeWebSocket) -> WebSocket:
    return cast(WebSocket, fake)


@pytest.mark.asyncio
async def test_broadcast_evicts_dead_and_slow_clients() -> None:
    manager = ConnectionManager(send_timeout=0.05)
    healthy, dead, slow = FakeWebSocket(), FakeWebSocket(fail=True), FakeWebSocket(delay=1.0)
    for fake in (healthy, dead, slow):
        assert await manager.connect(_ws(fake))
    assert manager.active_count == 3

    await manager.broadcast("tick")

    assert healthy.sent == ["tick"]
    assert manager.active_count == 1
    await manager.broadcast("tock")
    assert healthy.sent == ["tick", "tock"]


@pytest.mark.asyncio
async def test_connection_cap_rejects_with_1013() -> None:
    manager = ConnectionManager(max_connections=1)
    first, second = FakeWebSocket(), FakeWebSocket()
    assert await manager.connect(_ws(first)) is True
    assert await manager.connect(_ws(second)) is False
    assert second.accepted is False
    assert second.closed_code == 1013
    assert manager.active_count == 1


@pytest.mark.asyncio
async def test_publish_serializes_task_events() -> None:
    manager = ConnectionManager()
    fake = FakeWebSocket()
    await manager.connect(_ws(fake))
    task_id = uuid.uuid4()
    await manager.publish(
        [
            TaskEvent(
                event=TaskEventType.TASK_CLAIMED,
                task_id=task_id,
                status=TaskStatus.IN_PROGRESS,
                assignee_name=LegendaryBot.TODAR_MAL,
                occurred_at=datetime.now(UTC),
            )
        ]
    )
    payload = json.loads(fake.sent[0])
    assert payload["event"] == "TASK_CLAIMED"
    assert payload["task_id"] == str(task_id)
    assert payload["assignee_name"] == "TODAR MAL"


def _ws_app(monkeypatch: pytest.MonkeyPatch, manager: ConnectionManager) -> FastAPI:
    monkeypatch.setattr(hive_api, "manager", manager)
    app = FastAPI()
    app.include_router(hive_api.router)
    app.dependency_overrides[get_ws_user] = lambda: None  # authenticated user stand-in
    return app


def test_live_board_ping_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ConnectionManager()
    with TestClient(_ws_app(monkeypatch, manager)) as test_client:
        with test_client.websocket_connect("/hive/board/live") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"event": "pong"}
            assert manager.active_count == 1


def test_live_board_rejects_over_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ConnectionManager(max_connections=0)
    with TestClient(_ws_app(monkeypatch, manager)) as test_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect("/hive/board/live"):
                pass
    assert exc_info.value.code == 1013


def test_live_board_rejects_unauthenticated_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _ws_app(monkeypatch, ConnectionManager())
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect("/hive/board/live"):
                pass
    assert exc_info.value.code == 1008
