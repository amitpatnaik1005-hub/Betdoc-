"""Live portfolio (Group 64): every open position marked against the live books, with its hedges,
and the arbitrage scanner.

Per market (fixture + market type) the user holds a *book*: ``P_k``, what it makes if outcome ``k``
wins, from every confirmed open bet on it. Each book is priced against the best live back price for
every outcome across the fresh bookmaker books Aryabhata keeps in Redis, after each bookmaker's
commission and, for a foreign-currency account, the live FX rate and haircut
(``app.domain.math.arbitrage_calc``). It gets:

* the balanced hedge (equal profit on every outcome: the cash-out value of the book right now),
* the free-bet hedge (exactly nothing lost if the anchor outcome fails, all profit on it),
* ``profitable``: the balanced hedge locks in a profit (the Active Portfolio row pulses green).

A book holding a bet whose placement is unconfirmed (or dead-lettered) is shown but not hedged: a
hedge against a bet that may not exist would be a naked bet of its own.

The scanner looks at every pre-match market on the live board for ``sum(1 / True_Odds) < 1`` over
the best executable price per outcome, every price commission- and FX-adjusted.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.core.live_odds import read_snapshot
from app.domain.math.arbitrage_calc import (
    HOME_CURRENCY,
    ODDS_QUANTUM,
    ONE,
    PAISA,
    ZERO,
    Arbitrage,
    ArbitrageMathError,
    FxQuote,
    HedgePlan,
    HeldBet,
    Offer,
    best_offers,
    book_profits,
    commission_adjusted_odds,
    find_arbitrage,
    hedge_book,
)
from app.services.aryabhata_engine import BookLine, ordered_labels
from app.services.aryabhata_pipeline import read_market_books
from app.services.fx_rates import FxRate, FxRates, FxUnavailableError
from app.services.portfolio_positions import OpenBet, cached_open_bets, load_open_bets
from app.services.venue_costs import BookmakerTerms, TermsTable

logger = logging.getLogger("betdoc.portfolio")

_HUNDRED = Decimal(100)
VenueSource = Callable[[], Awaitable[Sequence[VenueConfig]]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------- one tick's pricing inputs
@dataclass(slots=True)
class PricingContext:
    """Read once per tick: commission and currency per bookmaker, the live FX rates, the clock."""

    terms: Callable[[str], BookmakerTerms]
    fx: FxRates
    rates: dict[str, FxRate]
    now: datetime
    max_age: timedelta
    routable: Callable[[str], bool]
    fx_errors: set[str] = field(default_factory=set)

    def fx_for(self, currency: str) -> FxQuote | None:
        return self.fx.quote(currency, self.rates)

    def offer(self, bookmaker_id: str, selection: str, raw_odds: Decimal) -> Offer:
        terms = self.terms(bookmaker_id)
        return Offer(selection, bookmaker_id, raw_odds, terms.commission, self.fx_for(terms.currency))

    def held(self, bet: OpenBet) -> HeldBet:
        """A bet as rupees-per-rupee: its book's commission, and a foreign stake's payout brought home
        at today's rate less the haircut (not the rate it was struck at)."""
        true = commission_adjusted_odds(bet.odds, self.terms(bet.bookmaker_id).commission)
        if bet.currency == HOME_CURRENCY or bet.stake_ccy is None:
            return HeldBet(bet.selection, bet.stake_inr, true)
        fx = self.fx_for(bet.currency)
        assert fx is not None
        payout_inr = bet.stake_ccy * true * fx.inr_per_unit * (ONE - fx.haircut)
        return HeldBet(bet.selection, bet.stake_inr, payout_inr / bet.stake_inr)

    def fresh(self, book: BookLine) -> bool:
        return not book.is_suspended and self.now - book.seen_at <= self.max_age


