"""HTTP surface of the read-only presentation layer.

Dependency injection is exercised for real. ``get_state_store`` and
``get_broadcaster`` resolve off ``app.state``, so the fixtures wire genuine
singletons there rather than installing ``dependency_overrides``. Overriding
the dependency would bypass the exact resolution path under test and would let
a mis-wired composition root pass the suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from betdoc.adapters.cache.state_store import InMemoryStateStore
from betdoc.domain.intelligence.account_models import (
    BettorProfile,
    LinkedBookmaker,
    SportType,
)
from betdoc.presentation.api.app import build_api
from betdoc.presentation.api.broadcaster import WebsocketBroadcaster
from betdoc.presentation.api.routers.intelligence import (
    AccountsResponse,
    CacheHealthResponse,
)
from tests.conftest import (
    TEST_PROFILE_ID,
    make_account_state,
    make_profile,
)

pytestmark = pytest.mark.asyncio

_PINNACLE_BALANCE: Final[int] = 5_000_000
_PINNACLE_EXPOSURE: Final[int] = 250_000
_BETFAIR_BALANCE: Final[int] = 3_000_000
_BETFAIR_EXPOSURE: Final[int] = 125_000


# --------------------------------------------------------------------------- #
# Fixtures with two bookmakers loaded
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def two_book_store(profile: BettorProfile) -> InMemoryStateStore:
    """Store pre-loaded with a profile and two distinct account snapshots."""
    store = InMemoryStateStore()
    await store.set_profile(profile)
    await store.set_account_state(
        make_account_state(
            bookmaker=LinkedBookmaker.PINNACLE,
            realized_balance_paise=_PINNACLE_BALANCE,
            unsettled_exposure_paise=_PINNACLE_EXPOSURE,
        )
    )
    await store.set_account_state(
        make_account_state(
            bookmaker=LinkedBookmaker.BETFAIR,
            realized_balance_paise=_BETFAIR_BALANCE,
            unsettled_exposure_paise=_BETFAIR_EXPOSURE,
        )
    )
    return store


@pytest_asyncio.fixture
async def two_book_client(
    two_book_store: InMemoryStateStore, broadcaster: WebsocketBroadcaster
) -> AsyncIterator[AsyncClient]:
    """Client over an app wired to the two-bookmaker store."""
    app = build_api(
        two_book_store,
        broadcaster,
        profile_id=TEST_PROFILE_ID,
        enable_docs=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://betdoc.test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def cold_client(
    state_store: InMemoryStateStore, broadcaster: WebsocketBroadcaster
) -> AsyncIterator[AsyncClient]:
    """Client over a cold cache: a profile but no account snapshots."""
    app = build_api(
        state_store, broadcaster, profile_id=TEST_PROFILE_ID, enable_docs=False
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://betdoc.test"
    ) as client:
        yield client


# --------------------------------------------------------------------------- #
# Cache health
# --------------------------------------------------------------------------- #


async def test_intelligence_health_returns_200_and_a_valid_model(
    two_book_client: AsyncClient,
) -> None:
    """200 with a body that validates against ``CacheHealthResponse``.

    Validating through the model rather than by hand-picking keys means a field
    rename in the response schema breaks this test, which is the point: the
    frontend contract is the schema, not a dictionary shape.
    """
    response = await two_book_client.get("/api/v1/intelligence/health")

    assert response.status_code == 200
    payload: dict[str, Any] = response.json()

    model = CacheHealthResponse.model_validate(payload)
    assert model.is_warm is True
    assert model.profiles == 1
    assert model.accounts == 2
    assert model.active_modifiers == 0
    assert isinstance(model.detail, dict)
    assert model.detail["is_warm"] is True
    assert set(model.detail["accounts"].keys()) == {"pinnacle", "betfair"}


async def test_intelligence_health_reports_a_cold_cache(
    cold_client: AsyncClient,
) -> None:
    """A cold cache must be visible, not silently rendered as healthy.

    ``is_warm`` false means the orchestrator is skipping batches rather than
    trading badly, which is correct behaviour that an operator must be able to
    see.
    """
    response = await cold_client.get("/api/v1/intelligence/health")

    assert response.status_code == 200
    model = CacheHealthResponse.model_validate(response.json())
    assert model.is_warm is False
    assert model.profiles == 1
    assert model.accounts == 0


# --------------------------------------------------------------------------- #
# Accounts projection
# --------------------------------------------------------------------------- #


async def test_accounts_returns_both_bookmakers_with_accurate_sums(
    two_book_client: AsyncClient,
) -> None:
    """The headline projection: two books, exact balances, exact totals.

    Sums are asserted in integer paise against hand-computed constants, so a
    float creeping into the aggregation path fails here rather than surfacing
    as an unexplainable rupee of drift on a dashboard.
    """
    response = await two_book_client.get("/api/v1/intelligence/accounts")

    assert response.status_code == 200
    payload = response.json()
    # computed fields are serialized out but not accepted as input when extra="forbid"
    for acc in payload.get("accounts", {}).values():
        acc.pop("total_account_value_paise", None)
    model = AccountsResponse.model_validate(payload)

    assert set(model.accounts.keys()) == {"pinnacle", "betfair"}

    pinnacle = model.accounts["pinnacle"]
    assert pinnacle.bookmaker is LinkedBookmaker.PINNACLE
    assert pinnacle.realized_balance_paise == _PINNACLE_BALANCE
    assert pinnacle.unsettled_exposure_paise == _PINNACLE_EXPOSURE
    assert pinnacle.deployable_capital_paise == _PINNACLE_BALANCE
    assert pinnacle.exposure_for_sport_paise(SportType.SOCCER) == _PINNACLE_EXPOSURE

    betfair = model.accounts["betfair"]
    assert betfair.bookmaker is LinkedBookmaker.BETFAIR
    assert betfair.realized_balance_paise == _BETFAIR_BALANCE
    assert betfair.unsettled_exposure_paise == _BETFAIR_EXPOSURE

    assert model.total_realized_balance_paise == _PINNACLE_BALANCE + _BETFAIR_BALANCE
    assert model.total_realized_balance_paise == 8_000_000
    assert (
        model.total_unsettled_exposure_paise
        == _PINNACLE_EXPOSURE + _BETFAIR_EXPOSURE
    )
    assert model.total_unsettled_exposure_paise == 375_000
    assert model.total_account_value_paise == 8_375_000

    assert set(model.age_seconds.keys()) == {"pinnacle", "betfair"}
    for age in model.age_seconds.values():
        assert 0.0 <= age < 60.0

    assert isinstance(model.as_of, str)
    assert datetime.fromisoformat(model.as_of).tzinfo is not None


async def test_accounts_money_fields_are_integers_on_the_wire(
    two_book_client: AsyncClient,
) -> None:
    """Paise must serialise as JSON integers, never as floats.

    A float on the wire is how a frontend eventually renders
    ``5000000.0000001`` and how a reconciliation break becomes unexplainable.
    """
    payload: dict[str, Any] = (
        await two_book_client.get("/api/v1/intelligence/accounts")
    ).json()

    assert isinstance(payload["total_realized_balance_paise"], int)
    assert isinstance(payload["total_unsettled_exposure_paise"], int)
    for account in payload["accounts"].values():
        assert isinstance(account["realized_balance_paise"], int)
        assert isinstance(account["unsettled_exposure_paise"], int)
        assert not isinstance(account["realized_balance_paise"], bool)


async def test_accounts_on_a_cold_cache_returns_an_empty_projection(
    cold_client: AsyncClient,
) -> None:
    """No snapshots means an empty map and zero totals, not a 500."""
    response = await cold_client.get("/api/v1/intelligence/accounts")

    assert response.status_code == 200
    payload = response.json()
    for acc in payload.get("accounts", {}).values():
        acc.pop("total_account_value_paise", None)
    model = AccountsResponse.model_validate(payload)
    assert model.accounts == {}
    assert model.age_seconds == {}
    assert model.total_realized_balance_paise == 0
    assert model.total_unsettled_exposure_paise == 0


async def test_single_account_endpoint_returns_the_snapshot(
    two_book_client: AsyncClient,
) -> None:
    response = await two_book_client.get("/api/v1/intelligence/accounts/betfair")

    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert payload["bookmaker"] == "betfair"
    assert payload["realized_balance_paise"] == _BETFAIR_BALANCE
    assert payload["total_account_value_paise"] == _BETFAIR_BALANCE + _BETFAIR_EXPOSURE


async def test_single_account_endpoint_404s_for_an_uncached_bookmaker(
    two_book_client: AsyncClient,
) -> None:
    """A valid but uncached bookmaker is a 404, not an empty object.

    An empty snapshot would render as a zero balance, which is
    indistinguishable from a genuinely empty account.
    """
    response = await two_book_client.get("/api/v1/intelligence/accounts/stake")

    assert response.status_code == 404
    assert "no cached state" in response.json()["detail"]


async def test_single_account_endpoint_422s_for_an_unknown_bookmaker(
    two_book_client: AsyncClient,
) -> None:
    """Enum-typed path parameters are rejected by FastAPI before the handler."""
    response = await two_book_client.get(
        "/api/v1/intelligence/accounts/not-a-bookmaker"
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Profile projection
# --------------------------------------------------------------------------- #


async def test_profile_returns_the_active_policy(
    two_book_client: AsyncClient,
) -> None:
    """The profile projection, including its derived Kelly multiplier."""
    response = await two_book_client.get("/api/v1/intelligence/profile")

    assert response.status_code == 200
    payload: dict[str, Any] = response.json()

    assert payload["profile_id"] == TEST_PROFILE_ID
    assert payload["daily_loss_limit_paise"] == 1_000_000
    assert payload["max_exposure_per_sport_paise"] == 600_000
    assert payload["max_single_stake_paise"] == 200_000
    assert payload["volatility_tolerance"] == pytest.approx(0.30)
    assert payload["preferred_sports"] == ["soccer"]
    assert payload["allow_parlays"] is False
    # 0.10 + 0.30 * (0.50 - 0.10) = 0.22, and never full Kelly by construction.
    assert payload["kelly_fraction_multiplier"] == pytest.approx(0.22)
    assert payload["kelly_fraction_multiplier"] < 1.0


async def test_profile_404s_when_the_id_is_not_cached(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """A mismatched profile id must 404, not return zeroed limits.

    A dashboard showing a zero daily loss limit is indistinguishable from one
    showing a genuine hard stop, so an empty object would be actively unsafe.
    """
    store = InMemoryStateStore()
    await store.set_profile(make_profile(profile_id="someone-else"))
    app = build_api(
        store, broadcaster, profile_id="missing-profile", enable_docs=False
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://betdoc.test"
    ) as client:
        response = await client.get("/api/v1/intelligence/profile")

    assert response.status_code == 404
    assert "missing-profile" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# System endpoints and wiring
# --------------------------------------------------------------------------- #


async def test_liveness_does_not_depend_on_the_cache(
    cold_client: AsyncClient,
) -> None:
    """Liveness must stay 200 while the cache is cold.

    A liveness probe that inspects dependencies gets the process restarted
    during a transient cache miss, converting a recoverable degradation into
    an outage.
    """
    response = await cold_client.get("/health")

    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "betdoc-advisor"
    assert isinstance(payload["version"], str)


async def test_readiness_is_200_when_warm_and_503_when_cold(
    two_book_client: AsyncClient, cold_client: AsyncClient
) -> None:
    """Readiness gates traffic; liveness does not."""
    warm = await two_book_client.get("/ready")
    assert warm.status_code == 200
    assert warm.json()["ready"] is True
    assert warm.json()["cache_warm"] is True
    assert warm.json()["websocket_clients"] == 0

    cold = await cold_client.get("/ready")
    assert cold.status_code == 503
    assert cold.json()["ready"] is False
    assert cold.json()["cache_warm"] is False


async def test_version_header_is_present_on_every_response(
    two_book_client: AsyncClient,
) -> None:
    """The middleware must apply to routed and system endpoints alike."""
    for path in (
        "/health",
        "/api/v1/intelligence/health",
        "/api/v1/intelligence/accounts",
    ):
        response = await two_book_client.get(path)
        assert response.headers.get("X-BetDoc-Version"), f"missing header on {path}"


async def test_cors_allows_a_browser_origin(two_book_client: AsyncClient) -> None:
    """Wildcard CORS with credentials disabled, as configured."""
    response = await two_book_client.options(
        "/api/v1/intelligence/accounts",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "GET" in response.headers.get("access-control-allow-methods", "")
    assert "access-control-allow-credentials" not in response.headers, (
        "credentials must not be enabled on a wildcard origin"
    )


async def test_mutating_methods_are_not_exposed(
    two_book_client: AsyncClient,
) -> None:
    """The presentation layer is read-only by construction.

    No HTTP request can move money, whatever a future authentication bug lets
    through. 405 rather than 404 confirms the route exists and the method is
    refused.
    """
    for method, path in (
        ("POST", "/api/v1/intelligence/profile"),
        ("PUT", "/api/v1/intelligence/profile"),
        ("DELETE", "/api/v1/intelligence/accounts/pinnacle"),
        ("PATCH", "/api/v1/intelligence/accounts/pinnacle"),
    ):
        response = await two_book_client.request(method, path)
        assert response.status_code in (404, 405), (
            f"{method} {path} returned {response.status_code}; "
            "a mutation path may have been introduced"
        )


async def test_docs_are_disabled_when_requested(
    two_book_client: AsyncClient,
) -> None:
    """``enable_docs=False`` must remove the schema surface entirely."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = await two_book_client.get(path)
        assert response.status_code == 404, f"{path} was still served"


