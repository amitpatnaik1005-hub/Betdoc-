"""
DEVRAYA: Queue-based high-frequency odds simulator.

Run from scrapers/devraya/:
    python main.py
Stress test (PowerShell):
    $env:TICK_RATE_MS = "50"; $env:BATCH_SIZE = "200"; python main.py

Ctrl+C once: graceful shutdown (producers stop, queue drains, connections close).
Ctrl+C twice: force quit.
"""

import asyncio
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import dotenv
import httpx

from schema import MarketTick, MarketType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [devraya] %(message)s",
)
# httpx logs every request at INFO, which floods the console at high tick rates
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("devraya")


# --- Configuration ---

BASE_DIR = Path(__file__).resolve().parent
# Resolved from this file's location, not the working directory, so it works from anywhere
BACKEND_ENV_PATH = (BASE_DIR / ".." / ".." / "backend" / ".env").resolve()


def _load_api_key() -> str:
    # dotenv_values reads only what we ask for. load_dotenv would copy the backend's
    # DATABASE_URL, SECRET_KEY and ENCRYPTION_KEY into this process too, which a
    # scraper has no need for. A real environment variable takes precedence.
    key = os.environ.get("INGESTION_API_KEY") or dotenv.dotenv_values(BACKEND_ENV_PATH).get(
        "INGESTION_API_KEY"
    )
    if not key or key.startswith("<"):
        sys.exit(
            f"FATAL: INGESTION_API_KEY not found. Set it in {BACKEND_ENV_PATH} "
            "(run backend/generate_keys.py) or as an environment variable."
        )
    return key


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"FATAL: {name} must be an integer, got {raw!r}")
    if not minimum <= value <= maximum:
        sys.exit(f"FATAL: {name} must be between {minimum} and {maximum}, got {value}")
    return value


API_KEY = _load_api_key()
TICK_RATE_MS = _env_int("TICK_RATE_MS", 500, 10, 60_000)
BATCH_SIZE = _env_int("BATCH_SIZE", 10, 1, 1000)  # Backend rejects batches over 1000
INGEST_URL = os.environ.get("INGEST_URL", "http://127.0.0.1:8000/api/v1/ingest")

TICK_INTERVAL_S = TICK_RATE_MS / 1000
# A partial batch is sent after this long, so ticks aren't held back waiting for a full batch
FLUSH_TIMEOUT_S = TICK_INTERVAL_S
DRAIN_TIMEOUT_S = 5.0
QUEUE_MAXSIZE = BATCH_SIZE * 100
STATS_INTERVAL_S = 10.0


# --- Market state (random walk) ---

MIN_ODDS = 1.01
MAX_ODDS = 20.0
ODDS_STEPS = (-0.02, -0.01, 0.0, 0.0, 0.01, 0.02)  # Zero twice: prices often don't move
MEAN_REVERSION = 0.02  # Gentle pull back to the opening price, so odds don't drift off forever
EDGE_DECAY = 0.95
EDGE_NOISE = 0.004
MAX_EDGE = 0.08
SUSPEND_PROBABILITY = 0.01  # Per tick. Mimics goals, VAR checks and red cards.


@dataclass
class MarketState:
    home_team: str
    away_team: str
    market_type: MarketType
    opening_odds: float
    odds: float
    edge: float = 0.0  # Model's disagreement with the market, used to derive true_probability
    suspended_ticks: int = 0


