"""The six live smallcases (Group 107, point b.8): what each basket holds, chosen afresh from the live market on every
scan. Developed for Amit Ashok Kumar Patnaik.

A smallcase here is what it is on a stock exchange: a basket of separate holdings bought together, each with its own
weight. Each holding is a single, a quoted selection whose best retail price (Parimatch, 1xBet, Stake) beats BetDoc's
fair probability by at least ``SMALLCASE_MIN_EV``; one per fixture, so no two holdings move together; at most
``SMALLCASE_MAX_LEGS``. Nothing is a stored example: a theme the market cannot fill right now is empty, and says why.

* SAFE_COMPOUNDER (fortress): pre-match legs BetDoc gives at least ``SMALLCASE_SAFE_MIN_PROBABILITY``, likeliest first.
* CRICKET_POWERPLAY_EDGE (tactical alpha): pre-match cricket legs, best EV first. BetDoc prices cricket's match winner
  (and totals where a book quotes them); powerplay and innings-runs markets are not among BetDoc's priced markets yet.
* EUROPEAN_FOOTBALL_FORTRESS (fortress): pre-match legs in ``SMALLCASE_EUROPEAN_LEAGUES``, best EV first.
* HIGH_YIELD_UNDERDOG_BASKET (asymmetric): pre-match legs priced at ``SMALLCASE_UNDERDOG_MIN_ODDS`` or more.
* LIVE_MOMENTUM_ALPHA (momentum): legs on fixtures already in play whose in-play price beats fair. The feeds carry no
  live score or clock, so no momentum model runs: the basket is the in-play mispricings, nothing more is claimed.
* MULTI_SPORT_CHEFS_BASKET (chef special): the best pre-match leg of each sport, two sports at least.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Pick:
    """One holding a basket can take: a quoted selection priced at its best retail book."""

    leg_id: str
    fixture_id: str
    fixture: str
    label: str
    market: str
    market_kind: str
    sport_key: str
    sport: str | None
    league: str | None
    kickoff: str | None
    in_play: bool
    book: str
    price: float
    fair_probability: float
    ev: float
    prices: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {"leg_id": self.leg_id, "fixture_id": self.fixture_id, "fixture": self.fixture, "label": self.label, "market": self.market,
                "market_kind": self.market_kind, "sport_key": self.sport_key, "sport": self.sport, "league": self.league, "kickoff": self.kickoff,
                "in_play": self.in_play, "book": self.book, "price": self.price, "fair_probability": self.fair_probability, "ev": self.ev, "prices": self.prices}

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> Pick:
        return cls(**{name: row[name] for name in cls.__slots__})  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class ThemePolicy:
    max_legs: int
    min_ev: float
    safe_min_probability: float
    underdog_min_odds: float
    european_leagues: tuple[str, ...]

    @classmethod
    def from_settings(cls, s: Any) -> ThemePolicy:
        return cls(int(s.SMALLCASE_MAX_LEGS), float(s.SMALLCASE_MIN_EV), float(s.SMALLCASE_SAFE_MIN_PROBABILITY), float(s.SMALLCASE_UNDERDOG_MIN_ODDS),
                   tuple(k.casefold() for k in s.SMALLCASE_EUROPEAN_LEAGUES))


def _by_ev(p: Pick) -> tuple[float, str]:
    return (-p.ev, p.leg_id)


def _by_probability(p: Pick) -> tuple[float, float, str]:
    return (-p.fair_probability, -p.ev, p.leg_id)


def _one_per_fixture(picks: Sequence[Pick], key: Callable[[Pick], Any], limit: int) -> list[Pick]:
    """The best-ranked leg of each fixture, best first, at most ``limit``: no two holdings on one fixture."""
    out: list[Pick] = []
    seen: set[str] = set()
    for p in sorted(picks, key=key):
        if p.fixture_id not in seen:
            out.append(p)
            seen.add(p.fixture_id)
        if len(out) == limit:
            break
    return out


def _chefs(pool: Sequence[Pick], limit: int) -> list[Pick]:
    best: dict[str, Pick] = {}
    for p in sorted(pool, key=_by_ev):
        sport = p.sport or p.sport_key.split("_", 1)[0]
        best.setdefault(sport, p)
    picks = sorted(best.values(), key=_by_ev)[:limit]
    return picks if len(picks) >= 2 else []


@dataclass(frozen=True, slots=True)
class Theme:
    id: str
    name: str
    tier: str
    scope: str  # what the basket holds, in plain words
    empty: str  # why it can be empty
    choose: Callable[[Sequence[Pick], ThemePolicy], list[Pick]]


def _pre(pool: Sequence[Pick]) -> list[Pick]:
    return [p for p in pool if not p.in_play]


THEMES: tuple[Theme, ...] = (
    Theme("SAFE_COMPOUNDER", "Safe Compounder", "FORTRESS",
          "Pre-match legs BetDoc gives the highest chance of landing, each still priced above fair: the likeliest first.",
          "No pre-match leg with that chance is priced above fair at the retail books right now.",
          lambda pool, pol: _one_per_fixture([p for p in _pre(pool) if p.fair_probability >= pol.safe_min_probability], _by_probability, pol.max_legs)),
    Theme("CRICKET_POWERPLAY_EDGE", "Cricket Powerplay Edge", "TACTICAL_ALPHA",
          "Pre-match cricket legs priced above fair, best EV first. BetDoc prices cricket's match winner (and totals where a book quotes them); "
          "powerplay and innings-runs markets are not among BetDoc's priced markets yet.",
          "No cricket leg is priced above fair at the retail books right now.",
          lambda pool, pol: _one_per_fixture([p for p in _pre(pool) if p.sport_key.startswith("cricket")], _by_ev, pol.max_legs)),
    Theme("EUROPEAN_FOOTBALL_FORTRESS", "European Football Fortress", "FORTRESS",
          "Pre-match legs in Europe's top leagues and the European cups, best EV first.",
          "No leg in the configured European competitions is priced above fair right now.",
          lambda pool, pol: _one_per_fixture([p for p in _pre(pool) if p.sport_key.casefold() in pol.european_leagues], _by_ev, pol.max_legs)),
    Theme("HIGH_YIELD_UNDERDOG_BASKET", "High-Yield Underdog Basket", "ASYMMETRIC",
          "Pre-match long prices (underdogs, handicaps, totals) that still beat fair: small stakes, large returns, best EV first.",
          "No long price beats fair at the retail books right now.",
          lambda pool, pol: _one_per_fixture([p for p in _pre(pool) if p.price >= pol.underdog_min_odds], _by_ev, pol.max_legs)),
    Theme("LIVE_MOMENTUM_ALPHA", "Live Momentum Alpha", "MOMENTUM",
          "Legs on fixtures already in play whose in-play price beats fair. The feeds carry no live score or clock, so no momentum model runs: "
          "these are the in-play mispricings, nothing more is claimed.",
          "No fixture in play has a price above fair right now.",
          lambda pool, pol: _one_per_fixture([p for p in pool if p.in_play], _by_ev, pol.max_legs)),
    Theme("MULTI_SPORT_CHEFS_BASKET", "Multi-Sport Chef's Basket", "CHEF_SPECIAL",
          "The best pre-match leg of each sport, two sports at least: holdings that share no fixture, league or sport.",
          "Fewer than two sports have a pre-match leg priced above fair right now.",
          lambda pool, pol: _chefs(_pre(pool), pol.max_legs)),
)
THEMES_BY_ID: dict[str, Theme] = {t.id: t for t in THEMES}


def eligible(picks: Sequence[Pick], policy: ThemePolicy) -> list[Pick]:
    """The pool every basket draws from: legs at or over the minimum EV at their best retail price."""
    return [p for p in picks if p.ev >= policy.min_ev]


def choose(theme: Theme, picks: Sequence[Pick], policy: ThemePolicy) -> list[Pick]:
    return theme.choose(eligible(picks, policy), policy)
