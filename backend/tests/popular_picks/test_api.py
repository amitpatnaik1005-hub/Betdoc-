"""HTTP tests for Oracle Popular Picks using httpx.AsyncClient + ASGITransport."""

from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/popular-picks"


async def _first_parlay_id(client) -> str:
    response = await client.get(BASE)
    assert response.status_code == 200
    return response.json()[0]["id"]


async def test_list_seeds_and_returns_active_picks(client):
    response = await client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert {p["pick_type"] for p in body} == {"TRENDING", "AI_PREDICTED", "SHARP_MONEY"}
    for parlay in body:
        assert parlay["is_active"] is True
        assert parlay["total_odds"] >= 1.0
        assert 0.0 <= parlay["historical_success_rate"] <= 1.0
        assert len(parlay["legs"]) >= 2
        assert set(parlay["legs"][0]) == {"match_id", "selection", "odds"}


async def test_list_is_stable_across_calls(client):
    first = {p["id"] for p in (await client.get(BASE)).json()}
    second = {p["id"] for p in (await client.get(BASE)).json()}
    assert first == second
    assert len(second) == 3


async def test_list_returns_existing_picks_without_seeding(client, make_parlay):
    parlay_id = await make_parlay(title="Sharp Custom", pick_type="SHARP_MONEY")
    body = (await client.get(BASE)).json()
    assert [p["id"] for p in body] == [str(parlay_id)]


@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED", "MODIFIED"])
async def test_review_valid_decision_returns_200(client, decision):
    parlay_id = await _first_parlay_id(client)
    response = await client.post(f"{BASE}/{parlay_id}/review", json={"decision": decision})
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == decision
    assert body["parlay_id"] == parlay_id
    assert body["user_id"] is None
    assert body["created_at"]
    UUID(body["id"])


async def test_review_unknown_parlay_returns_404(client):
    missing_id = uuid4()
    response = await client.post(f"{BASE}/{missing_id}/review", json={"decision": "ACCEPTED"})
    assert response.status_code == 404
    assert str(missing_id) in response.json()["detail"]


async def test_review_invalid_decision_returns_422(client):
    parlay_id = await _first_parlay_id(client)
    response = await client.post(f"{BASE}/{parlay_id}/review", json={"decision": "MAYBE"})
    assert response.status_code == 422


async def test_review_extra_field_returns_422(client):
    parlay_id = await _first_parlay_id(client)
    response = await client.post(
        f"{BASE}/{parlay_id}/review", json={"decision": "ACCEPTED", "user_id": str(uuid4())}
    )
    assert response.status_code == 422


async def test_review_malformed_uuid_returns_422(client):
    response = await client.post(f"{BASE}/not-a-uuid/review", json={"decision": "ACCEPTED"})
    assert response.status_code == 422