# One market type per match_id: the backend and frontend currently key markets by
# match_id alone, so two market types sharing a match_id would overwrite each other.
MARKETS: dict[str, MarketState] = {
    "EPL-ARS-CHE": MarketState("Arsenal", "Chelsea", "Match Odds", 2.10, 2.10),
    "EPL-LIV-MCI": MarketState("Liverpool", "Manchester City", "Over/Under 2.5", 1.85, 1.85),
    "LAL-RMA-BAR": MarketState("Real Madrid", "Barcelona", "Asian Handicap", 1.95, 1.95),
    "BUN-BAY-BVB": MarketState("Bayern Munich", "Borussia Dortmund", "Match Odds", 1.65, 1.65),
    "SEA-INT-JUV": MarketState("Inter", "Juventus", "Over/Under 2.5", 2.05, 2.05),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _next_tick(match_id: str, state: MarketState) -> MarketTick:
    # Suspensions: markets freeze for a few ticks, then reopen
    if state.suspended_ticks > 0:
        state.suspended_ticks -= 1
    elif random.random() < SUSPEND_PROBABILITY:
        state.suspended_ticks = random.randint(3, 10)

    is_suspended = state.suspended_ticks > 0

    if not is_suspended:
        step = random.choice(ODDS_STEPS)
        reversion = MEAN_REVERSION * (state.opening_odds - state.odds)
        # Rounded to 2dp, like real exchange price ticks
        state.odds = round(_clamp(state.odds + step + reversion, MIN_ODDS, MAX_ODDS), 2)

    # The model's view drifts independently of the price and slowly returns to zero,
    # so +EV opportunities appear and disappear the way they do on a real desk
    state.edge = _clamp(state.edge * EDGE_DECAY + random.gauss(0, EDGE_NOISE), -MAX_EDGE, MAX_EDGE)
    true_probability = round(_clamp((1 / state.odds) * (1 + state.edge), 0.01, 0.99), 4)

    return MarketTick(
        match_id=match_id,
        home_team=state.home_team,
        away_team=state.away_team,
        market_type=state.market_type,
        odds=state.odds,
        true_probability=true_probability,
        is_suspended=is_suspended,
    )


# --- Producer ---

async def market_producer(
    queue: asyncio.Queue[MarketTick],
    shutdown_event: asyncio.Event,
    match_id: str,
) -> None:
    """One producer per match, so each market ticks on its own schedule."""
    state = MARKETS[match_id]

    while not shutdown_event.is_set():
        tick = _next_tick(match_id, state)

        # Never block on a full queue. A blocked producer would never see the shutdown
        # event, and the flusher (waiting for producers to finish) would deadlock.
        # Dropping the OLDEST tick is right for live odds: newer prices replace older ones.
        if queue.full():
            try:
                queue.get_nowait()
                logger.warning("Queue full, dropped oldest tick (API too slow or down?)")
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(tick)

        # Jitter keeps markets from ticking in lockstep. Waiting on the event instead of
        # sleeping means shutdown takes effect immediately, not after a full tick interval.
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=TICK_INTERVAL_S * random.uniform(0.5, 1.5),
            )
        except TimeoutError:
            pass


# --- Consumer (flusher) ---

async def _collect_batch(queue: asyncio.Queue[MarketTick], timeout: float) -> list[MarketTick]:
    batch: list[MarketTick] = []
    try:
        batch.append(await asyncio.wait_for(queue.get(), timeout=timeout))
    except TimeoutError:
        return batch

    while len(batch) < BATCH_SIZE:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _post_batch(
    client: httpx.AsyncClient,
    batch: list[MarketTick],
    shutdown_event: asyncio.Event,
) -> bool:
    payload = [tick.model_dump(mode="json", by_alias=True) for tick in batch]

    try:
        response = await client.post(INGEST_URL, json=payload)
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in (401, 403):
            # A wrong key won't fix itself; retrying just floods the API with rejected requests
            logger.critical("Ingestion key rejected (HTTP %d). Shutting down.", status_code)
            shutdown_event.set()
        else:
            logger.error(
                "Batch of %d rejected: HTTP %d %s",
                len(batch),
                status_code,
                exc.response.text[:500],
            )
    except httpx.RequestError as exc:
        # Failed batches are dropped, not retried: the next ticks carry newer prices anyway
        logger.error("Network error sending %d ticks: %s: %s", len(batch), type(exc).__name__, exc)

    return False


