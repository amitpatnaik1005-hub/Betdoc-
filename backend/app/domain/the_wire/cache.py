"""Asyncio-safe, stampede-proof TTL cache with scatter-gather and negative caching.

All expiry arithmetic uses time.monotonic() (immune to NTP/wall-clock shifts).
Safety relies on asyncio's single-threaded model: every check-and-register
section runs synchronously with no await in between.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Generic, TypeVar, cast

logger = logging.getLogger("betdoc.the_wire")

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

DEFAULT_NEGATIVE_TTL_SECONDS: Final[float] = 10.0
DEFAULT_MAX_ENTRIES: Final[int] = 10_000


class _Missing:
    """Sentinel distinguishing 'not cached' from a negatively cached None."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final[_Missing] = _Missing()


@dataclass(slots=True)
class _Entry(Generic[V]):
    value: V | None
    expires_at: float


class CoalescingTTLCache(Generic[K, V]):
    def __init__(
        self,
        name: str,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if not math.isfinite(negative_ttl_seconds) or negative_ttl_seconds <= 0:
            raise ValueError("negative_ttl_seconds must be a positive finite number")
        self.name = name
        self._max_entries = max_entries
        self._negative_ttl = negative_ttl_seconds
        self._clock = clock
        self._store: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._in_flight: dict[K, asyncio.Future[V | None]] = {}

    def __len__(self) -> int:
        return len(self._store)

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    # ------------------------------------------------------------------ basic ops
    def get(self, key: K) -> V | None | _Missing:
        """Return the cached value (possibly a negatively cached None) or MISSING."""
        entry = self._store.get(key)
        if entry is None:
            return MISSING
        if entry.expires_at <= self._clock():
            del self._store[key]
            return MISSING
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: K, value: V | None, ttl_seconds: float) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            self._store.pop(key, None)
            return
        now = self._clock()
        self._store[key] = _Entry(value=value, expires_at=now + ttl_seconds)
        self._store.move_to_end(key)
        if len(self._store) > self._max_entries:
            self._purge_expired(now)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)  # evict least recently used

    def _purge_expired(self, now: float | None = None) -> int:
        current = self._clock() if now is None else now
        expired = [k for k, e in self._store.items() if e.expires_at <= current]
        for k in expired:
            del self._store[k]
        return len(expired)

    def invalidate(self, key: K) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    # ------------------------------------------------------------ single-key fetch
    async def get_or_fetch(
        self,
        key: K,
        ttl_seconds: float,
        fetch_coroutine_func: Callable[[], Awaitable[V]],
    ) -> V | None:
        # ---- synchronous section: no await until the Future is registered ----
        cached = self.get(key)
        if cached is not MISSING:
            return cast("V | None", cached)
        pending = self._in_flight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[V | None] = asyncio.get_running_loop().create_future()
        self._in_flight[key] = future
        # ---- end synchronous section ----

        result: V | None = None
        try:
            try:
                result = await fetch_coroutine_func()
            except Exception:
                logger.warning(
                    "Cache %s: fetch failed for %r; negative-caching for %.0fs",
                    self.name, key, self._negative_ttl, exc_info=True,
                )
                result = None
                self.set(key, None, self._negative_ttl)
            else:
                self.set(key, result, ttl_seconds)
            return result
        finally:
            if self._in_flight.get(key) is future:
                del self._in_flight[key]
            if not future.done():
                future.set_result(result)  # None on cancellation; never an exception

    # ------------------------------------------------------- scatter-gather fetch
    async def get_many_or_fetch(
        self,
        keys: Iterable[K],
        ttl_seconds: float,
        bulk_fetch_func: Callable[[list[K]], Awaitable[Mapping[K, V]]],
    ) -> dict[K, V | None]:
        """Per-key cache lookup; ONLY un-cached, un-owned keys go to bulk_fetch_func."""
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return {}

        results: dict[K, V | None] = {}
        waiting: dict[K, asyncio.Future[V | None]] = {}
        owned: dict[K, asyncio.Future[V | None]] = {}

        # ---- synchronous section: classify every key and claim misses ----
        loop = asyncio.get_running_loop()
        for key in ordered:
            cached = self.get(key)
            if cached is not MISSING:
                results[key] = cast("V | None", cached)
                continue
            pending = self._in_flight.get(key)
            if pending is not None:
                waiting[key] = pending
                continue
            future: asyncio.Future[V | None] = loop.create_future()
            self._in_flight[key] = future
            owned[key] = future
        # ---- end synchronous section ----

        if owned:
            results.update(await self._fetch_owned(owned, ttl_seconds, bulk_fetch_func))
        if waiting:
            values = await asyncio.gather(*(asyncio.shield(f) for f in waiting.values()))
            results.update(zip(waiting.keys(), values))

        return {k: results.get(k) for k in ordered}

    async def _fetch_owned(
        self,
        owned: dict[K, asyncio.Future[V | None]],
        ttl_seconds: float,
        bulk_fetch_func: Callable[[list[K]], Awaitable[Mapping[K, V]]],
    ) -> dict[K, V | None]:
        keys = list(owned)
        resolved: dict[K, V | None] = {}
        try:
            try:
                fetched = await bulk_fetch_func(keys)
            except Exception:
                logger.warning(
                    "Cache %s: bulk fetch failed for %d keys; negative-caching for %.0fs",
                    self.name, len(keys), self._negative_ttl, exc_info=True,
                )
                for k in keys:
                    self.set(k, None, self._negative_ttl)
                    resolved[k] = None
            else:
                for k in keys:  # unrequested keys in `fetched` are ignored
                    if k in fetched:
                        value = fetched[k]
                        self.set(k, value, ttl_seconds)
                        resolved[k] = value
                    else:
                        self.set(k, None, self._negative_ttl)
                        resolved[k] = None
            return resolved
        finally:
            for k, fut in owned.items():
                if self._in_flight.get(k) is fut:
                    del self._in_flight[k]
                if not fut.done():
                    fut.set_result(resolved.get(k))
