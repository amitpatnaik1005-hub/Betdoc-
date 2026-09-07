"""Single-bet Kelly (kelly.py) silently assumes every bet you place is
independent of every other bet you currently hold. That assumption breaks
immediately for:
  - Same-game parlays (legs are correlated by construction)
  - Multiple arbitrage legs across books on the same event
  - Multiple simultaneous bets on related markets (e.g. moneyline + spread
    on the same game)

Applying per-bet Kelly to a correlated portfolio systematically OVER-BETS,
because it doesn't see that a loss on one leg is more likely to coincide with
a loss on another. This module treats staking as a portfolio optimization
problem instead of N independent formulas.

Two solvers are provided:

1. `kelly_quadratic_approx` — fast, uses a mean/covariance (mean-variance)
   second-order approximation to log-growth. Good default for larger
   portfolios (10+ simultaneous positions) where exact scenario enumeration
   is too slow for the hot path.

2. `kelly_scenario_exact` — exact multivariate Kelly via convex optimization
   over enumerated joint outcome scenarios (e.g. from a Dixon-Coles or
   Monte Carlo correlation model). More accurate, but scenario count grows
   fast — practical for small correlated clusters (same-game parlay legs,
   a handful of arb legs), not your whole open book.
"""

from __future__ import annotations

import cvxpy as cp
import numpy as np


def kelly_quadratic_approx(
    expected_returns: np.ndarray,  # mu_i = p_i * decimal_odds_i - 1, per bet
    covariance: np.ndarray,  # Sigma, correlated returns between bets
    kelly_multiplier: float = 0.25,
    max_total_exposure: float = 0.25,  # cap on sum of stakes as fraction of bankroll
) -> np.ndarray:
    """Maximize mu^T f - 0.5 f^T Sigma f  (2nd-order Taylor approx of E[log(1+f^T R)]
    around f=0), subject to f >= 0 and sum(f) <= max_total_exposure.

    Returns the fraction-of-bankroll stake vector, already scaled by
    kelly_multiplier for the same reason single-bet Kelly is fractionalized:
    protection against covariance/edge misestimation.
    """
    n = len(expected_returns)
    f = cp.Variable(n, nonneg=True)

    growth_approx = expected_returns @ f - 0.5 * cp.quad_form(f, covariance)
    constraints = [cp.sum(f) <= max_total_exposure]

    problem = cp.Problem(cp.Maximize(growth_approx), constraints)
    problem.solve()

    if f.value is None:
        return np.zeros(n)

    return np.maximum(f.value, 0.0) * kelly_multiplier


def kelly_scenario_exact(
    scenario_probs: np.ndarray,  # shape (S,) — probability of each joint scenario
    scenario_returns: np.ndarray,  # shape (S, N) — per-bet return in each scenario
    kelly_multiplier: float = 0.25,
    max_total_exposure: float = 0.25,
) -> np.ndarray:
    """Exact multivariate Kelly: maximize sum_s p_s * log(1 + R_s . f).

    log(affine(f)) is concave, so this is a proper convex program — no
    approximation error, unlike the quadratic method. Build scenario_returns
    from your correlation model (Dixon-Coles for soccer/hockey scorelines,
    or Monte Carlo joint sampling — see parlay.py).
    """
    n = scenario_returns.shape[1]
    f = cp.Variable(n, nonneg=True)

    portfolio_growth = scenario_returns @ f  # shape (S,), one value per scenario
    expected_log_growth = scenario_probs @ cp.log(1 + portfolio_growth)

    constraints = [cp.sum(f) <= max_total_exposure]
    problem = cp.Problem(cp.Maximize(expected_log_growth), constraints)
    problem.solve()

    if f.value is None:
        return np.zeros(n)

    return np.maximum(f.value, 0.0) * kelly_multiplier
