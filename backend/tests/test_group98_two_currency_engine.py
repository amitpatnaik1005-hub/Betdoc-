"""Group 98: TODAR MAL's strict two-currency engine (INR canonical, USD display) that fails closed.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.v1.currency_tax import router as currency_router
from app.core.config import Settings, get_settings
from app.models import Base
from app.models.control_panel import SystemSettingsModel
from app.models.tax_and_fx import CurrencyExchangeRate
from app.services import currency_service
from app.services.currency_service import CurrencyService

ONLY_TWO = "Only INR and USD are supported"


def _frankfurter(*, up: bool = True, usd_per_inr: float = 1 / 83.5) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.frankfurter.app" and request.url.params["to"] == "USD"
        if not up:
            return httpx.Response(503)
        return httpx.Response(200, json={"base": "INR", "rates": {"USD": usd_per_inr}})
    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- configuration
def test_only_inr_and_usd_can_be_configured() -> None:
    assert get_settings().CURRENCY_SUPPORTED == ["INR", "USD"]
    for bad in (["INR", "USD", "EUR"], ["INR", "BTC"], ["USD"]):
        with pytest.raises(ValidationError):
            Settings(CURRENCY_SUPPORTED=bad)
    assert not hasattr(get_settings(), "CURRENCY_CRYPTO_URL")
    assert not hasattr(get_settings(), "CURRENCY_USD_INR_FALLBACK")  # fail closed: no guessed rate exists to fall back to
    assert currency_service.SYMBOLS == {"INR": "₹", "USD": "$"} and not hasattr(currency_service, "COINGECKO_IDS")


# --------------------------------------------------------------------------- the API
@pytest_asyncio.fixture
async def desk(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, dict[str, httpx.MockTransport]]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[CurrencyExchangeRate.__table__, SystemSettingsModel.__table__])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    transport = {"now": _frankfurter()}
    real_init = CurrencyService.__init__

    def init(self: CurrencyService, settings: Settings | None = None, redis: object = None, *, transport_: object = None, **_: object) -> None:
        real_init(self, settings, None, transport=transport["now"])  # no Redis: the table and the provider only

    monkeypatch.setattr(CurrencyService, "__init__", init)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(currency_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), role="QUANT")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, transport
    await engine.dispose()


@pytest.mark.asyncio
async def test_board_and_conversion_carry_inr_and_usd_only(desk: tuple[AsyncClient, dict[str, httpx.MockTransport]]) -> None:
    client, _ = desk
    board = await client.get("/api/v1/currency/board")
    assert board.status_code == 200
    body = board.json()
    assert set(body["rates"]) == {"INR", "USD"} and body["missing"] == {}
    assert body["rates"]["INR"]["inr_per_unit"] == 1.0 and body["rates"]["USD"]["inr_per_unit"] == pytest.approx(83.5)
    assert body["rates"]["USD"]["provider"] == "frankfurter:ecb"

    usd = (await client.get("/api/v1/currency/convert", params={"amount": 1000, "from": "INR", "to": "USD"})).json()
    assert usd["converted"] == 11.98 and usd["to_currency"] == "USD"  # 1000 / 83.5 = 11.976, to the cent
    inr = (await client.get("/api/v1/currency/convert", params={"amount": 12, "from": "USD", "to": "INR"})).json()
    assert inr["converted"] == 1002.0


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "params"), [
    ("/api/v1/currency/convert", {"amount": 1000, "from": "INR", "to": "EUR"}),
    ("/api/v1/currency/convert", {"amount": 1000, "from": "GBP", "to": "INR"}),
    ("/api/v1/currency/rate", {"base": "BTC", "quote": "INR"}),
    ("/api/v1/currency/rate", {"base": "USD", "quote": "USDT"}),
])
async def test_any_other_currency_is_refused(desk: tuple[AsyncClient, dict[str, httpx.MockTransport]], path: str, params: dict[str, object]) -> None:
    client, _ = desk
    resp = await client.get(path, params=params)
    assert resp.status_code == 422 and resp.json()["detail"] == ONLY_TWO


@pytest.mark.asyncio
async def test_an_unreachable_feed_hides_usd_instead_of_guessing(desk: tuple[AsyncClient, dict[str, httpx.MockTransport]]) -> None:
    client, transport = desk
    transport["now"] = _frankfurter(up=False)
    board = (await client.get("/api/v1/currency/board")).json()
    assert set(board["rates"]) == {"INR"} and set(board["missing"]) == {"USD"}  # rupees still shown; USD reported missing
    down = await client.get("/api/v1/currency/convert", params={"amount": 1000, "from": "INR", "to": "USD"})
    assert down.status_code == 503 and "USD" in down.json()["detail"]
    inr = (await client.get("/api/v1/currency/convert", params={"amount": 1000, "from": "INR", "to": "INR"})).json()
    assert inr["converted"] == 1000.0 and inr["provider"] == "identity"  # INR never needs a rate


@pytest.mark.asyncio
async def test_the_tds_estimator_stays_in_rupees(desk: tuple[AsyncClient, dict[str, httpx.MockTransport]]) -> None:
    client, _ = desk
    est = (await client.get("/api/v1/tax/estimate", params={"gross_win": 25000, "stake": 10000})).json()
    assert (est["tds_deducted"], est["net_take_home"], est["tds_rate_pct"], est["currency"]) == (4500.0, 20500.0, 30.0, "INR")


def test_service_rounding_uses_two_places_for_both() -> None:
    assert currency_service.round_display(Decimal("11.976"), "USD") == Decimal("11.98")
    assert currency_service.round_display(Decimal("1000.005"), "INR") == Decimal("1000.01")
