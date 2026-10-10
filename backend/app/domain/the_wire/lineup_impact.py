"""Confirmed absences and what they cost a side (Group 78).

    Delta_side = -min(cap, sum over absences  full x rating/scale x lambda(position) x (1 - replacement) x w(status))

``full`` (``WIRE_RATING_FULL_COST``, 0.10) is what a player rated at the top of the scale, at weight 1, is worth to
the side's win probability: the brief's WAR/100 with WAR on 0-10.

* ``rating`` (0 .. ``WIRE_RATING_SCALE``): the player's worth to the side, the brief's WAR. No feed BetDoc reads
  carries it, so it is the operator's (``PATCH /the-wire/absences/{id}``) and carries over to the player's later
  absences. An unrated absence counts in nothing: the fortress's injury pillar stays UNVERIFIED for the fixture
  until it is rated.
* ``lambda``: the position's weight (``WIRE_POSITION_WEIGHTS`` per sport: a quarterback 3.5, a goalkeeper 1.4 ...).
* ``replacement``: how good the stand-in is (0 .. 1).
* ``w``: OUT and SUSPENDED 1, DOUBTFUL 0.5, QUESTIONABLE 0.25.

Win probabilities move by each side's delta and are renormalised (a draw keeps its share):

    P'_i = (P_i + Delta_i) / (1 + Delta_home + Delta_away)

The fortress's injury pillar reads each absence's impact on a 0-1 scale (1 irreplaceable):

    impact = min(1, rating/scale x lambda / lambda_max(sport) x (1 - replacement))
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.config import Settings

SIDES = ("HOME", "AWAY")


@dataclass(frozen=True, slots=True)
class LineupPolicy:
    weights: dict[str, dict[str, float]]
    default_weight: float
    status_weights: dict[str, float]
    rating_scale: float
    max_delta: float
    replacement_default: float
    full_cost: float

    @classmethod
    def from_settings(cls, s: Settings) -> LineupPolicy:
        return cls({k.casefold(): {p.upper(): float(w) for p, w in v.items()} for k, v in s.WIRE_POSITION_WEIGHTS.items()}, s.WIRE_POSITION_DEFAULT_WEIGHT,
                   {k.upper(): float(v) for k, v in s.WIRE_ABSENCE_STATUS_WEIGHTS.items()}, s.WIRE_RATING_SCALE, s.WIRE_LINEUP_MAX_DELTA, s.WIRE_REPLACEMENT_QUALITY_DEFAULT,
                   s.WIRE_RATING_FULL_COST)

    def weight(self, family: str, position: str | None) -> float:
        return self.weights.get(family, {}).get((position or "").upper(), self.default_weight)

    def top_weight(self, family: str) -> float:
        return max([self.default_weight, *self.weights.get(family, {}).values()])


@dataclass(frozen=True, slots=True)
class Absence:
    side: str
    player: str
    position: str | None
    status: str
    rating: float | None
    replacement_quality: float | None = None


def cost(a: Absence, family: str, p: LineupPolicy) -> float | None:
    """The win probability this absence takes from its side (positive), or None when unrated."""
    if a.rating is None:
        return None
    rq = p.replacement_default if a.replacement_quality is None else a.replacement_quality
    return (a.rating / p.rating_scale) * p.weight(family, a.position) * (1.0 - rq) * p.status_weights.get(a.status.upper(), 0.0) * p.full_cost


def deltas(absences: Sequence[Absence], family: str, p: LineupPolicy) -> tuple[dict[str, float], list[Absence]]:
    """Each side's delta (negative or 0) and the absences still unrated (they are not counted)."""
    totals = {s: 0.0 for s in SIDES}
    unrated = []
    for a in absences:
        c = cost(a, family, p)
        if c is None:
            if p.status_weights.get(a.status.upper(), 0.0) > 0:
                unrated.append(a)
            continue
        totals[a.side] += c
    return {s: -round(min(p.max_delta, v), 4) for s, v in totals.items()}, unrated


def adjust(probabilities: Mapping[str, float], delta: Mapping[str, float]) -> dict[str, float]:
    """Each side's probability moved by its delta, the whole renormalised; a side never goes below 0."""
    moved = {k: max(0.0, v + delta.get(k, 0.0)) for k, v in probabilities.items()}
    total = sum(moved.values())
    return {k: round(v / total, 4) for k, v in moved.items()} if total > 0 else dict(probabilities)


def fortress_impact(a: Absence, family: str, p: LineupPolicy) -> float | None:
    if a.rating is None:
        return None
    rq = p.replacement_default if a.replacement_quality is None else a.replacement_quality
    return round(min(1.0, (a.rating / p.rating_scale) * p.weight(family, a.position) / p.top_weight(family) * (1.0 - rq)), 4)
