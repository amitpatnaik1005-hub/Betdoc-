"""Retry and circuit breaking. They are different mechanisms, both required.

Tenacity retries. It does not break circuits, and conflating the two is a real
outage pattern: when a venue is down, pure retry hammers a dead endpoint,
amplifies load, and burns a paid API quota to no purpose.

So this module composes two layers:

#. **Circuit breaker** (three states: CLOSED, OPEN, HALF_OPEN). After N
   consecutive failures the circuit opens and calls fail immediately without
   touching the network. After a cooldown it admits exactly one probe. Success
   closes it; failure re-opens it with the cooldown restarted.
#. **Tenacity retry** with ``wait_exponential_jitter``. Jitter is not cosmetic:
   without it, every worker that failed at the same instant retries at the same
   instant, producing a synchronised thundering herd precisely when the
   dependency is least able to absorb it.

Retry classification is allowlist-based. Only transport faults are retried.
``ValueError`` and ``KeyError`` are deterministic logic bugs, and retrying a
logic bug just executes it five times.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import wraps
from typing import Final, ParamSpec, TypeVar

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

__all__ = [
    "NON_RETRYABLE_EXCEPTIONS",
    "RETRYABLE_EXCEPTIONS",
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitState",
    "with_circuit_breaker",
]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(component="application.resilience")

P = ParamSpec("P")
R = TypeVar("R")


def _build_retryable_exceptions() -> tuple[type[BaseException], ...]:
    retryable: list[type[BaseException]] = [
        asyncio.TimeoutError,
        TimeoutError,
        ConnectionError,
        ConnectionResetError,
        ConnectionAbortedError,
        OSError,
    ]
    try:
        from aiohttp import ClientError as AiohttpClientError

        retryable.append(AiohttpClientError)
    except ImportError:  # pragma: no cover
        pass
    try:
        import httpx

        retryable.extend((httpx.TransportError, httpx.HTTPStatusError))
    except ImportError:  # pragma: no cover
        pass
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        retryable.extend((RedisConnectionError, RedisTimeoutError))
    except ImportError:  # pragma: no cover
        pass
    return tuple(retryable)


RETRYABLE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = _build_retryable_exceptions()

NON_RETRYABLE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    NotImplementedError,
)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    def __init__(self, name: str, seconds_until_retry: float) -> None:
        super().__init__(
            f"circuit {name!r} is open; next probe permitted in {seconds_until_retry:.1f}s"
        )
        self.name = name
        self.seconds_until_retry = seconds_until_retry


class CircuitBreaker:
    __slots__ = (
        "_consecutive_failures",
        "_failure_threshold",
        "_last_failure_monotonic",
        "_lock",
        "_name",
        "_opened_count",
        "_reset_timeout",
        "_state",
        "_total_failures",
        "_total_rejections",
        "_total_successes",
    )

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
    ) -> None:
        if failure_threshold < 1:
            msg = f"failure_threshold must be positive, got {failure_threshold}"
            raise ValueError(msg)
        if reset_timeout_seconds <= 0.0:
            msg = f"reset_timeout_seconds must be positive, got {reset_timeout_seconds}"
            raise ValueError(msg)

        self._name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_monotonic = 0.0
        self._lock = asyncio.Lock()
        self._total_successes = 0
        self._total_failures = 0
        self._total_rejections = 0
        self._opened_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    def snapshot(self) -> dict[str, str | int | float]:
        return {
            "circuit": self._name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "total_rejections": self._total_rejections,
            "times_opened": self._opened_count,
        }

    async def before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.CLOSED:
                return

            idle = time.monotonic() - self._last_failure_monotonic
            if idle >= self._reset_timeout:
                self._state = CircuitState.HALF_OPEN
                _log.info(
                    "circuit.half_open",
                    circuit=self._name,
                    idle_seconds=round(idle, 3),
                    detail="admitting a single probe",
                )
                return

            self._total_rejections += 1
            raise CircuitBreakerOpenError(self._name, self._reset_timeout - idle)

    async def record_success(self) -> None:
        async with self._lock:
            self._total_successes += 1
            if self._state is not CircuitState.CLOSED:
                _log.info(
                    "circuit.closed",
                    circuit=self._name,
                    previous_state=self._state.value,
                )
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0

    async def record_failure(self, error: BaseException) -> None:
        async with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            self._last_failure_monotonic = time.monotonic()

            was_half_open = self._state is CircuitState.HALF_OPEN
            if was_half_open or self._consecutive_failures >= self._failure_threshold:
                if self._state is not CircuitState.OPEN:
                    self._opened_count += 1
                    _log.error(
                        "circuit.opened",
                        circuit=self._name,
                        consecutive_failures=self._consecutive_failures,
                        threshold=self._failure_threshold,
                        reset_timeout_seconds=self._reset_timeout,
                        trigger="failed_probe" if was_half_open else "threshold",
                        error=type(error).__name__,
                    )
                self._state = CircuitState.OPEN


def _log_before_sleep(name: str) -> Callable[[RetryCallState], None]:
    def _hook(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        error = outcome.exception() if outcome is not None else None
        _log.warning(
            "resilience.retrying",
            circuit=name,
            attempt=retry_state.attempt_number,
            sleep_seconds=round(retry_state.idle_for, 3),
            error=type(error).__name__ if error else None,
            detail=str(error)[:200] if error else None,
        )

    return _hook


def with_circuit_breaker(
    breaker: CircuitBreaker | None = None,
    *,
    name: str | None = None,
    attempts: int = 5,
    initial_wait_seconds: float = 1.0,
    max_wait_seconds: float = 10.0,
    failure_threshold: int = 5,
    reset_timeout_seconds: float = 30.0,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    if attempts < 1:
        msg = f"attempts must be positive, got {attempts}"
        raise ValueError(msg)

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        circuit_name: str = name or getattr(func, "__qualname__", None) or "unnamed"
        active = breaker or CircuitBreaker(
            circuit_name,
            failure_threshold=failure_threshold,
            reset_timeout_seconds=reset_timeout_seconds,
        )

        retrying = AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
            wait=wait_exponential_jitter(
                initial=initial_wait_seconds, max=max_wait_seconds, jitter=1.0
            ),
            stop=stop_after_attempt(attempts),
            before_sleep=_log_before_sleep(circuit_name),
            reraise=True,
        )

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            async for attempt in retrying:
                with attempt:
                    await active.before_call()
                    started = time.monotonic()
                    try:
                        result = await func(*args, **kwargs)
                    except NON_RETRYABLE_EXCEPTIONS as exc:
                        _log.error(
                            "resilience.non_retryable",
                            circuit=circuit_name,
                            error=type(exc).__name__,
                            detail=str(exc)[:200],
                        )
                        raise
                    except RETRYABLE_EXCEPTIONS as exc:
                        await active.record_failure(exc)
                        raise
                    else:
                        await active.record_success()
                        _log.debug(
                            "resilience.call_succeeded",
                            circuit=circuit_name,
                            duration_ms=round((time.monotonic() - started) * 1000, 3),
                        )
                        return result
            msg = f"retry loop for {circuit_name!r} exited without a result"
            raise RuntimeError(msg)  # pragma: no cover

        wrapper.circuit_breaker = active  # type: ignore[attr-defined]
        return wrapper

    return decorator
