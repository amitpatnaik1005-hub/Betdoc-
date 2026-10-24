"""The Bet Builder's pure rules (Group 104, point b.5). Developed for Amit Ashok Kumar Patnaik.

* ``choose_offender``: which leg the 1-click auto-fix replaces (a second leg on one fixture, else a negative-EV leg,
  else the heaviest-margin leg under a Heavy Juice badge, else the weakest leg of a vetoed slip). None: nothing to fix.
* ``serve``: the Chef's Table's three plates from slips already re-rated through the fortress at the retail books,
  only from slips with positive joint EV there and no pillar vetoing them: #1 the handcrafted parlay of the day (the
  best-scored multiple), #2 the safe fortress (the likeliest to win of the rest), #3 the high-yield special (the
  longest odds of the rest).
* ``scoreline_link`` / ``stress_test``: what a leg's fixture-fitted Dixon-Coles matrix says about it: its result at
  the likeliest score, its chance overall, and its chance inside three final-score scenarios (a low-scoring game, a
  game decided by one goal or level, an open game). Only three-way football carries a matrix; other legs get none.

Nothing here invents a figure: every number is read from the fortress's audit, Ashoka's legs or the fitted matrix.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.domain.oracle.markets import LegResult, MarketRef, settle_selection
from app.domain.oracle.match_model import MAX_GOALS, likeliest_score, outcome_distribution

PAID = (LegResult.WON, LegResult.HALF_WON)


# ================================================================ the auto-fix
def choose_offender(leg_ids: Sequence[str], evs: Mapping[str, float | None], margins: Mapping[str, float | None],
                    badge_codes: set[str], vetoed: bool) -> tuple[str, str] | None:
    """(leg_id, why) of the leg the auto-fix replaces, or None when the slip has nothing to fix."""
    def ev(leg_id: str) -> float:
        value = evs.get(leg_id)
        return value if value is not None else float("-inf")

    fixtures: dict[str, list[str]] = {}
    for leg_id in leg_ids:
        fixtures.setdefault(leg_id.split("|", 1)[0], []).append(leg_id)
    repeated = [legs for legs in fixtures.values() if len(legs) > 1]
    if repeated:
        worst = min((leg for legs in repeated for leg in legs), key=ev)
        return worst, "a second leg on the same fixture (Same-Match Tax)"
    worst = min(leg_ids, key=ev)
    if ev(worst) < 0:
        return worst, f"negative EV at this book ({ev(worst):+.1%})" if evs.get(worst) is not None else "no measurable EV"
    if "HEAVY_JUICE" in badge_codes:
        priced = [leg for leg in leg_ids if margins.get(leg) is not None]
        if priced:
            heaviest = max(priced, key=lambda leg: margins[leg])  # type: ignore[arg-type, return-value]
            return heaviest, f"the heaviest margin at this book ({margins[heaviest]:.1%})"
    if vetoed:
        return worst, "the weakest leg of a slip a pillar vetoed"
    return None


# ================================================================ the Chef's Table
@dataclass(frozen=True, slots=True)
class Rated:
    """One vetted slip after its fortress re-rating."""

    key: str
    legs: int
    score: float
    joint_ev: float
    joint_probability: float
    odds: float
    payload: Mapping[str, Any]
    vetoed: bool = False  # a pillar failed: never served


PLATES: tuple[tuple[str, str], ...] = (
    ("PARLAY_OF_THE_DAY", "#1 Handcrafted parlay of the day"),
    ("SAFE_FORTRESS", "#2 Safe fortress"),
    ("HIGH_YIELD", "#3 High-yield special"),
)


def serve(rated: Sequence[Rated]) -> list[tuple[str, str, Rated]]:
    """The three plates (fewer when fewer slips qualify), each slip served once. Only a slip with positive joint EV at
    the books it was rated at and no vetoing pillar is servable: the kitchen serves nothing rather than a loser."""
    left = [r for r in rated if r.joint_ev > 0 and not r.vetoed]
    plates: list[tuple[str, str, Rated]] = []

    def take(code: str, title: str, pool: list[Rated], key: Callable[[Rated], Any]) -> None:
        if pool:
            best = max(pool, key=key)
            plates.append((code, title, best))
            left.remove(best)

    multiples = [r for r in left if r.legs >= 2]
    take(*PLATES[0], multiples or left, lambda r: (r.score, r.joint_ev))
    take(*PLATES[1], list(left), lambda r: (r.joint_probability, r.score))
    take(*PLATES[2], list(left), lambda r: (r.odds, r.score))
    return plates


_PLATE_ORDER: dict[str, Callable[[Rated], Any]] = {
    "PARLAY_OF_THE_DAY": lambda r: (r.score, r.joint_ev),
    "SAFE_FORTRESS": lambda r: (r.joint_probability, r.score),
    "HIGH_YIELD": lambda r: (r.odds, r.score),
}


def serve_with_reference(rated: Sequence[Rated]) -> list[tuple[str, str, Rated, bool]]:
    """``serve``'s plates, then (Group 129) each plate it left empty filled for reference from the slips that miss the
    bar: still positive joint EV (a loser is never plated), the unvetoed first. The last field says the plate is below the
    bar: the kitchen labels it so, with the pillars it fails, and never presents it as served."""
    out = [(code, title, r, False) for code, title, r in serve(rated)]
    used = {r.key for _, _, r, _ in out}
    reserve = [r for r in rated if r.joint_ev > 0 and r.key not in used]
    for code, title in PLATES:
        if any(c == code for c, _, _, _ in out) or not reserve:
            continue
        pool = [r for r in reserve if r.legs >= 2] if code == "PARLAY_OF_THE_DAY" else reserve
        best = max(pool or reserve, key=lambda r, order=_PLATE_ORDER[code]: (not r.vetoed, order(r)))
        out.append((code, title, best, True))
        reserve.remove(best)
    rank = [code for code, _ in PLATES]
    return sorted(out, key=lambda plate: rank.index(plate[0]))


# ================================================================ a leg's scoreline evidence
SCENARIOS: tuple[tuple[str, str, Callable[[np.ndarray, np.ndarray], np.ndarray]], ...] = (
    ("LOW_SCORING", "A tight, low-scoring game (0 or 1 goals)", lambda total, margin: total <= 1),
    ("ONE_GOAL", "Decided by one goal or level", lambda total, margin: np.abs(margin) <= 1),
    ("OPEN", "An open game (4 or more goals)", lambda total, margin: total >= 4),
)


def _paid(matrix: np.ndarray, ref: MarketRef, selection: str) -> tuple[float, float]:
    d = outcome_distribution(matrix, ref, selection)
    return d[LegResult.WON] + d[LegResult.HALF_WON], d[LegResult.VOID]


def scoreline_link(matrix: np.ndarray | None, ref: MarketRef, selection: str) -> dict[str, Any] | None:
    """The leg against its fixture's fitted scorelines: the likeliest score and the leg's result there, its chance."""
    if matrix is None:
        return None
    top = likeliest_score(matrix)
    home, away = (int(x) for x in str(top["score"]).split("-"))
    result = settle_selection(ref, selection, home, away)
    paid, push = _paid(matrix / matrix.sum(), ref, selection)
    return {"likeliest_score": top["score"], "likeliest_probability": top["probability"], "expected_goals": top["expected_goals"],
            "result_at_likeliest": result.value, "synced": result in PAID, "leg_probability": round(paid, 6), "push_probability": round(push, 6)}


def stress_test(matrix: np.ndarray | None, ref: MarketRef, selection: str) -> list[dict[str, Any]]:
    """The leg's chance of paying inside each final-score scenario, and how likely each scenario is."""
    if matrix is None:
        return []
    m = matrix / matrix.sum()
    goals = np.arange(MAX_GOALS + 1)
    total, margin = np.add.outer(goals, goals), np.subtract.outer(goals, goals)
    out = []
    for code, label, rule in SCENARIOS:
        mask = rule(total, margin)
        mass = float(m[mask].sum())
        if mass <= 1e-9:
            continue
        paid, _ = _paid(np.where(mask, m, 0.0) / mass, ref, selection)
        out.append({"code": code, "scenario": label, "probability": round(mass, 6), "leg_pays": round(paid, 6)})
    return out
