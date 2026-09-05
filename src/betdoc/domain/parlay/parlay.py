"""Parlay/same-game-parlay EV.

The common mistake: multiplying implied probabilities of each leg together
to get a combined probability. That's only valid if the legs are
INDEPENDENT. Same-game parlay legs almost never are — e.g. "Team A wins" and
"Over 2.5 goals" are positively correlated if Team A tends to win high-scoring
games. Multiplying independent-assumption probabilities systematically
misprices the parlay (usually understating true probability, meaning the
book's parlay odds are worse for the bettor than naive math suggests, but the
error can go either way depending on the correlation sign).

This module uses Monte Carlo simulation over a supplied correlation
structure. In production, the correlation matrix or joint distribution
should come from a proper scoreline model (e.g. Dixon-Coles for soccer/
hockey, which corrects for the known under-dispersion of low scorelines
under a plain independent-Poisson model), not a guess.

Reference: Dixon, M.J. and Coles, S.G. (1997), "Modelling Association
Football Scores and Inefficiencies in the Football Betting Market."
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ParlayEvResult:
    naive_independent_prob: float   # what you'd get from simple multiplication
    simulated_true_prob: float      # Monte Carlo estimate under correlation
    naive_ev: float
    true_ev: float
    correlation_mispricing_pct: float  # how far off the naive number is


def naive_independent_probability(leg_probs: np.ndarray) -> float:
    """The (usually wrong) textbook parlay probability."""
    return float(np.prod(leg_probs))


def simulate_correlated_parlay(
    leg_probs: np.ndarray,
    correlation_matrix: np.ndarray,
    n_simulations: int = 200_000,
    rng_seed: int | None = None,
) -> float:
    """Estimate true joint hit probability via a Gaussian copula:

      1. Draw correlated standard normals using the given correlation matrix.
      2. Convert each to a uniform via the normal CDF (this is the copula step
         — it preserves each leg's marginal probability while inducing the
         specified pairwise correlation between legs).
      3. A leg "hits" in a given simulation if its uniform draw < leg_probs[i].
      4. True parlay probability = fraction of simulations where ALL legs hit.

    This is a standard, tractable way to inject correlation into an
    already-estimated marginal probability without needing a full joint
    scoreline model for every leg combination.
    """
    rng = np.random.default_rng(rng_seed)
    n_legs = len(leg_probs)

    normals = rng.multivariate_normal(mean=np.zeros(n_legs), cov=correlation_matrix, size=n_simulations)
    from scipy.stats import norm

    uniforms = norm.cdf(normals)
    hits = uniforms < leg_probs  # shape (n_simulations, n_legs)
    all_legs_hit = hits.all(axis=1)

    return float(all_legs_hit.mean())


def evaluate_parlay(
    leg_probs: list[float],
    parlay_decimal_odds: float,
    correlation_matrix: np.ndarray | None = None,
    n_simulations: int = 200_000,
) -> ParlayEvResult:
    probs = np.array(leg_probs)
    naive_prob = naive_independent_probability(probs)

    if correlation_matrix is None:
        correlation_matrix = np.eye(len(probs))  # falls back to independence

    true_prob = simulate_correlated_parlay(probs, correlation_matrix, n_simulations)

    naive_ev = naive_prob * parlay_decimal_odds - 1.0
    true_ev = true_prob * parlay_decimal_odds - 1.0

    mispricing_pct = (true_prob - naive_prob) / naive_prob if naive_prob > 0 else 0.0

    return ParlayEvResult(
        naive_independent_prob=round(naive_prob, 6),
        simulated_true_prob=round(true_prob, 6),
        naive_ev=round(naive_ev, 4),
        true_ev=round(true_ev, 4),
        correlation_mispricing_pct=round(mispricing_pct, 4),
    )
