"""Composition root. The only module permitted to construct concrete adapters.



Everything below is wiring. No business rule lives here, and nothing here is

imported by the domain, which is what keeps the dependency arrow pointing

inwards.



Shutdown contract

-----------------

``SIGINT`` and ``SIGTERM`` set a single ``asyncio.Event``. Every long-running

loop polls it and returns normally, so the ``TaskGroup`` unwinds without

cancellation. Cancellation is the fallback, not the mechanism: a cancelled

consumer would abandon its in-flight Redis message and a cancelled console

write can leave the terminal in a mangled state.



Signal registration prefers ``loop.add_signal_handler`` and falls back to

``signal.signal``, because ``add_signal_handler`` raises ``NotImplementedError``

on the Windows Proactor loop. Both paths converge on the same event.



Teardown order is fixed and load-bearing: tasks first, then the notifier (so

the terminal is restored before anything else can print), then the event bus

client, then the connection pool. Reversing any two of those produces either a

use-after-close or a corrupted terminal.

"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

import structlog
import uvicorn
from redis.asyncio.connection import ConnectionPool

from betdoc.adapters.cache.state_store import (
    AccountRefreshDaemon,
    InMemoryStateStore,
    RefreshConfig,
)
from betdoc.adapters.notifiers.console_notifier import (
    AntiSpamConfig,
    AntiSpamRegistry,
    RichConsoleNotifier,
)
from betdoc.application.config import Settings, configure_logging, get_settings
from betdoc.application.event_bus import EventBus
from betdoc.application.events import EventEnvelope, Streams
from betdoc.application.orchestrator import (
    OpportunityOrchestrator,
    OrchestratorConfig,
)
from betdoc.application.ports.intelligence_clients import AccountStateAdapter
from betdoc.domain.intelligence.account_models import (
    AccountState,
    BetRecord,
    BetStatus,
    BettorProfile,
    LinkedBookmaker,
    SportType,
    utc_now,
)
from betdoc.domain.intelligence.advisor_models import MarketOpportunity
from betdoc.presentation.api.app import build_api
from betdoc.presentation.api.broadcaster import CompositeNotifier, WebsocketBroadcaster
from betdoc.services.advisor.twin_engine import TwinAdvisorConfig, TwinAdvisorService

__all__ = ["main", "run"]


_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(component="main")


_PROFILE_ID: Final[str] = "primary"

_WINDOWS_WAKEUP_INTERVAL: Final[float] = 0.25

_MOCK_PUBLISH_INTERVAL: Final[float] = 1.5


# --------------------------------------------------------------------------- #

# Mock adapter: the seam where a real bookmaker adapter is plugged in

# --------------------------------------------------------------------------- #


class _MockAccountAdapter(AccountStateAdapter):
    """Deterministic stand-in for a real account adapter.



    Satisfies the Phase 3 port exactly, so replacing it with a live adapter is

    a one-line change in :func:`_build_adapters`. Deliberately read-only: this

    port cannot place a bet, so no wiring mistake here can move money.

    """

    __slots__ = ("_balance_paise", "_open_stake_paise", "_rng")

    def __init__(
        self,
        bookmaker: LinkedBookmaker,
        *,
        balance_paise: int = 5_000_000,
        open_stake_paise: int = 250_000,
    ) -> None:

        super().__init__(bookmaker)

        self._balance_paise = balance_paise

        self._open_stake_paise = open_stake_paise

        self._rng = random.Random(hash(bookmaker.value) & 0xFFFF)

    async def connect(self) -> None:

        return None

    async def close(self) -> None:

        return None

    async def fetch_account_state(self) -> AccountState:

        await asyncio.sleep(self._rng.uniform(0.05, 0.25))

        drift = self._rng.randint(-20_000, 20_000)

        exposure = max(self._open_stake_paise + drift, 0)

        return AccountState(
            bookmaker=self.bookmaker,
            as_of=utc_now(),
            realized_balance_paise=self._balance_paise,
            unsettled_exposure_paise=exposure,
            open_bet_count=1 if exposure > 0 else 0,
            exposure_by_sport_paise={SportType.SOCCER: exposure} if exposure else {},
            max_accepted_stake_paise=2_000_000,
        )

    async def fetch_active_bets(self) -> tuple[BetRecord, ...]:

        await asyncio.sleep(self._rng.uniform(0.02, 0.10))

        return (
            BetRecord(
                bet_id=f"{self.bookmaker.value}-open-1",
                bookmaker=self.bookmaker,
                sport_type=SportType.SOCCER,
                event_id="soccer_epl:evt-open",
                market_key="h2h",
                selection="home",
                stake_paise=self._open_stake_paise,
                matched_odds=2.05,
                status=BetStatus.UNSETTLED,
                placed_at=utc_now() - timedelta(minutes=45),
            ),
        )

    async def fetch_settled_bets(
        self, *, since: datetime, limit: int = 500
    ) -> tuple[BetRecord, ...]:

        return ()

    async def is_available(self) -> bool:

        return True

    async def fetch_max_accepted_stake_paise(self) -> int | None:

        return 2_000_000


def _build_adapters() -> Mapping[LinkedBookmaker, AccountStateAdapter]:
    """Swap the mocks for live adapters here and nowhere else."""

    return {
        LinkedBookmaker.PINNACLE: _MockAccountAdapter(LinkedBookmaker.PINNACLE),
        LinkedBookmaker.BETFAIR: _MockAccountAdapter(
            LinkedBookmaker.BETFAIR, balance_paise=3_000_000
        ),
    }


def _seed_profile() -> BettorProfile:
    """Bootstrap policy. Replaced by a persisted profile in Phase 5."""

    return BettorProfile(
        profile_id=_PROFILE_ID,
        daily_loss_limit_paise=1_000_000,
        max_exposure_per_sport_paise=600_000,
        max_single_stake_paise=200_000,
        volatility_tolerance=0.30,
        preferred_sports=(SportType.SOCCER,),
        allow_parlays=False,
        settled_bet_count=0,
    )


# --------------------------------------------------------------------------- #

# Mock ingestion: publishes opportunities onto the bus

# --------------------------------------------------------------------------- #


async def _mock_opportunity_publisher(bus: EventBus, shutdown: asyncio.Event) -> None:
    """Emit synthetic opportunities so the pipeline is exercised end to end.



    Deliberately re-quotes the *same* selection with a tiny price drift on most

    cycles. That is the anti-spam test case: the debouncer must alert once and

    then stay silent until the edge moves materially or the TTL lapses. If the

    terminal fills with panels, the debouncer is broken.

    """

    rng = random.Random(20240101)

    cycle = 0

    published = 0

    while not shutdown.is_set():
        started = time.monotonic()

        trace_id = uuid.uuid4().hex

        # A stable selection with cosmetic drift, plus an occasional new one.

        drift = rng.uniform(-0.01, 0.01)

        batch: list[MarketOpportunity] = [
            MarketOpportunity(
                opportunity_id=f"arsenal-ml-{cycle}",
                bookmaker=LinkedBookmaker.PINNACLE,
                sport_type=SportType.SOCCER,
                fixture_id="soccer_epl:ars-che",
                market_key="h2h",
                outcome_key="home",
                selection_label="Arsenal moneyline",
                offered_odds=2.10 + drift,
                fair_probability=0.505,
                quoted_at=utc_now(),
                max_accepted_stake_paise=2_000_000,
            )
        ]

        if cycle % 7 == 0:
            batch.append(
                MarketOpportunity(
                    opportunity_id=f"totals-over-{cycle}",
                    bookmaker=LinkedBookmaker.PINNACLE,
                    sport_type=SportType.SOCCER,
                    fixture_id="soccer_epl:liv-mci",
                    market_key="totals@2.5",
                    outcome_key="over",
                    selection_label="Liverpool v Man City Over 2.5",
                    offered_odds=1.92,
                    fair_probability=0.545,
                    quoted_at=utc_now(),
                )
            )

        try:
            await bus.publish_many(
                Streams.OPPORTUNITIES,
                [
                    EventEnvelope.wrap(opportunity, trace_id=trace_id, event_type="opportunity")
                    for opportunity in batch
                ],
            )

            published += len(batch)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            _log.warning(
                "mock_publisher.failed",
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )

        cycle += 1

        elapsed = time.monotonic() - started

        await _interruptible_sleep(shutdown, max(_MOCK_PUBLISH_INTERVAL - elapsed, 0.0))

    _log.info("mock_publisher.stopped", cycles=cycle, published=published)


async def _interruptible_sleep(shutdown: asyncio.Event, seconds: float) -> None:
    """Sleep that wakes immediately on shutdown."""

    if seconds <= 0.0:
        return

    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)


async def _windows_wakeup(shutdown: asyncio.Event) -> None:
    """Keep the Windows Proactor loop waking so Ctrl+C is observed promptly."""

    while not shutdown.is_set():
        await asyncio.sleep(_WINDOWS_WAKEUP_INTERVAL)


# --------------------------------------------------------------------------- #

# Signal handling

# --------------------------------------------------------------------------- #


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Route SIGINT and SIGTERM to the shutdown event, on any platform.



    ``loop.add_signal_handler`` is the correct mechanism on POSIX because it is

    delivered inside the loop rather than interrupting arbitrary bytecode. It

    raises ``NotImplementedError`` on Windows, so ``signal.signal`` plus

    ``call_soon_threadsafe`` is the fallback. Both paths set the same event, so

    the rest of the system cannot tell which fired.

    """

    loop = asyncio.get_running_loop()

    def _trigger(signal_name: str) -> None:

        _log.warning(
            "main.signal_received",
            signal=signal_name,
            detail="draining tasks and closing resources",
        )

        shutdown.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)

        if sig is None:
            continue

        try:
            loop.add_signal_handler(sig, _trigger, name)

        except (NotImplementedError, RuntimeError, ValueError, OSError):
            with contextlib.suppress(ValueError, OSError, RuntimeError):
                signal.signal(
                    sig,
                    lambda _signum, _frame, _name=name: loop.call_soon_threadsafe(_trigger, _name),
                )


