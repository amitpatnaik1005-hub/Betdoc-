"""Grading a bet from a verified final score (Group 113, points b.6 and b.12). Developed for Amit Ashok Kumar Patnaik.

Every ledger BetDoc settles (the Oracle's placed bets, the real-money desk's tickets, the exchange orders) is graded the
same way, by Ashoka's market rules (``app.domain.oracle.markets.settle_selection``: 90 minutes plus stoppage time,
quarter lines split, a whole line that lands pushes). This module turns the markets and selections those ledgers store
("Match Odds" / "Arsenal", "Over/Under 2.5" / "Over 2.5", "Asian Handicap" / "Chelsea +1.5") into Ashoka's (``Match
Odds`` / ``HOME``, ``Totals 2.5`` / ``OVER``, ``Asian Handicap -1.5`` / ``AWAY``), and pairs a waiting fixture with a
results feed's finished match.

Nothing is guessed. A market or selection that cannot be read, a level score in a sport without draws, a postponement
inside the books' grace period, or a results match that is not the only candidate leaves the bet pending (None): it is
never voided to make it go away. Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.live_context import ProviderFixture, side_similarity
from app.domain.oracle.markets import LegResult, MarketKind, MarketRef, has_draws, parse_market, settle_selection

POSTPONED_GRACE = timedelta(hours=48)  # the books' wait before a postponed match's bets void (as the Oracle's settler)
_NUMBER = re.compile(r"([-+]?\d+(?:\.\d+)?)")
_DRAW = {"draw", "x", "tie"}
_RESULT_WORDS = {"full time result", "3-way", "three way", "result", "winner"}


@dataclass(frozen=True, slots=True)
class Leg:
    ref: MarketRef
    selection: str  # one of ref.selections


def _line(text: str) -> float | None:
    found = _NUMBER.findall(text.replace(",", "."))
    return float(found[-1]) if found else None


def _ref(kind: MarketKind, line: float | None = None) -> MarketRef | None:
    try:
        return MarketRef(kind, line)
    except ValueError:  # not a quarter line, or a negative goal line
        return None


def _kind(market: str) -> MarketKind | None:
    """A market Ashoka's parser does not name, read from its words ("Total Goals", "Handicap", "Full Time Result")."""
    low = market.casefold()
    if any(w in low for w in ("over", "under", "total")):
        return MarketKind.TOTALS
    if any(w in low for w in ("handicap", "spread")):
        return MarketKind.ASIAN_HANDICAP
    return MarketKind.MATCH_ODDS if low.strip() in _RESULT_WORDS else None


def _side(selection: str, home: str, away: str, cutoff: float) -> str | None:
    """HOME / AWAY / DRAW for a selection written as a token or a team name; None when it names neither side clearly."""
    token = _NUMBER.sub("", selection).strip().casefold()
    if token in ("home", "1", "home_team"):
        return "HOME"
    if token in ("away", "2", "away_team"):
        return "AWAY"
    if token in _DRAW:
        return "DRAW"
    h, a = side_similarity(token, (home,)), side_similarity(token, (away,))
    if h >= cutoff and h > a:
        return "HOME"
    if a >= cutoff and a > h:
        return "AWAY"
    return None


def canonical_leg(market: str, selection: str, home: str, away: str, *, cutoff: float = 0.85) -> Leg | None:
    """The stored market and selection as Ashoka's, or None when they cannot be read without guessing."""
    ref = parse_market(market)
    upper = selection.strip().upper()
    if ref is not None and upper in ref.selections:
        return Leg(ref, upper)
    kind = ref.kind if ref is not None else _kind(market)
    if kind in (MarketKind.MATCH_ODDS, MarketKind.DRAW_NO_BET):
        side = _side(selection, home, away, cutoff)
        target = ref or _ref(kind)
        return Leg(target, side) if target is not None and side in target.selections else None
    if kind is MarketKind.TOTALS:
        word = selection.strip().casefold()
        over, under = word.startswith("over"), word.startswith("under")
        line = ref.line if ref is not None else _line(selection) if _line(selection) is not None else _line(market)
        totals = _ref(MarketKind.TOTALS, line) if over != under and line is not None else None
        return Leg(totals, "OVER" if over else "UNDER") if totals is not None else None
    if kind is MarketKind.ASIAN_HANDICAP:
        side = _side(selection, home, away, cutoff)
        if side not in ("HOME", "AWAY"):
            return None
        given = _line(selection)
        if given is None:
            return Leg(ref, side) if ref is not None else None  # the market carries the home line
        home_line = given if side == "HOME" else -given  # a stored handicap is the selected side's; Ashoka keys the home line
        if ref is not None and ref.line != home_line:
            return None  # the market and the selection disagree on the line
        handicap = _ref(MarketKind.ASIAN_HANDICAP, home_line)
        return Leg(handicap, side) if handicap is not None else None
    return None


def grade(leg: Leg, *, status: str, home_goals: int | None, away_goals: int | None, sport_key: str | None,
          kickoff: datetime | None, now: datetime) -> LegResult | None:
    """The leg's result from a recorded score: ABANDONED voids; POSTPONED voids once the grace period has passed; a FINAL
    score settles by Ashoka's rules, except a level score on the match winner of a sport without draws (the book's tie
    rule decides, so a person does). None: not yet, or not for a machine."""
    if status == "ABANDONED":
        return LegResult.VOID
    if status == "POSTPONED":
        return LegResult.VOID if kickoff is not None and now - kickoff >= POSTPONED_GRACE else None
    if status != "FINAL" or home_goals is None or away_goals is None:
        return None
    if leg.ref.kind is MarketKind.MATCH_ODDS and home_goals == away_goals and leg.selection != "DRAW" and not has_draws(sport_key):
        return None
    return settle_selection(leg.ref, leg.selection, home_goals, away_goals)


@dataclass(frozen=True, slots=True)
class Waiting:
    """A fixture a pending bet waits on, as its ledger names it."""

    fixture_id: str
    family: str  # "soccer", "cricket"... (an Odds API sport key's first word)
    home: str
    away: str
    kickoff: datetime | None  # None: only a window is known (a ticket placed before an unknown kickoff)
    earliest: datetime | None = None
    latest: datetime | None = None


def family(sport_key: str | None) -> str:
    return (sport_key or "").split("_", 1)[0].casefold()


def match_result(waiting: Waiting, finished: Sequence[ProviderFixture], *, cutoff: float, window: timedelta) -> tuple[ProviderFixture, bool] | None:
    """The one finished match this fixture is, and whether the feed lists it the other way round. Both sides must reach
    ``cutoff`` (no loose rule where money moves); the kickoff must be within ``window`` (or inside the waiting window);
    two candidates mean none."""
    found: list[tuple[ProviderFixture, bool]] = []
    for p in finished:
        if family(p.sport_key) != waiting.family:
            continue
        if waiting.kickoff is not None:
            if abs(p.kickoff - waiting.kickoff) > window:
                continue
        elif waiting.earliest is None or waiting.latest is None or not waiting.earliest <= p.kickoff <= waiting.latest:
            continue
        straight = min(side_similarity(waiting.home, p.home), side_similarity(waiting.away, p.away))
        swapped = min(side_similarity(waiting.home, p.away), side_similarity(waiting.away, p.home))
        if straight >= cutoff and straight >= swapped:
            found.append((p, False))
        elif swapped >= cutoff:
            found.append((p, True))
    return found[0] if len(found) == 1 else None
