"""Shared fixtures for the BetDoc suite.

Two rules this file enforces, both learned the hard way:

* **No ``MagicMock`` for domain objects.** Every model here is a genuinely
  valid Pydantic V2 instance that satisfies its own validators. A mock passes
  any assertion you write and none that the production validators would apply,
  so a suite built on mocks proves the tests work rather than the system.
* **No unbounded awaits.** Every fixture that starts a task registers its
  teardown, and every test-side await is wrapped in a timeout. A hung test
  runner in CI is indistinguishable from a broken build.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from betdoc.adapters.cache.state_store import InMemoryStateStore
from betdoc.application.events import EventEnvelope
from betdoc.application.ports.notifiers import (
    DebounceDecision,
    NotificationChannel,
    RecommendationNotifier,
)
from betdoc.domain.intelligence.account_models import (
    AccountState,
    BettorProfile,
    LinkedBookmaker,
    SportType,
)
from betdoc.domain.intelligence.advisor_models import (
    ActionType,
    ForecastVerdict,
    MarketOpportunity,
    RejectionReason,
    SessionForecast,
    TwinRecommendation,
    VolatilityRegime,
)
from betdoc.presentation.api.app import build_api
from betdoc.presentation.api.broadcaster import (
    AlwaysAllowDebouncer,
    BroadcasterConfig,
    WebsocketBroadcaster,
)

TEST_PROFILE_ID: Final[str] = "test-profile"
FIXED_NOW: Final[datetime] = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
DEFAULT_TIMEOUT: Final[float] = 5.0


# --------------------------------------------------------------------------- #
# Domain fixtures: real Pydantic V2 instances, not mocks
# --------------------------------------------------------------------------- #


def make_opportunity(
    *,
    opportunity_id: str = "opp-1",
    bookmaker: LinkedBookmaker = LinkedBookmaker.PINNACLE,
    fixture_id: str = "soccer_epl:ars-che",
    market_key: str = "h2h",
    outcome_key: str = "home",
    offered_odds: float = 2.10,
    fair_probability: float = 0.52,
    quoted_at: datetime | None = None,
    sport_type: SportType = SportType.SOCCER,
) -> MarketOpportunity:
    """A valid, positive-EV opportunity.

    Default edge is ``0.52 * 2.10 - 1 = +9.2%`` per unit staked, comfortably
    above every gate in :class:`TwinAdvisorConfig`, so a test that expects a
    recommendation gets one for the right reason.
    """
    return MarketOpportunity(
        opportunity_id=opportunity_id,
        bookmaker=bookmaker,
        sport_type=sport_type,
        fixture_id=fixture_id,
        market_key=market_key,
        outcome_key=outcome_key,
        selection_label=f"{fixture_id} {outcome_key}",
        offered_odds=offered_odds,
        fair_probability=fair_probability,
        quoted_at=quoted_at or datetime.now(UTC),
        max_accepted_stake_paise=2_000_000,
    )


def make_recommendation(
    *,
    recommendation_id: str = "rec-1",
    opportunity_id: str = "opp-1",
    action_type: ActionType = ActionType.SINGLE,
    stake_paise: int = 150_000,
    target_odds: float = 2.10,
    rejection_reason: RejectionReason | None = None,
    trace_id: str = "trace-abcdef123456",
    generated_at: datetime | None = None,
    news_adjusted_probability: float | None = 0.52,
    uncertainty_penalty: float = 1.0,
) -> TwinRecommendation:
    """A valid recommendation, honouring the PASS / actionable invariants.

    :class:`TwinRecommendation` refuses a PASS with a non-zero stake and an
    actionable verdict with a rejection reason, so this helper derives both
    sides from ``action_type`` rather than letting a caller build an object the
    production validator would reject.
    """
    is_pass = action_type is ActionType.PASS
    reason = rejection_reason or (RejectionReason.NEGATIVE_EV if is_pass else None)
    return TwinRecommendation(
        recommendation_id=recommendation_id,
        opportunity_id=opportunity_id,
        trace_id=trace_id,
        generated_at=generated_at or datetime.now(UTC),
        action_type=action_type,
        bookmaker=LinkedBookmaker.PINNACLE,
        sport_type=SportType.SOCCER,
        selection_label="Arsenal moneyline",
        suggested_stake_paise=0 if is_pass else stake_paise,
        target_odds=target_odds,
        justification_string="fixture-generated recommendation for testing",
        rejection_reason=reason,
        fair_probability=0.52,
        news_adjusted_probability=news_adjusted_probability,
        applied_uncertainty_penalty=uncertainty_penalty,
        full_kelly_fraction=0.0 if is_pass else 0.09,
        final_kelly_fraction=0.0 if is_pass else 0.02,
        expected_value_paise=0 if is_pass else 13_800,
        binding_constraint="protective_kelly" if not is_pass else "negative_ev",
        valid_for_seconds=60.0,
    )


def make_forecast(
    *,
    verdict: ForecastVerdict = ForecastVerdict.ACCEPTABLE,
    screened: int = 1,
    issued: int = 1,
    stake_paise: int = 150_000,
    deployable_paise: int = 5_000_000,
    trace_id: str = "trace-abcdef123456",
) -> SessionForecast:
    """A valid forecast satisfying every cross-field validator."""
    return SessionForecast(
        forecast_id="forecast-1",
        generated_at=datetime.now(UTC),
        trace_id=trace_id,
        verdict=verdict,
        volatility_regime=VolatilityRegime.CALM,
        opportunities_screened=screened,
        recommendations_issued=issued,
        rejections_by_reason={},
        total_recommended_stake_paise=stake_paise,
        aggregate_expected_value_paise=13_800,
        remaining_loss_budget_paise=800_000,
        deployable_capital_paise=deployable_paise,
        active_news_alerts=0,
        mean_uncertainty_penalty=1.0,
        max_uncertainty_penalty=1.0,
        warnings=(),
    )


def make_profile(
    *,
    profile_id: str = TEST_PROFILE_ID,
    daily_loss_limit_paise: int = 1_000_000,
    max_exposure_per_sport_paise: int = 600_000,
    max_single_stake_paise: int = 200_000,
    volatility_tolerance: float = 0.30,
) -> BettorProfile:
    """A valid profile.

    ``max_single_stake_paise`` is kept below ``max_exposure_per_sport_paise``
    because the model rejects the inverse: a single-stake cap above the sport
    cap could never bind and would therefore be inert policy.
    """
    return BettorProfile(
        profile_id=profile_id,
        daily_loss_limit_paise=daily_loss_limit_paise,
        max_exposure_per_sport_paise=max_exposure_per_sport_paise,
        max_single_stake_paise=max_single_stake_paise,
        volatility_tolerance=volatility_tolerance,
        preferred_sports=(SportType.SOCCER,),
        allow_parlays=False,
        settled_bet_count=0,
        historical_roi_percentage=0.0,
    )


def make_account_state(
    *,
    bookmaker: LinkedBookmaker = LinkedBookmaker.PINNACLE,
    realized_balance_paise: int = 5_000_000,
    unsettled_exposure_paise: int = 250_000,
    as_of: datetime | None = None,
    is_limited: bool = False,
) -> AccountState:
    """A valid account snapshot.

    ``open_bet_count`` and ``max_accepted_stake_paise`` are derived rather than
    hardcoded, because the model rejects positive exposure with zero open bets
    and exposure above ``ceiling * open_bet_count`` as a suspected units error.
    """
    has_exposure = unsettled_exposure_paise > 0
    return AccountState(
        bookmaker=bookmaker,
        as_of=as_of or datetime.now(UTC),
        realized_balance_paise=realized_balance_paise,
        unsettled_exposure_paise=unsettled_exposure_paise,
        open_bet_count=1 if has_exposure else 0,
        exposure_by_sport_paise=(
            {SportType.SOCCER: unsettled_exposure_paise} if has_exposure else {}
        ),
        is_limited=is_limited,
        max_accepted_stake_paise=max(unsettled_exposure_paise, 1) * 2,
    )


def wrap(opportunity: MarketOpportunity, *, trace_id: str = "trace-abcdef123456") -> EventEnvelope[MarketOpportunity]:
    """Envelope an opportunity exactly as the scanner would."""
    return EventEnvelope.wrap(opportunity, trace_id=trace_id, event_type="opportunity")


@pytest.fixture
def opportunity() -> MarketOpportunity:
    return make_opportunity()


@pytest.fixture
def recommendation() -> TwinRecommendation:
    return make_recommendation()


@pytest.fixture
def rejection() -> TwinRecommendation:
    return make_recommendation(
        recommendation_id="rec-pass",
        action_type=ActionType.PASS,
        rejection_reason=RejectionReason.NEGATIVE_EV,
    )


@pytest.fixture
def forecast() -> SessionForecast:
    return make_forecast()


@pytest.fixture
def profile() -> BettorProfile:
    return make_profile()


@pytest.fixture
def account_state() -> AccountState:
    return make_account_state()


# --------------------------------------------------------------------------- #
# Test doubles that are real subclasses, not mocks
# --------------------------------------------------------------------------- #


class RecordingNotifier(RecommendationNotifier):
    """Captures every delivery. A real subclass, so the ABC contract is honoured.

    An optional ``delay`` and ``gate`` make it usable as the await point for
    concurrency tests: the orchestrator's semaphore is held across
    ``notify_session``, so blocking here is what makes concurrency observable.
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
        channel: NotificationChannel = NotificationChannel.NULL,
    ) -> None:
        super().__init__(channel, AlwaysAllowDebouncer())
        self.delay = delay
        self.gate = gate
        self.started = 0
        self.closed = 0
        self.sessions: list[tuple[SessionForecast, tuple[TwinRecommendation, ...]]] = []
        self.recommendations: list[TwinRecommendation] = []
        self.rejections: list[tuple[TwinRecommendation, ...]] = []
        self.forecasts: list[SessionForecast] = []
        self.concurrent_now = 0
        self.concurrent_peak = 0
        self.entered = asyncio.Event()

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def notify_recommendation(
        self, recommendation: TwinRecommendation, *, now: datetime | None = None
    ) -> DebounceDecision:
        self.recommendations.append(recommendation)
        return self._debouncer.evaluate(recommendation, now=now)

    async def notify_rejections(
        self,
        rejections: Sequence[TwinRecommendation],
        *,
        now: datetime | None = None,
    ) -> int:
        self.rejections.append(tuple(rejections))
        return len(rejections)

    async def notify_forecast(
        self, forecast: SessionForecast, *, now: datetime | None = None
    ) -> None:
        self.forecasts.append(forecast)

    async def notify_session(
        self,
        *,
        forecast: SessionForecast,
        recommendations: Sequence[TwinRecommendation],
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """Override so the concurrency instrumentation wraps the whole session."""
        self.concurrent_now += 1
        self.concurrent_peak = max(self.concurrent_peak, self.concurrent_now)
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay > 0.0:
                await asyncio.sleep(self.delay)
            self.sessions.append((forecast, tuple(recommendations)))
            actionable = [
                item for item in recommendations if item.action_type.requires_stake
            ]
            rejected = [
                item for item in recommendations if item.action_type is ActionType.PASS
            ]
            self.recommendations.extend(actionable)
            self.rejections.append(tuple(rejected))
            self.forecasts.append(forecast)
            return len(actionable), len(rejected)
        finally:
            self.concurrent_now -= 1


class FailingNotifier(RecommendationNotifier):
    """Raises on every delivery. Proves fan-out isolation is real."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(NotificationChannel.NULL, AlwaysAllowDebouncer())
        self.error = error or RuntimeError("child notifier is down")
        self.attempts = 0
        self.closed = 0

    async def start(self) -> None:
        raise self.error

    async def close(self) -> None:
        self.closed += 1
        raise self.error

    async def notify_recommendation(
        self, recommendation: TwinRecommendation, *, now: datetime | None = None
    ) -> DebounceDecision:
        self.attempts += 1
        raise self.error

    async def notify_rejections(
        self,
        rejections: Sequence[TwinRecommendation],
        *,
        now: datetime | None = None,
    ) -> int:
        self.attempts += 1
        raise self.error

    async def notify_forecast(
        self, forecast: SessionForecast, *, now: datetime | None = None
    ) -> None:
        self.attempts += 1
        raise self.error

    async def notify_session(
        self,
        *,
        forecast: SessionForecast,
        recommendations: Sequence[TwinRecommendation],
        now: datetime | None = None,
    ) -> tuple[int, int]:
        self.attempts += 1
        raise self.error


class FakeWebSocket:
    """Minimal Starlette-compatible socket.

    Records sends and can be made arbitrarily slow, which is how a slow client
    is simulated without opening a real TCP connection.
    """

    def __init__(self, *, send_delay: float = 0.0, fail_after: int | None = None) -> None:
        from starlette.websockets import WebSocketState

        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.sent: list[str] = []
        self.send_delay = send_delay
        self.fail_after = fail_after
        self.closed_with: tuple[int, str] | None = None
        self.app: Any = None

    async def accept(self) -> None:
        return None

    async def send_text(self, data: str) -> None:
        if self.send_delay > 0.0:
            await asyncio.sleep(self.send_delay)
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            msg = "simulated transport failure"
            raise RuntimeError(msg)
        self.sent.append(data)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)
        return ""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        from starlette.websockets import WebSocketState

        self.closed_with = (code, reason)
        self.client_state = WebSocketState.DISCONNECTED


# --------------------------------------------------------------------------- #
# Infrastructure fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def state_store(profile: BettorProfile) -> InMemoryStateStore:
    """A real store, pre-seeded with a profile so the cache is warm."""
    store = InMemoryStateStore()
    await store.set_profile(profile)
    return store


@pytest_asyncio.fixture
async def warm_state_store(
    state_store: InMemoryStateStore, account_state: AccountState
) -> InMemoryStateStore:
    """Store with one profile and one account snapshot."""
    await state_store.set_account_state(account_state)
    return state_store


@pytest_asyncio.fixture
async def broadcaster() -> AsyncIterator[WebsocketBroadcaster]:
    """A real broadcaster with a deliberately tiny queue, always torn down."""
    instance = WebsocketBroadcaster(
        BroadcasterConfig(
            client_queue_size=10,
            max_clients=8,
            max_consecutive_drops=3,
            heartbeat_seconds=30.0,
            send_timeout_seconds=1.0,
        )
    )
    await instance.start()
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.close(), timeout=DEFAULT_TIMEOUT)


@pytest.fixture
def api_app(
    warm_state_store: InMemoryStateStore, broadcaster: WebsocketBroadcaster
) -> FastAPI:
    """The real app with real singletons wired onto ``app.state``.

    No dependency overrides. ``get_state_store`` and ``get_broadcaster`` read
    ``app.state`` directly, so wiring the state is the correct and complete
    injection mechanism; overriding the dependency would bypass the very
    resolution path under test.
    """
    return build_api(
        warm_state_store,
        broadcaster,
        profile_id=TEST_PROFILE_ID,
        enable_docs=False,
    )


@pytest_asyncio.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """In-process ASGI client. No sockets, no ports, no flakiness."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://betdoc.test") as client:
        yield client


@pytest.fixture
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


async def wait_for(condition: Any, *, timeout: float = DEFAULT_TIMEOUT, interval: float = 0.01) -> bool:
    """Poll ``condition()`` until true or the timeout expires.

    Returns a bool rather than raising so the caller writes the assertion, and
    the failure message names the invariant rather than saying "TimeoutError".
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return condition()
