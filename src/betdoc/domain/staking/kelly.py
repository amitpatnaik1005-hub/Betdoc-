"""Kelly criterion staking. Deliberately kept as pure functions with no I/O —
this is the module that decides how much money to risk, so it must be the
most heavily unit- and property-tested code in the repo (see tests/test_kelly.py
and Hypothesis-based invariant tests).

IMPORTANT: full Kelly (f*) is almost never what you want to bet in production.
It assumes your probability estimate `p` is exact. In reality `p` comes from a
model with estimation error, and full Kelly's growth-rate optimality comes
with violent variance — a small overestimate of edge leads to oversized bets
and drawdowns that are large multiples of what pure Kelly math suggests.
Standard practice (and what this module defaults to) is FRACTIONAL Kelly:
bet f* * fraction, where fraction is typically 0.25-0.5. This trades a small
amount of long-run growth rate for a large reduction in variance/drawdown.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class KellyResult:
    edge: float                 # p * decimal_odds - 1  (expected value per unit staked)
    full_kelly_fraction: float  # f* — fraction of bankroll, UNCAPPED
    recommended_fraction: float  # after applying kelly_fraction and bankroll cap
    stake: float


def kelly_fraction(prob_win: float, decimal_odds: float) -> float:
    """Full Kelly fraction f* for a single simple bet.

    Standard form: f* = (b*p - q) / b
      where b = decimal_odds - 1 (net odds), p = win probability, q = 1 - p.

    Returns 0 if there is no edge (f* would be negative) — never bet negative
    edge, and never let a formula produce a "recommendation" to lay off /
    short a bet type this module isn't designed to size.
    """
    if not (0.0 < prob_win < 1.0):
        raise ValueError("prob_win must be strictly between 0 and 1")
    if decimal_odds <= 1.0:
        raise ValueError("decimal_odds must be > 1.0")

    b = decimal_odds - 1.0
    q = 1.0 - prob_win
    f_star = (b * prob_win - q) / b
    return max(f_star, 0.0)


def size_bet(
    prob_win: float,
    decimal_odds: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_stake_pct_of_bankroll: float = 0.05,
) -> KellyResult:
    """Compute a safe, bounded stake.

    Two independent caps are applied on top of full Kelly:
      1. kelly_multiplier (e.g. 0.25 = quarter-Kelly) — protects against
         model/edge estimation error.
      2. max_stake_pct_of_bankroll — a hard ceiling regardless of what the
         model says, protecting against a single bad model update or a bug
         producing a wildly overconfident probability.
    """
    edge = prob_win * decimal_odds - 1.0
    f_star = kelly_fraction(prob_win, decimal_odds)
    recommended = min(f_star * kelly_multiplier, max_stake_pct_of_bankroll)
    # Floor to cents rather than round-to-nearest: this is a safety ceiling,
    # not a display value, and round-to-nearest can push the actual stake
    # a cent past max_stake_pct_of_bankroll * bankroll on small bankrolls.
    stake = math.floor(recommended * bankroll * 100) / 100

    return KellyResult(
        edge=edge,
        full_kelly_fraction=f_star,
        recommended_fraction=recommended,
        stake=stake,
    )
