"""Scoreline models for one fixture, and every market priced from them (Group 69).

Ashoka needs the *joint* distribution of a match, not just its 1X2 line: it is what prices a totals or
handicap leg, says whether two legs of the same match are correlated, and drives the Monte Carlo paths.
The distribution is a scoreline matrix ``P[home goals, away goals]``:

* ``poisson``: independent Poisson goals with rates (lambda, mu);
* ``dixon_coles``: the same, corrected at 0-0 / 1-0 / 0-1 / 1-1 by ``rho`` (low-score dependence);
* ``xg``: Poisson on expected-goals figures, when the caller has them;
* ``elo``: an Elo-rating 1X2 (no scorelines), when the caller has ratings.

No live xG or Elo feed exists, so without them the rates are *fitted to the market*: the (lambda, mu)
whose Dixon-Coles 1X2 (and Over/Under, when quoted) best matches the de-vigged consensus. Those models
then agree with the market on 1X2 by construction and add what the market alone cannot: consistent
prices for every other market of the match, and the correlation between them.

``outcome_distribution`` gives, for one selection, the probability of each settlement result
(won, half won, void, half lost, lost), computed exactly from the matrix with ``markets.settle_selection``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson as poisson_dist

from app.domain.oracle.markets import LegResult, MarketKind, MarketRef, settle_selection

MAX_GOALS = 10  # the matrix runs 0..10 per side; the tail beyond is folded into 10
DEFAULT_RHO = -0.13  # Dixon-Coles low-score dependence, a typical football estimate
RESULTS: tuple[LegResult, ...] = (LegResult.WON, LegResult.HALF_WON, LegResult.VOID, LegResult.HALF_LOST, LegResult.LOST)


def goal_pmf(rate: float) -> np.ndarray:
    """P(0..MAX_GOALS goals), the last cell holding the whole tail."""
    pmf = poisson_dist.pmf(np.arange(MAX_GOALS + 1), max(rate, 1e-9))
    pmf[-1] += max(0.0, 1.0 - pmf.sum())
    return pmf / pmf.sum()


def poisson_matrix(home_rate: float, away_rate: float) -> np.ndarray:
    return np.outer(goal_pmf(home_rate), goal_pmf(away_rate))


def dixon_coles_matrix(home_rate: float, away_rate: float, rho: float = DEFAULT_RHO) -> np.ndarray:
    matrix = poisson_matrix(home_rate, away_rate)
    tau = np.ones_like(matrix)
    lam, mu = home_rate, away_rate
    tau[0, 0] = 1.0 - lam * mu * rho
    tau[0, 1] = 1.0 + lam * rho
    tau[1, 0] = 1.0 + mu * rho
    tau[1, 1] = 1.0 - rho
    adjusted = matrix * np.clip(tau, 0.0, None)
    return adjusted / adjusted.sum()


def one_x_two(matrix: np.ndarray) -> tuple[float, float, float]:
    return float(np.tril(matrix, -1).sum()), float(np.trace(matrix)), float(np.triu(matrix, 1).sum())


@lru_cache(maxsize=4096)
def _result_grid(kind: MarketKind, line: float | None, selection: str) -> np.ndarray:
    """For every scoreline, the index (into RESULTS) of this selection's settlement."""
    ref = MarketRef(kind, line)
    grid = np.empty((MAX_GOALS + 1, MAX_GOALS + 1), dtype=np.int8)
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            grid[h, a] = RESULTS.index(settle_selection(ref, selection, h, a))
    return grid


def outcome_distribution(matrix: np.ndarray, ref: MarketRef, selection: str) -> dict[LegResult, float]:
    """The probability of each settlement result of one selection under a scoreline matrix."""
    grid = _result_grid(ref.kind, ref.line, selection)
    mass = np.bincount(grid.ravel(), weights=matrix.ravel(), minlength=len(RESULTS))
    return {result: float(mass[i]) for i, result in enumerate(RESULTS)}


def win_probability(distribution: Mapping[LegResult, float]) -> float:
    """The chance the leg pays more than it costs (a half win counts; a void does not)."""
    return distribution.get(LegResult.WON, 0.0) + distribution.get(LegResult.HALF_WON, 0.0)


def expected_factor(distribution: Mapping[LegResult, float], odds: float) -> float:
    """What one unit on the leg returns on average: EV is this minus one."""
    factors = {LegResult.WON: odds, LegResult.HALF_WON: (odds + 1) / 2, LegResult.VOID: 1.0, LegResult.HALF_LOST: 0.5, LegResult.LOST: 0.0}
    return sum(distribution.get(r, 0.0) * f for r, f in factors.items())


@dataclass(frozen=True, slots=True)
class MarketTargets:
    """What the market says about a fixture (de-vigged), to fit goal rates to."""

    home: float
    draw: float
    away: float
    totals: Mapping[float, float] = field(default_factory=dict)  # goal line -> P(over)


