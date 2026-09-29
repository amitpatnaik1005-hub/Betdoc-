"""HTTP tests for the Scout Oracle using httpx.AsyncClient + ASGITransport."""

from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/oracle-scout"


@pytest.mark.parametrize(
    ("page_context", "expected_phrase"),
    [
        ("/vault", "bankroll management"),
        ("/arena/live", "live odds"),
        ("/dashboard", "sharp betting"),
    ],
)
async def test_chat_returns_context_aware_response(client, page_context, expected_phrase):
    response = await client.post(
        f"{BASE}/chat",
        json={"page_context": page_context, "user_message": "What should I do next?", "user_id": str(uuid4())},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"response_text", "history_id"}
    assert expected_phrase in body["response_text"]
    UUID(body["history_id"])


async def test_chat_without_context_or_user_uses_default(client):
    response = await client.post(f"{BASE}/chat", json={"user_message": "Hello Scout"})
    assert response.status_code == 200
    assert "sharp betting" in response.json()["response_text"]

    history = await client.get(f"{BASE}/history")
    assert history.status_code == 200
    entries = history.json()
    assert len(entries) == 1
    assert entries[0]["page_context"] is None
    assert entries[0]["user_id"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"user_message": "   "},
        {"user_message": ""},
        {"page_context": "/vault"},
        {"user_message": "Hi", "page_context": "x" * 121},
        {"user_message": "Hi", "mood": "optimistic"},
        {"user_message": "Hi", "user_id": "not-a-uuid"},
    ],
)
async def test_chat_invalid_payloads_return_422(client, payload):
    response = await client.post(f"{BASE}/chat", json=payload)
    assert response.status_code == 422


async def test_history_empty_returns_200(client):
    response = await client.get(f"{BASE}/history", params={"user_id": str(uuid4())})
    assert response.status_code == 200
    assert response.json() == []


async def test_history_contains_chat_entries_for_user(client):
    user_id = str(uuid4())
    history_ids = []
    for context in ("/vault", "/arena", "/dashboard"):
        chat = await client.post(
            f"{BASE}/chat", json={"page_context": context, "user_message": f"Question from {context}", "user_id": user_id}
        )
        assert chat.status_code == 200
        history_ids.append(chat.json()["history_id"])

    response = await client.get(f"{BASE}/history", params={"user_id": user_id})
    assert response.status_code == 200
    entries = response.json()
    assert {entry["id"] for entry in entries} == set(history_ids)
    assert all(entry["user_id"] == user_id for entry in entries)
    assert all(entry["created_at"] for entry in entries)


async def test_history_is_chronological_with_limit(client, insert_history):
    user_id = uuid4()
    ids = await insert_history(6, user_id=user_id)

    response = await client.get(f"{BASE}/history", params={"user_id": str(user_id), "limit": 4})
    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()] == [str(i) for i in ids[2:]]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"user_id": "not-a-uuid"}])
async def test_history_invalid_query_returns_422(client, params):
    response = await client.get(f"{BASE}/history", params=params)
    assert response.status_code == 422