def market_outcomes(books: Iterable[BookLine], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Every outcome any book has quoted for the market (stale ones too) plus any held: an outcome
    missing from this list would make a hedge or an arbitrage look complete when it is not."""
    labels = set(extra)
    for book in books:
        labels |= set(book.prices)
    return ordered_labels(frozenset(labels))


def market_offers(books: Iterable[BookLine], ctx: PricingContext) -> tuple[list[Offer], list[str]]:
    offers: list[Offer] = []
    notes: set[str] = set()
    for book in books:
        if not ctx.fresh(book):
            continue
        for label, price in book.prices.items():
            # The price an order can actually carry (4 places), rounded down: a prediction market's
            # 1/p has sixteen digits, and rounding it up could manufacture an arbitrage
            executable = price.quantize(ODDS_QUANTUM, rounding=ROUND_DOWN)
            if executable <= ONE:
                continue
            try:
                offers.append(ctx.offer(book.bookmaker_id, label, executable))
            except FxUnavailableError as exc:
                ctx.fx_errors.add(exc.currency)
                notes.add(f"{book.bookmaker_id} skipped: no live {exc.currency}/INR rate")
            except ArbitrageMathError:
                continue
    return offers, sorted(notes)


def offer_json(offer: Offer) -> dict[str, Any]:
    return {
        "selection": offer.selection,
        "bookmaker_id": offer.provider,
        "odds": str(offer.raw_odds),
        "true_odds": str(offer.true_odds().quantize(Decimal("0.0001"))),
        "rupee_odds": str(offer.rupee_odds().quantize(Decimal("0.0001"))),
        "commission": str(offer.commission),
        "currency": offer.currency,
    }


def plan_json(plan: HedgePlan) -> dict[str, Any]:
    return {
        "kind": plan.kind,
        "fraction": str(plan.fraction),
        "anchor": plan.anchor,
        "target": str(plan.target.quantize(PAISA)),
        "hedge_stake_inr": str(plan.hedge_stake_inr),
        "legs": [{**offer_json(leg.offer), "stake_inr": str(leg.stake_inr), "stake_ccy": str(leg.stake_ccy)} for leg in plan.legs],
        "after": {k: str(v) for k, v in plan.after.items()},
        "worst_after": str(plan.worst_after),
        "best_after": str(plan.best_after),
        "locks_profit": plan.locks_profit,
    }


def arbitrage_id(fixture_id: str, market: str, arb: Arbitrage) -> str:
    legs = "|".join(f"{leg.offer.selection}@{leg.offer.provider}:{leg.offer.raw_odds}" for leg in arb.legs)
    return hashlib.sha1(f"{fixture_id}|{market}|{legs}".encode()).hexdigest()[:16]


def arbitrage_json(meta: MarketMeta, arb: Arbitrage, now: datetime) -> dict[str, Any]:
    return {
        "id": arbitrage_id(meta.fixture_id, meta.market, arb),
        "fixture_id": meta.fixture_id,
        "market": meta.market,
        "home": meta.home,
        "away": meta.away,
        "commence_time": meta.commence_time.isoformat() if meta.commence_time else None,
        "booksum": str(arb.booksum.quantize(Decimal("0.000001"))),
        "margin_pct": str((arb.margin * _HUNDRED).quantize(Decimal("0.001"))),
        "roi_pct": str((arb.roi * _HUNDRED).quantize(Decimal("0.01"))),
        "total_stake_inr": str(arb.total_stake_inr),
        "guaranteed_profit_inr": str(arb.guaranteed_profit_inr),
        "legs": [{**offer_json(leg.offer), "stake_inr": str(leg.stake_inr), "stake_ccy": str(leg.stake_ccy), "payout_inr": str(leg.payout_inr)} for leg in arb.legs],
        "detected_at": now.isoformat(),
    }


# ---------------------------------------------------------------- what the board says about a market
@dataclass(slots=True)
class MarketMeta:
    fixture_id: str
    market: str
    home: str = ""
    away: str = ""
    commence_time: datetime | None = None
    labels: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.fixture_id}|{self.market}"


async def board_markets(redis: Redis | None) -> dict[str, MarketMeta]:
    markets: dict[str, MarketMeta] = {}
    for tick in await read_snapshot(redis) or []:
        meta = markets.setdefault(
            f"{tick.match_id}|{tick.market_type}",
            MarketMeta(tick.match_id, tick.market_type, tick.home_team, tick.away_team, tick.commence_time),
        )
        meta.labels.add(tick.selection)
    return markets


# ---------------------------------------------------------------- one book
def evaluate_book(market_key: str, bets: Sequence[OpenBet], books: Sequence[BookLine], ctx: PricingContext, meta: MarketMeta | None) -> dict[str, Any]:
    fixture_id, _, market = market_key.partition("|")
    outcomes = market_outcomes(books, [*(b.selection for b in bets), *(meta.labels if meta else ())])
    offers, notes = market_offers(books, ctx)
    best = best_offers(offers)
    confirmed = [b for b in bets if not b.unconfirmed]
    row: dict[str, Any] = {
        "market_key": market_key,
        "fixture_id": fixture_id,
        "market": market,
        "home": meta.home if meta else "",
        "away": meta.away if meta else "",
        "commence_time": meta.commence_time.isoformat() if meta and meta.commence_time else None,
        "in_play": bool(meta and meta.commence_time and meta.commence_time <= ctx.now),
        "outcomes": list(outcomes),
        "bets": [
            {
                "id": b.id, "selection": b.selection, "bookmaker_id": b.bookmaker_id, "stake_inr": str(b.stake_inr),
                "requested_stake_inr": _s(b.requested_stake_inr), "odds": str(b.odds), "status": b.status,
                "unconfirmed": b.unconfirmed, "strategy": b.strategy, "group_id": b.group_id, "currency": b.currency,
            }
            for b in bets
        ],
        "staked_inr": str(sum((b.stake_inr for b in bets), ZERO).quantize(PAISA)),
        "live": {sel: offer_json(offer) for sel, offer in best.items() if sel in outcomes},
        "profits": None,
        "worst_case": None,
        "best_case": None,
        "hedge": None,
        "cash_out": None,
        "profitable": False,
        "blocked": None,
        "notes": notes,
    }
    if row["in_play"]:
        notes.append("In play: polled prices may lag the market")
    if len(outcomes) < 2:
        row["blocked"] = "NO_MARKET"
        return row
    try:
        held = [ctx.held(b) for b in confirmed]
        profits = book_profits(held, outcomes)
    except FxUnavailableError as exc:
        row["blocked"] = f"NO_FX_{exc.currency}"
        return row
    except ArbitrageMathError as exc:
        row["blocked"] = "UNPRICEABLE"
        notes.append(str(exc))
        return row
    row["profits"] = {k: str(v.quantize(PAISA)) for k, v in profits.items()}
    row["worst_case"] = str(min(profits.values()).quantize(PAISA))
    row["best_case"] = str(max(profits.values()).quantize(PAISA))
    if len(confirmed) != len(bets):
        row["blocked"] = "UNCONFIRMED_BET"
        notes.append("A bet here is unconfirmed: resolve it before hedging")
        return row
    if not confirmed:
        row["blocked"] = "NOTHING_TO_HEDGE"
        return row
    try:
        balanced = hedge_book(profits, best, fraction=ONE)
        free_bet = hedge_book(profits, best, anchor=balanced.anchor, fraction=ZERO)
    except ArbitrageMathError as exc:
        row["blocked"] = "NO_HEDGE"
        notes.append(str(exc))
        return row
    row["hedge"] = {"anchor": balanced.anchor, "balanced": plan_json(balanced), "free_bet": plan_json(free_bet)}
    row["cash_out"] = str(balanced.worst_after)
    row["profitable"] = balanced.locks_profit
    return row


def portfolio_payload(
    user_id: uuid.UUID | str,
    bets: Sequence[OpenBet],
    books_by_market: Mapping[str, Sequence[BookLine]],
    ctx: PricingContext,
    metas: Mapping[str, MarketMeta],
    arbs: Sequence[dict[str, Any]] = (),
    seq: int = 0,
) -> dict[str, Any]:
    grouped: dict[str, list[OpenBet]] = defaultdict(list)
    for bet in bets:
        grouped[bet.market_key].append(bet)
    rows = [evaluate_book(key, group, books_by_market.get(key, ()), ctx, metas.get(key)) for key, group in grouped.items()]
    rows.sort(key=lambda r: (not r["profitable"], r["commence_time"] or "9999", r["market_key"]))

    def total(field_name: str) -> str | None:
        values = [Decimal(r[field_name]) for r in rows if r[field_name] is not None]
        return str(sum(values, ZERO).quantize(PAISA)) if values else None

    return {
        "type": "portfolio",
        "seq": seq,
        "user_id": str(user_id),
        "ts": ctx.now.isoformat(),
        "totals": {
            "open_bets": len(bets),
            "markets": len(rows),
            "staked_inr": str(sum((b.stake_inr for b in bets), ZERO).quantize(PAISA)),
            "cash_out": total("cash_out"),
            "worst_case": total("worst_case"),
            "best_case": total("best_case"),
            "profitable_hedges": sum(1 for r in rows if r["profitable"]),
        },
        "markets": rows,
        "arbs": list(arbs),
        "fx_missing": sorted(ctx.fx_errors),
    }


def scan_arbitrage(
    metas: Mapping[str, MarketMeta], books_by_market: Mapping[str, Sequence[BookLine]], ctx: PricingContext, settings: Settings
) -> list[dict[str, Any]]:
    """Pre-match markets only: a polled in-play price is already stale, and a stale leg is a fake arb."""
    reference = Decimal(str(settings.ARB_REFERENCE_STAKE_INR))
    min_margin = Decimal(str(settings.ARB_MIN_MARGIN_PCT)) / _HUNDRED
    found: list[tuple[Decimal, dict[str, Any]]] = []
    for key, meta in metas.items():
        if meta.commence_time is None or meta.commence_time <= ctx.now:
            continue
        books = books_by_market.get(key, ())
        offers, _ = market_offers(books, ctx)
        offers = [o for o in offers if ctx.routable(o.provider)]
        try:
            arb = find_arbitrage(offers, market_outcomes(books, meta.labels), reference)
        except ArbitrageMathError:
            continue
        if arb is not None and arb.margin >= min_margin:
            found.append((arb.margin, arbitrage_json(meta, arb, ctx.now)))
    found.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [item for _, item in found[: settings.ARB_MAX_RESULTS]]


# ---------------------------------------------------------------- the service
class PortfolioManager:
    """Loads what a tick needs (Redis only, except positions: see ``portfolio_positions``)."""

    def __init__(
        self,
        redis: Redis | None,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        venues: VenueSource | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings
        self.venues = venues
        self.clock = clock
        self.fx = FxRates(redis, settings, clock=lambda: self.clock().timestamp())

    async def context(self) -> PricingContext:
        venues: Sequence[VenueConfig] = await self.venues() if self.venues is not None else ()
        live = self.settings.CFO_EXECUTION_MODE == "live"

        def routable(bookmaker_id: str) -> bool:
            return not live or any(v.handles(bookmaker_id) for v in venues)  # paper fills every bookmaker

        return PricingContext(
            terms=TermsTable(self.settings, venues),
            fx=self.fx,
            rates=await self.fx.snapshot(),
            now=self.clock(),
            max_age=timedelta(seconds=self.settings.PORTFOLIO_PRICE_MAX_AGE_SECONDS),
            routable=routable,
        )

    async def books(self, market_keys: Sequence[str]) -> dict[str, tuple[BookLine, ...]]:
        if self.redis is None or not market_keys:
            return {}
        try:
            return await read_market_books(self.redis, self.settings, list(dict.fromkeys(market_keys)))
        except (RedisError, OSError, TimeoutError):
            logger.warning("Live books unreadable; portfolio priced without them")
            return {}

    async def snapshot(self, user_id: uuid.UUID, *, cached: bool = True, arbs: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
        if cached:
            bets = await cached_open_bets(self.redis, self.session_factory, self.settings, user_id)
        else:
            async with self.session_factory() as session:
                bets = await load_open_bets(session, user_id)
        ctx = await self.context()
        books = await self.books([b.market_key for b in bets])
        metas = await board_markets(self.redis)
        return portfolio_payload(user_id, bets, books, ctx, metas, arbs)

    async def market(self, fixture_id: str, market: str) -> tuple[PricingContext, tuple[BookLine, ...], MarketMeta | None]:
        key = f"{fixture_id}|{market}"
        ctx = await self.context()
        books = (await self.books([key])).get(key, ())
        meta = (await board_markets(self.redis)).get(key)
        return ctx, books, meta

    async def scan(self) -> list[dict[str, Any]]:
        metas = await board_markets(self.redis)
        ctx = await self.context()
        books = await self.books([k for k, m in metas.items() if m.commence_time is not None and m.commence_time > ctx.now])
        return scan_arbitrage(metas, books, ctx, self.settings)
