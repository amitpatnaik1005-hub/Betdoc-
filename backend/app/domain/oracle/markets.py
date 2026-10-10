"""The markets Ashoka prices and settles: one grammar, one settlement rule (Group 69).

A market travels through BetDoc as a string (the Aryabhata frame's ``market_type``): ``"Match Odds"``,
``"Totals 2.5"``, ``"BTTS"``, ``"Asian Handicap -0.5"`` (the home side's line), ``"Double Chance"``,
``"Draw No Bet"``. ``parse_market`` reads every spelling the feeds use (``"Over/Under 2.5"`` too) into a
``MarketRef``; ``MarketRef.key`` writes the canonical one back.

``settle_selection`` is the only place a score becomes a leg result. The Monte Carlo engine and the
user's bet ledger both call it, so a simulated slip and a settled one can never disagree. Quarter
lines (2.25, -0.75) are two half-stakes on the neighbouring lines: a half win, a half loss.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum


class MarketKind(StrEnum):
    MATCH_ODDS = "MATCH_ODDS"
    TOTALS = "TOTALS"
    BTTS = "BTTS"
    ASIAN_HANDICAP = "ASIAN_HANDICAP"
    DOUBLE_CHANCE = "DOUBLE_CHANCE"
    DRAW_NO_BET = "DRAW_NO_BET"


class LegResult(StrEnum):
    WON = "WON"
    HALF_WON = "HALF_WON"
    VOID = "VOID"
    HALF_LOST = "HALF_LOST"
    LOST = "LOST"


SELECTIONS: dict[MarketKind, tuple[str, ...]] = {
    MarketKind.MATCH_ODDS: ("HOME", "DRAW", "AWAY"),
    MarketKind.TOTALS: ("OVER", "UNDER"),
    MarketKind.BTTS: ("YES", "NO"),
    MarketKind.ASIAN_HANDICAP: ("HOME", "AWAY"),
    MarketKind.DOUBLE_CHANCE: ("1X", "12", "X2"),
    MarketKind.DRAW_NO_BET: ("HOME", "AWAY"),
}
_TOTALS = re.compile(r"^(?:totals?|over/under|o/u)\s*([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)
_HANDICAP = re.compile(r"^(?:asian handicap|ah|spreads?)\s*([+-]?[0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)


def _quarter(line: float) -> float:
    if not math.isfinite(line) or abs(line * 4 - round(line * 4)) > 1e-9:
        raise ValueError(f"line {line!r} is not a multiple of 0.25")
    return round(line * 4) / 4


def _fmt(line: float) -> str:
    return f"{line:g}"


@dataclass(frozen=True, slots=True)
class MarketRef:
    kind: MarketKind
    line: float | None = None  # totals: the goal line; Asian handicap: the HOME side's handicap

    def __post_init__(self) -> None:
        needs_line = self.kind in (MarketKind.TOTALS, MarketKind.ASIAN_HANDICAP)
        if needs_line != (self.line is not None):
            raise ValueError(f"{self.kind} {'needs' if needs_line else 'takes no'} line")
        if self.line is not None:
            object.__setattr__(self, "line", _quarter(float(self.line)))
            if self.kind is MarketKind.TOTALS and self.line < 0:
                raise ValueError("a goal line cannot be negative")

    @property
    def key(self) -> str:
        """The canonical ``market_type`` string."""
        if self.kind is MarketKind.MATCH_ODDS:
            return "Match Odds"
        if self.kind is MarketKind.TOTALS:
            return f"Totals {_fmt(self.line)}"  # type: ignore[arg-type]
        if self.kind is MarketKind.BTTS:
            return "BTTS"
        if self.kind is MarketKind.ASIAN_HANDICAP:
            return f"Asian Handicap {self.line:+g}"
        return "Double Chance" if self.kind is MarketKind.DOUBLE_CHANCE else "Draw No Bet"

    @property
    def selections(self) -> tuple[str, ...]:
        return SELECTIONS[self.kind]

    @property
    def has_push(self) -> bool:
        """Can a stake come back (an integer or quarter line, draw no bet)?"""
        if self.kind is MarketKind.DRAW_NO_BET:
            return True
        if self.line is None:
            return False
        return abs((self.line * 2) % 2 - 1) > 1e-9  # an x.5 line never pushes; integer and quarter lines can


def parse_market(market_type: str) -> MarketRef | None:
    """Every spelling the feeds use, or None for a market Ashoka does not price."""
    text = " ".join(str(market_type).split())
    low = text.casefold()
    if low in ("match odds", "h2h", "1x2", "match winner", "match_winner_1x2", "moneyline"):
        return MarketRef(MarketKind.MATCH_ODDS)
    if low in ("btts", "both teams to score", "both teams score"):
        return MarketRef(MarketKind.BTTS)
    if low in ("double chance", "double_chance"):
        return MarketRef(MarketKind.DOUBLE_CHANCE)
    if low in ("draw no bet", "draw_no_bet", "dnb"):
        return MarketRef(MarketKind.DRAW_NO_BET)
    try:
        if match := _TOTALS.match(text):
            return MarketRef(MarketKind.TOTALS, float(match.group(1)))
        if match := _HANDICAP.match(text):
            return MarketRef(MarketKind.ASIAN_HANDICAP, float(match.group(1)))
    except ValueError:
        return None
    return None


DRAW_SPORTS = ("soccer", "rugbyleague", "rugbyunion", "cricket_test")  # a level score is a result here, not a tie to resolve


def has_draws(sport_key: str | None) -> bool:
    """Does a level score settle Match Odds as a draw? Elsewhere (tennis, NBA, T20) the books' tie rules apply."""
    return sport_key is None or sport_key.startswith(DRAW_SPORTS)


