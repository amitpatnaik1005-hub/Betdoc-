"""
Currency, tax and leg-translation tests (Group 80's TODAR MAL pieces, kept when Group 97 retired the Paper Trading Sandbox).
Developed for Amit Ashok Kumar Patnaik | BetDoc.

* the pure pieces: Section 115BBJ and the Indian financial year, Ashoka's leg translation (the real-money desk uses it);
* TODAR MAL: rates from Frankfurter and CoinGecko (a mock transport) into the table and ``fx:rates:latest``, cross
  rates through INR, the execution FX hash first, failing closed when no provider answers (SQLite and, with
  ``TEST_POSTGRES_URL``, PostgreSQL);
* the API at its new home (``/currency``, ``/tax``) through ``httpx.ASGITransport(app=app)``; the Celery beat; the roster.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_user, get_db
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.domain.paper_trading.legs import LegTranslationError, translate
from app.domain.paper_trading.tax import financial_year, section_115bbj
from app.main import app
from app.models import User
from app.models.control_panel import SystemSettingsModel
from app.models.tax_and_fx import CurrencyExchangeRate, TaxLedgerEntry
from app.models.the_hive import LegendaryBot
from app.services.commander_supervisor import PROBES
from app.services.currency_service import CurrencyService, CurrencyUnavailableError

DEVELOPER = "Amit Ashok Kumar Patnaik"
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-currency-and-tax"


# ================================================================ fixtures: databases, Redis, the app
TABLES: list[Table] = [User.__table__, TaxLedgerEntry.__table__, CurrencyExchangeRate.__table__, SystemSettingsModel.__table__]  # type: ignore[list-item]


def _closure(tables: list[Table]) -> list[Table]:
    seen: dict[str, Table] = {}
    stack = list(tables)
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen[t.name] = t
        stack.extend(fk.column.table for fk in t.foreign_keys)
    return [t for t in User.metadata.sorted_tables if t.name in seen]


ALL_TABLES = _closure(TABLES)


def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in ALL_TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=ALL_TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def async_session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _new_user(session: AsyncSession, role: str = "QUANT") -> uuid.UUID:
    user = User(id=uuid.uuid4(), username=f"{role.lower()}-{uuid.uuid4().hex[:8]}", hashed_password="x", role=role, is_active=True)
    session.add(user)
    await session.commit()
    return user.id




async def test_currency_conversion_and_tds() -> None:
    """Tests Section 115BBJ 30% TDS (the brief's figures)."""
    svc = CurrencyService()
    tds = svc.calculate_indian_tds(gross_win=25000.0, stake=10000.0)
    assert tds["gross_win"] == 25000.0
    assert tds["stake"] == 10000.0
    assert tds["net_profit"] == 15000.0
    assert tds["tds_deducted"] == 4500.0
    assert tds["net_take_home"] == 20500.0
    assert tds["tds_rate_pct"] == 30.0
    tds_loss = svc.calculate_indian_tds(gross_win=0.0, stake=5000.0)
    assert tds_loss["net_profit"] == 0.0
    assert tds_loss["tds_deducted"] == 0.0
    assert tds_loss["net_take_home"] == 0.0


def test_section_115bbj_and_financial_year() -> None:
    b = section_115bbj(Decimal("30000"), Decimal("10000"))
    assert (b.net_winnings, b.tds, b.net_post_tax_payout) == (Decimal("20000.00"), Decimal("6000.00"), Decimal("24000.00"))
    assert b.cess == Decimal("240.00")  # 4% health and education cess on the tax
    assert math.isclose(b.effective_rate_pct, 31.2)  # 30% TDS plus the 4% cess on it
    push = section_115bbj(Decimal("10000"), Decimal("10000"))
    assert push.tds == 0 and push.net_post_tax_payout == Decimal("10000.00")
    odd = section_115bbj(Decimal("1234.57"), Decimal("1000"))
    assert odd.tds == Decimal("70.37")  # 0.30 x 234.57 = 70.371, to the paisa
    assert financial_year(datetime(2026, 3, 31, 18, 29, tzinfo=UTC)) == "2025-26"  # 23:59 IST on 31 March
    assert financial_year(datetime(2026, 3, 31, 18, 31, tzinfo=UTC)) == "2026-27"  # 00:01 IST on 1 April
    assert financial_year(datetime(2027, 1, 15, tzinfo=UTC)) == "2026-27"


def test_ashoka_leg_translation() -> None:
    home = translate("soccer_epl", "Match Odds", "home")
    assert (home.sport_key, home.market_type, home.selection, home.line, home.quote_market, home.quote_selection) == (
        "soccer", "moneyline", "home", None, "Match Odds", "HOME")
    over = translate(None, "Totals 2.5", "OVER")
    assert (over.market_type, over.selection, over.line) == ("total", "over", 2.5)
    away_ah = translate("soccer_epl", "Asian Handicap -0.75", "AWAY")
    assert (away_ah.market_type, away_ah.selection, away_ah.line) == ("asian_handicap", "away", 0.75)  # the away side's own line
    assert translate(None, "Asian Handicap -0.75", "HOME").line == -0.75
    assert translate(None, "Double Chance", "1X").market_type == "double_chance"
    assert translate(None, "BTTS", "YES").market_type == "btts"
    assert translate(None, "Draw No Bet", "AWAY").market_type == "draw_no_bet"
    with pytest.raises(LegTranslationError):
        translate(None, "Corners 9.5", "OVER")
    with pytest.raises(LegTranslationError):
        translate(None, "Match Odds", "OVER")


def _fx_transport(calls: list[str], *, fiat: bool = True, crypto: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "api.frankfurter.app":
            if not fiat:
                return httpx.Response(503)
            assert request.url.params["from"] == "INR"
            return httpx.Response(200, json={"base": "INR", "rates": {"USD": 0.012, "EUR": 0.0110, "GBP": 0.0095}})
        if not crypto:
            return httpx.Response(429)
        return httpx.Response(200, json={"tether": {"inr": 83.9}, "bitcoin": {"inr": 5600000}})
    return httpx.MockTransport(handler)


async def test_currency_service_refresh_cache_and_cross_rates(async_session: AsyncSession, redis: Redis) -> None:
    settings = get_settings()
    calls: list[str] = []
    svc = CurrencyService(settings, redis, transport=_fx_transport(calls))
    now = datetime.now(UTC)
    usd = await svc.get_exchange_rate(async_session, "USD", "INR", now)  # nothing cached: one refresh from the providers
    assert math.isclose(float(usd.rate), 1 / 0.012, rel_tol=1e-9) and usd.provider == "frankfurter:ecb"
    assert sorted(set(calls)) == ["api.coingecko.com", "api.frankfurter.app"]
    cached = json.loads(await redis.hget(settings.CURRENCY_CACHE_KEY, "USD_INR"))
    assert settings.CURRENCY_CACHE_KEY == "fx:rates:latest" and Decimal(cached["rate"]) > 83
    calls.clear()
    eur_usd = await svc.get_exchange_rate(async_session, "EUR", "USD", now)  # a cross rate through INR, from the cache
    assert calls == [] and math.isclose(float(eur_usd.rate), 0.012 / 0.0110, rel_tol=1e-9)
    btc, quote = await svc.convert(async_session, Decimal("1120000"), "INR", "BTC", now)
    assert btc == Decimal("0.20000000") and quote.provider == "coingecko"
    rows = (await async_session.execute(select(func.count()).select_from(CurrencyExchangeRate))).scalar_one()
    assert rows == 10  # five currencies, both directions
    await redis.delete(settings.CURRENCY_CACHE_KEY)
    from_table = await svc.get_exchange_rate(async_session, "GBP", "INR", now)
    assert calls == [] and math.isclose(float(from_table.rate), 1 / 0.0095, rel_tol=1e-6)
    board = await svc.board(async_session, now)
    assert set(board["rates"]) == {"INR", "USD", "EUR", "GBP", "USDT", "BTC"} and board["missing"] == {}
    # the execution FX hash wins: the rates real money is converted with
    await redis.hset(settings.FX_RATES_KEY, "USD", json.dumps({"inr_per_unit": "84.25", "as_of": now.timestamp(), "source": "admin"}))
    execution = await svc.get_exchange_rate(async_session, "USD", "INR", now)
    assert execution.rate == Decimal("84.25") and execution.provider.startswith("execution:")
    with pytest.raises(ValueError, match="not a supported currency"):
        await svc.get_exchange_rate(async_session, "JPY", "INR", now)


async def test_currency_service_fails_closed(async_session: AsyncSession, redis: Redis) -> None:
    calls: list[str] = []
    svc = CurrencyService(get_settings(), redis, transport=_fx_transport(calls, fiat=False, crypto=False))
    with pytest.raises(CurrencyUnavailableError):
        await svc.get_exchange_rate(async_session, "USD", "INR")  # never a guessed constant
    assert calls and (await async_session.execute(select(func.count()).select_from(CurrencyExchangeRate))).scalar_one() == 0
    stale_at = datetime.now(UTC) - timedelta(seconds=get_settings().CURRENCY_MAX_AGE_SECONDS + 60)
    await svc.store(async_session, {"USD": (Decimal("83.10"), "frankfurter:ecb")}, stale_at)
    with pytest.raises(CurrencyUnavailableError):
        await svc.get_exchange_rate(async_session, "USD", "INR")  # a stale rate is no rate
    board = await svc.board(async_session)
    assert "USD" in board["missing"] and board["rates"]["INR"]["inr_per_unit"] == 1.0


# ================================================================ the API (SQLite and PostgreSQL)
@pytest_asyncio.fixture
async def api(sessions: async_sessionmaker[AsyncSession], redis: Redis) -> AsyncIterator[dict[str, Any]]:
    async with sessions() as s:
        users = {}
        for role in ("QUANT", "ADMIN", "OTHER"):
            uid = await _new_user(s, "ADMIN" if role == "ADMIN" else "QUANT")
            users[role] = await s.get(User, uid)
    caller = {"user": users["QUANT"]}

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    previous_redis = getattr(app.state, "redis", None)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    app.state.redis = redis
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def as_role(role: str) -> None:
        caller["user"] = users[role]

    try:
        yield {"client": client, "as_role": as_role, "users": users, "sessions": sessions, "redis": redis}
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.state.redis = previous_redis


async def test_currency_refresh_is_admin_only_and_fails_closed(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client: httpx.AsyncClient = api["client"]

    async def fake_fetch(self: CurrencyService) -> dict[str, tuple[Decimal, str]]:
        return {"USD": (Decimal("83.50"), "frankfurter:ecb"), "BTC": (Decimal("5600000"), "coingecko")}

    assert (await client.post("/api/v1/currency/refresh")).status_code == 403
    monkeypatch.setattr(CurrencyService, "_fetch", fake_fetch)
    api["as_role"]("ADMIN")
    board = (await client.post("/api/v1/currency/refresh")).json()
    assert board["rates"]["USD"]["inr_per_unit"] == 83.5 and board["rates"]["BTC"]["symbol"] == "₿"
    assert set(board["missing"]) == {"EUR", "GBP", "USDT"} and board["developer_credit"] == DEVELOPER

    async def nothing(self: CurrencyService) -> dict[str, tuple[Decimal, str]]:
        return {}

    monkeypatch.setattr(CurrencyService, "_fetch", nothing)
    down = await client.get("/api/v1/currency/rate?base=EUR&quote=INR")
    assert down.status_code == 503 and "EUR" in down.json()["detail"]


async def test_tds_estimator_lives_at_its_new_address(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    est = (await client.get("/api/v1/tax/estimate", params={"gross_win": 25000, "stake": 10000})).json()
    assert (est["gross_profit"], est["tds_deducted"], est["net_take_home"], est["tds_rate_pct"]) == (15000.0, 4500.0, 20500.0, 30.0)
    assert est["developer_credit"] == DEVELOPER
    assert (await client.get("/api/v1/paper-trading/currency/board")).status_code == 404  # the sandbox router is gone


# ================================================================ the fleet: beat, the roster
def test_celery_beat_and_tasks_registered() -> None:
    import app.workers.currency_tasks  # noqa: F401, PLC0415 - registers the task

    settings = get_settings()
    assert "currency.refresh_rates" in celery_app.tasks and "paper_trading.settle_pending_paper_bets" not in celery_app.tasks
    beat = celery_app.conf.beat_schedule
    assert "paper-trading-auto-settlement" not in beat
    assert beat["currency-refresh-rates"]["schedule"] == float(settings.CURRENCY_REFRESH_SECONDS)
    assert "app.workers.currency_tasks" in celery_app.conf.include and "app.workers.paper_trading_tasks" not in celery_app.conf.include


def test_ashvatthama_closes_the_roster() -> None:
    roster = [bot.value for bot in LegendaryBot]
    # 15 since Group 97 retired ARJUNA (Group 96 retired GARUDA); ASHVATTHAMA is still the last commander
    assert len(roster) == 15 and roster[-1] == "ASHVATTHAMA" and not {"DEVRAYA", "GARUDA", "ARJUNA"} & set(roster)
    assert LegendaryBot.ASHVATTHAMA in PROBES and set(PROBES) == set(LegendaryBot)
