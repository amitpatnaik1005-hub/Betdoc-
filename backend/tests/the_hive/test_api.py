import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, get_db
from app.api.v1 import the_hive as hive_api
from app.schemas.the_hive import TaskEvent, TaskEventType


class RecordingManager:
    """Stands in for ConnectionManager; captures every published event."""

    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    async def publish(self, events: Sequence[TaskEvent]) -> None:
        self.events.extend(events)

    def types(self) -> list[TaskEventType]:
        return [event.event for event in self.events]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> RecordingManager:
    recording = RecordingManager()
    monkeypatch.setattr(hive_api, "manager", recording)
    return recording


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], recorder: RecordingManager
) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    auth_dependency = CurrentUser.__metadata__[0].dependency
    app = FastAPI()
    app.include_router(hive_api.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[auth_dependency] = lambda: SimpleNamespace(id=uuid.uuid4())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _new_task(client: AsyncClient, **overrides: Any) -> str:
    body = {"title": "Scan Pinnacle vs Betfair", "priority": 50, **overrides}
    response = await client.post("/hive/tasks", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.asyncio
async def test_create_task_returns_201_and_broadcasts(client: AsyncClient, recorder: RecordingManager) -> None:
    task_id = await _new_task(client, payload={"market": "MATCH_WINNER_1X2"})
    fetched = await client.get(f"/hive/tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "BACKLOG"
    assert fetched.json()["parent_task_ids"] == []
    assert recorder.types() == [TaskEventType.TASK_CREATED]


@pytest.mark.asyncio
async def test_create_task_validation(client: AsyncClient) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (await client.post("/hive/tasks", json={"title": "t", "expires_at": past})).status_code == 422
    assert (await client.post("/hive/tasks", json={"title": "t", "priority": 101})).status_code == 422
    assert (await client.post("/hive/tasks", json={"title": "t", "status": "DONE"})).status_code == 422
    assert (await client.get("/hive/tasks", params={"limit": 0})).status_code == 422


@pytest.mark.asyncio
async def test_heartbeat_handles_multi_word_bot_name(client: AsyncClient) -> None:
    body = {"status": "ONLINE", "uptime_seconds": 120, "resource_metrics": {"cpu": 0.42}}
    response = await client.post(f"/hive/bots/{quote('TODAR MAL')}/heartbeat", json=body)
    assert response.status_code == 200
    assert response.json()["bot_name"] == "TODAR MAL"
    assert response.json()["resource_metrics"] == {"cpu": 0.42}
    assert (await client.post("/hive/bots/RAVANA/heartbeat", json=body)).status_code == 422


@pytest.mark.asyncio
async def test_dependency_errors_map_to_http(client: AsyncClient) -> None:
    a = await _new_task(client, title="A")
    b = await _new_task(client, title="B")
    linked = await client.post(f"/hive/tasks/{b}/dependencies", json={"parent_task_id": a})
    assert linked.status_code == 201
    assert linked.json()["parent_task_ids"] == [a]

    cycle = await client.post(f"/hive/tasks/{a}/dependencies", json={"parent_task_id": b})
    assert cycle.status_code == 409

    missing = await client.post(f"/hive/tasks/{a}/dependencies", json={"parent_task_id": str(uuid.uuid4())})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_claim_complete_flow(client: AsyncClient, recorder: RecordingManager) -> None:
    task_id = await _new_task(client)

    claimed = await client.post("/hive/tasks/claim", json={"bot_name": "SHIVAJI"})
    assert claimed.status_code == 200
    assert claimed.json()["id"] == task_id
    assert claimed.json()["assignee_name"] == "SHIVAJI"

    empty = await client.post("/hive/tasks/claim", json={"bot_name": "GARUDA"})
    assert empty.status_code == 204

    intruder = await client.patch(f"/hive/tasks/{task_id}/complete", json={"bot_name": "CHANAKYA"})
    assert intruder.status_code == 409

    done = await client.patch(
        f"/hive/tasks/{task_id}/complete", json={"bot_name": "SHIVAJI", "result_payload": {"arb_pct": 1.7}}
    )
    assert done.status_code == 200
    assert done.json()["status"] == "DONE"
    assert done.json()["result_payload"] == {"arb_pct": 1.7}

    assert recorder.types() == [
        TaskEventType.TASK_CREATED,
        TaskEventType.TASK_CLAIMED,
        TaskEventType.TASK_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_fail_retry_then_cascade(client: AsyncClient, recorder: RecordingManager) -> None:
    parent = await _new_task(client, title="flaky", max_retries=1, priority=90)
    child = await _new_task(client, title="dependent", priority=10)
    assert (await client.post(f"/hive/tasks/{child}/dependencies", json={"parent_task_id": parent})).status_code == 201

    await client.post("/hive/tasks/claim", json={"bot_name": "KARNA"})
    first = await client.patch(f"/hive/tasks/{parent}/fail", json={"bot_name": "KARNA", "error": {"code": 503}})
    assert first.status_code == 200
    assert first.json()["retried"] is True
    assert first.json()["task"]["status"] == "BACKLOG"
    assert first.json()["task"]["assignee_name"] is None

    await client.post("/hive/tasks/claim", json={"bot_name": "KARNA"})
    second = await client.patch(f"/hive/tasks/{parent}/fail", json={"bot_name": "KARNA", "error": {"code": 503}})
    assert second.status_code == 200
    assert second.json()["retried"] is False
    assert second.json()["task"]["status"] == "FAILED"
    assert second.json()["cascaded_task_ids"] == [child]

    assert (await client.get(f"/hive/tasks/{child}")).json()["status"] == "FAILED"
    assert TaskEventType.TASK_RETRY_SCHEDULED in recorder.types()
    assert TaskEventType.TASK_FAILED in recorder.types()
    assert TaskEventType.TASK_CASCADE_FAILED in recorder.types()

    not_owner = await client.patch(f"/hive/tasks/{parent}/fail", json={"bot_name": "KARNA", "error": {}})
    assert not_owner.status_code == 409


@pytest.mark.asyncio
async def test_learning_log_endpoint(client: AsyncClient) -> None:
    body = {
        "bot_name": "DEVRAYA",
        "parameter_name": "kelly_multiplier",
        "old_value": 0.25,
        "new_value": 0.2,
        "reasoning": "Backtest drawdown breached tolerance",
        "confidence_score": 0.87,
    }
    created = await client.post("/hive/learning-logs", json=body)
    assert created.status_code == 201
    assert created.json()["new_value"] == 0.2

    assert (await client.post("/hive/learning-logs", json={**body, "confidence_score": 1.5})).status_code == 422