# --------------------------------------------------------------------------- #

# Composition

# --------------------------------------------------------------------------- #


async def _run(settings: Settings, shutdown: asyncio.Event) -> None:
    """Build the object graph and supervise every long-running task."""

    store = InMemoryStateStore()

    await store.set_profile(_seed_profile())

    adapters = _build_adapters()

    console_notifier = RichConsoleNotifier(
        AntiSpamRegistry(
            AntiSpamConfig(
                ttl_seconds=300.0,
                edge_shift_threshold=0.02,
                max_entries=4_096,
                max_per_minute=30,
            )
        )
    )
    broadcaster = WebsocketBroadcaster()
    notifier = CompositeNotifier(console_notifier, broadcaster)

    pool = ConnectionPool.from_url(
        settings.redis.url.get_secret_value(),
        max_connections=settings.redis.max_connections,
        socket_timeout=settings.redis.socket_timeout_seconds,
        socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
        health_check_interval=settings.redis.health_check_interval_seconds,
        decode_responses=True,
    )

    bus = EventBus(
        pool,
        stream_max_length=settings.redis.stream_max_length,
        dlq_max_length=settings.redis.dlq_max_length,
        claim_min_idle_ms=settings.redis.claim_min_idle_ms,
        max_delivery_attempts=settings.redis.max_delivery_attempts,
    )

    orchestrator = OpportunityOrchestrator(
        bus=bus,
        twin=TwinAdvisorService(TwinAdvisorConfig()),
        state=store,
        notifier=notifier,
        config=OrchestratorConfig(
            profile_id=_PROFILE_ID,
            consumer_name=f"advisor-{os.getpid()}",
            queue_max_size=5_000,
            max_concurrent_sessions=4,
            batch_max_size=32,
            batch_window_ms=250.0,
            consume_block_ms=1_000,
        ),
        shutdown=shutdown,
    )

    daemon = AccountRefreshDaemon(
        adapters=adapters,
        store=store,
        config=RefreshConfig(interval_seconds=30.0, jitter_fraction=0.15),
        shutdown=shutdown,
    )

    try:
        await bus.ping()

        await notifier.start()

        _log.info(
            "main.started",
            environment=settings.environment,
            bookmakers=[key.value for key in adapters],
            stream=Streams.OPPORTUNITIES,
            platform=sys.platform,
            pid=os.getpid(),
        )

        # TaskGroup is cooperative here: every member polls the shutdown event

        # and returns normally, so the group unwinds without cancelling and no

        # in-flight Redis message is abandoned.

        api_app = build_api(store=store, broadcaster=broadcaster, profile_id=_PROFILE_ID)
        server = uvicorn.Server(
            uvicorn.Config(api_app, host="127.0.0.1", port=8000, log_config=None, lifespan="off")
        )
        server.install_signal_handlers = False

        async def _uvicorn_watcher() -> None:
            await shutdown.wait()
            server.should_exit = True

        async with asyncio.TaskGroup() as group:
            group.create_task(daemon.run(), name="account-refresh-daemon")

            group.create_task(orchestrator.run(), name="opportunity-orchestrator")

            group.create_task(_mock_opportunity_publisher(bus, shutdown), name="mock-ingestion")

            group.create_task(server.serve(), name="fastapi-uvicorn")
            group.create_task(_uvicorn_watcher(), name="uvicorn-watcher")

            if sys.platform == "win32":
                group.create_task(_windows_wakeup(shutdown), name="win-wakeup")

    except* asyncio.CancelledError:
        _log.warning("main.cancelled", detail="tasks cancelled; proceeding to teardown")

    except* Exception as group_error:
        for error in group_error.exceptions:
            _log.error(
                "main.task_failed",
                error=type(error).__name__,
                detail=str(error)[:300],
                exc_info=error,
            )

        shutdown.set()

    finally:
        # Teardown order is load-bearing. Notifier first so the terminal is

        # restored before anything else can write to it, then the bus client,

        # then the pool that the bus borrowed but does not own.

        shutdown.set()

        with contextlib.suppress(Exception):
            await notifier.close()

        with contextlib.suppress(Exception):
            await bus.close()

        with contextlib.suppress(Exception):
            await pool.disconnect(inuse_connections=True)

        _log.info(
            "main.stopped",
            orchestrator=orchestrator.metrics.snapshot(),
            debouncer=(
                console_notifier.debouncer.snapshot()
                if isinstance(console_notifier.debouncer, AntiSpamRegistry)
                else {}
            ),
        )


async def main() -> None:
    """Async entry point: configure logging, install handlers, run."""

    settings = get_settings()

    configure_logging(settings)

    shutdown = asyncio.Event()

    _install_signal_handlers(shutdown)

    try:
        await _run(settings, shutdown)

    except KeyboardInterrupt:
        _log.warning("main.keyboard_interrupt", detail="draining")

        shutdown.set()

    except asyncio.CancelledError:
        _log.warning("main.cancelled_at_top_level")

        shutdown.set()

        raise


def run() -> None:
    """Synchronous entry point.



    ``KeyboardInterrupt`` is trapped here as the outermost backstop only. By

    the time it reaches this frame the loop has already unwound, so the real

    graceful path is the shutdown event set by the signal handler. This trap

    exists to exit quietly instead of printing a traceback over the terminal

    the notifier has just restored.

    """

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        _log.warning("main.interrupted", detail="process exiting")

    except asyncio.CancelledError:
        _log.warning("main.cancelled_during_shutdown")


if __name__ == "__main__":
    run()
