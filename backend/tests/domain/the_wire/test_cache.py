import asyncio
import time

import pytest

from app.domain.the_wire.cache import MISSING, CoalescingTTLCache

pytestmark = pytest.mark.asyncio


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(clock: FakeClock) -> CoalescingTTLCache[str, str]:
    return CoalescingTTLCache("test", negative_ttl_seconds=10.0, clock=clock)


async def test_default_clock_is_monotonic() -> None:
    assert CoalescingTTLCache("x")._clock is time.monotonic


async def test_entries_expire_on_monotonic_clock(cache, clock) -> None:
    cache.set("k", "v", 15)
    clock.advance(14.9)
    assert cache.get("k") == "v"
    clock.advance(0.1)
    assert cache.get("k") is MISSING


async def test_get_or_fetch_coalesces_concurrent_callers(cache) -> None:
    calls = 0
    release = asyncio.Event()

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "v"

    tasks = [asyncio.create_task(cache.get_or_fetch("k", 60, fetch)) for _ in range(50)]
    await asyncio.sleep(0)
    assert cache.in_flight_count == 1
    release.set()
    assert await asyncio.gather(*tasks) == ["v"] * 50
    assert calls == 1
    assert cache.in_flight_count == 0


async def test_failure_is_negatively_cached_for_ten_seconds(cache, clock) -> None:
    calls = 0

    async def boom() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider down")

    assert await cache.get_or_fetch("k", 60, boom) is None
    assert await cache.get_or_fetch("k", 60, boom) is None
    assert calls == 1
    assert cache.get("k") is None  # negative entry, not MISSING
    assert cache.in_flight_count == 0

    clock.advance(10.0)
    assert cache.get("k") is MISSING
    await cache.get_or_fetch("k", 60, boom)
    assert calls == 2


async def test_only_misses_reach_bulk_fetch(cache) -> None:
    cache.set("a", "A", 60)
    seen: list[list[str]] = []

    async def bulk(keys: list[str]) -> dict[str, str]:
        seen.append(keys)
        return {k: k.upper() for k in keys}

    result = await cache.get_many_or_fetch(["a", "b", "c", "b"], 60, bulk)
    assert seen == [["b", "c"]]
    assert result == {"a": "A", "b": "B", "c": "C"}
    assert cache.get("c") == "C"


async def test_overlapping_bulk_requests_share_in_flight_keys(cache) -> None:
    seen: list[list[str]] = []
    release = asyncio.Event()

    async def bulk(keys: list[str]) -> dict[str, str]:
        seen.append(sorted(keys))
        await release.wait()
        return {k: k.upper() for k in keys}

    first = asyncio.create_task(cache.get_many_or_fetch(["a", "b"], 60, bulk))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.get_many_or_fetch(["b", "c"], 60, bulk))
    await asyncio.sleep(0)
    release.set()

    r1, r2 = await asyncio.gather(first, second)
    assert seen == [["a", "b"], ["c"]]  # "b" fetched exactly once
    assert r1 == {"a": "A", "b": "B"}
    assert r2 == {"b": "B", "c": "C"}
    assert cache.in_flight_count == 0


async def test_bulk_failure_negatively_caches_each_missed_key(cache, clock) -> None:
    calls = 0

    async def bulk(keys: list[str]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    assert await cache.get_many_or_fetch(["a", "b"], 60, bulk) == {"a": None, "b": None}
    clock.advance(9.9)
    assert await cache.get_many_or_fetch(["a", "b"], 60, bulk) == {"a": None, "b": None}
    assert calls == 1
    assert cache.in_flight_count == 0


async def test_omitted_keys_negative_cached_and_unrequested_keys_ignored(cache) -> None:
    async def bulk(keys: list[str]) -> dict[str, str]:
        return {"a": "A", "zzz": "Z"}

    assert await cache.get_many_or_fetch(["a", "b"], 60, bulk) == {"a": "A", "b": None}
    assert cache.get("b") is None
    assert cache.get("zzz") is MISSING


async def test_waiter_cancellation_does_not_cancel_owner(cache) -> None:
    release = asyncio.Event()

    async def fetch() -> str:
        await release.wait()
        return "v"

    owner = asyncio.create_task(cache.get_or_fetch("k", 60, fetch))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cache.get_or_fetch("k", 60, fetch))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await owner == "v"
    assert cache.get("k") == "v"


async def test_owner_cancellation_releases_waiters_without_caching(cache) -> None:
    async def fetch() -> str:
        await asyncio.Event().wait()  # never completes
        return "unreachable"

    owner = asyncio.create_task(cache.get_or_fetch("k", 60, fetch))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cache.get_or_fetch("k", 60, fetch))
    await asyncio.sleep(0)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await asyncio.wait_for(waiter, timeout=1) is None
    assert cache.get("k") is MISSING
    assert cache.in_flight_count == 0


async def test_lru_eviction_respects_max_entries(clock) -> None:
    lru: CoalescingTTLCache[str, int] = CoalescingTTLCache("lru", max_entries=2, clock=clock)
    lru.set("a", 1, 60)
    lru.set("b", 2, 60)
    assert lru.get("a") == 1  # touch "a" so "b" becomes LRU
    lru.set("c", 3, 60)
    assert lru.get("b") is MISSING
    assert lru.get("a") == 1
    assert lru.get("c") == 3


async def test_non_positive_ttl_is_not_stored(cache) -> None:
    cache.set("k", "v", 0)
    assert cache.get("k") is MISSING
