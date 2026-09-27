#!/usr/bin/env python3
"""DEVRAYA: high-frequency stochastic market ingestor (zero external dependencies).

Simulates live 1X2 ("Match Odds") markets for several football matches and
POSTs tick batches to the BetDoc ingestion endpoint.

Configuration (environment variables):
    INGESTION_API_KEY       (required) value for the X-Ingestion-Key header
    DEVRAYA_INGESTION_URL   (optional) default http://localhost:8000/api/v1/ingest
    DEVRAYA_SEED            (optional) integer RNG seed for reproducible runs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Final, TypedDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger: logging.Logger = logging.getLogger("devraya")

SELECTIONS: Final[tuple[str, str, str]] = ("HOME", "DRAW", "AWAY")
MARKET_TYPE: Final[str] = "Match Odds"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _require_env(name: str) -> str:
    # Use hardcoded fallback for local development if not provided in env
    fallback = "4ZeGFctgTu9s8VnYhsmnvSld_S5gjORQLXUQetVf-droAh4uFgXGNxjgb2WyqVZS"
    value: str | None = os.environ.get(name, fallback)
    if not value:
        sys.exit(f"error: environment variable {name} is required")
    return value


@dataclass(frozen=True)
class Config:
    url: str = field(
        default_factory=lambda: os.environ.get(
            "DEVRAYA_INGESTION_URL", "http://127.0.0.1:8000/api/v1/ingest"
        )
    )
    api_key: str = field(default_factory=lambda: _require_env("INGESTION_API_KEY"))
    overround: float = 1.04
    drift_sigma: float = 0.015
    prob_floor: float = 0.01
    mean_reversion: float = 0.01
    min_odds: float = 1.01
    suspend_chance: float = 0.05
    min_interval_s: float = 0.3
    max_interval_s: float = 1.2
    http_timeout_s: float = 3.0
    max_backoff_s: float = 10.0
    stats_every_n_batches: int = 50

    def __repr__(self) -> str:
        return f"Config(url={self.url!r}, api_key='***', overround={self.overround})"


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #
class Tick(TypedDict):
    match_id: str
    home_team: str
    away_team: str
    market_type: str
    selection: str
    odds: float
    true_probability: float
    is_suspended: bool


# --------------------------------------------------------------------------- #
# Stochastic market model
# --------------------------------------------------------------------------- #
def _normalize(probs: list[float]) -> list[float]:
    total: float = sum(probs)
    return [p / total for p in probs]


def _round_to_unit_simplex(probs: list[float], places: int = 4) -> list[float]:
    rounded: list[float] = [round(p, places) for p in probs]
    residual: float = round(1.0 - sum(rounded), places)
    if residual != 0.0:
        idx: int = max(range(len(rounded)), key=lambda i: rounded[i])
        rounded[idx] = round(rounded[idx] + residual, places)
    return rounded


class MarketState:
    def __init__(
        self,
        config: Config,
        match_id: str,
        home_team: str,
        away_team: str,
        prior: tuple[float, float, float],
    ) -> None:
        self._config: Config = config
        self.match_id: str = match_id
        self.home_team: str = home_team
        self.away_team: str = away_team
        self._prior: list[float] = _normalize(list(prior))
        self._probs: list[float] = list(self._prior)

    def _step(self) -> list[float]:
        cfg: Config = self._config
        drifted: list[float] = []
        for p, anchor in zip(self._probs, self._prior):
            p_next: float = (
                p + cfg.mean_reversion * (anchor - p) + random.gauss(0.0, cfg.drift_sigma)
            )
            drifted.append(max(cfg.prob_floor, p_next))
        self._probs = _normalize(drifted)
        return _round_to_unit_simplex(self._probs, places=4)

    def _to_odds(self, true_probability: float) -> float:
        cfg: Config = self._config
        raw_odds: float = 1.0 / (max(true_probability, 1e-4) * cfg.overround)
        return round(max(cfg.min_odds, raw_odds), 2)

    def generate_ticks(self) -> list[Tick]:
        probabilities: list[float] = self._step()
        is_suspended: bool = random.random() < self._config.suspend_chance
        return [
            Tick(
                match_id=self.match_id,
                home_team=self.home_team,
                away_team=self.away_team,
                market_type=MARKET_TYPE,
                selection=selection,
                odds=self._to_odds(tp),
                true_probability=tp,
                is_suspended=is_suspended,
            )
            for selection, tp in zip(SELECTIONS, probabilities)
        ]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class DevrayaEngine:
    def __init__(self, config: Config, shutdown_event: asyncio.Event) -> None:
        self._config: Config = config
        self._shutdown: asyncio.Event = shutdown_event
        self._markets: list[MarketState] = [
            MarketState(config, "EPL-LIV-ARS", "Liverpool", "Arsenal", (0.44, 0.27, 0.29)),
            MarketState(config, "LAL-RMA-BAR", "Real Madrid", "Barcelona", (0.42, 0.25, 0.33)),
            MarketState(config, "EPL-MCI-MUN", "Man City", "Man United", (0.62, 0.21, 0.17)),
            MarketState(config, "BUN-BAY-BVB", "Bayern Munich", "Dortmund", (0.58, 0.22, 0.20)),
        ]
        self._consecutive_failures: int = 0
        self._batches_sent: int = 0
        self._ticks_sent: int = 0
        self._started_at: float = time.monotonic()

    def _send_request(self, json_bytes: bytes) -> int:
        req: urllib.request.Request = urllib.request.Request(
            self._config.url,
            data=json_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Ingestion-Key": self._config.api_key,
                "User-Agent": "devraya-worker/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._config.http_timeout_s) as response:
                response.read()
                status: int = response.status
                return status
        except urllib.error.HTTPError as err:
            try:
                body: str = err.read(512).decode("utf-8", errors="replace")
            finally:
                err.close()
            logger.warning("Ingestion rejected batch: HTTP %d %s", err.code, body.strip())
            return err.code

    async def emit_batch(self, batch: list[Tick]) -> None:
        try:
            payload: bytes = json.dumps(batch, allow_nan=False, separators=(",", ":")).encode("utf-8")
            status: int = await asyncio.to_thread(self._send_request, payload)
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures == 1 or self._consecutive_failures % 10 == 0:
                logger.error(
                    "Ingestion unreachable (%d consecutive failures): %s: %s",
                    self._consecutive_failures,
                    exc.__class__.__name__,
                    exc,
                )
            return

        if 200 <= status < 300:
            if self._consecutive_failures:
                logger.info("Ingestion recovered after %d failures", self._consecutive_failures)
            self._consecutive_failures = 0
            self._batches_sent += 1
            self._ticks_sent += len(batch)
            if self._batches_sent % self._config.stats_every_n_batches == 0:
                elapsed: float = max(time.monotonic() - self._started_at, 1e-9)
                logger.info(
                    "Sent %d batches / %d ticks (%.1f ticks/s)",
                    self._batches_sent,
                    self._ticks_sent,
                    self._ticks_sent / elapsed,
                )
        else:
            self._consecutive_failures += 1

    def _next_delay(self) -> float:
        cfg: Config = self._config
        base: float = random.uniform(cfg.min_interval_s, cfg.max_interval_s)
        if self._consecutive_failures == 0:
            return base
        return min(cfg.max_backoff_s, base * (2 ** min(self._consecutive_failures, 6)))

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        logger.info("DEVRAYA online: %d markets -> %s", len(self._markets), self._config.url)
        while not self._shutdown.is_set():
            await self._interruptible_sleep(self._next_delay())
            if self._shutdown.is_set():
                break

            batch: list[Tick] = []
            for market in self._markets:
                batch.extend(market.generate_ticks())

            await self.emit_batch(batch)

        logger.info(
            "DEVRAYA stopped cleanly: %d batches / %d ticks sent",
            self._batches_sent,
            self._ticks_sent,
        )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> None:
    signals: list[signal.Signals] = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)

    def _on_signal(sig: signal.Signals) -> None:
        if shutdown.is_set():
            logger.warning("Second %s received: forcing exit", sig.name)
            os._exit(130)
        logger.info("%s received: finishing in-flight request, then shutting down", sig.name)
        shutdown.set()

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            signal.signal(
                sig,
                lambda _signum, _frame, s=sig: loop.call_soon_threadsafe(_on_signal, s),
            )


async def _main() -> None:
    config: Config = Config()

    seed_raw: str | None = os.environ.get("DEVRAYA_SEED")
    if seed_raw is not None:
        random.seed(int(seed_raw))
        logger.info("RNG seeded with %s", seed_raw)

    shutdown: asyncio.Event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown)

    engine: DevrayaEngine = DevrayaEngine(config, shutdown)
    await engine.run()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Interrupted before startup completed")


if __name__ == "__main__":
    main()
