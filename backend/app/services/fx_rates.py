"""Live FX for multi-currency legs, from Redis, failing closed.

Hash ``FX_RATES_KEY``: currency -> ``{"inr_per_unit": "91.42", "as_of": <unix>, "source": "..."}``.
A rate older than ``FX_MAX_AGE_SECONDS`` (or missing, or not positive) is refused: a leg in that
currency then cannot be priced, hedged or fired. Nothing ever falls back to a guessed rate.

The rates are written by an administrator (``PUT /omni/fx-rates``) or by any sanctioned feed that
calls ``FxRates.publish``. INR needs no rate.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.domain.math.arbitrage_calc import HOME_CURRENCY, FxQuote

logger = logging.getLogger("betdoc.fx")

_HUNDRED = Decimal(100)


class FxUnavailableError(RuntimeError):
    def __init__(self, currency: str, reason: str) -> None:
        super().__init__(f"No usable {currency}/INR rate ({reason})")
        self.currency = currency
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FxRate:
    currency: str
    inr_per_unit: Decimal
    as_of: float
    source: str

    def as_dict(self, now: float) -> dict[str, Any]:
        return {"currency": self.currency, "inr_per_unit": str(self.inr_per_unit), "as_of": self.as_of, "age_seconds": round(now - self.as_of, 1), "source": self.source}


def _parse(currency: str, raw: str | None) -> FxRate | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        rate = Decimal(str(data["inr_per_unit"]))
        as_of = float(data["as_of"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not rate.is_finite() or rate <= 0:
        return None
    return FxRate(currency, rate, as_of, str(data.get("source", "")))


class FxRates:
    """Read once per tick (``snapshot``), then answer every leg from that snapshot."""

    def __init__(self, redis: Redis | None, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.redis = redis
        self.settings = settings
        self.clock = clock
        self.haircut = Decimal(str(settings.FX_HAIRCUT_PCT)) / _HUNDRED

    async def snapshot(self) -> dict[str, FxRate]:
        if self.redis is None:
            return {}
        try:
            raw: dict[str, str] = await self.redis.hgetall(self.settings.FX_RATES_KEY)
        except (RedisError, OSError):
            logger.warning("FX rates unreadable; foreign-currency legs are refused until Redis answers")
            return {}
        return {ccy: rate for ccy, value in raw.items() if (rate := _parse(ccy, value)) is not None}

    def quote(self, currency: str, rates: dict[str, FxRate]) -> FxQuote | None:
        """``None`` for INR. Raises ``FxUnavailableError`` when the currency can't be priced now."""
        currency = currency.upper()
        if currency == HOME_CURRENCY:
            return None
        rate = rates.get(currency)
        if rate is None:
            raise FxUnavailableError(currency, "missing")
        if self.clock() - rate.as_of > self.settings.FX_MAX_AGE_SECONDS:
            raise FxUnavailableError(currency, "stale")
        return FxQuote(currency, rate.inr_per_unit, self.haircut)

    async def publish(self, currency: str, inr_per_unit: Decimal, source: str) -> FxRate:
        currency = currency.upper()
        if currency == HOME_CURRENCY or len(currency) != 3 or not currency.isalpha():
            raise ValueError("a three-letter currency other than INR")
        if not inr_per_unit.is_finite() or inr_per_unit <= 0:
            raise ValueError("the rate must be positive")
        if self.redis is None:
            raise FxUnavailableError(currency, "redis unavailable")
        rate = FxRate(currency, inr_per_unit, self.clock(), source[:64])
        payload = json.dumps({"inr_per_unit": str(inr_per_unit), "as_of": rate.as_of, "source": rate.source}, separators=(",", ":"))
        await self.redis.hset(self.settings.FX_RATES_KEY, currency, payload)
        return rate