def fit_rates(targets: MarketTargets, rho: float = DEFAULT_RHO) -> tuple[float, float, float]:
    """(lambda, mu, squared error): the Dixon-Coles rates that best reproduce the market."""
    want = np.array([targets.home, targets.draw, targets.away], dtype=float)
    lines = [(line, p) for line, p in targets.totals.items() if 0.0 < p < 1.0]

    def loss(params: np.ndarray) -> float:
        lam, mu = math.exp(params[0]), math.exp(params[1])
        matrix = dixon_coles_matrix(lam, mu, rho)
        err = float(np.sum((np.array(one_x_two(matrix)) - want) ** 2))
        for line, p_over in lines:
            dist = outcome_distribution(matrix, MarketRef(MarketKind.TOTALS, line), "OVER")
            err += (win_probability(dist) + dist[LegResult.VOID] / 2 - p_over) ** 2
        return err

    # A sensible start: home edge from the 1X2 split, total from the typical 2.6 goals
    share = (targets.home + targets.draw / 2) / max(targets.home + targets.draw + targets.away, 1e-9)
    start = np.log([max(2.6 * share, 0.2), max(2.6 * (1 - share), 0.2)])
    best = minimize(loss, start, method="L-BFGS-B", bounds=[(math.log(0.05), math.log(6.0))] * 2)
    lam, mu = (math.exp(v) for v in best.x)
    return lam, mu, float(best.fun)


def elo_one_x_two(home_elo: float, away_elo: float, home_advantage: float = 100.0) -> tuple[float, float, float]:
    """Elo expectation split into home / draw / away (the draw peaks when the sides are level)."""
    diff = home_elo - away_elo + home_advantage
    expected_home = 1.0 / (1.0 + 10 ** (-diff / 400.0))
    draw = 0.28 * math.exp(-(diff**2) / (2 * 150.0**2))
    home, away = max(0.0, expected_home - draw / 2), max(0.0, 1.0 - expected_home - draw / 2)
    total = home + draw + away
    return home / total, draw / total, away / total


@dataclass(slots=True)
class FixtureModels:
    """Every model Ashoka can run for one fixture, and what it says about a selection."""

    matrices: dict[str, np.ndarray]  # model name -> scoreline matrix
    elo: tuple[float, float, float] | None = None
    fit_error: float | None = None
    rates: tuple[float, float] | None = None

    @property
    def names(self) -> list[str]:
        return [*self.matrices, *(["elo"] if self.elo else [])]

    def distribution(self, model: str, ref: MarketRef, selection: str) -> dict[LegResult, float] | None:
        if model in self.matrices:
            return outcome_distribution(self.matrices[model], ref, selection)
        if model == "elo" and self.elo is not None and ref.kind is MarketKind.MATCH_ODDS:
            p = dict(zip(("HOME", "DRAW", "AWAY"), self.elo, strict=True))[selection]
            return {LegResult.WON: p, LegResult.HALF_WON: 0.0, LegResult.VOID: 0.0, LegResult.HALF_LOST: 0.0, LegResult.LOST: 1 - p}
        return None

    def mixture(self) -> np.ndarray:
        """The average scoreline matrix (what a same-match Monte Carlo path draws from)."""
        return np.mean(list(self.matrices.values()), axis=0)


def build_models(
    targets: MarketTargets | None,
    *,
    home_xg: float | None = None,
    away_xg: float | None = None,
    home_elo: float | None = None,
    away_elo: float | None = None,
    rho: float = DEFAULT_RHO,
) -> FixtureModels:
    """Poisson and Dixon-Coles (on xG when given, else on market-fitted rates), xG, Elo."""
    matrices: dict[str, np.ndarray] = {}
    fit_error = rates = None
    if targets is not None:
        lam, mu, fit_error = fit_rates(targets, rho)
        rates = (lam, mu)
        matrices["poisson"] = poisson_matrix(lam, mu)
        matrices["dixon_coles"] = dixon_coles_matrix(lam, mu, rho)
    if home_xg is not None and away_xg is not None and home_xg > 0 and away_xg > 0:
        matrices["xg"] = dixon_coles_matrix(home_xg, away_xg, rho)
        if targets is None:
            matrices["poisson"] = poisson_matrix(home_xg, away_xg)
            matrices["dixon_coles"] = matrices["xg"]
            rates = (home_xg, away_xg)
    elo = elo_one_x_two(home_elo, away_elo) if home_elo is not None and away_elo is not None else None
    return FixtureModels(matrices, elo, fit_error, rates)


def leg_correlation(matrix: np.ndarray, a: tuple[MarketRef, str], b: tuple[MarketRef, str]) -> float:
    """Pearson correlation between two same-match legs' win indicators (half results count half)."""
    def payoff(ref: MarketRef, selection: str) -> np.ndarray:
        grid = _result_grid(ref.kind, ref.line, selection)
        weights = np.array([1.0, 0.75, 0.5, 0.25, 0.0])[grid]  # won .. lost on a 0..1 scale
        return weights

    x, y, w = payoff(*a).ravel(), payoff(*b).ravel(), matrix.ravel()
    mx, my = float(np.dot(w, x)), float(np.dot(w, y))
    cov = float(np.dot(w, (x - mx) * (y - my)))
    vx, vy = float(np.dot(w, (x - mx) ** 2)), float(np.dot(w, (y - my) ** 2))
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    return cov / math.sqrt(vx * vy)


def consensus_two_way(prices: Sequence[Mapping[str, float]], labels: Sequence[str]) -> dict[str, float] | None:
    """Median multiplicative de-vig across books (helper for callers without Aryabhata's engine)."""
    fair: dict[str, list[float]] = {label: [] for label in labels}
    for book in prices:
        if not all(label in book and book[label] > 1.0 for label in labels):
            continue
        implied = [1.0 / book[label] for label in labels]
        total = sum(implied)
        for label, p in zip(labels, implied, strict=True):
            fair[label].append(p / total)
    if not all(fair.values()):
        return None
    medians = {label: float(np.median(values)) for label, values in fair.items()}
    total = sum(medians.values())
    return {label: p / total for label, p in medians.items()}
