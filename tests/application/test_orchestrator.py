"""Orchestrator behaviour under load, failure and shutdown.

Every test here starts ``run()`` as a task and unwinds it with
``shutdown.set()``. Nothing awaits an unbounded loop directly, so a regression
produces a failed assertion rather than a hung CI job.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pytest

from betdoc.adapters.cache.state_store import InMemoryStateStore
from betdoc.application.event_bus import ConsumedMessage, EventBus
from betdoc.application.events import EventEnvelope
from betdoc.application.orchestrator import (
    OpportunityOrchestrator,
    OrchestratorConfig,
)
from betdoc.application.ports.intelligence_clients import (
    AccountUnavailableError,
    TransientIntelligenceError,
)
from betdoc.domain.intelligence.account_models import (
    AccountState,
    BettorProfile,
    LinkedBookmaker,
)
from betdoc.domain.intelligence.advisor_models import MarketOpportunity
from betdoc.services.advisor.twin_engine import (
    TwinAdvisorConfig,
    TwinAdvisorService,
    TwinSessionResult,
)
from tests.conftest import (
    DEFAULT_TIMEOUT,
    TEST_PROFILE_ID,
    RecordingNotifier,
    make_account_state,
    make_forecast,
    make_opportunity,
    make_recommendation,
    wait_for,
    wrap,
)

pytestmark = pytest.mark.asyncio

_STREAM: Final[str] = "test:opportunities"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeEventBus:
    """Yields a controlled set of messages, then parks until shutdown.

    Parking rather than terminating is deliberate: the real bus generator is
    infinite, so a fake that ends would let the consume loop exit for a reason
    that never occurs in production and would hide shutdown bugs.
    """

    def __init__(self, shutdown: asyncio.Event) -> None:
        self._shutdown = shutdown
        self.pending: list[EventEnvelope[MarketOpportunity]] = []
        self.acknowledged: list[str] = []
        self.consume_calls = 0
        self.closed = False

    def push(self, *envelopes: EventEnvelope[MarketOpportunity]) -> None:
        self.pending.extend(envelopes)

    async def consume(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        payload_model: type[Any],
        batch_size: int = 10,
        *,
        block_ms: int = 5_000,
        reclaim_pending: bool = True,
    ) -> AsyncIterator[ConsumedMessage[MarketOpportunity]]:
        self.consume_calls += 1
        index = 0
        while True:
            if self.pending:
                envelope = self.pending.pop(0)
                message_id = f"{index}-0"
                index += 1
                message = ConsumedMessage(stream_name, message_id, envelope)
                yield message
                # Mirrors the real bus contract: the ack happens on resumption,
                # which is only reached when the consumer body did not raise.
                self.acknowledged.append(message_id)
                continue
            if self._shutdown.is_set():
                return
            await asyncio.sleep(0.005)

    async def close(self) -> None:
        self.closed = True


class ScriptedTwin(TwinAdvisorService):
    """Real subclass with a scripted ``evaluate_session``.

    Subclassed rather than mocked so the orchestrator's type contract is
    exercised and so a signature change in the engine breaks these tests, which
    is exactly what should happen.
    """

    __slots__ = ("calls", "error", "result", "seen_opportunities")

    def __init__(
        self,
        *,
        result: TwinSessionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(TwinAdvisorConfig())
        self.calls = 0
        self.error = error
        self.result = result
        self.seen_opportunities: list[tuple[str, ...]] = []

    def evaluate_session(
        self,
        *,
        profile: BettorProfile,
        account_state: AccountState,
        opportunities: Sequence[MarketOpportunity],
        modifiers: Sequence[Any] = (),
        realized_pnl_today_paise: int = 0,
        trace_id: str = "",
        now: datetime | None = None,
    ) -> TwinSessionResult:
        self.calls += 1
        self.seen_opportunities.append(
            tuple(item.opportunity_id for item in opportunities)
        )
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return super().evaluate_session(
            profile=profile,
            account_state=account_state,
            opportunities=opportunities,
            modifiers=modifiers,
            realized_pnl_today_paise=realized_pnl_today_paise,
            trace_id=trace_id,
            now=now,
        )


def canned_result(*, screened: int = 1) -> TwinSessionResult:
    """A session result with one actionable bet and one refusal."""
    actionable = make_recommendation(recommendation_id="rec-ok")
    refusal = make_recommendation(
        recommendation_id="rec-pass",
        opportunity_id="opp-2",
        action_type=__import__(
            "betdoc.domain.intelligence.advisor_models", fromlist=["ActionType"]
        ).ActionType.PASS,
    )
    return TwinSessionResult(
        forecast=make_forecast(screened=screened, issued=1),
        recommendations=(actionable, refusal),
        sizing_audit=(),
    )


def build_orchestrator(
    *,
    bus: FakeEventBus,
    twin: TwinAdvisorService,
    store: InMemoryStateStore,
    notifier: RecordingNotifier,
    shutdown: asyncio.Event,
    max_concurrent_sessions: int = 4,
    batch_max_size: int = 32,
    batch_window_ms: float = 40.0,
    max_consecutive_failures: int = 20,
    queue_max_size: int = 5_000,
) -> OpportunityOrchestrator:
    return OpportunityOrchestrator(
        bus=cast(EventBus, bus),
        twin=twin,
        state=store,
        notifier=notifier,
        config=OrchestratorConfig(
            stream_name=_STREAM,
            consumer_group="test-group",
            consumer_name="test-consumer",
            profile_id=TEST_PROFILE_ID,
            queue_max_size=queue_max_size,
            max_concurrent_sessions=max_concurrent_sessions,
            batch_max_size=batch_max_size,
            batch_window_ms=batch_window_ms,
            consume_batch_size=64,
            consume_block_ms=50,
            max_opportunity_age_seconds=30.0,
            transient_backoff_seconds=0.01,
            max_consecutive_failures=max_consecutive_failures,
        ),
        shutdown=shutdown,
    )


async def stop(task: asyncio.Task[None], shutdown: asyncio.Event) -> None:
    """Unwind ``run()`` cleanly, cancelling only as a last resort."""
    shutdown.set()
    try:
        await asyncio.wait_for(task, timeout=DEFAULT_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        pytest.fail("orchestrator.run() did not unwind on shutdown within the timeout")


# --------------------------------------------------------------------------- #
# Standard consumption
# --------------------------------------------------------------------------- #


async def test_consumes_batch_evaluates_and_notifies(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """The happy path: stream to Twin to notifier, end to end."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result(screened=2))
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity(opportunity_id="opp-a")))
    bus.push(wrap(make_opportunity(opportunity_id="opp-b")))

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: notifier.sessions), "no session was delivered"
    await stop(task, shutdown_event)

    assert twin.calls >= 1
    assert bus.consume_calls == 1
    assert len(bus.acknowledged) == 2, "both messages must be acknowledged"

    forecast, recommendations = notifier.sessions[0]
    assert forecast.verdict.should_trade
    assert len(recommendations) == 2
    assert orchestrator.metrics.messages_consumed == 2
    assert orchestrator.metrics.batches_evaluated >= 1
    assert orchestrator.metrics.batches_failed == 0
    assert orchestrator.metrics.recommendations_issued >= 1
    assert orchestrator.metrics.rejections_issued >= 1


