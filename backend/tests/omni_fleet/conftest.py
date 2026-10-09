"""Fixtures for the Group 60 ingestion fleet.

Redis-backed tests use a real server (the fleet relies on Lua-scripted locks and pub/sub), on a
dedicated database index that is flushed around each test. They skip when no server is
reachable, and refuse to touch a database that already holds someone else's keys.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.models import Base
from app.models.canonical import CanonicalEntity
from app.models.odds import OddsSnapshot
from app.models.omni_vault import OmniFleetSource, OmniQuarantineLog

TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
_SENTINEL = "betdoc:test-sentinel"
_TABLES = [OmniFleetSource.__table__, CanonicalEntity.__table__, OddsSnapshot.__table__, OmniQuarantineLog.__table__]


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        size = await client.dbsize()
        if size and not await client.exists(_SENTINEL) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_SENTINEL, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def fleet_settings() -> Callable[..., Settings]:
    """Settings with test-friendly overrides (no real backoff waits unless a test asks)."""

    def build(**overrides: Any) -> Settings:
        base = {
            "OMNI_FLEET_MAX_ATTEMPTS": 1,
            "OMNI_FLEET_FAILURE_THRESHOLD": 3,
            "ODDS_SPORT_KEYS": "soccer_epl",
            "POLYMARKET_LEAGUES": "epl",
            "ODDS_QUOTA_FLOOR": 10,
        }
        base.update(overrides)
        return get_settings().model_copy(update=base)

    return build


# ---------------------------------------------------------------- provider payloads (shapes copied from live responses)
KICKOFF = (datetime.now(UTC) + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)


def polymarket_sports() -> list[dict[str, Any]]:
    return [
        {"id": 2, "sport": "epl", "name": "Premier League", "ordering": "home", "primaryTagId": 306, "series": "10188"},
        {"id": 10, "sport": "nfl", "name": "NFL", "ordering": "away", "primaryTagId": 450, "series": "12185"},
    ]


def _pm_market(market_id: str, event: dict[str, Any], *, title: str | None, outcomes: list[str], prices: list[str], bid: float, ask: float) -> dict[str, Any]:
    return {
        "id": market_id,
        "question": f"Q {market_id}",
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps(prices),
        "groupItemTitle": title,
        "bestBid": bid,
        "bestAsk": ask,
        "spread": round(ask - bid, 4),
        "liquidityNum": 150000.0,
        "acceptingOrders": True,
        "closed": False,
        "sportsMarketType": "moneyline",
        "gameStartTime": KICKOFF.strftime("%Y-%m-%d %H:%M:%S+00"),
        "updatedAt": (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "events": [event],
    }


def polymarket_epl_markets(home_yes: tuple[float, float] = (0.70, 0.71)) -> list[dict[str, Any]]:
    event = {"id": "1089269", "title": "Arsenal FC vs. Leeds United FC", "startTime": KICKOFF.isoformat()}
    return [
        _pm_market("1", event, title="Arsenal FC", outcomes=["Yes", "No"], prices=["0.705", "0.295"], bid=home_yes[0], ask=home_yes[1]),
        _pm_market("2", event, title="Draw (Arsenal FC vs. Leeds United FC)", outcomes=["Yes", "No"], prices=["0.185", "0.815"], bid=0.18, ask=0.19),
        _pm_market("3", event, title="Leeds United FC", outcomes=["Yes", "No"], prices=["0.105", "0.895"], bid=0.10, ask=0.11),
        # A futures market without a game time must be ignored
        {**_pm_market("4", {"id": "9", "title": "Premier League Winner"}, title="Arsenal", outcomes=["Yes", "No"], prices=["0.3", "0.7"], bid=0.29, ask=0.31), "gameStartTime": None},
    ]


def polymarket_nfl_markets() -> list[dict[str, Any]]:
    event = {"id": "925590", "title": "Buccaneers vs. Cowboys", "startTime": KICKOFF.isoformat()}
    return [_pm_market("10", event, title=None, outcomes=["Buccaneers", "Cowboys"], prices=["0.195", "0.805"], bid=0.19, ask=0.20)]


def odds_api_epl(home: float = 1.45, draw: float = 4.6, away: float = 7.5) -> list[dict[str, Any]]:
    stamp = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")

    def book(key: str, h: float, d: float, a: float) -> dict[str, Any]:
        return {
            "key": key,
            "title": key.title(),
            "last_update": stamp,
            "markets": [{"key": "h2h", "last_update": stamp, "outcomes": [
                {"name": "Arsenal", "price": h}, {"name": "Leeds United", "price": a}, {"name": "Draw", "price": d},
            ]}],
        }

    return [
        {
            "id": "e912304de2b2964b5aed6d8e6b2a4e5c",
            "sport_key": "soccer_epl",
            "sport_title": "EPL",
            "commence_time": KICKOFF.isoformat().replace("+00:00", "Z"),
            "home_team": "Arsenal",
            "away_team": "Leeds United",
            "bookmakers": [book("pinnacle", home, draw, away), book("betfair_ex_uk", home - 0.03, draw + 0.1, away + 0.2), book("williamhill", home - 0.05, draw - 0.1, away - 0.3)],
        }
    ]


class ProviderStub:
    """httpx MockTransport handler: canned responses per path, a request log, optional failures."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_override: int | None = None
        self.failing: dict[str, int] = {}  # path suffix -> status, for one provider down while others work
        self.responses: dict[str, Any] = {}
        self.headers: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"message": "nope"}, headers=self.headers)
        path = request.url.path
        for suffix, code in self.failing.items():
            if path.endswith(suffix):
                return httpx.Response(code, json={"message": "down"})
        for suffix, body in self.responses.items():
            if path.endswith(suffix):
                return httpx.Response(200, json=body, headers=self.headers)
        return httpx.Response(404, json={"message": f"no stub for {path}"})


@pytest.fixture
def stub() -> ProviderStub:
    provider = ProviderStub()
    provider.responses = {
        "/sports": polymarket_sports(),
        "/markets": polymarket_epl_markets(),
        "/sports/soccer_epl/odds": odds_api_epl(),
    }
    provider.headers = {"x-requests-remaining": "480"}
    return provider


@pytest_asyncio.fixture
async def http(stub: ProviderStub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(stub)) as client:
        yield client
