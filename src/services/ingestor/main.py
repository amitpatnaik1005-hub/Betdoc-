"""Ingestor service: the entry point for live odds.

Four properties this loop guarantees:

* **No poll drift.** Each cycle measures its own duration with
  ``time.monotonic`` and sleeps the remainder of the interval. A 2.0s cadence
  with a 1.2s call sleeps exactly 0.8s. Naively sleeping the full interval
  yields a 3.2s effective cadence and a slowly growing lag nobody notices until
  the prices are stale.
* **No overlapping polls.** Each bookmaker owns one sequential worker, so a slow
  response delays that venue only and can never produce two concurrent calls
  to the same API (which is how rate limits get tripped).
* **Bounded memory under backpressure.** Pollers hand off to a bounded
  ``asyncio.Queue``. If Redis stalls, the queue fills and the drop policy
  discards the *oldest* tick, because in betting a stale price is worthless
  while an unbounded queue is an OOM kill.
* **Windows-safe graceful shutdown.** ``loop.add_signal_handler`` raises
  ``NotImplementedError`` on Windows and is not used anywhere. Shutdown is
  driven by an ``asyncio.Event``, set either by ``signal.signal`` (which does
  work on Windows for SIGINT) or by the ``KeyboardInterrupt`` trap around
  ``asyncio.run``. Workers observe the event, break their loops, the publisher
  drains the queue within its budget, and only then is the Redis pool closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final

import structlog
from redis.asyncio.connection import ConnectionPool

from application.config import IngestorSettings, Settings, configure_logging, get_settings
from application.event_bus import EventBus
from application.events import EventEnvelope, RawOddsPayload, Streams, new_trace_id
from application.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    with_circuit_breaker,
)

__all__ = ["main", "run"]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="services.ingestor"
)

_WINDOWS_WAKEUP_INTERVAL: Final[float] = 0.25

_DRAIN_SENTINEL: Final[None] = None

_venue_breakers: Final[dict[str, CircuitBreaker]] = {}


def _breaker_for(bookmaker: str, settings: IngestorSettings) -> CircuitBreaker:
    existing = _venue_breakers.get(bookmaker)
    if existing is not None:
        return existing
    created = CircuitBreaker(
        f"venue::{bookmaker}",
        failure_threshold=settings.breaker_failure_threshold,
        reset_timeout_seconds=settings.breaker_reset_timeout_seconds,
    )
    _venue_breakers[bookmaker] = created
    return created

async def _fetch_odds_from_venue(
    bookmaker: str, sport: str, markets: tuple[str, ...]
) -> list[dict[str, object]]:
    await asyncio.sleep(random.uniform(0.05, 0.40))
    if random.random() < 0.05:
        msg = f"simulated transport failure from {bookmaker}"
        raise TimeoutError(msg)
    now = datetime.now(UTC)
    return [
        {
            "bookmaker": bookmaker,
            "event_id": f"{sport}:evt-{index}",
            "market_key": market,
            "outcome_key": outcome,
            "decimal_odds": round(random.uniform(1.40, 4.50), 2),
            "bookmaker_timestamp": now,
            "sport_key": sport,
        }
        for index in range(3)
        for market in markets
        for outcome in ("home", "away")
    ]


def _guarded_fetch(
    bookmaker: str, settings: IngestorSettings
) -> object:
    return with_circuit_breaker(
        breaker=_breaker_for(bookmaker, settings),
        attempts=settings.max_retry_attempts,
        initial_wait_seconds=settings.retry_initial_wait_seconds,
        max_wait_seconds=settings.retry_max_wait_seconds,
    )(_fetch_odds_from_venue)

async def _interruptible_sleep(shutdown: asyncio.Event, seconds: float) -> None:
    if seconds <= 0.0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)

async def _poll_venue(
    bookmaker: str,
    settings: IngestorSettings,
    queue: asyncio.Queue[EventEnvelope[RawOddsPayload] | None],
    shutdown: asyncio.Event,
) -> None:
    fetch = _guarded_fetch(bookmaker, settings)
    interval = settings.poll_interval_seconds
    cycles = 0
    dropped = 0

    while not shutdown.is_set():
        cycle_started = time.monotonic()
        trace_id = new_trace_id()
        log = _log.bind(bookmaker=bookmaker, trace_id=trace_id, cycle=cycles)

        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        try:
            for sport in settings.sports:
                raw_rows = await fetch(bookmaker, sport, settings.markets)  # type: ignore[operator]
                for row in raw_rows:
                    envelope = _envelope_from_row(row, trace_id, log)
                    if envelope is None:
                        continue
                    if _offer(queue, envelope, log):
                        continue
                    dropped += 1
        except CircuitBreakerOpenError as exc:
            log.warning(
                "ingestor.circuit_open",
                seconds_until_retry=round(exc.seconds_until_retry, 2),
                detail="skipping this cycle without a network call",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error(
                "ingestor.cycle_failed",
                error=type(exc).__name__,
                detail=str(exc)[:300],
                exc_info=True,
            )
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")

        cycles += 1
        elapsed = time.monotonic() - cycle_started
        remaining = interval - elapsed

        if remaining <= 0.0:
            log.warning(
                "ingestor.poll_overrun",
                elapsed_seconds=round(elapsed, 3),
                interval_seconds=interval,
                overrun_seconds=round(-remaining, 3),
                detail="cycle exceeded its interval; cadence is degrading",
            )
        else:
            log.debug(
                "ingestor.cycle_complete",
                elapsed_seconds=round(elapsed, 3),
                sleeping_seconds=round(remaining, 3),
            )

        await _interruptible_sleep(shutdown, max(remaining, 0.0))

    _log.info(
        "ingestor.poller_stopped",
        bookmaker=bookmaker,
        cycles=cycles,
        dropped=dropped,
        **_breaker_for(bookmaker, settings).snapshot(),
    )


def _envelope_from_row(
    row: dict[str, object],
    trace_id: str,
    log: structlog.stdlib.BoundLogger,
) -> EventEnvelope[RawOddsPayload] | None:
    try:
        payload = RawOddsPayload.model_validate(row)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ingestor.row_rejected",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None
    return EventEnvelope.wrap(payload, trace_id=trace_id, event_type="raw_odds")


def _offer(
    queue: asyncio.Queue[EventEnvelope[RawOddsPayload] | None],
    envelope: EventEnvelope[RawOddsPayload],
    log: structlog.stdlib.BoundLogger,
) -> bool:
    try:
        queue.put_nowait(envelope)
        return True
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
            queue.task_done()
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:  # pragma: no cover
            log.error("ingestor.tick_dropped", reason="queue full after eviction")
            return False
        log.warning(
            "ingestor.tick_evicted",
            reason="publisher backpressure; dropped the oldest tick",
            queue_size=queue.qsize(),
        )
        return True


async def _publish_loop(
    bus: EventBus,
    queue: asyncio.Queue[EventEnvelope[RawOddsPayload] | None],
    settings: IngestorSettings,
    shutdown: asyncio.Event,
) -> None:
    published = 0
    batches = 0

    while True:
        first = await queue.get()
        if first is _DRAIN_SENTINEL:
            queue.task_done()
            break

        batch: list[EventEnvelope[RawOddsPayload]] = [first]
        queue.task_done()

        while len(batch) < settings.publish_batch_size:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            if item is _DRAIN_SENTINEL:
                shutdown.set()
                break
            batch.append(item)

        try:
            await bus.publish_many(Streams.RAW_ODDS, batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "ingestor.publish_failed",
                error=type(exc).__name__,
                detail=str(exc)[:300],
                batch_size=len(batch),
                exc_info=True,
            )
            await _interruptible_sleep(shutdown, 1.0)
            continue

        published += len(batch)
        batches += 1

    _log.info("ingestor.publisher_stopped", published=published, batches=batches)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handler(signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)
        _log.warning("ingestor.signal_received", signal=signal.Signals(signum).name)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, _handler)


async def _windows_wakeup(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        await asyncio.sleep(_WINDOWS_WAKEUP_INTERVAL)


@contextlib.asynccontextmanager
async def _redis_pool(settings: Settings) -> AsyncIterator[ConnectionPool]:
    pool = ConnectionPool.from_url(
        settings.redis.url.get_secret_value(),
        max_connections=settings.redis.max_connections,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
        health_check_interval=settings.redis.health_check_interval_seconds,
        decode_responses=True,
    )
    try:
        yield pool
    finally:
        await pool.disconnect(inuse_connections=True)
        _log.info("ingestor.redis_pool_closed")


async def _run(settings: Settings, shutdown: asyncio.Event) -> None:
    ingestor = settings.ingestor
    queue: asyncio.Queue[EventEnvelope[RawOddsPayload] | None] = asyncio.Queue(
        maxsize=ingestor.publish_queue_size
    )

    async with _redis_pool(settings) as pool:
        bus = EventBus(
            pool,
            stream_max_length=settings.redis.stream_max_length,
            dlq_max_length=settings.redis.dlq_max_length,
            claim_min_idle_ms=settings.redis.claim_min_idle_ms,
            max_delivery_attempts=settings.redis.max_delivery_attempts,
        )
        try:
            await bus.ping()
            _log.info(
                "ingestor.started",
                bookmakers=list(ingestor.bookmakers),
                sports=list(ingestor.sports),
                markets=list(ingestor.markets),
                poll_interval_seconds=ingestor.poll_interval_seconds,
                queue_size=ingestor.publish_queue_size,
                stream=Streams.RAW_ODDS,
                platform=sys.platform,
            )

            publisher = asyncio.create_task(
                _publish_loop(bus, queue, ingestor, shutdown), name="publisher"
            )
            pollers = [
                asyncio.create_task(
                    _poll_venue(bookmaker, ingestor, queue, shutdown),
                    name=f"poller:{bookmaker}",
                )
                for bookmaker in ingestor.bookmakers
            ]
            helpers: list[asyncio.Task[None]] = []
            if sys.platform == "win32":
                helpers.append(
                    asyncio.create_task(_windows_wakeup(shutdown), name="win-wakeup")
                )

            try:
                await asyncio.gather(*pollers)
            finally:
                shutdown.set()
                for task in helpers:
                    task.cancel()
                await queue.put(_DRAIN_SENTINEL)
                try:
                    await asyncio.wait_for(
                        publisher, timeout=ingestor.shutdown_drain_seconds
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    _log.error(
                        "ingestor.drain_timeout",
                        budget_seconds=ingestor.shutdown_drain_seconds,
                        unpublished=queue.qsize(),
                        detail="publisher did not finish; those ticks are lost",
                    )
                    publisher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await publisher
                await asyncio.gather(*helpers, return_exceptions=True)
        finally:
            await bus.close()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)

    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    try:
        await _run(settings, shutdown)
    except KeyboardInterrupt:
        _log.warning("ingestor.keyboard_interrupt", detail="draining")
        shutdown.set()
    except asyncio.CancelledError:
        _log.warning("ingestor.cancelled", detail="draining")
        shutdown.set()
        raise
    finally:
        _log.info("ingestor.stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log.warning("ingestor.interrupted", detail="process exiting")
    except asyncio.CancelledError:
        _log.warning("ingestor.cancelled_at_top_level")


if __name__ == "__main__":
    run()
