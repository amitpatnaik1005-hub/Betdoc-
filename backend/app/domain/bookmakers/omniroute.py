"""OmniRoute gateway: simulated real-time programmatic bet routing."""

from __future__ import annotations

import asyncio
import math
import random
import threading
import uuid
from collections.abc import Mapping
from numbers import Real
from typing import Any, Final, Literal, TypedDict

FAILURE_SENTINEL_ODDS: Final[float] = 1.01

OmniRouteStatus = Literal["SUCCESS", "FAILED"]


class OmniRouteResponse(TypedDict):
    status: OmniRouteStatus
    bookmaker_name: str
    requested_odds: float
    executed_odds: float | None
    latency_ms: float
    transaction_id: str
    failure_reason: str | None


def _require_positive_finite(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a real number, got {type(value).__name__}.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a finite, positive number, got {number!r}.")
    return number


def _require_non_negative_finite(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a real number, got {type(value).__name__}.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite, non-negative number, got {number!r}.")
    return number


class OmniRouteClient:
    """Simulated execution gateway.

    Requested odds of exactly ``FAILURE_SENTINEL_ODDS`` (1.01) always return
    ``FAILED``, which keeps failure-path tests deterministic.
    """

    def __init__(
        self,
        *,
        base_latency_ms: float = 35.0,
        jitter_ms: float = 25.0,
        simulate_delay: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._base_latency_ms = _require_non_negative_finite("base_latency_ms", base_latency_ms)
        self._jitter_ms = _require_non_negative_finite("jitter_ms", jitter_ms)
        self._simulate_delay = simulate_delay
        self._rng = rng if rng is not None else random.Random()
        self._rng_lock = threading.Lock()

    @property
    def base_latency_ms(self) -> float:
        return self._base_latency_ms

    @property
    def jitter_ms(self) -> float:
        return self._jitter_ms

    def _draw_latency_ms(self) -> float:
        with self._rng_lock:
            jitter = self._rng.uniform(0.0, self._jitter_ms) if self._jitter_ms > 0 else 0.0
        return round(self._base_latency_ms + jitter, 3)

    async def execute_bet(self, bookmaker_name: str, payload: Mapping[str, Any]) -> OmniRouteResponse:
        if not isinstance(bookmaker_name, str) or not bookmaker_name.strip():
            raise ValueError("bookmaker_name must be a non-empty string.")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping.")
        if "odds" not in payload:
            raise ValueError("payload must include 'odds'.")

        odds = _require_positive_finite("odds", payload["odds"])
        stake = payload.get("stake")
        if stake is not None:
            _require_positive_finite("stake", stake)

        name = bookmaker_name.strip()
        latency_ms = self._draw_latency_ms()
        if self._simulate_delay and latency_ms > 0:
            await asyncio.sleep(latency_ms / 1000.0)

        transaction_id = f"omr_{uuid.uuid4().hex}"

        if math.isclose(odds, FAILURE_SENTINEL_ODDS, rel_tol=0.0, abs_tol=1e-9):
            return OmniRouteResponse(
                status="FAILED",
                bookmaker_name=name,
                requested_odds=odds,
                executed_odds=None,
                latency_ms=latency_ms,
                transaction_id=transaction_id,
                failure_reason=(
                    f"OmniRoute rejected the order: requested odds {FAILURE_SENTINEL_ODDS} "
                    "are the deterministic failure sentinel."
                ),
            )

        return OmniRouteResponse(
            status="SUCCESS",
            bookmaker_name=name,
            requested_odds=odds,
            executed_odds=odds,
            latency_ms=latency_ms,
            transaction_id=transaction_id,
            failure_reason=None,
        )


default_omniroute_client: Final[OmniRouteClient] = OmniRouteClient()