def _parts(line: float) -> tuple[float, ...]:
    """A quarter line is two half-stakes on its neighbours; any other line is one stake."""
    if abs(line * 2 - round(line * 2)) > 1e-9:  # x.25 or x.75
        return (line - 0.25, line + 0.25)
    return (line,)


def _combine(margins: tuple[float, ...]) -> LegResult:
    signs = [0 if abs(m) < 1e-9 else (1 if m > 0 else -1) for m in margins]
    if len(signs) == 1:
        return {1: LegResult.WON, 0: LegResult.VOID, -1: LegResult.LOST}[signs[0]]
    total = sum(signs)
    return {2: LegResult.WON, 1: LegResult.HALF_WON, 0: LegResult.VOID, -1: LegResult.HALF_LOST, -2: LegResult.LOST}[total]


def settle_selection(ref: MarketRef, selection: str, home_goals: int, away_goals: int) -> LegResult:
    """The result of one selection given the final score (90 minutes plus stoppage time)."""
    if selection not in ref.selections:
        raise ValueError(f"{selection!r} is not a selection of {ref.key}")
    h, a = int(home_goals), int(away_goals)
    if h < 0 or a < 0:
        raise ValueError("goals cannot be negative")
    diff = h - a
    if ref.kind is MarketKind.MATCH_ODDS:
        won = {"HOME": diff > 0, "DRAW": diff == 0, "AWAY": diff < 0}[selection]
        return LegResult.WON if won else LegResult.LOST
    if ref.kind is MarketKind.BTTS:
        both = h > 0 and a > 0
        return LegResult.WON if both == (selection == "YES") else LegResult.LOST
    if ref.kind is MarketKind.DOUBLE_CHANCE:
        won = {"1X": diff >= 0, "12": diff != 0, "X2": diff <= 0}[selection]
        return LegResult.WON if won else LegResult.LOST
    if ref.kind is MarketKind.DRAW_NO_BET:
        if diff == 0:
            return LegResult.VOID
        return LegResult.WON if (diff > 0) == (selection == "HOME") else LegResult.LOST
    line = float(ref.line)  # type: ignore[arg-type]
    if ref.kind is MarketKind.TOTALS:
        goals = h + a
        sign = 1 if selection == "OVER" else -1
        return _combine(tuple(sign * (goals - part) for part in _parts(line)))
    # Asian handicap: the line is the home side's; the away side carries its negation
    if selection == "HOME":
        return _combine(tuple(diff + part for part in _parts(line)))
    return _combine(tuple(-diff - part for part in _parts(line)))


def payout_factor(result: LegResult, odds: float) -> float:
    """What one unit staked on the leg returns (the parlay multiplies these)."""
    return {
        LegResult.WON: float(odds),
        LegResult.HALF_WON: (float(odds) + 1.0) / 2.0,
        LegResult.VOID: 1.0,
        LegResult.HALF_LOST: 0.5,
        LegResult.LOST: 0.0,
    }[result]
