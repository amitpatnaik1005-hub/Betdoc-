"""Wires the event bus to the Twin engine under bounded concurrency.



Three production hazards this module exists to contain:



#. **Unbounded fan-in.** A 5,000 msg/sec spike must never translate into 5,000

   concurrent tasks or an unbounded queue. Concurrency is capped by a

   semaphore, buffering is capped by a bounded queue, and overflow *sheds load

   by discarding the oldest item*. Discarding the oldest is correct for odds:

   the newest price is the only one with value, and a stale one is worthless

   even if it is delivered.

#. **Micro-batching, not per-message evaluation.** The Twin evaluates a

   *session*, sharing one loss budget and one set of per-sport headrooms across

   candidates. Calling it once per message would let each bet see the full

   budget independently and collectively breach it. Opportunities are therefore

   accumulated for a bounded window and evaluated together.

#. **Fault isolation.** ``TransientIntelligenceError`` and every other

   adapter fault are absorbed per batch. Nothing an adapter can do may kill the

   consume loop, because a dead consumer stops acknowledging and the stream's

   pending list grows without bound.

"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol, Self, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from betdoc.application.event_bus import ConsumedMessage, EventBus
from betdoc.application.events import EventEnvelope, Streams
from betdoc.application.ports.intelligence_clients import (
    AccountUnavailableError,
    IntelligenceError,
    ReconciliationBreachError,
    StaleAccountStateError,
    TransientIntelligenceError,
)
from betdoc.application.ports.notifiers import RecommendationNotifier
from betdoc.domain.intelligence.account_models import (
    AccountState,
    BettorProfile,
    LinkedBookmaker,
)
from betdoc.domain.intelligence.advisor_models import MarketOpportunity
from betdoc.domain.intelligence.news_models import ProbabilityModifier
from betdoc.services.advisor.twin_engine import TwinAdvisorService, TwinSessionResult

__all__ = [
    "OpportunityOrchestrator",
    "OrchestratorConfig",
    "OrchestratorMetrics",
    "StateProvider",
]


_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="application.orchestrator"
)


_SHED_LOG_EVERY: Final[int] = 100

"""Log one shed event per hundred. Logging every drop during a spike turns the

logger itself into the bottleneck."""


@runtime_checkable
class StateProvider(Protocol):
    """Memory-speed access to account and profile state.



    Deliberately synchronous-in-spirit but declared async so an implementation

    may await an internal lock. It must never perform network I/O: the whole

    point of the L1 cache is that the orchestrator's hot path cannot block on a

    bookmaker.

    """

    async def get_account_state(self, bookmaker: LinkedBookmaker) -> AccountState | None: ...

    async def get_profile(self, profile_id: str) -> BettorProfile | None: ...

    async def get_active_modifiers(
        self, *, now: datetime | None = None
    ) -> tuple[ProbabilityModifier, ...]: ...

    async def get_realized_pnl_today_paise(self, bookmaker: LinkedBookmaker) -> int: ...


class OrchestratorConfig(BaseModel):
    """Backpressure, batching and fault-handling policy."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    stream_name: str = Field(default=Streams.OPPORTUNITIES, min_length=1)

    consumer_group: str = Field(default="betdoc-advisor", min_length=1)

    consumer_name: str = Field(default="advisor-1", min_length=1)

    profile_id: str = Field(min_length=1, max_length=128)

    queue_max_size: int = Field(
        default=5_000,
        ge=10,
        le=1_000_000,
        description=(
            "Hard buffer ceiling. Bounded because an unbounded queue converts a "
            "downstream stall into an OOM kill rather than into backpressure."
        ),
    )

    max_concurrent_sessions: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Semaphore width over Twin evaluations plus notification.",
    )

    batch_max_size: int = Field(default=32, ge=1, le=500)

    batch_window_ms: float = Field(
        default=250.0,
        gt=0.0,
        le=10_000.0,
        description=(
            "Accumulation window. Trades a quarter second of latency for a "
            "shared loss budget across candidates, which is what makes the "
            "session-level caps actually bind."
        ),
    )

    consume_batch_size: int = Field(default=64, ge=1, le=1_000)

    consume_block_ms: int = Field(
        default=1_000,
        ge=50,
        le=30_000,
        description="Bounded so shutdown latency never exceeds one block period.",
    )

    max_opportunity_age_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=3_600.0,
        description="Discard before evaluation. The Twin gates again, tighter.",
    )

    transient_backoff_seconds: float = Field(default=2.0, gt=0.0, le=60.0)

    max_consecutive_failures: int = Field(
        default=20,
        ge=1,
        le=1_000,
        description="Consecutive batch failures after which the loop stands down.",
    )

    @model_validator(mode="after")
    def _batching_fits_inside_the_buffer(self) -> Self:

        if self.batch_max_size > self.queue_max_size:
            msg = "batch_max_size cannot exceed queue_max_size"

            raise ValueError(msg)

        return self


