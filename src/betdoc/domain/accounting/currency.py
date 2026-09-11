"""Asynchronous FX engine with tiered caching and a circuit breaker.



Freshness model

---------------

Two windows, not one:



``fresh_ttl_seconds`` (default 60)

    How long a rate is served without consulting the provider. Inside this

    window the cached rate is authoritative.



``retention_seconds`` (default 24h)

    How long a rate is *kept* after it stops being fresh. This is what makes

    "fall back to the last known rate" possible at all: a single 60s TTL

    evicts the entry at the exact moment the fallback is needed.



Between the two windows a rate is stale-but-usable. Callers that can tolerate

that (batch P&L reconciliation) pass ``allow_stale=True`` and get the rate

plus a warning. Callers that cannot (pricing a live order) pass

``allow_stale=False`` and get :class:`StaleRateError`. Past the retention

window, or with an empty cache and a dead provider, the result is always

:class:`FXRateUnavailableError`. No rate is ever synthesised.

"""



from __future__ import annotations

import abc
import asyncio
import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol, runtime_checkable

from betdoc.domain.accounting.types import (
    CurrencyCode,
    ExchangeRate,
    FXRateUnavailableError,
    StaleRateError,
    money_context,
)

__all__ = [

    "CurrencyConverter",

    "HttpRateProvider",

    "InMemoryRateCache",

    "RateCache",

    "RateProvider",

    "RedisRateCache",

    "StaticRateProvider",

]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_FRESH_TTL_SECONDS: Final[float] = 60.0

DEFAULT_RETENTION_SECONDS: Final[float] = 86_400.0

DEFAULT_BREAKER_THRESHOLD: Final[int] = 5

DEFAULT_BREAKER_COOLDOWN_SECONDS: Final[float] = 30.0





# ------------------------------------------------------------------------------

# Providers

# ------------------------------------------------------------------------------





@runtime_checkable

class RateProvider(Protocol):

    """Upstream source of rates quoted against a single base currency."""



    @property

    def name(self) -> str:

        """Identifier recorded in :attr:`ExchangeRate.source`."""



    async def fetch(self, base: CurrencyCode) -> Mapping[CurrencyCode, Decimal]:

        """Return quote-currency rates for ``base``, excluding ``base`` itself."""



    async def close(self) -> None:

        """Release any transport resources. Must be idempotent."""





class StaticRateProvider:

    """Fixed rate table.



    Intended for tests, backtests against a historical snapshot, and

    air-gapped deployments. Rates never change, so anything built on it is

    reproducible.

    """



    __slots__ = ("_name", "_table")



    def __init__(

        self,

        table: Mapping[CurrencyCode, Mapping[CurrencyCode, Decimal | str]],

        *,

        name: str = "static",

    ) -> None:

        with money_context():

            self._table: dict[CurrencyCode, dict[CurrencyCode, Decimal]] = {

                CurrencyCode(base): {

                    CurrencyCode(quote): (

                        rate if isinstance(rate, Decimal) else Decimal(str(rate))

                    )

                    for quote, rate in quotes.items()

                }

                for base, quotes in table.items()

            }

        self._name: str = name



    @property

    def name(self) -> str:

        """Provider identifier."""

        return self._name



    async def fetch(self, base: CurrencyCode) -> Mapping[CurrencyCode, Decimal]:

        """Return the configured rates for ``base``."""

        try:

            return dict(self._table[base])

        except KeyError as error:

            raise FXRateUnavailableError(

                base.value,

                "*",

                message=f"static table has no rates for base {base.value}",

            ) from error



    async def close(self) -> None:

        """No-op: nothing to release."""

        return None





