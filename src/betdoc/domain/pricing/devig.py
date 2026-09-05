"""De-vigging: turning a bookmaker's quoted odds into a fair (true) probability
by removing the bookmaker's margin (the "vig" or "overround").

Two methods are implemented:

1. Proportional (multiplicative) — the textbook approach. Divide each implied
   probability by the sum of all implied probabilities. FAST but systematically
   WRONG: it assumes the vig is spread evenly across all outcomes, which is
   false. Bookmakers shade more margin into favorites (the "favorite-longshot
   bias"), so proportional devigging overstates the true probability of
   favorites and understates longshots.

2. Shin's method (Shin, 1992/1993) — models the market as if a fraction `z`
   of bettors have inside information, and solves for the `z` that is
   consistent with the observed odds. This corrects the favorite-longshot
   bias and gives materially more accurate fair probabilities, which matters
   because your entire +EV signal depends on how good this number is.

Reference: Shin, H.S. (1993), "Measuring the Incidence of Insider Trading in
a Market for State-Contingent Claims", The Economic Journal.
"""

from __future__ import annotations

import warnings

import numpy as np
from scipy.optimize import brentq


def implied_probabilities(decimal_odds: np.ndarray) -> np.ndarray:
    """Raw implied probability per outcome, before removing the vig."""
    return 1.0 / decimal_odds


def overround(decimal_odds: np.ndarray) -> float:
    """Bookmaker's total margin. 1.0 = no margin (a "fair" book); typical
    sportsbook overrounds sit around 1.02-1.08 for two-way markets."""
    return float(np.sum(implied_probabilities(decimal_odds)))


def devig_proportional(decimal_odds: np.ndarray) -> np.ndarray:
    """Fast, simple, biased. Use only as a fallback or a sanity cross-check
    against Shin's method — never as your primary signal."""
    p = implied_probabilities(decimal_odds)
    return p / p.sum()


def devig_shin(decimal_odds: np.ndarray) -> np.ndarray:
    """Shin's method. Solves for insider-trading fraction z such that:

        sum_i [ sqrt(z^2 + 4*(1-z)*p_i^2/S) - z ] / (2*(1-z)) = 1

    where p_i are raw implied probabilities and S = sum(p_i) (the overround).
    We solve for z numerically (root-find on [0, 0.5)), then back out the
    fair probabilities.
    """
    p = implied_probabilities(decimal_odds)
    s = p.sum()

    if s <= 1.0:
        # No overround detected (or a mispriced/arb-able book) — nothing to
        # remove, return proportional as-is.
        return p / s

    def f(z: float) -> float:
        inner = z**2 + 4 * (1 - z) * (p**2) / s
        return float(np.sum((np.sqrt(inner) - z) / (2 * (1 - z))) - 1.0)

    # z is bounded in [0, ~0.5), but brentq requires a bracket where f changes
    # sign. For realistic sportsbook overrounds (a few percent) that always
    # holds near z=0. For a degenerate/malformed market (e.g. a bad tick with
    # an absurd overround, or corrupted odds data), no such bracket may exist
    # in this range — Shin's model simply has no solution there. Scan for a
    # real sign change instead of assuming one, and fall back safely rather
    # than crashing the pricing pipeline on a single bad tick.
    grid = np.linspace(0.0, 0.499999, 50)
    f_vals = np.array([f(z) for z in grid])
    sign_changes = np.where(np.diff(np.sign(f_vals)) != 0)[0]

    if len(sign_changes) == 0:
        warnings.warn(
            "Shin's method found no valid solution for this market (likely an "
            "extreme or corrupted overround); falling back to proportional devig.",
            stacklevel=2,
        )
        return p / s

    lo, hi = grid[sign_changes[0]], grid[sign_changes[0] + 1]
    z = brentq(f, lo, hi, xtol=1e-10)

    fair = (np.sqrt(z**2 + 4 * (1 - z) * (p**2) / s) - z) / (2 * (1 - z))
    return fair / fair.sum()  # renormalize to kill residual float error


def fair_decimal_odds(decimal_odds: np.ndarray, method: str = "shin") -> np.ndarray:
    """Convenience: go straight from bookmaker odds to fair (no-vig) decimal
    odds, using the chosen de-vig method."""
    fair_probs = devig_shin(decimal_odds) if method == "shin" else devig_proportional(decimal_odds)
    return 1.0 / fair_probs
