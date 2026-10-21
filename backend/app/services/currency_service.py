"""
Multi-Currency & FX Exchange Rate Service (Commander TODAR MAL, Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Every amount is kept in canonical INR and converted for display through INR: a pair's rate is
``INR per unit of base / INR per unit of quote``. INR per unit of each supported currency (USD, EUR, GBP, USDT,
BTC) is read, freshest source first:

1. the execution FX hash (``FX_RATES_KEY``, Group 70's ``PUT /omni/fx-rates``): an administrator's rate wins;
2. TODAR MAL's Redis cache (``CURRENCY_CACHE_KEY``, ``fx:rates:latest``);
3. the ``currency_exchange_rates`` table;
4. a refresh: ECB reference rates (Frankfurter) for the fiat currencies, CoinGecko for USDT and BTC.

A rate older than ``CURRENCY_MAX_AGE_SECONDS`` is never used, and nothing falls back to a guessed constant: an
amount that cannot be converted is refused (``CurrencyUnavailableError``) rather than shown at a made-up rate.
TODAR MAL does not write the execution hash; legs priced for real money keep their own administrator-set rates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.paper_trading.tax import TDS_RATE, section_115bbj
from app.models.tax_and_fx import CurrencyExchangeRate
from app.services.fx_rates import FxRates

logger = logging.getLogger("betdoc.currency_service")

HOME = "INR"
COINGECKO_IDS: dict[str, str] = {"USDT": "tether", "BTC": "bitcoin"}
SYMBOLS: dict[str, str] = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "USDT": "₮", "BTC": "₿"}
DISPLAY_PLACES: dict[str, int] = {"BTC": 8, "USDT": 2}


class CurrencyUnavailableError(RuntimeError):
    def __init__(self, currency: str, reason: str) -> None:
        super().__init__(f"No usable {currency} rate ({reason})")
        self.currency = currency
        self.reason = reason


@dataclass(frozen=True, slots=True)
class InrRate:
    currency: str
    inr_per_unit: Decimal
    as_of: datetime
    provider: str


@dataclass(frozen=True, slots=True)
class RateQuote:
    base: str
    quote: str
    rate: Decimal  # quote units per base unit
    as_of: datetime
    provider: str

    def as_dict(self) -> dict[str, Any]:
        return {"base_currency": self.base, "quote_currency": self.quote, "rate": float(self.rate), "last_updated_at": self.as_of.isoformat(),
                "provider": self.provider}


def _decimal(value: object) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def round_display(amount: Decimal, currency: str) -> Decimal:
    places = DISPLAY_PLACES.get(currency, 2)
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class CurrencyService:
    """
    Manages live FX conversions, Redis caching, and Section 115BBJ Indian TDS tax estimations.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    def __init__(self, settings: Settings | None = None, redis: Redis | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = settings or get_settings()
        self.settings = settings
        self.redis = redis
        self.transport = transport
        self.supported = tuple(c.upper() for c in settings.CURRENCY_SUPPORTED)

    def _check(self, currency: str) -> str:
        code = currency.strip().upper()
        if code not in self.supported:
            raise ValueError(f"{currency!r} is not a supported currency ({', '.join(self.supported)})")
        return code

    def _fresh(self, at: datetime, now: datetime) -> bool:
        return (now - _aware(at)).total_seconds() <= self.settings.CURRENCY_MAX_AGE_SECONDS

    # ------------------------------------------------------------------ reading
    async def _from_execution_hash(self, currency: str, now: datetime) -> InrRate | None:
        if self.redis is None:
            return None
        rates = await FxRates(self.redis, self.settings).snapshot()
        rate = rates.get(currency)
        if rate is None:
            return None
        as_of = datetime.fromtimestamp(rate.as_of, UTC)
        if (now - as_of).total_seconds() > self.settings.FX_MAX_AGE_SECONDS:
            return None
        return InrRate(currency, rate.inr_per_unit, as_of, f"execution:{rate.source or 'admin'}")

    async def _from_cache(self, currency: str, now: datetime) -> InrRate | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.hget(self.settings.CURRENCY_CACHE_KEY, f"{currency}_{HOME}")
        except (RedisError, OSError):
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            rate = _decimal(data["rate"])
            as_of = datetime.fromisoformat(data["as_of"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if rate is None or not self._fresh(as_of, now):
            return None
        return InrRate(currency, rate, _aware(as_of), str(data.get("provider", "cache")))

    async def _from_table(self, db: AsyncSession, currency: str, now: datetime) -> InrRate | None:
        row = (await db.execute(select(CurrencyExchangeRate).where(CurrencyExchangeRate.base_currency == currency,
                                                                   CurrencyExchangeRate.quote_currency == HOME))).scalar_one_or_none()
        if row is None or not self._fresh(row.last_updated_at, now):
            return None
        return InrRate(currency, Decimal(row.rate), _aware(row.last_updated_at), row.provider)

    async def inr_per_unit(self, db: AsyncSession, currency: str, now: datetime, *, allow_refresh: bool = True) -> InrRate:
        code = self._check(currency)
        if code == HOME:
            return InrRate(HOME, Decimal(1), now, "identity")
        found = await self._from_execution_hash(code, now) or await self._from_cache(code, now) or await self._from_table(db, code, now)
        if found is not None:
            return found
        if allow_refresh:
            refreshed = await self.refresh(db, now)
            if code in refreshed:
                return refreshed[code]
        raise CurrencyUnavailableError(code, "no fresh rate from the execution hash, the cache, the table or the providers")

    async def get_exchange_rate(self, db: AsyncSession, base: str, quote: str, now: datetime | None = None) -> RateQuote:
        """Quote units per one base unit, through INR."""
        moment = now or datetime.now(UTC)
        b, q = self._check(base), self._check(quote)
        if b == q:
            return RateQuote(b, q, Decimal(1), moment, "identity")
        rb = await self.inr_per_unit(db, b, moment)
        rq = await self.inr_per_unit(db, q, moment)
        as_of = min(rb.as_of, rq.as_of)
        provider = rb.provider if q == HOME else rq.provider if b == HOME else f"{rb.provider}/{rq.provider}"
        return RateQuote(b, q, rb.inr_per_unit / rq.inr_per_unit, as_of, provider)

    async def convert(self, db: AsyncSession, amount: Decimal, from_curr: str, to_curr: str, now: datetime | None = None) -> tuple[Decimal, RateQuote]:
        quote = await self.get_exchange_rate(db, from_curr, to_curr, now)
        return round_display(Decimal(amount) * quote.rate, quote.quote), quote

    async def board(self, db: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
        """INR per unit of every supported currency (a missing one is reported, not guessed)."""
        moment = now or datetime.now(UTC)
        rates: dict[str, Any] = {}
        missing: dict[str, str] = {}
        refreshed = False
        for code in self.supported:
            try:
                r = await self.inr_per_unit(db, code, moment, allow_refresh=not refreshed)
                rates[code] = {"inr_per_unit": float(r.inr_per_unit), "as_of": r.as_of.isoformat(), "provider": r.provider, "symbol": SYMBOLS.get(code, code)}
            except CurrencyUnavailableError as exc:
                refreshed = True
                missing[code] = exc.reason
        return {"base": HOME, "rates": rates, "missing": missing, "max_age_seconds": self.settings.CURRENCY_MAX_AGE_SECONDS}

    # ------------------------------------------------------------------ refreshing
    async def _fetch(self) -> dict[str, tuple[Decimal, str]]:
        fiat = [c for c in self.supported if c not in COINGECKO_IDS and c != HOME]
        crypto = [c for c in self.supported if c in COINGECKO_IDS]
        found: dict[str, tuple[Decimal, str]] = {}
        async with httpx.AsyncClient(timeout=self.settings.CURRENCY_HTTP_TIMEOUT_SECONDS, transport=self.transport,
                                     headers={"User-Agent": "BetDoc-TodarMal/1.0"}) as client:
            if fiat:
                try:
                    resp = await client.get(f"{self.settings.CURRENCY_FIAT_URL.rstrip('/')}/latest", params={"from": HOME, "to": ",".join(fiat)})
                    resp.raise_for_status()
                    per_inr: Mapping[str, Any] = resp.json().get("rates", {})
                    for code in fiat:
                        units = _decimal(per_inr.get(code))
                        if units is not None:
                            found[code] = (Decimal(1) / units, "frankfurter:ecb")
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    logger.warning("TODAR MAL: fiat rates unavailable (%s)", type(exc).__name__)
            if crypto:
                try:
                    resp = await client.get(f"{self.settings.CURRENCY_CRYPTO_URL.rstrip('/')}/simple/price",
                                            params={"ids": ",".join(COINGECKO_IDS[c] for c in crypto), "vs_currencies": "inr"})
                    resp.raise_for_status()
                    body: Mapping[str, Any] = resp.json()
                    for code in crypto:
                        inr = _decimal((body.get(COINGECKO_IDS[code]) or {}).get("inr"))
                        if inr is not None:
                            found[code] = (inr, "coingecko")
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    logger.warning("TODAR MAL: crypto rates unavailable (%s)", type(exc).__name__)
        return found

    async def store(self, db: AsyncSession, rates: Mapping[str, tuple[Decimal, str]], now: datetime) -> dict[str, InrRate]:
        out: dict[str, InrRate] = {}
        dialect = db.get_bind().dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        cache: dict[str, str] = {}
        for code, (inr_per_unit, provider) in rates.items():
            for base, quote, rate in ((code, HOME, inr_per_unit), (HOME, code, Decimal(1) / inr_per_unit)):
                stmt = insert(CurrencyExchangeRate).values(base_currency=base, quote_currency=quote, rate=rate, provider=provider[:64], last_updated_at=now)
                await db.execute(stmt.on_conflict_do_update(index_elements=["base_currency", "quote_currency"],
                                                            set_={"rate": rate, "provider": provider[:64], "last_updated_at": now}))
                cache[f"{base}_{quote}"] = json.dumps({"rate": str(rate), "as_of": now.isoformat(), "provider": provider[:64]})
            out[code] = InrRate(code, inr_per_unit, now, provider)
        await db.commit()
        if self.redis is not None and cache:
            try:
                await self.redis.hset(self.settings.CURRENCY_CACHE_KEY, mapping=cache)
            except (RedisError, OSError):
                logger.warning("TODAR MAL: the rate cache is unwritable; the table still holds the rates")
        return out

    async def refresh(self, db: AsyncSession, now: datetime | None = None) -> dict[str, InrRate]:
        """Fetch every supported rate the providers answer for and store it (table and cache)."""
        moment = now or datetime.now(UTC)
        fetched = await self._fetch()
        if not fetched:
            return {}
        return await self.store(db, fetched, moment)

    # ------------------------------------------------------------------ tax
    def calculate_indian_tds(self, gross_win: float, stake: float) -> dict[str, float]:
        """Section 115BBJ: 30% TDS on the net winnings of one bet (no set-off of losses on other bets)."""
        rate = Decimal(str(self.settings.ORACLE_TDS_RATE)) if self.settings.ORACLE_TDS_RATE is not None else TDS_RATE
        b = section_115bbj(Decimal(str(gross_win)), Decimal(str(stake)), rate)
        return {"gross_win": float(b.gross_payout), "stake": float(b.stake), "net_profit": float(b.net_winnings), "tds_deducted": float(b.tds),
                "net_take_home": float(b.net_post_tax_payout), "cess": float(b.cess), "tds_rate_pct": float(rate * 100),
                "effective_rate_pct": round(b.effective_rate_pct, 4)}
