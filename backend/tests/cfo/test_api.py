"""HTTP tests for THE VAULT - CFO Advisory using httpx.AsyncClient + ASGITransport."""

from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/the-vault/cfo"


async def test_advisory_endpoint_returns_decoded_suggestions(client):
    response = await client.post(
        f"{BASE}/advisory",
        json={"current_bankroll": 2_000.0, "active_exposure": 300.0, "variance_threshold_pct": 20.0},
    )
    assert response.status_code == 200
    body = response.json()
    UUID(body["id"])
    assert body["capital_health_score"] == pytest.approx(77.5)  # 100 - (15% * 1.5)
    assert body["variance_status"] == "STABLE"
    assert isinstance(body["suggestions"], list) and body["suggestions"]
    assert "suggestions_json" not in body
    assert body["created_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {"current_bankroll": 0, "active_exposure": 10, "variance_threshold_pct": 30},
        {"current_bankroll": 100, "active_exposure": -1, "variance_threshold_pct": 30},
        {"current_bankroll": 100, "active_exposure": 10, "variance_threshold_pct": 101},
        {"current_bankroll": 100, "active_exposure": 10},  # threshold is required: no hidden default
        {"current_bankroll": 100, "active_exposure": 10, "variance_threshold_pct": 30, "extra": 1},
    ],
)
async def test_advisory_invalid_payloads_return_422(client, payload):
    response = await client.post(f"{BASE}/advisory", json=payload)
    assert response.status_code == 422


async def test_stress_test_endpoint(client):
    response = await client.post(
        f"{BASE}/stress-test",
        json={"scenario": "Liquidity crunch", "portfolio_value": 8_000.0, "shock_pct": 12.5,
              "survival_threshold_pct": 20.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scenario_name"] == "Liquidity crunch"
    assert body["simulated_pnl"] == pytest.approx(-1_000.0)
    assert body["simulated_drawdown_pct"] == pytest.approx(12.5)
    assert body["survived"] is True
    assert body["recommendation"] == "Buffer sufficient."


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario": "X", "portfolio_value": 100, "shock_pct": 150, "survival_threshold_pct": 30},
        {"scenario": "X", "portfolio_value": 0, "shock_pct": 10, "survival_threshold_pct": 30},
        {"scenario": "", "portfolio_value": 100, "shock_pct": 10, "survival_threshold_pct": 30},
        {"scenario": "X", "portfolio_value": 100, "shock_pct": 10},
    ],
)
async def test_stress_test_invalid_payloads_return_422(client, payload):
    response = await client.post(f"{BASE}/stress-test", json=payload)
    assert response.status_code == 422


async def test_tax_endpoint_upserts_per_year(client):
    first = await client.post(
        f"{BASE}/taxes", json={"year": 2026, "total_profit": 30_000.0, "tax_allowance": 5_000.0, "tax_rate_pct": 20.0}
    )
    assert first.status_code == 200
    assert first.json()["taxable_amount"] == pytest.approx(25_000.0)
    assert first.json()["estimated_tax"] == pytest.approx(5_000.0)

    second = await client.post(
        f"{BASE}/taxes", json={"year": 2026, "total_profit": 40_000.0, "tax_allowance": 5_000.0, "tax_rate_pct": 40.0}
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["estimated_tax"] == pytest.approx(14_000.0)


async def test_tax_invalid_rate_returns_422(client):
    response = await client.post(
        f"{BASE}/taxes", json={"year": 2026, "total_profit": 1.0, "tax_allowance": 0.0, "tax_rate_pct": 120.0}
    )
    assert response.status_code == 422


async def test_alerts_empty_without_seeding(client):
    response = await client.get(f"{BASE}/alerts")
    assert response.status_code == 200
    assert response.json() == []


async def test_alert_lifecycle_from_high_variance_advisory(client):
    await client.post(
        f"{BASE}/advisory",
        json={"current_bankroll": 1_000.0, "active_exposure": 900.0, "variance_threshold_pct": 25.0},
    )
    alerts = (await client.get(f"{BASE}/alerts")).json()
    assert len(alerts) == 1
    assert alerts[0]["level"] == "WARNING"
    assert alerts[0]["is_read"] is False

    marked = await client.patch(f"{BASE}/alerts/{alerts[0]['id']}/read")
    assert marked.status_code == 200
    assert marked.json()["is_read"] is True
    assert (await client.get(f"{BASE}/alerts")).json() == []


async def test_failed_stress_test_raises_critical_alert(client):
    await client.post(
        f"{BASE}/stress-test",
        json={"scenario": "Market crash", "portfolio_value": 5_000.0, "shock_pct": 60.0,
              "survival_threshold_pct": 35.0},
    )
    alerts = (await client.get(f"{BASE}/alerts")).json()
    assert [a["level"] for a in alerts] == ["CRITICAL"]


async def test_mark_unknown_alert_returns_404(client):
    response = await client.patch(f"{BASE}/alerts/{uuid4()}/read")
    assert response.status_code == 404


async def test_mark_alert_malformed_id_returns_422(client):
    response = await client.patch(f"{BASE}/alerts/not-a-uuid/read")
    assert response.status_code == 422
