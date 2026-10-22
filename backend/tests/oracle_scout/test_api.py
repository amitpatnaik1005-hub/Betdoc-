"""HTTP tests for the Scout Oracle using httpx.AsyncClient + ASGITransport."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/oracle-scout"


def _auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, timedelta(minutes=5))}"}


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
    assert set(body) == {"response_text", "history_id", "actions", "source"} and body["source"] == "quant_nlg"
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
    response = await client.get(f"{BASE}/history", headers=_auth(uuid4()))
    assert response.status_code == 200
    assert response.json() == []


async def test_history_contains_chat_entries_for_user(client):
    user_id = uuid4()
    history_ids = []
    for context in ("/vault", "/arena", "/dashboard"):
        chat = await client.post(
            f"{BASE}/chat",
            json={"page_context": context, "user_message": f"Question from {context}"},
            headers=_auth(user_id),
        )
        assert chat.status_code == 200
        history_ids.append(chat.json()["history_id"])

    # The caller's own id in the query is still accepted, and gives the same answer as no id at all.
    for params in ({}, {"user_id": str(user_id)}):
        response = await client.get(f"{BASE}/history", params=params, headers=_auth(user_id))
        assert response.status_code == 200
        entries = response.json()
        assert {entry["id"] for entry in entries} == set(history_ids)
        assert all(entry["user_id"] == str(user_id) for entry in entries)
        assert all(entry["created_at"] for entry in entries)


async def test_history_is_chronological_with_limit(client, insert_history):
    user_id = uuid4()
    ids = await insert_history(6, user_id=user_id)

    response = await client.get(f"{BASE}/history", params={"limit": 4}, headers=_auth(user_id))
    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()] == [str(i) for i in ids[2:]]


async def test_history_cannot_read_another_users_rows(client, insert_history):
    victim, attacker = uuid4(), uuid4()
    await insert_history(3, user_id=victim)

    as_attacker = await client.get(f"{BASE}/history", params={"user_id": str(victim)}, headers=_auth(attacker))
    assert as_attacker.status_code == 403
    anonymous = await client.get(f"{BASE}/history", params={"user_id": str(victim)})
    assert anonymous.status_code == 403

    own = await client.get(f"{BASE}/history", headers=_auth(attacker))
    assert own.status_code == 200 and own.json() == []
    anonymous_bucket = await client.get(f"{BASE}/history")
    assert anonymous_bucket.status_code == 200 and anonymous_bucket.json() == []


async def test_history_with_invalid_token_is_anonymous(client, insert_history):
    victim = uuid4()
    await insert_history(2, user_id=victim)
    headers = {"Authorization": "Bearer not-a-real-token"}

    assert (await client.get(f"{BASE}/history", params={"user_id": str(victim)}, headers=headers)).status_code == 403
    response = await client.get(f"{BASE}/history", headers=headers)
    assert response.status_code == 200 and response.json() == []


async def test_anonymous_chat_ignores_payload_user_id(client):
    victim = uuid4()
    chat = await client.post(f"{BASE}/chat", json={"user_message": "Planting a note", "user_id": str(victim)})
    assert chat.status_code == 200

    victim_view = await client.get(f"{BASE}/history", headers=_auth(victim))
    assert victim_view.status_code == 200 and victim_view.json() == []

    anonymous = await client.get(f"{BASE}/history")
    assert [entry["id"] for entry in anonymous.json()] == [chat.json()["history_id"]]
    assert anonymous.json()[0]["user_id"] is None


async def test_authenticated_chat_ignores_payload_user_id(client):
    caller, victim = uuid4(), uuid4()
    chat = await client.post(
        f"{BASE}/chat", json={"user_message": "Who am I?", "user_id": str(victim)}, headers=_auth(caller)
    )
    assert chat.status_code == 200

    assert (await client.get(f"{BASE}/history", headers=_auth(victim))).json() == []
    entries = (await client.get(f"{BASE}/history", headers=_auth(caller))).json()
    assert [entry["user_id"] for entry in entries] == [str(caller)]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"user_id": "not-a-uuid"}])
async def test_history_invalid_query_returns_422(client, params):
    response = await client.get(f"{BASE}/history", params=params)
    assert response.status_code == 422