async def test_dependency_returns_503_when_state_is_not_wired(
    broadcaster: WebsocketBroadcaster, warm_state_store: InMemoryStateStore
) -> None:
    """A mis-wired composition root must surface as 503, not AttributeError.

    A missing dependency is an operational state ("not ready"), not a bug in
    the request, and the type check catches a root that stored the wrong
    instance on ``app.state``.
    """
    app: FastAPI = build_api(
        warm_state_store, broadcaster, profile_id=TEST_PROFILE_ID, enable_docs=False
    )
    app.state.store = "not a state store"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://betdoc.test"
    ) as client:
        response = await client.get("/api/v1/intelligence/accounts")

    assert response.status_code == 503
    assert "misconfigured" in response.json()["detail"]


async def test_stale_snapshot_is_still_served_with_its_age(
    broadcaster: WebsocketBroadcaster, profile: BettorProfile
) -> None:
    """Stale state is served and labelled, never withheld.

    The Twin already refuses to size against a stale snapshot through its own
    ``max_account_state_age_seconds`` gate. Duplicating that policy in the API
    would guarantee the two eventually disagree, so the API reports the age and
    lets the client decide how to render it.
    """
    store = InMemoryStateStore()
    await store.set_profile(profile)
    await store.set_account_state(
        make_account_state(
            bookmaker=LinkedBookmaker.PINNACLE,
            as_of=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    app = build_api(store, broadcaster, profile_id=TEST_PROFILE_ID, enable_docs=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://betdoc.test"
    ) as client:
        response = await client.get("/api/v1/intelligence/accounts")

    assert response.status_code == 200
    payload = response.json()
    for acc in payload.get("accounts", {}).values():
        acc.pop("total_account_value_paise", None)
    model = AccountsResponse.model_validate(payload)
    assert "pinnacle" in model.accounts
    # ``age_seconds`` tracks the *snapshot* clock, so a 30-minute-old
    # ``as_of`` must be visible even though the cache entry was just written.
    account = model.accounts["pinnacle"]
    assert account.staleness_seconds() > 1_500