class HttpRateProvider:

    """Fetches rates from a JSON HTTP endpoint.



    Expects a response shaped ``{"base": "USD", "rates": {"EUR": "0.92", ...}}``.

    Numeric values are read via ``str()`` before entering :class:`Decimal`,

    because the JSON parser has already produced a binary float and

    ``Decimal(0.92)`` would preserve that representation error. ``str()`` on

    the float recovers the shortest round-tripping decimal, which is the

    value the provider actually published.

    """



    __slots__ = ("_headers", "_name", "_owns_session", "_session", "_timeout", "_url")



    def __init__(

        self,

        url: str,

        *,

        name: str = "http",

        api_key: str | None = None,

        timeout_seconds: float = 5.0,

        session: Any = None,

    ) -> None:

        if not url:

            raise ValueError("url must not be empty")

        self._url: str = url.rstrip("/")

        self._name: str = name

        self._timeout: float = float(timeout_seconds)

        self._session: Any = session

        self._owns_session: bool = session is None

        self._headers: dict[str, str] = {"Accept": "application/json"}

        if api_key:

            self._headers["Authorization"] = f"Bearer {api_key}"



    @property

    def name(self) -> str:

        """Provider identifier."""

        return self._name



    async def _ensure_session(self) -> Any:

        import aiohttp



        if self._session is None or self._session.closed:

            self._session = aiohttp.ClientSession(

                timeout=aiohttp.ClientTimeout(total=self._timeout)

            )

            self._owns_session = True

        return self._session



    async def fetch(self, base: CurrencyCode) -> Mapping[CurrencyCode, Decimal]:

        """Retrieve rates for ``base`` from the remote endpoint."""

        import aiohttp



        session = await self._ensure_session()

        try:

            async with session.get(

                f"{self._url}/latest",

                params={"base": base.value},

                headers=self._headers,

            ) as response:

                if response.status >= 400:

                    raise FXRateUnavailableError(

                        base.value,

                        "*",

                        message=f"FX provider returned HTTP {response.status}",

                        details={"http_status": response.status},

                    )

                body: Any = await response.json(content_type=None)

        except TimeoutError as error:

            raise FXRateUnavailableError(

                base.value, "*", message="FX provider request timed out"

            ) from error

        except aiohttp.ClientError as error:

            raise FXRateUnavailableError(

                base.value, "*", message=f"FX provider transport failure: {error}"

            ) from error



        if not isinstance(body, Mapping) or not isinstance(body.get("rates"), Mapping):

            raise FXRateUnavailableError(

                base.value,

                "*",

                message="FX provider response is missing a 'rates' object",

            )



        parsed: dict[CurrencyCode, Decimal] = {}

        with money_context():

            for code, raw in body["rates"].items():

                try:

                    currency = CurrencyCode(str(code).upper())

                except ValueError:

                    continue  # Unsupported currency: skip, do not fail the batch.

                try:

                    value = Decimal(str(raw))

                except Exception:

                    _LOG.warning("ignoring unparseable rate for %s: %r", code, raw)

                    continue

                if value.is_finite() and value > 0:

                    parsed[currency] = value



        if not parsed:

            raise FXRateUnavailableError(

                base.value, "*", message="FX provider returned no usable rates"

            )

        return parsed



    async def close(self) -> None:

        """Close the HTTP session if this provider owns it."""

        if self._owns_session and self._session is not None and not self._session.closed:

            await self._session.close()

        self._session = None





# ------------------------------------------------------------------------------

# Caches

# ------------------------------------------------------------------------------





class RateCache(abc.ABC):

    """Storage for observed rates, keyed by currency pair."""



    @staticmethod

    def key(base: CurrencyCode, quote: CurrencyCode) -> str:

        """Return the canonical cache key for a pair."""

        return f"fx:{base.value}:{quote.value}"



    @abc.abstractmethod

    async def get(self, base: CurrencyCode, quote: CurrencyCode) -> ExchangeRate | None:

        """Return the stored rate for a pair, or ``None`` if absent."""



    @abc.abstractmethod

    async def put(self, rate: ExchangeRate) -> None:

        """Store a rate, replacing any previous observation for the pair."""



    @abc.abstractmethod

    async def close(self) -> None:

        """Release resources. Must be idempotent."""