async def api_flusher(
    queue: asyncio.Queue[MarketTick],
    shutdown_event: asyncio.Event,
    producers_done: asyncio.Event,
) -> None:
    client = httpx.AsyncClient(timeout=5.0, headers={"X-Ingestion-Key": API_KEY})

    sent = failed = 0
    last_stats = time.monotonic()

    try:
        while not shutdown_event.is_set():
            batch = await _collect_batch(queue, FLUSH_TIMEOUT_S)
            if batch:
                if await _post_batch(client, batch, shutdown_event):
                    sent += len(batch)
                else:
                    failed += len(batch)

            now = time.monotonic()
            if now - last_stats >= STATS_INTERVAL_S:
                logger.info(
                    "Stats: %d ticks sent, %d failed, queue depth %d",
                    sent, failed, queue.qsize(),
                )
                last_stats = now

        # --- Drain ---
        # Wait until producers have fully stopped, so no tick lands after the drain
        await producers_done.wait()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_TIMEOUT_S
        logger.info("Draining %d queued ticks...", queue.qsize())

        # Deadline: if the API is down, shutdown must not hang on endless failing requests
        while not queue.empty() and loop.time() < deadline:
            batch: list[MarketTick] = []
            while len(batch) < BATCH_SIZE and not queue.empty():
                batch.append(queue.get_nowait())
            if await _post_batch(client, batch, shutdown_event):
                sent += len(batch)
            else:
                failed += len(batch)

        if not queue.empty():
            logger.warning("Drain timed out, %d ticks dropped", queue.qsize())

        logger.info("Final: %d ticks sent, %d failed", sent, failed)
    finally:
        await client.aclose()
        logger.info("HTTP connection pool closed")


# --- Shutdown handling ---

def _install_shutdown_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        if shutdown_event.is_set():
            return
        logger.info("Shutdown requested, draining queue (Ctrl+C again to force quit)")
        shutdown_event.set()

    try:
        # Unix: a signal handler registered with the event loop
        def unix_handler() -> None:
            request_shutdown()
            # Put back the default handler, so a second Ctrl+C force quits
            loop.remove_signal_handler(signal.SIGINT)

        loop.add_signal_handler(signal.SIGINT, unix_handler)
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
    except NotImplementedError:
        # Windows: the event loop doesn't support signal handlers. Inside a coroutine,
        # Ctrl+C shows up as a CancelledError (Python 3.11+), not a KeyboardInterrupt, so
        # `except KeyboardInterrupt` inside main() would never run. A plain signal handler
        # is set up instead.
        def windows_handler(signum: int, frame: object) -> None:
            signal.signal(signal.SIGINT, signal.default_int_handler)  # Second Ctrl+C force quits
            loop.call_soon_threadsafe(request_shutdown)

        signal.signal(signal.SIGINT, windows_handler)


# --- Entrypoint ---

async def main() -> None:
    queue: asyncio.Queue[MarketTick] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    shutdown_event = asyncio.Event()
    producers_done = asyncio.Event()

    _install_shutdown_handlers(shutdown_event)

    async def run_producers() -> None:
        try:
            results = await asyncio.gather(
                *(market_producer(queue, shutdown_event, match_id) for match_id in MARKETS),
                return_exceptions=True,
            )
            for match_id, result in zip(MARKETS, results):
                if isinstance(result, Exception):
                    logger.error("Producer %s crashed: %r", match_id, result)
        finally:
            producers_done.set()

    logger.info(
        "Devraya online: %d markets, tick %dms, batch %d, target %s",
        len(MARKETS), TICK_RATE_MS, BATCH_SIZE, INGEST_URL,
    )

    try:
        await asyncio.gather(
            run_producers(),
            api_flusher(queue, shutdown_event, producers_done),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        shutdown_event.set()
        raise

    logger.info("Devraya shut down cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Forced shutdown, unsent ticks were dropped")
