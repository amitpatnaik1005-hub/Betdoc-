"""Referee tendencies from what they actually did (Group 78).

The records are officiated matches (``wire_officiating_records``): the cards, penalties and goals of each side,
read from ESPN's match events once a match finishes. ESPN names the officials for some leagues (the NFL, the
NHL), not for football; there an administrator names a fixture's referee and the record is filled in when the
match ends. Every finished match feeds its league's baseline, named referee or not.

A referee's rate is shrunk towards the league's mean by ``k = WIRE_REFEREE_PRIOR_MATCHES`` pseudo-matches, so a
referee seen five times is mostly league-average:

    rate_ref = (sum over the referee's matches + k x mean_league) / (n + k)

    cards_per_game     yellow + red, both sides
    penalties_per_90   penalties, both sides (one match is 90 minutes)
    home_bias_ratio    (cards shown to the visitors + 1) / (cards shown to the hosts + 1): over 1 books the visitors more
    over_totals_pct    matches over WIRE_REFEREE_TOTALS_LINE goals, shrunk the same way

Nothing is seeded: a referee nobody has recorded has no profile. Under ``WIRE_REFEREE_MIN_MATCHES`` the profile is
shown but not written to the fortress, whose referee pillar then stays UNVERIFIED.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Record:
    referee: str | None
    league: str
    home_yellow: int
    away_yellow: int
    home_red: int
    away_red: int
    home_penalties: int
    away_penalties: int
    home_goals: int
    away_goals: int

    @property
    def cards(self) -> int:
        return self.home_yellow + self.away_yellow + self.home_red + self.away_red


@dataclass(frozen=True, slots=True)
class Baseline:
    league: str
    matches: int
    yellow: float
    red: float
    penalties: float
    over_share: float


@dataclass(frozen=True, slots=True)
class Profile:
    referee: str
    league: str
    matches: int
    avg_yellow_cards: float
    avg_red_cards: float
    cards_per_game: float
    penalties_per_90: float
    home_bias_ratio: float
    over_totals_pct: float
    baseline_matches: int
    fortress_ready: bool

    def as_dict(self) -> dict[str, object]:
        return {"referee": self.referee, "league": self.league, "matches": self.matches, "avg_yellow_cards": self.avg_yellow_cards, "avg_red_cards": self.avg_red_cards,
                "cards_per_game": self.cards_per_game, "penalties_per_90": self.penalties_per_90, "home_bias_ratio": self.home_bias_ratio,
                "over_totals_pct": self.over_totals_pct, "baseline_matches": self.baseline_matches, "fortress_ready": self.fortress_ready}


def baseline(records: Sequence[Record], league: str, line: float) -> Baseline | None:
    rows = [r for r in records if r.league == league]
    if not rows:
        return None
    n = len(rows)
    return Baseline(league, n, sum(r.home_yellow + r.away_yellow for r in rows) / n, sum(r.home_red + r.away_red for r in rows) / n,
                    sum(r.home_penalties + r.away_penalties for r in rows) / n, sum(1 for r in rows if r.home_goals + r.away_goals > line) / n)


def profile(records: Sequence[Record], referee: str, league: str, *, prior: float, line: float, min_matches: int) -> Profile | None:
    """``referee``'s tendencies in ``league`` from ``records`` (every record of the league: the baseline uses them all)."""
    key = referee.casefold().strip()
    mine = [r for r in records if r.league == league and r.referee is not None and r.referee.casefold().strip() == key]
    if not mine:
        return None
    base = baseline(records, league, line)
    n = len(mine)
    k = prior if base is not None else 0.0

    def shrink(total: float, mean: float) -> float:
        return (total + k * mean) / (n + k)

    yellow = shrink(sum(r.home_yellow + r.away_yellow for r in mine), base.yellow if base else 0.0)
    red = shrink(sum(r.home_red + r.away_red for r in mine), base.red if base else 0.0)
    pens = shrink(sum(r.home_penalties + r.away_penalties for r in mine), base.penalties if base else 0.0)
    over = shrink(sum(1 for r in mine if r.home_goals + r.away_goals > line), base.over_share if base else 0.0)
    home_cards = sum(r.home_yellow + r.home_red for r in mine)
    away_cards = sum(r.away_yellow + r.away_red for r in mine)
    return Profile(referee=mine[-1].referee or referee, league=league, matches=n, avg_yellow_cards=round(yellow, 3), avg_red_cards=round(red, 3),
                   cards_per_game=round(yellow + red, 3), penalties_per_90=round(pens, 3), home_bias_ratio=round((away_cards + 1) / (home_cards + 1), 3),
                   over_totals_pct=round(100.0 * over, 1), baseline_matches=0 if base is None else base.matches, fortress_ready=n >= min_matches)