class InMemoryRateCache(RateCache):

    """Process-local cache with retention-based eviction.



    Suitable for a single-process deployment. With multiple API replicas each

    keeps its own copy, so they can briefly disagree by up to the freshness

    window; use :class:`RedisRateCache` when that matters.

    """



    __slots__ = ("_lock", "_retention", "_store")



    def __init__(self, *, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> None:

        self._retention: float = float(retention_seconds)

        self._store: dict[str, ExchangeRate] = {}

        self._lock: asyncio.Lock = asyncio.Lock()



    async def get(self, base: CurrencyCode, quote: CurrencyCode) -> ExchangeRate | None:

        """Return the stored rate, evicting it if past retention."""

        async with self._lock:

            rate = self._store.get(self.key(base, quote))

            if rate is None:

                return None

            if rate.age_seconds() > self._retention:

                del self._store[self.key(base, quote)]

                return None

            return rate



    async def put(self, rate: ExchangeRate) -> None:

        """Store a rate and opportunistically evict expired neighbours."""

        async with self._lock:

            self._store[self.key(rate.base, rate.quote)] = rate

            expired = [

                key

                for key, stored in self._store.items()

                if stored.age_seconds() > self._retention

            ]

            for key in expired:

                del self._store[key]



    async def close(self) -> None:

        """Drop every cached rate."""

        async with self._lock:

            self._store.clear()





class RedisRateCache(RateCache):

    """Shared cache backed by Redis, with retention enforced by key expiry.



    A Redis failure is logged and treated as a cache miss rather than

    propagated. The converter can still reach the provider, so a cache outage

    degrades latency instead of halting settlement.

    """



    __slots__ = ("_client", "_retention")



    def __init__(self, client: Any, *, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> None:

        self._client: Any = client

        self._retention: int = int(retention_seconds)



    async def get(self, base: CurrencyCode, quote: CurrencyCode) -> ExchangeRate | None:

        """Read and deserialise a stored rate."""

        try:

            raw = await self._client.get(self.key(base, quote))

        except Exception as error:

            _LOG.warning("redis rate cache read failed: %s", error)

            return None

        if not raw:

            return None

        try:

            payload = json.loads(raw)

            return ExchangeRate(

                base=CurrencyCode(payload["base"]),

                quote=CurrencyCode(payload["quote"]),

                rate=Decimal(payload["rate"]),

                fetched_at=datetime.fromisoformat(payload["fetched_at"]),

                source=payload.get("source", "redis"),

            )

        except Exception as error:

            _LOG.warning("discarding corrupt cached rate for %s/%s: %s", base, quote, error)

            return None



    async def put(self, rate: ExchangeRate) -> None:

        """Serialise and store a rate with a retention TTL.



        The rate is written as a *string*, never a float, so it survives the

        round trip bit-for-bit.

        """

        payload = json.dumps(

            {

                "base": rate.base.value,

                "quote": rate.quote.value,

                "rate": str(rate.rate),

                "fetched_at": rate.fetched_at.isoformat(),

                "source": rate.source,

            }

        )

        try:

            await self._client.set(

                self.key(rate.base, rate.quote), payload, ex=self._retention

            )

        except Exception as error:

            _LOG.warning("redis rate cache write failed: %s", error)



    async def close(self) -> None:

        """Close the Redis client if it owns a connection pool."""

        close = getattr(self._client, "aclose", None) or getattr(

            self._client, "close", None

        )

        if close is not None:

            result = close()

            if asyncio.iscoroutine(result):

                await result





# ------------------------------------------------------------------------------

# Circuit breaker

# ------------------------------------------------------------------------------





class _CircuitBreaker:

    """Trips after consecutive provider failures and recovers on a timer.



    Without this, every conversion during an FX outage pays the full provider

    timeout. At settlement volume that turns a provider blip into a queue

    backlog measured in hours, even though a perfectly usable cached rate was

    sitting right there.

    """



    __slots__ = ("_cooldown", "_failures", "_opened_at", "_threshold")



    def __init__(self, *, threshold: int, cooldown_seconds: float) -> None:

        self._threshold: int = max(1, int(threshold))

        self._cooldown: float = float(cooldown_seconds)

        self._failures: int = 0

        self._opened_at: float | None = None



    @property

    def is_open(self) -> bool:

        """Whether provider calls are currently short-circuited."""

        if self._opened_at is None:

            return False

        if (time.monotonic() - self._opened_at) >= self._cooldown:

            # Half-open: allow exactly one probe through.

            self._opened_at = None

            self._failures = self._threshold - 1

            return False

        return True



    @property

    def failures(self) -> int:

        """Consecutive failure count."""

        return self._failures



    def record_success(self) -> None:

        """Reset the breaker after a successful call."""

        self._failures = 0

        self._opened_at = None



    def record_failure(self) -> None:

        """Count a failure and open the breaker at the threshold."""

        self._failures += 1

        if self._failures >= self._threshold and self._opened_at is None:

            self._opened_at = time.monotonic()

            _LOG.error(

                "FX circuit breaker opened after %d consecutive failures; "

                "serving cached rates for %.0fs",

                self._failures,

                self._cooldown,

            )





# ------------------------------------------------------------------------------

# Converter

# ------------------------------------------------------------------------------





class CurrencyConverter:

    """Converts :class:`Money` between currencies using cached live rates."""



    __slots__ = (

        "_base",

        "_breaker",

        "_cache",

        "_fresh_ttl",

        "_inflight",

        "_inflight_lock",

        "_provider",

        "_retention",

    )



    def __init__(

        self,

        base_currency: CurrencyCode | str,

        provider: RateProvider,

        *,

        cache: RateCache | None = None,

        fresh_ttl_seconds: float = DEFAULT_FRESH_TTL_SECONDS,

        retention_seconds: float = DEFAULT_RETENTION_SECONDS,

        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,

        breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS,

    ) -> None:

        if fresh_ttl_seconds <= 0:

            raise ValueError("fresh_ttl_seconds must be positive")

        if retention_seconds < fresh_ttl_seconds:

            raise ValueError(

                "retention_seconds must be >= fresh_ttl_seconds, otherwise a rate "

                "is evicted before it can ever be used as a fallback"

            )



        self._base: CurrencyCode = CurrencyCode(base_currency)

        self._provider: RateProvider = provider

        self._cache: RateCache = cache or InMemoryRateCache(

            retention_seconds=retention_seconds

        )

        self._fresh_ttl: float = float(fresh_ttl_seconds)

        self._retention: float = float(retention_seconds)

        self._breaker: _CircuitBreaker = _CircuitBreaker(

            threshold=breaker_threshold, cooldown_seconds=breaker_cooldown_seconds

        )

        self._inflight: dict[CurrencyCode, asyncio.Future[None]] = {}

        self._inflight_lock: asyncio.Lock = asyncio.Lock()



    @property

    def base_currency(self) -> CurrencyCode:

        """The currency all normalisation targets by default."""

        return self._base



    @property

    def breaker_open(self) -> bool:

        """Whether the provider circuit breaker is currently open."""

        return self._breaker.is_open



    async def get_rate(

        self,

        base: CurrencyCode | str,

        quote: CurrencyCode | str,

        *,

        allow_stale: bool = True,

    ) -> ExchangeRate:

        """Return the rate to convert ``base`` into ``quote``.



        Resolution order: identity, fresh cache, provider refresh, stale

        cache. Cross rates that do not involve the configured base currency

        are derived as ``(base→quote) = (base→pivot) / (base→pivot)`` through

        the pivot, since providers quote against one base at a time.



        Raises

        ------

        StaleRateError

            When only a stale rate exists and ``allow_stale`` is ``False``.

        FXRateUnavailableError

            When no rate exists within the retention window.

        """

        base_code = CurrencyCode(base)

        quote_code = CurrencyCode(quote)



        if base_code is quote_code:

            return ExchangeRate(

                base=base_code,

                quote=quote_code,

                rate=Decimal(1),

                fetched_at=datetime.now(tz=UTC),

                source="identity",

            )



        direct = await self._resolve_pair(base_code, quote_code, allow_stale=allow_stale)

        if direct is not None:

            return direct



        # Derive through the configured base currency.

        left = await self._resolve_pair(self._base, base_code, allow_stale=allow_stale)

        right = await self._resolve_pair(self._base, quote_code, allow_stale=allow_stale)

        if left is None or right is None:

            raise FXRateUnavailableError(

                base_code.value,

                quote_code.value,

                message=(

                    f"cannot derive {base_code.value}/{quote_code.value}: no usable "

                    f"rate via pivot {self._base.value}"

                ),

            )



        with money_context():

            derived = right.rate / left.rate

        return ExchangeRate(

            base=base_code,

            quote=quote_code,

            rate=derived,

            fetched_at=min(left.fetched_at, right.fetched_at),

            source=f"derived:{self._base.value}",

        )



    async def _resolve_pair(

        self, base: CurrencyCode, quote: CurrencyCode, *, allow_stale: bool

    ) -> ExchangeRate | None:

        """Resolve one pair from cache or provider, or return ``None``."""

        if base is quote:

            return ExchangeRate(

                base=base,

                quote=quote,

                rate=Decimal(1),

                fetched_at=datetime.now(tz=UTC),

                source="identity",

            )



        cached = await self._cache.get(base, quote)

        if cached is not None and cached.is_fresh(self._fresh_ttl):

            return cached



        if not self._breaker.is_open:

            await self._refresh(base)

            refreshed = await self._cache.get(base, quote)

            if refreshed is not None and refreshed.is_fresh(self._fresh_ttl):

                return refreshed

            cached = refreshed or cached



        if cached is None:

            return None



        age = cached.age_seconds()

        if age > self._retention:

            return None



        if not allow_stale:

            raise StaleRateError(

                base.value, quote.value, age, self._fresh_ttl, rate=cached.rate

            )



        _LOG.warning(

            "serving stale rate %s/%s (%.1fs old, fresh window %.0fs)",

            base.value,

            quote.value,

            age,

            self._fresh_ttl,

        )

        return cached



    async def _refresh(self, base: CurrencyCode) -> None:

        """Fetch and cache every rate for ``base``, collapsing concurrent calls.



        Single-flight matters here: normalising a 50,000-row ledger would

        otherwise fire 50,000 simultaneous provider requests the moment the

        60-second window lapses, and most FX APIs answer that with a 429.

        """

        async with self._inflight_lock:

            existing = self._inflight.get(base)

            if existing is not None:

                leader = False

                waiter = existing

            else:

                leader = True

                waiter = asyncio.get_running_loop().create_future()

                self._inflight[base] = waiter



        if not leader:

            await asyncio.shield(waiter)

            return



        try:

            rates = await self._provider.fetch(base)

            observed = datetime.now(tz=UTC)

            for quote, value in rates.items():

                if quote is base:

                    continue

                rate = ExchangeRate(

                    base=base,

                    quote=quote,

                    rate=value,

                    fetched_at=observed,

                    source=self._provider.name,

                )

                await self._cache.put(rate)

                # Cache the reciprocal so a B→A lookup is a hit rather than a

                # second provider round trip.

                await self._cache.put(rate.inverted())

            self._breaker.record_success()

            _LOG.debug("refreshed %d rate(s) for base %s", len(rates), base.value)

        except Exception as error:

            self._breaker.record_failure()

            _LOG.warning("FX refresh for %s failed: %s", base.value, error)

        finally:

            async with self._inflight_lock:

                self._inflight.pop(base, None)

            if not waiter.done():

                waiter.set_result(None)



    async def convert(

        self,

        amount: Any,

        target: CurrencyCode | str,

        *,

        allow_stale: bool = True,

    ) -> Any:

        """Convert a :class:`Money` into ``target``.



        Returns the input unchanged when it is already in ``target``, which

        keeps the common single-currency path free of any FX lookup.

        """

        from betdoc.domain.accounting.types import Money



        if not isinstance(amount, Money):

            raise TypeError(

                f"convert expects Money, got {type(amount).__name__}; "

                f"a bare number has no currency and cannot be converted"

            )



        target_code = CurrencyCode(target)

        if amount.currency is target_code:

            return amount



        rate = await self.get_rate(

            amount.currency, target_code, allow_stale=allow_stale

        )

        with money_context():

            return Money(value=amount.value * rate.rate, currency=target_code)



    async def to_base(self, amount: Any, *, allow_stale: bool = True) -> Any:

        """Convert a :class:`Money` into the configured base currency."""

        return await self.convert(amount, self._base, allow_stale=allow_stale)



    async def close(self) -> None:

        """Release the provider and cache."""

        await self._provider.close()

        await self._cache.close()