@dataclass(slots=True)
class OrchestratorMetrics:
    """Counters scraped by the observability layer, never read by logic."""

    messages_consumed: int = 0

    messages_shed: int = 0

    messages_expired: int = 0

    messages_undecodable: int = 0

    batches_evaluated: int = 0

    batches_failed: int = 0

    recommendations_issued: int = 0

    rejections_issued: int = 0

    notifications_delivered: int = 0

    notifications_suppressed: int = 0

    state_unavailable: int = 0

    consecutive_failures: int = 0

    peak_queue_depth: int = 0

    last_batch_monotonic_ns: int | None = field(default=None)

    def snapshot(self) -> dict[str, int | None]:

        return {
            "messages_consumed": self.messages_consumed,
            "messages_shed": self.messages_shed,
            "messages_expired": self.messages_expired,
            "messages_undecodable": self.messages_undecodable,
            "batches_evaluated": self.batches_evaluated,
            "batches_failed": self.batches_failed,
            "recommendations_issued": self.recommendations_issued,
            "rejections_issued": self.rejections_issued,
            "notifications_delivered": self.notifications_delivered,
            "notifications_suppressed": self.notifications_suppressed,
            "state_unavailable": self.state_unavailable,
            "consecutive_failures": self.consecutive_failures,
            "peak_queue_depth": self.peak_queue_depth,
        }


