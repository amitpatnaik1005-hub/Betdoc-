"""Pre-dispatch slippage guard and venue commission adjustment (Group 71).

Between reserving the stake and firing the slices, the market can move. Before anything leaves, every
slice's price is read again from the Garuda quote stream (the books Aryabhata keeps in Redis) and the
whole order is aborted, with every reservation released, when any slice's venue:

* has no live price for the selection, a suspended market, or a price older than
  ``ROUTER_MAX_QUOTE_AGE_SECONDS``;
* now quotes below the order's ``min_acceptable_odds``, or has slipped more than ``max_slippage_pct``
  below the price the order asked for;
* no longer clears the order net of its own commission (Parimatch 0%, Betfair 5% of net winnings):
  with the order's true probability, the slice's net EV must reach ``ROUTER_MIN_SLICE_EV``; without
  one, the commission-adjusted price must still reach the order's floor.

The same checks run at planning time, where a failing venue is simply left out of the plan.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.domain.bookmakers.adapters import canonical_bookmaker
from app.services.venue_costs import BookmakerTerms, TermsTable

if TYPE_CHECKING:
    from app.services.execution.smart_router import ExecutionOrder

ONE = Decimal(1)
HUNDRED = Decimal(100)


def book_key(raw: str) -> str:
    """A feed's or a caller's bookmaker id as the Vault keys it: ``"onexbet"`` -> ``"1xbet"``."""
    return canonical_bookmaker(raw) or raw.strip().casefold()


@dataclass(frozen=True, slots=True)
class LiveQuote:
    bookmaker_id: str
    odds: Decimal
    seen_at: datetime
    suspended: bool = False
    source: str = ""


class QuoteSource(Protocol):
    async def quotes(self, match_id: str, market: str, selection: str, bookmakers: Sequence[str]) -> dict[str, LiveQuote]: ...


class GarudaQuoteSource:
    """The live books in Redis (``read_market_books``): per bookmaker, its freshest price for the selection."""

    def __init__(self, redis: Redis | None, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def quotes(self, match_id: str, market: str, selection: str, bookmakers: Sequence[str]) -> dict[str, LiveQuote]:
        if self.redis is None:
            return {}
        from app.services.aryabhata_pipeline import read_market_books  # noqa: PLC0415 - heavy module, loaded on first use

        key = f"{match_id}|{market}"
        try:
            books = await read_market_books(self.redis, self.settings, [key])
        except (RedisError, OSError, TimeoutError):
            return {}
        wanted = {book_key(b) for b in bookmakers}
        target = selection.casefold()
        out: dict[str, LiveQuote] = {}
        for line in books.get(key, ()):
            book = book_key(line.bookmaker_id)
            if book not in wanted:
                continue
            price = next((p for label, p in line.prices.items() if label.casefold() == target), None)
            if price is None:
                continue
            current = out.get(book)
            if current is None or line.seen_at > current.seen_at:
                out[book] = LiveQuote(book, Decimal(price), line.seen_at, bool(line.is_suspended), line.source)
        return out


def net_odds(odds: Decimal, commission: Decimal) -> Decimal:
    """Decimal odds after the venue takes its cut of net winnings."""
    return ONE + (odds - ONE) * (ONE - commission)


def net_ev(odds: Decimal, commission: Decimal, true_prob: Decimal) -> Decimal:
    return true_prob * net_odds(odds, commission) - ONE


@dataclass(frozen=True, slots=True)
class SliceCheck:
    venue_id: str
    ok: bool
    reason: str | None  # QUOTE_UNAVAILABLE | MARKET_SUSPENDED | QUOTE_STALE | BELOW_MIN_ODDS | SLIPPAGE_EXCEEDED | NET_BELOW_FLOOR | NEGATIVE_EV
    live_odds: Decimal | None = None
    commission: Decimal = Decimal(0)
    net_odds: Decimal | None = None
    net_ev: Decimal | None = None
    slippage_pct: Decimal | None = None
    age_seconds: float | None = None

    def as_dict(self) -> dict[str, object]:
        s = lambda v: None if v is None else str(v)  # noqa: E731
        return {"venue_id": self.venue_id, "ok": self.ok, "reason": self.reason, "live_odds": s(self.live_odds), "commission": str(self.commission),
                "net_odds": s(self.net_odds), "net_ev": s(self.net_ev), "slippage_pct": s(self.slippage_pct), "age_seconds": self.age_seconds}


def check_slice(order: ExecutionOrder, venue_id: str, quote: LiveQuote | None, terms: BookmakerTerms, *, now: datetime, settings: Settings) -> SliceCheck:
    """One venue's verdict for this order at this moment."""
    commission = terms.commission
    if quote is None:
        return SliceCheck(venue_id, False, "QUOTE_UNAVAILABLE", commission=commission)
    age = (now - quote.seen_at).total_seconds()
    live = quote.odds
    slippage = ((order.odds - live) / order.odds * HUNDRED).quantize(Decimal("0.001"))
    net = net_odds(live, commission)
    ev = None if order.true_prob is None else net_ev(live, commission, order.true_prob).quantize(Decimal("0.000001"))
    verdict = dict(live_odds=live, commission=commission, net_odds=net.quantize(Decimal("0.0001")), net_ev=ev, slippage_pct=slippage, age_seconds=round(age, 3))
    if quote.suspended:
        return SliceCheck(venue_id, False, "MARKET_SUSPENDED", **verdict)
    if age > float(settings.ROUTER_MAX_QUOTE_AGE_SECONDS):
        return SliceCheck(venue_id, False, "QUOTE_STALE", **verdict)
    if live < order.min_acceptable_odds:
        return SliceCheck(venue_id, False, "BELOW_MIN_ODDS", **verdict)
    if slippage > order.max_slippage_pct:
        return SliceCheck(venue_id, False, "SLIPPAGE_EXCEEDED", **verdict)
    if ev is not None:
        if ev < Decimal(settings.ROUTER_MIN_SLICE_EV):
            return SliceCheck(venue_id, False, "NEGATIVE_EV", **verdict)
    elif net < order.min_acceptable_odds:
        return SliceCheck(venue_id, False, "NET_BELOW_FLOOR", **verdict)
    return SliceCheck(venue_id, True, None, **verdict)


@dataclass(frozen=True, slots=True)
class GuardReport:
    ok: bool
    checks: tuple[SliceCheck, ...]

    @property
    def failures(self) -> tuple[SliceCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks]}


class SlippageGuard:
    """Re-reads the quote stream and judges every venue an order is about to use."""

    def __init__(self, quotes: QuoteSource, settings: Settings, terms: Callable[[str], BookmakerTerms] | None = None) -> None:
        self.quotes = quotes
        self.settings = settings
        self.terms = terms or TermsTable(settings)

    async def read(self, order: ExecutionOrder, venues: Sequence[str]) -> dict[str, LiveQuote]:
        return await self.quotes.quotes(order.match_id, order.market, order.selection, venues)

    def judge(self, order: ExecutionOrder, venues: Sequence[str], quotes: dict[str, LiveQuote], *, now: datetime) -> GuardReport:
        checks = tuple(check_slice(order, v, quotes.get(v), self.terms(v), now=now, settings=self.settings) for v in sorted(set(venues)))
        return GuardReport(all(c.ok for c in checks), checks)

    async def verify(self, order: ExecutionOrder, venues: Sequence[str], *, now: datetime) -> GuardReport:
        """The pre-dispatch check: a fresh read, every venue judged; one failure fails the order."""
        return self.judge(order, venues, await self.read(order, venues), now=now)
