"""Ashoka's slip legs in the Group 79 settlement grammar (Group 80). Developed for Amit Ashok Kumar Patnaik.

The Oracle prices markets in the platform grammar (``Match Odds``, ``Totals 2.5``, ``Asian Handicap -0.75`` with
the line on the HOME side, ``BTTS``, ``Double Chance``, ``Draw No Bet``) and selections as codes (``HOME``,
``DRAW``, ``AWAY``, ``OVER``, ``YES``, ``1X``...). A placed bet settles through the sport adapters, so each leg is
translated once, at placement: the market key, the selection and the line *of the selected side* (an away
Asian handicap is the home line negated). The original market and selection stay on the leg too: they are what
ASHVATTHAMA snaps the live Parimatch price with.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.oracle.markets import MarketKind, parse_market
from app.domain.sports.registry import sport_for_odds_key


class LegTranslationError(ValueError):
    """The leg's market or selection has no Group 79 settlement."""


@dataclass(frozen=True, slots=True)
class TranslatedLeg:
    sport_key: str  # the adapter's key ("soccer")
    market_type: str  # the adapter's market
    selection: str
    line: float | None
    quote_market: str  # the platform market key the books are quoted under
    quote_selection: str


def translate(sport_key: str | None, market: str, selection: str) -> TranslatedLeg:
    sport = sport_for_odds_key(sport_key) if sport_key else "soccer"
    if sport is None:
        raise LegTranslationError(f"sport {sport_key!r} has no adapter")
    ref = parse_market(market)
    if ref is None:
        raise LegTranslationError(f"market {market!r} is not one Ashoka prices")
    code = selection.strip().upper()
    if code not in ref.selections:
        raise LegTranslationError(f"{code!r} is not a selection of {ref.key}")
    lower = code.lower()
    if ref.kind is MarketKind.MATCH_ODDS:
        return TranslatedLeg(sport, "moneyline", lower, None, ref.key, code)
    if ref.kind is MarketKind.TOTALS:
        return TranslatedLeg(sport, "total", lower, ref.line, ref.key, code)
    if ref.kind is MarketKind.BTTS:
        return TranslatedLeg(sport, "btts", lower, None, ref.key, code)
    if ref.kind is MarketKind.ASIAN_HANDICAP:
        line = ref.line if code == "HOME" else -float(ref.line)  # type: ignore[arg-type]
        return TranslatedLeg(sport, "asian_handicap", lower, line, ref.key, code)
    if ref.kind is MarketKind.DOUBLE_CHANCE:
        return TranslatedLeg(sport, "double_chance", code.lower(), None, ref.key, code)
    return TranslatedLeg(sport, "draw_no_bet", lower, None, ref.key, code)
