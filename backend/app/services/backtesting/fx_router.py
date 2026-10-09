"""Historical FX for the backtester: what a foreign stake cost, and a foreign payout returned, in INR
at a past instant.

Live pricing fails closed without a current rate (``app.services.fx_rates``): a bet that cannot be
priced is not placed. A backtest must not quietly drop those bets instead (the survivors would be a
biased sample), so the router falls back in two steps:

1. the latest published historical fixing (``lab_hist_fx_rates``) at or before the instant, if it
   is no older than ``max_age``;
2. else the static configuration map (``LAB_STATIC_FX_RATES``): reference rates for simulation only.

A currency in neither raises ``FxUnavailableError``. Every resolution is counted by source, and the
backtest reports how many conversions leaned on the fallback. Reads are point-in-time: asking for an
instant after the simulation clock raises ``DataLeakageError``.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.math.arbitrage_calc import HOME_CURRENCY, FxQuote
from app.services.backtesting.time_lock import SimulationClock
from app.services.fx_rates import FxUnavailableError


@dataclass(frozen=True, slots=True)
class FxResolution:
    quote: FxQuote
    source: str  # home | fixing | static
    fixed_at: datetime | None = None

    @property
    def is_fallback(self) -> bool:
        return self.source == "static"


class HistoricalFxRouter:
    def __init__(
        self,
        fixings: Mapping[str, Sequence[tuple[datetime, Decimal]]],
        static: Mapping[str, Decimal | str],
        clock: SimulationClock,
        *,
        max_age: timedelta,
        haircut: Decimal = Decimal(0),
    ) -> None:
        self._times = {ccy.upper(): [at for at, _ in rows] for ccy, rows in fixings.items()}
        self._rates = {ccy.upper(): [rate for _, rate in rows] for ccy, rows in fixings.items()}
        for ccy, times in self._times.items():
            if any(b < a for a, b in zip(times, times[1:])):
                raise ValueError(f"{ccy} fixings must be in time order")
        self._static = {ccy.upper(): Decimal(str(rate)) for ccy, rate in static.items()}
        self.clock = clock
        self.max_age = max_age
        self.haircut = haircut
        self.used: Counter[tuple[str, str]] = Counter()

    def resolve(self, currency: str, at: datetime | None = None, *, record: bool = True) -> FxResolution:
        ccy = currency.upper()
        moment = self.clock.now if at is None else at
        self.clock.check(moment, f"the {ccy}/INR rate")
        if ccy == HOME_CURRENCY:
            return FxResolution(FxQuote(HOME_CURRENCY, Decimal(1)), "home")
        times = self._times.get(ccy, [])
        i = bisect.bisect_right(times, moment) - 1
        if i >= 0 and moment - times[i] <= self.max_age:
            self.clock.check_row(times[i], f"the {ccy} fixing")
            resolution = FxResolution(FxQuote(ccy, self._rates[ccy][i], self.haircut), "fixing", times[i])
        elif ccy in self._static:
            resolution = FxResolution(FxQuote(ccy, self._static[ccy], self.haircut), "static")
        else:
            reason = "no historical fixing within the window and no static rate configured"
            raise FxUnavailableError(ccy, reason)
        if record:
            self.used[(ccy, resolution.source)] += 1
        return resolution

    def report(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (ccy, source), n in sorted(self.used.items()):
            out.setdefault(ccy, {})[source] = n
        return out

    @property
    def fallbacks(self) -> int:
        return sum(n for (_, source), n in self.used.items() if source == "static")
