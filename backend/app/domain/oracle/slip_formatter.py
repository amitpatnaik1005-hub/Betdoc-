"""Ashoka's slips as each bookmaker shows them, side by side, and as text to paste (Group 69).

* A view per bookmaker: every leg as that book names it (``"Arsenal vs Chelsea"``, ``"1X2: Arsenal"``,
  ``"Totals: Over 2.5"``, its own search code), that book's price, and the slip's odds there (the
  product of the leg prices; an exchange takes no multiples, and a book missing a leg cannot take it).
* The comparison: the best book for the slip, and Parimatch against 1xBet in exact money. If a treble
  pays 3.85 on Parimatch and 4.15 on 1xBet, a ₹1,000 stake returns ₹300 more on 1xBet (+7.79%), and
  the slip says so: "⭐ Recommend placing on 1xBet for +7.79% higher payout (+₹300 on ₹1,000 stake)".
* The quick copy: the slip as plain text for WhatsApp or Telegram, or to type into a bookmaker's search.

Money is Decimal throughout: a payout difference is computed, never rounded into existence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, EXCHANGE_BOOKMAKERS, fixture_name, slip_line

PAISA = Decimal("0.01")
ODDS_Q = Decimal("0.001")
PRIORITY_PAIR = ("parimatch", "1xbet")


def _money(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def inr(value: Decimal | float) -> str:
    """₹1,234.50 (whole rupees print without paise: ₹300)."""
    amount = _money(Decimal(str(value)))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    body = f"{amount:,.0f}" if amount == amount.to_integral_value() else f"{amount:,.2f}"
    return f"{sign}₹{body}"


@dataclass(frozen=True, slots=True)
class LegPrice:
    """One leg as the slip needs it: the canonical market and selection, and each book's price."""

    fixture_id: str
    home: str
    away: str
    market: str  # canonical market type ("Match Odds", "Totals 2.5")
    selection: str
    prices: Mapping[str, float]  # canonical bookmaker -> decimal odds
    fair_probability: float | None = None
    league: str | None = None
    kickoff: str | None = None
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class BookView:
    bookmaker: str
    label: str
    available: bool  # every leg is quoted and the book takes this kind of slip
    odds: Decimal | None  # the slip's odds here
    payout: Decimal | None  # what the stake returns if it wins
    legs: tuple[dict[str, object], ...]
    missing: tuple[str, ...] = ()  # legs this book does not quote
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Comparison:
    best: str | None
    best_odds: Decimal | None
    stake: Decimal
    pair: Mapping[str, Decimal | None]  # Parimatch / 1xBet slip odds
    difference_inr: Decimal | None  # the better of the pair minus the other, at this stake
    difference_pct: Decimal | None  # better / worse - 1, in percent
    recommended: str | None  # the better of the pair
    recommendation: str | None


def book_view(book: str, legs: Sequence[LegPrice], stake: Decimal) -> BookView:
    label = ASHOKA_BOOKMAKERS.get(book, book)
    lines: list[dict[str, object]] = []
    missing: list[str] = []
    for leg in legs:
        line = slip_line(book, leg.market, leg.selection, leg.home, leg.away)
        price = leg.prices.get(book)
        if price is None:
            missing.append(line["fixture"])
        lines.append({**line, "odds": None if price is None else str(Decimal(str(price)).quantize(ODDS_Q)), "fair_probability": leg.fair_probability})
    if len(legs) > 1 and book in EXCHANGE_BOOKMAKERS:
        return BookView(book, label, False, None, None, tuple(lines), tuple(missing), note="Exchange: back each leg as a single; no multiples")
    if missing:
        return BookView(book, label, False, None, None, tuple(lines), tuple(missing), note="Not quoted by an authorised feed for every leg: check the price on the site")
    odds = math.prod((Decimal(str(leg.prices[book])) for leg in legs), start=Decimal(1)).quantize(ODDS_Q, rounding=ROUND_DOWN)  # exact: no float product
    return BookView(book, label, True, odds, _money(stake * odds), tuple(lines))


def compare(views: Sequence[BookView], stake: Decimal) -> Comparison:
    """The best book overall, and Parimatch against 1xBet in rupees and percent at this stake."""
    priced = [v for v in views if v.available and v.odds is not None]
    best = max(priced, key=lambda v: v.odds, default=None)  # type: ignore[arg-type, return-value]
    by_book = {v.bookmaker: v for v in views}
    pair = {b: (by_book[b].odds if b in by_book and by_book[b].available else None) for b in PRIORITY_PAIR}
    diff_inr = diff_pct = None
    recommended = recommendation = None
    a, b = pair[PRIORITY_PAIR[0]], pair[PRIORITY_PAIR[1]]
    if a is not None and b is not None:
        if a == b:
            recommended = PRIORITY_PAIR[0]
            recommendation = f"Parimatch and 1xBet pay the same ({a}): place on either"
            diff_inr, diff_pct = Decimal("0.00"), Decimal("0.00")
        else:
            hi, lo = (PRIORITY_PAIR[0], PRIORITY_PAIR[1]) if a > b else (PRIORITY_PAIR[1], PRIORITY_PAIR[0])
            hi_odds, lo_odds = max(a, b), min(a, b)
            diff_inr = _money(stake * (hi_odds - lo_odds))
            diff_pct = ((hi_odds / lo_odds - 1) * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
            recommended = hi
            recommendation = (
                f"⭐ Recommend placing on {ASHOKA_BOOKMAKERS[hi]} for +{diff_pct}% higher payout "
                f"(+{inr(diff_inr)} on {inr(stake)} stake) than {ASHOKA_BOOKMAKERS[lo]}"
            )
    elif a is not None or b is not None:
        recommended = PRIORITY_PAIR[0] if a is not None else PRIORITY_PAIR[1]
        other = PRIORITY_PAIR[1] if a is not None else PRIORITY_PAIR[0]
        recommendation = f"Only {ASHOKA_BOOKMAKERS[recommended]} is quoted for every leg; check {ASHOKA_BOOKMAKERS[other]} on its site before comparing"
    if best is not None and recommended is not None and best.bookmaker not in PRIORITY_PAIR and pair.get(recommended) is not None and best.odds > pair[recommended]:  # type: ignore[operator]
        recommendation = (recommendation or "") + f". Best anywhere: {best.label} at {best.odds}"
    return Comparison(best.bookmaker if best else None, best.odds if best else None, stake, pair, diff_inr, diff_pct, recommended, recommendation)


def quick_copy(title: str, legs: Sequence[LegPrice], book: str, odds: Decimal | None, stake: Decimal, extra: Sequence[str] = ()) -> str:
    """The slip as plain text: one line per leg as the chosen book names it, then stake and return."""
    label = ASHOKA_BOOKMAKERS.get(book, book)
    lines = [f"🎯 {title} · {label}" + (f" @ {odds}" if odds is not None else "")]
    for i, leg in enumerate(legs, 1):
        line = slip_line(book, leg.market, leg.selection, leg.home, leg.away)
        price = leg.prices.get(book)
        lines.append(f"{i}) {line['fixture']} — {line['market']}" + (f" @ {Decimal(str(price)).quantize(ODDS_Q)}" if price else "") + f"  [{line['search_code']}]")
    if odds is not None:
        lines.append(f"Stake {inr(stake)} → returns {inr(stake * odds)}")
    lines += list(extra)
    return "\n".join(lines)


def search_text(legs: Sequence[LegPrice]) -> str:
    """Just the fixtures, comma separated: what to type into a bookmaker's search bar."""
    return ", ".join(fixture_name(leg.home, leg.away) for leg in legs)