async def test_batch_deduplicates_repeated_opportunity_ids(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A re-quote inside one window must not consume the headroom twice.

    Without deduplication the same selection re-quoted three times in 40ms
    would be evaluated as three independent candidates and would draw on the
    per-sport exposure cap three times over.
    """
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
        batch_window_ms=120.0,
    )

    for _ in range(4):
        bus.push(wrap(make_opportunity(opportunity_id="same-id")))

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: twin.calls >= 1)
    await asyncio.sleep(0.2)
    await stop(task, shutdown_event)

    flattened = [item for call in twin.seen_opportunities for item in call]
    assert flattened.count("same-id") == len(twin.seen_opportunities), (
        "each evaluation must see the id at most once"
    )
    for call in twin.seen_opportunities:
        assert len(set(call)) == len(call), "duplicate ids leaked into one batch"


async def test_expired_opportunities_are_discarded_before_evaluation(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """Stale quotes are dropped at the door and counted, never evaluated."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(
        wrap(
            make_opportunity(
                opportunity_id="ancient",
                quoted_at=datetime.now(UTC) - timedelta(seconds=120),
            )
        )
    )

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: orchestrator.metrics.messages_expired >= 1)
    await asyncio.sleep(0.1)
    await stop(task, shutdown_event)

    assert orchestrator.metrics.messages_expired == 1
    assert twin.calls == 0, "an expired quote must never reach the Twin"
    assert notifier.sessions == []
    assert len(bus.acknowledged) == 1, "expired messages must still be acknowledged"


