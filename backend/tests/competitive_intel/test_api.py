"""HTTP tests for Competitive Intel using httpx.AsyncClient + ASGITransport."""

import pytest
from sqlalchemy import delete, select

from app.models.competitive_intel import FeatureGapAlertModel

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/rnd/competitive-intel"


async def test_dashboard_auto_scans_when_empty(client):
    response = await client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert {bot["target_site"] for bot in body["bots"]} == {"oddsshark.com", "actionnetwork.com", "pinnacle.com"}
    assert all(bot["status"] == "IDLE" for bot in body["bots"])
    assert len(body["gaps"]) == 6
    for gap in body["gaps"]:
        assert gap["is_resolved"] is False
        assert len(gap["suggestions"]) == 1
        assert gap["suggestions"][0]["gap_id"] == gap["id"]


async def test_dashboard_is_stable_across_calls(client):
    first = (await client.get(BASE)).json()
    second = (await client.get(BASE)).json()
    assert {b["id"] for b in first["bots"]} == {b["id"] for b in second["bots"]}
    assert {g["id"] for g in first["gaps"]} == {g["id"] for g in second["gaps"]}


async def test_scan_endpoint_returns_scan_complete(client):
    response = await client.post(f"{BASE}/scan")
    assert response.status_code == 200
    assert response.json() == {"status": "scan_complete"}

    dashboard = (await client.get(BASE)).json()
    assert len(dashboard["bots"]) == 3

    again = await client.post(f"{BASE}/scan")
    assert again.status_code == 200
    assert len((await client.get(BASE)).json()["gaps"]) == 6


async def test_dashboard_reflects_cascade_delete(client, session_factory):
    await client.post(f"{BASE}/scan")
    async with session_factory() as session:
        gap_id = (await session.execute(select(FeatureGapAlertModel.id).limit(1))).scalar_one()
        await session.execute(delete(FeatureGapAlertModel).where(FeatureGapAlertModel.id == gap_id))
        await session.commit()

    body = (await client.get(BASE)).json()
    assert len(body["gaps"]) == 5
    assert str(gap_id) not in {g["id"] for g in body["gaps"]}
    assert all(s["gap_id"] != str(gap_id) for g in body["gaps"] for s in g["suggestions"])


@pytest.mark.parametrize("site_name", ["OddsShark", "oddsshark", "ACTIONNETWORK", "Pinnacle"])
async def test_report_case_insensitive_returns_markdown(client, site_name):
    response = await client.get(f"{BASE}/reports/{site_name}")
    assert response.status_code == 200
    markdown = response.json()["markdown"]
    assert markdown.startswith("# Site Report")
    assert "| Feature |" in markdown
    assert "### Recommendation" in markdown


async def test_report_unknown_site_returns_404(client):
    response = await client.get(f"{BASE}/reports/Bet365")
    assert response.status_code == 404
    assert "not tracked" in response.json()["detail"]


async def test_report_blank_site_returns_400(client):
    response = await client.get(f"{BASE}/reports/%20%20")
    assert response.status_code == 400
    assert "blank" in response.json()["detail"]
