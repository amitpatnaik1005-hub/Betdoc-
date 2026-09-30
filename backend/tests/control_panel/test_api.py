"""HTTP tests for the Control Panel using httpx.AsyncClient + ASGITransport."""

import pytest
from sqlalchemy import select

from app.domain.control_panel.manager import EDITABLE_FIELDS
from app.models.control_panel import SystemSettingsModel
from app.schemas.control_panel import REDACTED

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/control-panel"


async def _stored_keys(session_factory) -> tuple[str | None, str | None]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(SystemSettingsModel.odds_api_key, SystemSettingsModel.news_api_key).where(
                    SystemSettingsModel.id == 1
                )
            )
        ).one()
    return row.odds_api_key, row.news_api_key


async def test_get_settings_returns_defaults(client):
    response = await client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    assert body["theme"] == "auto"
    assert body["bots_enabled"] is True
    assert body["odds_api_key"] is None  # nothing to redact
    assert body["created_at"] and body["updated_at"]


async def test_patch_redacts_keys_in_response_but_stores_real_values(client, session_factory):
    response = await client.patch(BASE, json={"odds_api_key": "odds-real", "news_api_key": "news-real"})
    assert response.status_code == 200
    body = response.json()
    assert body["odds_api_key"] == REDACTED
    assert body["news_api_key"] == REDACTED
    assert await _stored_keys(session_factory) == ("odds-real", "news-real")

    again = await client.get(BASE)
    assert again.json()["odds_api_key"] == REDACTED
    assert await _stored_keys(session_factory) == ("odds-real", "news-real")


async def test_round_tripping_redacted_form_preserves_keys(client, session_factory):
    await client.patch(BASE, json={"odds_api_key": "odds-real", "news_api_key": "news-real"})
    current = (await client.get(BASE)).json()

    # The frontend sends the whole form back, including the redacted placeholders.
    form = {key: value for key, value in current.items() if key in EDITABLE_FIELDS}
    form["theme"] = "dark"
    response = await client.patch(BASE, json=form)

    assert response.status_code == 200
    assert response.json()["theme"] == "dark"
    assert await _stored_keys(session_factory) == ("odds-real", "news-real")


async def test_patch_partial_update_leaves_other_fields(client):
    await client.patch(BASE, json={"max_bet_size": 20.0})
    body = (await client.patch(BASE, json={"accent_color": "#ff0000"})).json()
    assert body["accent_color"] == "#ff0000"
    assert body["max_bet_size"] == 20.0
    assert body["default_kelly_fraction"] == 0.25


async def test_patch_empty_body_is_a_no_op(client):
    before = (await client.get(BASE)).json()
    response = await client.patch(BASE, json={})
    assert response.status_code == 200
    after = response.json()
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"default_kelly_fraction": 1.5},
        {"default_kelly_fraction": -0.1},
        {"max_daily_exposure": -5},
        {"global_stop_loss": -1},
        {"research_frequency_minutes": 0},
        {"theme": "neon"},
        {"theme": None},
        {"accent_color": "blue"},
        {"omniroute_url": "ftp://omniroute.local"},
        {"id": 2},
        {"last_emergency_stop_at": "2026-01-01T00:00:00Z"},
        {"hack_the_planet": True},
    ],
)
async def test_patch_invalid_payloads_return_422(client, payload):
    response = await client.patch(BASE, json=payload)
    assert response.status_code == 422
    settings = (await client.get(BASE)).json()
    assert settings["default_kelly_fraction"] == 0.25
    assert settings["max_daily_exposure"] == 500.0


async def test_emergency_stop_endpoint(client, session_factory):
    await client.patch(BASE, json={"odds_api_key": "odds-real"})

    response = await client.post(f"{BASE}/emergency-stop")
    assert response.status_code == 200
    body = response.json()
    assert body["bots_enabled"] is False
    assert body["max_daily_exposure"] == 0.0
    assert body["last_emergency_stop_at"] is not None
    assert body["odds_api_key"] == REDACTED

    assert await _stored_keys(session_factory) == ("odds-real", None)
    persisted = (await client.get(BASE)).json()
    assert persisted["bots_enabled"] is False
    assert persisted["max_daily_exposure"] == 0.0