async def test_cold_cache_skips_the_batch_without_crashing(
    state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """No account snapshot means skip, not size a bet against ``None``."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity()))
    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: orchestrator.metrics.state_unavailable >= 1)
    await stop(task, shutdown_event)

    assert twin.calls == 0
    assert notifier.sessions == []
    assert orchestrator.metrics.state_unavailable >= 1
    assert not task.cancelled()


# --------------------------------------------------------------------------- #
# Error resilience
# --------------------------------------------------------------------------- #


async def test_transient_error_drops_the_batch_and_keeps_the_loop_alive(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A transient fault must cost one batch, never the consumer.

    A dead consumer stops acknowledging, and the Redis pending list then grows
    without bound, so keeping this loop alive is a memory-safety property, not
    merely a convenience.
    """
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(
        error=TransientIntelligenceError("upstream timeout", provider="test")
    )
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity(opportunity_id="fails-1")))
    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: orchestrator.metrics.batches_failed >= 1)

    # The loop must still be running and still consuming after the failure.
    assert not task.done(), "a transient fault killed the orchestrator"
    bus.push(wrap(make_opportunity(opportunity_id="fails-2")))
    assert await wait_for(lambda: twin.calls >= 2), "loop stopped consuming"

    await stop(task, shutdown_event)
    assert notifier.sessions == []
    assert orchestrator.metrics.batches_failed >= 2
    assert orchestrator.metrics.consecutive_failures >= 1


async def test_recovers_and_resets_the_failure_counter(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """One success must clear the consecutive-failure brake.

    If it did not, twenty failures spread across a whole day would eventually
    trip the stand-down as though they had occurred back to back.
    """
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(error=TransientIntelligenceError("blip", provider="test"))
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity(opportunity_id="blip-1")))
    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: orchestrator.metrics.consecutive_failures >= 1)

    twin.error = None
    twin.result = canned_result()
    bus.push(wrap(make_opportunity(opportunity_id="ok-1")))
    assert await wait_for(lambda: notifier.sessions)

    await stop(task, shutdown_event)
    assert orchestrator.metrics.consecutive_failures == 0
    assert orchestrator.metrics.batches_evaluated >= 1


async def test_unrecoverable_account_error_does_not_trip_the_brake(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A restricted account is a state, not a fault to retry against.

    It must not accumulate towards the stand-down threshold, otherwise one
    permanently limited bookmaker would eventually halt the whole engine.
    """
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(
        error=AccountUnavailableError(
            "account restricted", bookmaker=LinkedBookmaker.PINNACLE
        )
    )
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity(opportunity_id="restricted-1")))
    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: orchestrator.metrics.batches_failed >= 1)
    await stop(task, shutdown_event)

    assert orchestrator.metrics.batches_failed >= 1
    assert notifier.sessions == []


async def test_notifier_failure_does_not_abort_evaluation(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A dead channel must not stop bets from being evaluated.

    The recommendation is already computed and recorded; losing the alert is a
    degradation, losing the evaluation is an outage.
    """
    from tests.conftest import FailingNotifier

    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = FailingNotifier()
    orchestrator = OpportunityOrchestrator(
        bus=cast(EventBus, bus),
        twin=twin,
        state=warm_state_store,
        notifier=notifier,
        config=OrchestratorConfig(
            stream_name=_STREAM,
            consumer_group="test-group",
            consumer_name="test-consumer",
            profile_id=TEST_PROFILE_ID,
            batch_window_ms=40.0,
            consume_block_ms=50,
            transient_backoff_seconds=0.01,
        ),
        shutdown=shutdown_event,
    )

    bus.push(wrap(make_opportunity()))
    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: twin.calls >= 1)
    await stop(task, shutdown_event)

    assert notifier.attempts >= 1
    assert orchestrator.metrics.batches_evaluated >= 1
    assert orchestrator.metrics.batches_failed == 0
    assert orchestrator.metrics.notifications_delivered == 0


async def test_persistent_failure_stands_the_loop_down(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A hot failure loop must terminate itself rather than spin forever."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(error=TransientIntelligenceError("down", provider="test"))
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
        max_consecutive_failures=3,
        batch_max_size=1, # FORCE separate batches to trip the counter
    )

    for index in range(10):
        bus.push(wrap(make_opportunity(opportunity_id=f"bad-{index}")))

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    await asyncio.wait_for(task, timeout=DEFAULT_TIMEOUT)

    assert shutdown_event.is_set(), "stand-down must set the shutdown flag"
    assert orchestrator.metrics.consecutive_failures >= 3

# --------------------------------------------------------------------------- #
# Real-time concurrency
# --------------------------------------------------------------------------- #


