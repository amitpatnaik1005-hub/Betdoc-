"""Per-provider token buckets, so the fleet throttles itself before a provider has to (HTTP 429).

The bucket lives in Redis and is refilled inside a Lua script using Redis' own clock, so every
worker draws from the same bucket and a provider's published rate limit holds cluster-wide with no
clock skew between machines. If Redis is unreachable, an in-process bucket takes over: limits then
hold per process, which is still far better than none.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.adapters.ingestion.base import ThrottledError

# Returns the seconds to wait before a token is available; 0 means one was taken.
_TAKE_TOKEN = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 60)
return tostring(wait)
"""


@dataclass(frozen=True, slots=True)
class RateLimit:
    requests_per_minute: float
    burst: int

    @property
    def per_second(self) -> float:
        return self.requests_per_minute / 60.0


class _LocalBuckets:
    """In-process fallback with the same refill maths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str, limit: RateLimit) -> float:
        with self._lock:
            now = time.monotonic()
            tokens, stamp = self._state.get(key, (float(limit.burst), now))
            tokens = min(float(limit.burst), tokens + max(0.0, now - stamp) * limit.per_second)
            if tokens >= 1.0:
                self._state[key] = (tokens - 1.0, now)
                return 0.0
            self._state[key] = (tokens, now)
            return (1.0 - tokens) / limit.per_second


_LOCAL = _LocalBuckets()


class TokenBucket:
    def __init__(
        self,
        redis: Redis | None,
        prefix: str,
        max_wait_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._script = redis.register_script(_TAKE_TOKEN) if redis is not None else None

    def key(self, provider: str) -> str:
        return f"{self._prefix}:throttle:{provider}"

    async def acquire(self, provider: str, limit: RateLimit) -> float:
        """Wait (up to the budget) for a token. Returns seconds waited; raises ThrottledError past the budget."""
        waited = 0.0
        while True:
            wait = await self._take(provider, limit)
            if wait <= 0.0:
                return waited
            if waited + wait > self._max_wait:
                raise ThrottledError(f"{provider}: rate limit budget exhausted (next token in {wait:.1f}s)")
            await self._sleep(wait)
            waited += wait

    async def _take(self, provider: str, limit: RateLimit) -> float:
        if self._script is not None:
            try:
                return float(await self._script(keys=[self.key(provider)], args=[limit.per_second, limit.burst]))
            except (RedisError, OSError):
                pass  # Redis is down: fall through to the in-process bucket
        return _LOCAL.take(self.key(provider), limit)

    def limiter(self, provider: str, limit: RateLimit) -> Callable[[], Awaitable[float]]:
        """The per-request hook BaseDataIngestor calls before every HTTP attempt."""

        async def acquire() -> float:
            return await self.acquire(provider, limit)

        return acquire
