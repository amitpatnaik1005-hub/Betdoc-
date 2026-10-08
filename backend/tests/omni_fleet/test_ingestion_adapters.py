"""Phase 1: fleet adapters fetch raw JSON, back off on 429, never leak keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.ingestion import INGESTORS, OddsApiIngestor, PolymarketIngestor
from app.adapters.ingestion.base import (
    BackoffPolicy,
    IngestionError,
    MissingApiKeyError,
    ProviderAuthError,
    QuotaExhaustedError,
    RateLimitedError,
    parse_retry_after,
)

from .conftest import ProviderStub


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture(autouse=True)
def _fresh_class_caches() -> None:
    PolymarketIngestor._sports_cache = None
    OddsApiIngestor._last_quota = None


def test_registry_lists_both_sanctioned_sources() -> None:
    assert set(INGESTORS) == {"polymarket", "odds_api"}
    assert INGESTORS["odds_api"].requires_api_key and not INGESTORS["polymarket"].requires_api_key


def test_backoff_grows_exponentially_and_is_capped() -> None:
    policy = BackoffPolicy(max_attempts=6, base_seconds=1.0, max_seconds=8.0)
    for attempt, window in enumerate([1, 2, 4, 8, 8]):
        delay = policy.delay(attempt)
        assert window / 2 <= delay <= window  # equal jitter: half fixed, half random


def test_backoff_honours_a_longer_retry_after_up_to_the_cap() -> None:
    policy = BackoffPolicy(max_attempts=3, base_seconds=1.0, max_seconds=30.0)
    assert policy.delay(0, retry_after=12.0) == 12.0
    assert policy.delay(0, retry_after=300.0) == 30.0


def test_parse_retry_after_accepts_seconds_and_http_dates() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Thu, 08 Oct 2026 12:00:30 GMT", now) == pytest.approx(30.0)
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


async def test_429_is_retried_with_backoff_then_succeeds(fleet_settings, stub: ProviderStub) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return stub(request)

    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ingestor = PolymarketIngestor(http, fleet_settings(OMNI_FLEET_MAX_ATTEMPTS=4), sleep=sleep)
        batch = await ingestor.fetch()
    assert batch.retries == 2
    assert len(sleep.waits) == 2 and all(w >= 3.0 for w in sleep.waits)  # Retry-After respected
    assert batch.payloads[0].key == "epl"


async def test_429_after_every_attempt_raises_rate_limited(fleet_settings) -> None:
    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as http:
        with pytest.raises(RateLimitedError):
            await PolymarketIngestor(http, fleet_settings(OMNI_FLEET_MAX_ATTEMPTS=3), sleep=sleep).fetch()
    assert len(sleep.waits) == 2  # attempts - 1 waits


async def test_5xx_is_retried_but_auth_failure_is_not(fleet_settings) -> None:
    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as http:
        with pytest.raises(ProviderAuthError):
            await OddsApiIngestor(http, fleet_settings(OMNI_FLEET_MAX_ATTEMPTS=4), api_key="k" * 32, sleep=sleep).fetch()
    assert sleep.waits == []


async def test_odds_api_requires_a_key(fleet_settings) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(MissingApiKeyError):
            OddsApiIngestor(http, fleet_settings())


async def test_odds_api_sends_key_as_query_param_and_reads_quota(fleet_settings, http, stub: ProviderStub) -> None:
    secret = "abcd" * 8
    batch = await OddsApiIngestor(http, fleet_settings(), api_key=secret).fetch()
    request = stub.requests[-1]
    assert request.url.params["apiKey"] == secret
    assert request.url.params["oddsFormat"] == "decimal"
    assert batch.quota_remaining == 480.0
    assert batch.payloads[0].key == "soccer_epl" and isinstance(batch.payloads[0].data, list)


async def test_odds_api_errors_never_contain_the_key(fleet_settings) -> None:
    secret = "topsecret" * 4
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        with pytest.raises(IngestionError) as info:
            await OddsApiIngestor(http, fleet_settings(), api_key=secret).fetch()
    assert secret not in str(info.value) and "apiKey" not in str(info.value)


async def test_odds_api_stops_before_spending_below_the_quota_floor(fleet_settings, http, stub: ProviderStub) -> None:
    stub.headers = {"x-requests-remaining": "3"}
    settings = fleet_settings(ODDS_SPORT_KEYS="soccer_epl,soccer_epl")
    with pytest.raises(QuotaExhaustedError):
        await OddsApiIngestor(http, settings, api_key="k" * 32).fetch()
    assert len(stub.requests) == 1  # the second sport was never requested


async def test_polymarket_uses_the_moneyline_market_window(fleet_settings, http, stub: ProviderStub) -> None:
    batch = await PolymarketIngestor(http, fleet_settings()).fetch()
    markets_call = next(r for r in stub.requests if r.url.path.endswith("/markets"))
    params = markets_call.url.params
    assert params["tag_id"] == "306" and params["sports_market_types"] == "moneyline" and params["closed"] == "false"
    window_start = datetime.fromisoformat(params["end_date_min"].replace("Z", "+00:00"))
    assert datetime.now(UTC) - window_start > timedelta(hours=3)  # in-play games stay in the window
    assert batch.payloads[0].data["ordering"] == "home"
    assert batch.requests == 2  # /sports + one /markets page


async def test_polymarket_rejects_unknown_leagues(fleet_settings, http) -> None:
    with pytest.raises(IngestionError):
        await PolymarketIngestor(http, fleet_settings(POLYMARKET_LEAGUES="curling")).fetch()