async def test_semaphore_caps_concurrent_sessions_at_the_configured_width(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """Ten simultaneous events, four permits: exactly four run, six wait.

    The notifier is gated open, so every admitted session parks inside the
    semaphore. Peak observed concurrency is therefore an exact measurement of
    the permit width, not an approximation.

    This test fails against the pre-patch ``_dispatch_loop``, which awaited the
    evaluation inline and could only ever reach a concurrency of one.
    """
    gate = asyncio.Event()
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier(gate=gate)
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
        max_concurrent_sessions=4,
        batch_max_size=1,
        batch_window_ms=20.0,
    )

    for index in range(10):
        bus.push(wrap(make_opportunity(opportunity_id=f"conc-{index}")))

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")

    assert await wait_for(lambda: notifier.concurrent_now >= 4, timeout=DEFAULT_TIMEOUT), (
        f"never reached 4 concurrent sessions (peak {notifier.concurrent_peak})"
    )
    # Hold the gate closed and give the loop ample opportunity to over-admit.
    await asyncio.sleep(0.3)

    assert notifier.concurrent_now == 4, (
        f"expected exactly 4 in flight, saw {notifier.concurrent_now}"
    )
    assert notifier.concurrent_peak == 4, (
        f"semaphore breached: peak concurrency was {notifier.concurrent_peak}"
    )
    assert len(notifier.sessions) == 0, "gated sessions must not have completed"

    gate.set()
    assert await wait_for(lambda: len(notifier.sessions) >= 10, timeout=DEFAULT_TIMEOUT), (
        f"only {len(notifier.sessions)} of 10 sessions completed after release"
    )
    await stop(task, shutdown_event)

    assert notifier.concurrent_now == 0, "a permit leaked"
    assert notifier.concurrent_peak <= 4
    assert orchestrator.metrics.batches_evaluated >= 10


async def test_permits_are_released_when_a_session_raises(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """A crash inside a session must not leak its permit.

    Leaked permits are silent and cumulative: after ``max_concurrent_sessions``
    of them the pipeline stops entirely with no error to point at.
    """
    from tests.conftest import FailingNotifier

    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = FailingNotifier()
    orchestrator = OpportunityOrchestrator(
        bus=cast(EventBus, bus),
        twin=twin,
        state=warm_state_store,
        notifier=notifier,
        config=OrchestratorConfig(
            stream_name=_STREAM,
            consumer_group="test-group",
            consumer_name="test-consumer",
            profile_id=TEST_PROFILE_ID,
            max_concurrent_sessions=2,
            batch_max_size=1,
            batch_window_ms=20.0,
            consume_block_ms=50,
            transient_backoff_seconds=0.01,
        ),
        shutdown=shutdown_event,
    )

    for index in range(6):
        bus.push(wrap(make_opportunity(opportunity_id=f"leak-{index}")))

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    assert await wait_for(lambda: twin.calls >= 6, timeout=DEFAULT_TIMEOUT), (
        f"throughput stalled after {twin.calls} evaluations; a permit leaked"
    )
    await stop(task, shutdown_event)
    assert orchestrator.metrics.batches_evaluated >= 6


# --------------------------------------------------------------------------- #
# Backpressure and shutdown
# --------------------------------------------------------------------------- #


async def test_queue_overflow_sheds_oldest_and_never_blocks(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """``_offer`` must be non-blocking and bounded under a spike."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
        queue_max_size=10,
        batch_max_size=10, # Must not exceed queue_max_size
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    for index in range(5_000):
        orchestrator._offer(wrap(make_opportunity(opportunity_id=f"spike-{index}")))  # noqa: SLF001
    elapsed = loop.time() - started

    assert elapsed < 1.0, f"_offer took {elapsed:.3f}s for 5000 events; it blocked"
    assert orchestrator._queue.qsize() <= 10, "queue exceeded its bound"  # noqa: SLF001
    assert orchestrator.metrics.messages_shed == 4_990
    assert orchestrator.metrics.peak_queue_depth <= 10


async def test_shutdown_unwinds_both_loops_promptly(
    warm_state_store: InMemoryStateStore, shutdown_event: asyncio.Event
) -> None:
    """``run()`` must return on the shutdown flag, without cancellation."""
    bus = FakeEventBus(shutdown_event)
    twin = ScriptedTwin(result=canned_result())
    notifier = RecordingNotifier()
    orchestrator = build_orchestrator(
        bus=bus,
        twin=twin,
        store=warm_state_store,
        notifier=notifier,
        shutdown=shutdown_event,
    )

    task = asyncio.create_task(orchestrator.run(), name="orchestrator")
    await asyncio.sleep(0.05)
    assert not task.done()

    shutdown_event.set()
    await asyncio.wait_for(task, timeout=DEFAULT_TIMEOUT)

    assert task.done()
    assert not task.cancelled(), "shutdown must be cooperative, not cancellation"
    assert task.exception() is None