class OpportunityOrchestrator:
    """Consumes opportunities, evaluates them through the Twin, notifies.



    Two cooperating loops with a bounded queue between them:



    * :meth:`_consume_loop` reads the stream and offers into the queue. Because

      the event bus acknowledges on generator resumption, an exception raised

      here leaves the message pending for reclaim rather than losing it.

    * :meth:`_dispatch_loop` accumulates micro-batches and evaluates them under

      a semaphore.



    Separating them is what allows load shedding to happen at a single, well

    defined point instead of being smeared across the pipeline.

    """

    __slots__ = (
        "_bus",
        "_config",
        "_inflight",
        "_metrics",
        "_notifier",
        "_on_opportunity",
        "_queue",
        "_semaphore",
        "_shutdown",
        "_state",
        "_twin",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        twin: TwinAdvisorService,
        state: StateProvider,
        notifier: RecommendationNotifier,
        config: OrchestratorConfig,
        shutdown: asyncio.Event,
        on_opportunity: Callable[[MarketOpportunity], None] | None = None,
    ) -> None:

        self._on_opportunity = on_opportunity
        self._bus = bus

        self._twin = twin

        self._state = state

        self._notifier = notifier

        self._config = config

        self._shutdown = shutdown

        self._queue: asyncio.Queue[EventEnvelope[MarketOpportunity]] = asyncio.Queue(
            maxsize=config.queue_max_size
        )

        self._semaphore = asyncio.Semaphore(config.max_concurrent_sessions)
        self._inflight: set[asyncio.Task[None]] = set()

        self._metrics = OrchestratorMetrics()

    @property
    def metrics(self) -> OrchestratorMetrics:

        return self._metrics

    async def run(self) -> None:
        """Run both loops until shutdown, then drain what is already buffered."""

        _log.info(
            "orchestrator.started",
            stream=self._config.stream_name,
            group=self._config.consumer_group,
            consumer=self._config.consumer_name,
            queue_max_size=self._config.queue_max_size,
            max_concurrent_sessions=self._config.max_concurrent_sessions,
            batch_max_size=self._config.batch_max_size,
            batch_window_ms=self._config.batch_window_ms,
        )

        consumer = asyncio.create_task(self._consume_loop(), name="orch-consume")

        dispatcher = asyncio.create_task(self._dispatch_loop(), name="orch-dispatch")

        try:
            await asyncio.gather(consumer, dispatcher)

        except asyncio.CancelledError:
            consumer.cancel()

            dispatcher.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(consumer, dispatcher, return_exceptions=True)

            raise

        finally:
            _log.info("orchestrator.stopped", **self._metrics.snapshot())

    # ------------------------------- consume ------------------------------ #

    async def _consume_loop(self) -> None:
        """Read the stream and offer into the bounded queue.



        The ``break`` on shutdown closes the bus generator at its yield point,

        which by contract leaves the in-flight message unacknowledged and

        therefore redeliverable. Nothing is lost by stopping mid-stream.

        """

        stream = self._bus.consume(
            self._config.stream_name,
            self._config.consumer_group,
            self._config.consumer_name,
            MarketOpportunity,
            batch_size=self._config.consume_batch_size,
            block_ms=self._config.consume_block_ms,
        )

        try:
            async for message in stream:
                if self._shutdown.is_set():
                    break

                self._metrics.messages_consumed += 1

                self._handle_message(message)

        except asyncio.CancelledError:
            raise

        except Exception:
            _log.error("orchestrator.consume_loop_failed", exc_info=True)

            self._shutdown.set()

            raise

        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    def _handle_message(self, message: ConsumedMessage[MarketOpportunity]) -> None:
        """Freshness-gate then offer. Synchronous so the ack is not delayed."""

        envelope = message.envelope

        age = envelope.data.age_seconds()

        if age > self._config.max_opportunity_age_seconds:
            self._metrics.messages_expired += 1

            return

        if self._on_opportunity is not None:
            try:
                self._on_opportunity(envelope.data)
            except Exception:
                _log.exception("orchestrator.projection_failed")
        self._offer(envelope)

    def _offer(self, envelope: EventEnvelope[MarketOpportunity]) -> None:
        """Enqueue with an explicit drop-oldest load-shedding policy.



        Blocking on a full queue would apply backpressure all the way to the

        stream, stall acknowledgements and inflate the pending list. Shedding

        the oldest item keeps the consumer moving and keeps memory flat, at the

        cost of the least valuable data in the buffer.

        """

        try:
            self._queue.put_nowait(envelope)

        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

                self._queue.task_done()

            try:
                self._queue.put_nowait(envelope)

            except asyncio.QueueFull:  # pragma: no cover - a slot was just freed
                self._metrics.messages_shed += 1

                return

            self._metrics.messages_shed += 1

            if self._metrics.messages_shed % _SHED_LOG_EVERY == 0:
                _log.warning(
                    "orchestrator.load_shed",
                    total_shed=self._metrics.messages_shed,
                    queue_max_size=self._config.queue_max_size,
                    detail="dropped the oldest buffered opportunity",
                )

        depth = self._queue.qsize()

        self._metrics.peak_queue_depth = max(self._metrics.peak_queue_depth, depth)

    # ------------------------------ dispatch ------------------------------ #

    async def _dispatch_loop(self) -> None:
        while True:
            batch = await self._collect_batch()
            if not batch:
                if self._shutdown.is_set() and self._queue.empty():
                    await self._drain_inflight()
                    return
                continue

            await self._semaphore.acquire()
            task = asyncio.create_task(self._run_batch(batch), name="orch-batch")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

            if self._metrics.consecutive_failures >= self._config.max_consecutive_failures:
                _log.error(
                    "orchestrator.standing_down",
                    consecutive_failures=self._metrics.consecutive_failures,
                    threshold=self._config.max_consecutive_failures,
                    detail="persistent evaluation failure; halting to avoid a hot loop",
                )
                self._shutdown.set()
                await self._drain_inflight()
                return

    async def _run_batch(self, batch: Sequence[EventEnvelope[MarketOpportunity]]) -> None:
        try:
            await self._evaluate_batch(batch)
        finally:
            self._semaphore.release()

    async def _drain_inflight(self) -> None:
        if not self._inflight:
            return
        await asyncio.gather(*tuple(self._inflight), return_exceptions=True)

    async def _collect_batch(self) -> tuple[EventEnvelope[MarketOpportunity], ...]:
        """Gather up to ``batch_max_size`` items or until the window closes.



        Deduplicated by opportunity id, keeping the newest envelope. Without

        this, a venue re-quoting the same selection three times inside the

        window would be evaluated as three independent candidates and would

        consume the per-sport headroom three times over.

        """

        window = self._config.batch_window_ms / 1_000.0

        deadline = time.monotonic() + window

        collected: dict[str, EventEnvelope[MarketOpportunity]] = {}

        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=window)

        except TimeoutError:
            return ()

        except asyncio.CancelledError:
            raise

        self._queue.task_done()

        collected[first.data.opportunity_id] = first

        while len(collected) < self._config.batch_max_size:
            remaining = deadline - time.monotonic()

            if remaining <= 0.0:
                break

            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)

            except TimeoutError:
                break

            except asyncio.CancelledError:
                raise

            self._queue.task_done()

            collected[item.data.opportunity_id] = item

        return tuple(collected.values())

    async def _evaluate_batch(self, batch: Sequence[EventEnvelope[MarketOpportunity]]) -> None:
        """Fetch state, run the Twin, notify. Absorbs every adapter fault.



        Adapter faults are contained here and nowhere else. The consume loop

        must keep acknowledging regardless of what the intelligence layer does,

        because a stalled consumer converts a transient adapter problem into an

        unbounded Redis pending list.

        """

        started = time.monotonic()

        trace_id = batch[0].trace_id if batch else uuid.uuid4().hex

        log = _log.bind(trace_id=trace_id, batch_size=len(batch))

        bookmaker = batch[0].data.bookmaker

        opportunities = [envelope.data for envelope in batch]

        try:
            profile = await self._state.get_profile(self._config.profile_id)

            account_state = await self._state.get_account_state(bookmaker)

        except TransientIntelligenceError as exc:
            self._metrics.state_unavailable += 1

            self._metrics.consecutive_failures += 1

            log.warning(
                "orchestrator.state_transient_failure",
                bookmaker=bookmaker.value,
                error=type(exc).__name__,
                detail=str(exc)[:300],
            )

            await self._backoff()

            return

        except (
            AccountUnavailableError,
            StaleAccountStateError,
            ReconciliationBreachError,
        ) as exc:
            self._metrics.state_unavailable += 1

            log.error(
                "orchestrator.state_unusable",
                bookmaker=bookmaker.value,
                error=type(exc).__name__,
                detail=str(exc)[:300],
                exc_info=False,
            )

            return

        if profile is None or account_state is None:
            self._metrics.state_unavailable += 1

            log.warning(
                "orchestrator.state_missing",
                bookmaker=bookmaker.value,
                has_profile=profile is not None,
                has_account_state=account_state is not None,
                detail="L1 cache not yet warm; batch skipped without acknowledgement loss",
            )

            return

        try:
            modifiers = await self._state.get_active_modifiers()

            realized_pnl = await self._state.get_realized_pnl_today_paise(bookmaker)

            result = self._twin.evaluate_session(
                profile=profile,
                account_state=account_state,
                opportunities=opportunities,
                modifiers=modifiers,
                realized_pnl_today_paise=realized_pnl,
                trace_id=trace_id,
            )

        except TransientIntelligenceError as exc:
            self._metrics.batches_failed += 1

            self._metrics.consecutive_failures += 1

            log.warning(
                "orchestrator.evaluation_transient_failure",
                error=type(exc).__name__,
                detail=str(exc)[:300],
            )

            await self._backoff()

            return

        except IntelligenceError as exc:
            self._metrics.batches_failed += 1

            self._metrics.consecutive_failures += 1

            log.error(
                "orchestrator.evaluation_failed",
                error=type(exc).__name__,
                detail=str(exc)[:300],
                exc_info=True,
            )

            return

        except ValueError as exc:
            # Deterministic domain rejection: retrying will fail identically,

            # so it is counted but does not trip the consecutive-failure brake.

            self._metrics.batches_failed += 1

            log.error(
                "orchestrator.evaluation_invalid",
                error=type(exc).__name__,
                detail=str(exc)[:300],
                exc_info=True,
            )

            return

        except asyncio.CancelledError:
            raise

        except Exception:
            self._metrics.batches_failed += 1

            self._metrics.consecutive_failures += 1

            log.error("orchestrator.evaluation_crashed", exc_info=True)

            return

        self._metrics.consecutive_failures = 0

        self._metrics.batches_evaluated += 1

        self._metrics.last_batch_monotonic_ns = time.monotonic_ns()

        await self._publish_results(result, log)

        log.debug(
            "orchestrator.batch_complete",
            duration_ms=round((time.monotonic() - started) * 1_000.0, 3),
            issued=len(result.actionable),
            refused=len(result.refusals),
            verdict=result.forecast.verdict.value,
            queue_depth=self._queue.qsize(),
        )

    async def _publish_results(
        self, result: TwinSessionResult, log: structlog.stdlib.BoundLogger
    ) -> None:
        """Hand results to the notifier, absorbing channel failures.



        A notification failure is logged and dropped. The recommendation is

        already computed and recorded, and an unreachable channel must never

        abort evaluation of the next batch.

        """

        self._metrics.recommendations_issued += len(result.actionable)

        self._metrics.rejections_issued += len(result.refusals)

        try:
            delivered, _ = await self._notifier.notify_session(
                forecast=result.forecast,
                recommendations=result.recommendations,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.warning(
                "orchestrator.notification_failed",
                channel=self._notifier.channel.value,
                error=type(exc).__name__,
                detail=str(exc)[:300],
            )

            return

        self._metrics.notifications_delivered += delivered

        self._metrics.notifications_suppressed += max(len(result.actionable) - delivered, 0)

    async def _backoff(self) -> None:
        """Interruptible backoff after a transient fault."""

        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(
                self._shutdown.wait(), timeout=self._config.transient_backoff_seconds
            )
