"""Scenario 3: the Manual Gut-Feel Sizing trap.

Replaces heuristic allocation across N simultaneous bets with the exact
growth-optimal solution.

Why this is not a quadratic program
-----------------------------------
The objective is expected logarithmic wealth growth::

    maximise   sum_w  P(w) * log(1 + sum_j x_j * r_j(w))

which is **concave but not quadratic**. The familiar mean-variance form
``mu' x - 0.5 x' Sigma x`` is only its second-order Taylor expansion around
``x = 0``, accurate for small stakes and progressively wrong as stakes grow,
precisely in the regime where getting it wrong is expensive. This module
solves the exact form through cvxpy's exponential cone, and keeps the
quadratic version as :func:`quadratic_kelly_approximation`, clearly labelled,
for warm starts and sanity checks only.

Where negative covariance comes from
------------------------------------
It is not asserted, it is **constructed**. Candidates sharing a
``market_key`` are mutually exclusive outcomes, so every generated scenario
lets at most one of them win. The indicator variables therefore have
covariance ``-p_i * p_j < 0`` by construction, and the optimiser discovers on
its own that backing two outcomes of the same market is a partial hedge rather
than double exposure. No covariance matrix is estimated or supplied.

Why sizing repeated single-bet Kelly is wrong
---------------------------------------------
Applying the single-bet formula to twelve concurrent bets massively overbets,
because each calculation assumes the rest of the bankroll is idle. The joint
solve is not a refinement, it is a correction.

Sequential vs simultaneous, and the fractional multiplier
---------------------------------------------------------
Fractional Kelly is applied as a **post-scale** of the optimal vector. This is
the standard convention: scaling the growth-optimal vector by a constant
preserves the relative allocation across bets while reducing variance
super-linearly relative to the growth given up. Solving with the fraction
folded into the constraints would instead distort the relative weights.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from enum import StrEnum
from typing import Final, Self

import cvxpy as cp
import numpy as np
import structlog
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from betdoc.domain.math.errors import SolverFailureError
from betdoc.domain.math.ev_filter import kelly_fraction
from betdoc.domain.math.money import from_paise, to_paise

__all__ = [
    "BetAllocation",
    "BetCandidate",
    "PortfolioAllocation",
    "PortfolioConfig",
    "PortfolioVerdict",
    "PrefilterReason",
    "PrefilterRejection",
    "PrefilterResult",
    "ScenarioSource",
    "build_scenarios",
    "prefilter_candidates",
    "quadratic_kelly_approximation",
    "solve_portfolio_kelly",
]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="domain.math.portfolio_kelly"
)

_PROB_TOLERANCE: Final[float] = 1e-9
_INTERNAL_PRECISION: Final[int] = 34
_EV_QUANT: Final[Decimal] = Decimal("0.00000001")
_DEFAULT_SOLVERS: Final[tuple[str, ...]] = ("CLARABEL", "SCS", "ECOS")
"""Exponential-cone capable solvers, in preference order.

CLARABEL is the modern interior-point default and the most numerically robust
here. SCS is a first-order fallback that converges on problems CLARABEL
struggles with. ECOS is the legacy backstop.
"""

_STRICT: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_default=True,
    revalidate_instances="never",
    str_strip_whitespace=True,
)

CorrelationLookup = Mapping[tuple[str, str], float]
"""Sparse pairwise correlation, keyed by ordered ``(bet_id, bet_id)`` pairs.

Deliberately sparse. Materialising a dense N-by-N matrix in the pre-filter
would defeat the purpose of having a cheap pre-filter.
"""


# --------------------------------------------------------------------------- #
# Strict types
# --------------------------------------------------------------------------- #


class PrefilterReason(StrEnum):
    """Why a candidate did or did not reach the convex solver."""

    PASSED = "passed"
    NEGATIVE_EV = "negative_ev"
    BELOW_MIN_EDGE = "below_min_edge"
    ODDS_OUT_OF_RANGE = "odds_out_of_range"
    CORRELATION_DOMINATED = "correlation_dominated"
    EXCEEDS_CANDIDATE_CAP = "exceeds_candidate_cap"


class PortfolioVerdict(StrEnum):
    """Terminal outcome of an allocation attempt."""

    OPTIMAL = "optimal"
    OPTIMAL_REDUCED = "optimal_reduced"
    """Solved, then trimmed by the exact-integer bankroll re-verification."""

    INFEASIBLE = "infeasible"
    SOLVER_FAILED = "solver_failed"
    NO_CANDIDATES_SURVIVED = "no_candidates_survived"

    @property
    def is_actionable(self) -> bool:
        return self in (PortfolioVerdict.OPTIMAL, PortfolioVerdict.OPTIMAL_REDUCED)


class ScenarioSource(StrEnum):
    """How the scenario space was produced."""

    EXHAUSTIVE = "exhaustive"
    MONTE_CARLO = "monte_carlo"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class BetCandidate(BaseModel):
    """One candidate bet, already devigged upstream by ``ev_filter``."""

    model_config = _STRICT

    bet_id: str = Field(min_length=1, max_length=64)
    market_key: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Candidates sharing this key are mutually exclusive outcomes of the "
            "same market, which is how negative covariance enters the model."
        ),
    )
    outcome_key: str = Field(min_length=1, max_length=64)
    fair_probability: float = Field(gt=0.0, lt=1.0)
    decimal_odds: float = Field(gt=1.0, le=10_000.0)
    risk_factor_key: str | None = Field(default=None, max_length=128)

    @property
    def net_return_if_win(self) -> float:
        """``b`` in the Kelly formulation."""
        return self.decimal_odds - 1.0

    @property
    def ev_per_unit(self) -> float:
        return self.fair_probability * self.decimal_odds - 1.0

    @property
    def single_kelly_fraction(self) -> float:
        """Full single-bet Kelly, used only by the pre-filter and for tests."""
        return kelly_fraction(self.fair_probability, self.decimal_odds)


class PortfolioConfig(BaseModel):
    """Allocation policy. Every limit is explicit, auditable and testable."""

    model_config = _STRICT

    fractional_kelly: float = Field(default=0.25, gt=0.0, le=1.0)
    max_total_exposure_fraction: float = Field(
        default=0.25,
        gt=0.0,
        le=0.95,
        description=(
            "Capped strictly below 1.0. With x >= 0 and worst-case return -1 "
            "per bet, this guarantees 1 + R@x >= 1 - sum(x) > 0, so the log "
            "argument can never leave its domain and the solve cannot blow up."
        ),
    )
    per_bet_cap_fraction: float = Field(default=0.05, gt=0.0, le=0.95)
    min_edge_per_unit: float = Field(default=0.005, ge=0.0)
    min_kelly_fraction: float = Field(default=1e-4, gt=0.0)
    max_pairwise_correlation: float = Field(
        default=0.90,
        gt=0.0,
        le=1.0,
        description="Only POSITIVE correlation triggers a drop. See prefilter docs.",
    )
    max_candidates: int = Field(default=64, ge=1, le=512)
    max_scenarios: int = Field(default=4096, ge=8)
    monte_carlo_scenarios: int = Field(default=8192, ge=1_000)
    seed: int = Field(default=20240101, ge=0)
    solvers: tuple[str, ...] = Field(default=_DEFAULT_SOLVERS, min_length=1)

    @model_validator(mode="after")
    def _cap_coherence(self) -> Self:
        if self.per_bet_cap_fraction > self.max_total_exposure_fraction:
            msg = "per_bet_cap_fraction cannot exceed max_total_exposure_fraction"
            raise ValueError(msg)
        return self


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class PrefilterRejection(BaseModel):
    """A discarded candidate, with the numbers that got it discarded.

    Every rejection is retained as data and logged. The pre-filter trades
    exactness for throughput, so the cost of that trade must be measurable
    rather than invisible.
    """

    model_config = _STRICT

    bet_id: str
    reason: PrefilterReason
    edge_per_unit: float
    single_kelly_fraction: float
    detail: str


class PrefilterResult(BaseModel):
    """Survivors plus the full audit trail of what was thrown away."""

    model_config = _STRICT

    survivors: tuple[BetCandidate, ...]
    rejections: tuple[PrefilterRejection, ...]
    evaluated_count: int = Field(ge=0)

    @property
    def survivor_count(self) -> int:
        return len(self.survivors)

    @property
    def discarded_edge_total(self) -> float:
        """Sum of positive edge thrown away. The measurable cost of the trade."""
        return math.fsum(r.edge_per_unit for r in self.rejections if r.edge_per_unit > 0)


class BetAllocation(BaseModel):
    """Final sizing for one bet, in exact integer paise."""

    model_config = _STRICT

    bet_id: str
    market_key: str
    outcome_key: str
    fraction: float = Field(ge=0.0, le=1.0)
    stake_paise: int = Field(ge=0)
    decimal_odds: float = Field(gt=1.0)
    fair_probability: float = Field(gt=0.0, lt=1.0)
    single_kelly_fraction: float = Field(ge=0.0, le=1.0)
    ev_per_unit: Decimal

    @property
    def stake_inr(self) -> Decimal:
        return from_paise(self.stake_paise)

    @property
    def kelly_ratio(self) -> float:
        """Portfolio fraction over naive single-bet Kelly.

        Reliably well below 1.0 for a correlated book. That gap *is* the
        overbetting that gut-feel and repeated single-bet Kelly both commit.
        """
        if self.single_kelly_fraction <= 0.0:
            return 0.0
        return self.fraction / self.single_kelly_fraction


class PortfolioAllocation(BaseModel):
    """Complete, re-auditable record of one portfolio sizing decision."""

    model_config = _STRICT

    verdict: PortfolioVerdict
    allocations: tuple[BetAllocation, ...]
    bankroll_paise: int = Field(gt=0)
    total_allocated_paise: int = Field(ge=0)

    objective_value: float
    expected_log_growth: float
    fractional_kelly: float = Field(gt=0.0, le=1.0)

    solver: str | None
    solve_seconds: float = Field(ge=0.0)
    scenario_count: int = Field(ge=0)
    scenario_source: ScenarioSource

    prefilter_evaluated: int = Field(ge=0)
    prefilter_survivors: int = Field(ge=0)
    prefilter_rejections: tuple[PrefilterRejection, ...]

    integer_shave_paise: int = Field(ge=0)
    rejection_reason: str | None = None

    @property
    def total_allocated_inr(self) -> Decimal:
        return from_paise(self.total_allocated_paise)

    @property
    def exposure_fraction(self) -> float:
        return self.total_allocated_paise / self.bankroll_paise

    @model_validator(mode="after")
    def _exact_integer_invariants(self) -> Self:
        computed = sum(a.stake_paise for a in self.allocations)
        if computed != self.total_allocated_paise:
            msg = (
                f"allocation total {computed} disagrees with reported total "
                f"{self.total_allocated_paise}"
            )
            raise ValueError(msg)
        if self.total_allocated_paise > self.bankroll_paise:
            msg = "total allocation exceeds the bankroll in exact integer arithmetic"
            raise ValueError(msg)
        if not self.verdict.is_actionable and self.total_allocated_paise != 0:
            msg = "a non-actionable verdict must allocate exactly zero"
            raise ValueError(msg)
        return self


# --------------------------------------------------------------------------- #
# Tier 1: the cheap pre-filter
# --------------------------------------------------------------------------- #


def _pairwise_correlation(
    lookup: CorrelationLookup, left: BetCandidate, right: BetCandidate
) -> float:
    """Sparse lookup, symmetric, defaulting to independence.

    Candidates in the same market are mutually exclusive, so their true
    dependence is strongly negative. We return ``-1.0`` for that case, which
    guarantees they are never dropped by the positive-correlation gate. That is
    deliberate: mutually exclusive pairs are exactly the structure the joint
    optimiser exploits, and discarding them would destroy the hedging value.
    """
    if left.market_key == right.market_key:
        return -1.0
    if (value := lookup.get((left.bet_id, right.bet_id))) is not None:
        return value
    if (value := lookup.get((right.bet_id, left.bet_id))) is not None:
        return value
    return 0.0


def prefilter_candidates(
    candidates: Sequence[BetCandidate],
    config: PortfolioConfig | None = None,
    correlation_lookup: CorrelationLookup | None = None,
) -> PrefilterResult:
    """Discard garbage cheaply so the convex solver only sees real candidates.

    Complexity is ``O(N log N + N*K)`` where ``K`` is the accepted count,
    bounded by ``config.max_candidates``. Since ``K`` is a small constant, this
    is effectively linear in ``N`` and involves no matrix construction, no
    solver call, and no allocation of an N-by-N array. On 500 candidates it
    runs in well under a millisecond, against tens to hundreds of milliseconds
    for the cvxpy solve.

    Tier 1 gates, all scalar:

    #. Odds inside the tradable range.
    #. Positive expected value.
    #. Edge above ``min_edge_per_unit`` and single-bet Kelly above
       ``min_kelly_fraction``.
    #. Positive pairwise correlation above ``max_pairwise_correlation`` against
       an already-accepted candidate, in which case the weaker edge is dropped
       (candidates are processed in descending edge order, so the survivor is
       always the better of the pair).
    #. Hard cap on the number of candidates admitted.

    **Deliberate loss of exactness, stated plainly.** A candidate that is
    individually negative EV can still raise ``E[log wealth]`` when it is
    negatively correlated with the rest of the book, because it acts as a
    hedge. Gate 2 discards those. This is a throughput decision, not a
    mathematical one. Every discard is returned as data and logged with its
    edge, so the cumulative cost is measurable via
    :attr:`PrefilterResult.discarded_edge_total`. Audit it periodically; if it
    grows large, raise ``max_candidates`` and relax gate 2 to admit
    negative-EV hedges for the markets where you hold concentrated exposure.

    Args:
        candidates: Raw candidate set, any size.
        config: Policy. Defaults are cautious.
        correlation_lookup: Sparse pairwise correlations. Absent pairs are
            treated as independent.

    Returns:
        A :class:`PrefilterResult` with survivors and full rejection detail.
    """
    cfg = config or PortfolioConfig()
    lookup = correlation_lookup or {}

    seen: set[str] = set()
    for candidate in candidates:
        if candidate.bet_id in seen:
            msg = f"duplicate bet_id {candidate.bet_id!r} in candidate set"
            raise ValueError(msg)
        seen.add(candidate.bet_id)

    ordered = sorted(candidates, key=lambda c: c.ev_per_unit, reverse=True)
    survivors: list[BetCandidate] = []
    rejections: list[PrefilterRejection] = []

    def _reject(candidate: BetCandidate, reason: PrefilterReason, detail: str) -> None:
        rejection = PrefilterRejection(
            bet_id=candidate.bet_id,
            reason=reason,
            edge_per_unit=candidate.ev_per_unit,
            single_kelly_fraction=candidate.single_kelly_fraction,
            detail=detail,
        )
        rejections.append(rejection)
        _log.info(
            "prefilter.rejected",
            bet_id=candidate.bet_id,
            market_key=candidate.market_key,
            reason=reason.value,
            edge_per_unit=candidate.ev_per_unit,
            single_kelly_fraction=candidate.single_kelly_fraction,
            detail=detail,
        )

    for candidate in ordered:
        if not 1.0 < candidate.decimal_odds <= 10_000.0:  # pragma: no cover
            _reject(
                candidate,
                PrefilterReason.ODDS_OUT_OF_RANGE,
                f"odds {candidate.decimal_odds} outside the tradable range",
            )
            continue

        edge = candidate.ev_per_unit
        if edge <= 0.0:
            _reject(
                candidate,
                PrefilterReason.NEGATIVE_EV,
                f"edge {edge:.6f} is non-positive at the fair probability",
            )
            continue

        single = candidate.single_kelly_fraction
        if edge < cfg.min_edge_per_unit or single < cfg.min_kelly_fraction:
            _reject(
                candidate,
                PrefilterReason.BELOW_MIN_EDGE,
                (
                    f"edge {edge:.6f} below {cfg.min_edge_per_unit} or kelly "
                    f"{single:.8f} below {cfg.min_kelly_fraction}"
                ),
            )
            continue

        dominating = next(
            (
                accepted
                for accepted in survivors
                if _pairwise_correlation(lookup, candidate, accepted) > cfg.max_pairwise_correlation
            ),
            None,
        )
        if dominating is not None:
            _reject(
                candidate,
                PrefilterReason.CORRELATION_DOMINATED,
                (
                    f"correlation with {dominating.bet_id!r} exceeds "
                    f"{cfg.max_pairwise_correlation}; kept the larger edge"
                ),
            )
            continue

        if len(survivors) >= cfg.max_candidates:
            _reject(
                candidate,
                PrefilterReason.EXCEEDS_CANDIDATE_CAP,
                f"candidate cap of {cfg.max_candidates} already reached",
            )
            continue

        survivors.append(candidate)

    _log.info(
        "prefilter.completed",
        evaluated=len(candidates),
        survivors=len(survivors),
        rejected=len(rejections),
        discarded_positive_edge=math.fsum(
            r.edge_per_unit for r in rejections if r.edge_per_unit > 0
        ),
    )

    return PrefilterResult(
        survivors=tuple(survivors),
        rejections=tuple(rejections),
        evaluated_count=len(candidates),
    )


# --------------------------------------------------------------------------- #
# Scenario construction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScenarioSet:
    """Discrete outcome space over which ``E[log wealth]`` is evaluated.

    Attributes:
        returns: ``(n_scenarios, n_bets)`` matrix of net returns. Entry is
            ``odds_j - 1`` when bet ``j`` wins in that scenario, else ``-1``.
        probabilities: Scenario probabilities, summing to 1.
        source: Whether the space was enumerated exactly or sampled.
    """

    returns: NDArray[np.float64]
    probabilities: NDArray[np.float64]
    source: ScenarioSource

    @property
    def scenario_count(self) -> int:
        return int(self.returns.shape[0])

    def expected_log_growth(self, weights: NDArray[np.float64]) -> float:
        """``sum_w P(w) log(1 + r(w)'x)`` at a given allocation vector."""
        wealth = 1.0 + self.returns @ weights
        if np.any(wealth <= 0.0):
            return float("-inf")
        return float(self.probabilities @ np.log(wealth))


def build_scenarios(
    candidates: Sequence[BetCandidate], config: PortfolioConfig | None = None
) -> ScenarioSet:
    """Construct the joint outcome space across markets.

    Within a market the candidate outcomes are **mutually exclusive**, so each
    market contributes exactly one of ``len(group) + 1`` states: one where a
    specific candidate outcome wins, plus a residual state where none of our
    selected outcomes wins (probability ``1 - sum(p)``). The residual state is
    omitted when the candidates already exhaust the market.

    Markets are treated as independent, so the joint space is the Cartesian
    product. Above ``config.max_scenarios`` the exact product is replaced by
    seeded Monte Carlo sampling with uniform scenario weights, and the fallback
    is logged.

    Raises:
        ValueError: A market's candidate probabilities sum above 1, which means
            the devigged inputs are inconsistent and must not be optimised on.
    """
    cfg = config or PortfolioConfig()
    if not candidates:
        return ScenarioSet(
            returns=np.zeros((0, 0), dtype=np.float64),
            probabilities=np.zeros((0,), dtype=np.float64),
            source=ScenarioSource.EXHAUSTIVE,
        )

    n_bets = len(candidates)
    groups: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(candidate.market_key, []).append(index)

    market_states: list[list[int | None]] = []
    market_probabilities: list[list[float]] = []

    for market_key, indices in groups.items():
        probabilities = [candidates[i].fair_probability for i in indices]
        total = math.fsum(probabilities)
        if total > 1.0 + 1e-6:
            msg = (
                f"market {market_key!r} candidate probabilities sum to {total!r}, "
                "which exceeds 1.0; the devigged inputs are inconsistent"
            )
            raise ValueError(msg)

        states: list[int | None] = list(indices)
        state_probabilities = list(probabilities)
        residual = 1.0 - total
        if residual > _PROB_TOLERANCE:
            states.append(None)
            state_probabilities.append(residual)
        market_states.append(states)
        market_probabilities.append(state_probabilities)

    exhaustive_count = 1
    for states in market_states:
        exhaustive_count *= len(states)

    rng = np.random.default_rng(cfg.seed)

    if exhaustive_count <= cfg.max_scenarios:
        from itertools import product as iter_product

        rows: list[NDArray[np.float64]] = []
        weights: list[float] = []
        for combination in iter_product(*market_states):
            probability = 1.0
            for choice_index, choice in enumerate(combination):
                position = market_states[choice_index].index(choice)
                probability *= market_probabilities[choice_index][position]
            if probability <= _PROB_TOLERANCE:
                continue
            row = np.full(n_bets, -1.0, dtype=np.float64)
            for choice in combination:
                if choice is not None:
                    row[choice] = candidates[choice].net_return_if_win
            rows.append(row)
            weights.append(probability)

        returns = np.vstack(rows)
        probabilities_array = np.asarray(weights, dtype=np.float64)
        probabilities_array /= probabilities_array.sum()
        return ScenarioSet(
            returns=returns,
            probabilities=probabilities_array,
            source=ScenarioSource.EXHAUSTIVE,
        )

    _log.warning(
        "scenarios.monte_carlo_fallback",
        exhaustive_count=exhaustive_count,
        max_scenarios=cfg.max_scenarios,
        samples=cfg.monte_carlo_scenarios,
        seed=cfg.seed,
        reason="exact product exceeds the scenario cap",
    )

    samples = cfg.monte_carlo_scenarios
    returns = np.full((samples, n_bets), -1.0, dtype=np.float64)
    for market_index, states in enumerate(market_states):
        weights_array = np.asarray(market_probabilities[market_index], dtype=np.float64)
        weights_array = weights_array / weights_array.sum()
        draws = rng.choice(len(states), size=samples, p=weights_array)
        for state_index, choice in enumerate(states):
            if choice is None:
                continue
            mask = draws == state_index
            returns[mask, choice] = candidates[choice].net_return_if_win

    return ScenarioSet(
        returns=returns,
        probabilities=np.full(samples, 1.0 / samples, dtype=np.float64),
        source=ScenarioSource.MONTE_CARLO,
    )


# --------------------------------------------------------------------------- #
# Tier 2: the exact convex solve
# --------------------------------------------------------------------------- #


def quadratic_kelly_approximation(
    candidates: Sequence[BetCandidate],
    scenarios: ScenarioSet,
    config: PortfolioConfig | None = None,
) -> tuple[float, ...]:
    """Mean-variance approximation of the Kelly optimum. NOT the answer.

    Warning:
        This is the **second-order Taylor expansion** of ``E[log(1 + r'x)]``
        about ``x = 0``::

            E[log(1 + r'x)]  ~=  mu'x - 0.5 * x'(Sigma + mu mu')x

        It is accurate only for small stakes and degrades exactly where the
        allocation matters most. Use it as a warm start for the exact solve, or
        as an independent order-of-magnitude sanity check. **Never** as the
        production allocation. :func:`solve_portfolio_kelly` is the answer.

    Returns:
        Non-negative fractions, clipped to the per-bet cap and rescaled if the
        total exposure constraint binds.
    """
    cfg = config or PortfolioConfig()
    if not candidates or scenarios.scenario_count == 0:
        return ()

    weights = scenarios.probabilities
    mean = weights @ scenarios.returns
    centred = scenarios.returns - mean
    covariance = (centred * weights[:, None]).T @ centred
    second_moment = covariance + np.outer(mean, mean)

    try:
        raw = np.linalg.solve(second_moment + 1e-10 * np.eye(len(candidates)), mean)
    except np.linalg.LinAlgError:  # pragma: no cover - ridge makes this unreachable
        raw = np.linalg.lstsq(second_moment, mean, rcond=None)[0]

    clipped = np.clip(raw, 0.0, cfg.per_bet_cap_fraction)
    total = float(clipped.sum())
    if total > cfg.max_total_exposure_fraction and total > 0.0:
        clipped *= cfg.max_total_exposure_fraction / total
    return tuple(float(value) for value in clipped)


def _allocate_paise(
    fractions: Sequence[float], bankroll_paise: int, max_total_fraction: float
) -> tuple[tuple[int, ...], int]:
    """Convert float fractions to exact integer paise, never over-allocating.

    cvxpy works in ``float64``. Converting each fraction independently with
    ``ROUND_DOWN`` is necessary but not sufficient: the *sum* can still land
    one paisa above the exposure limit through accumulated representation
    error. So after conversion we re-verify in **exact integer arithmetic** and
    deterministically shave any excess from the largest allocation, which is
    the allocation least distorted in relative terms by losing a paisa.

    This is the INR conversion-safety guarantee the test suite asserts.

    Returns:
        The paise allocations and the total number of paise shaved.
    """
    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        bankroll = Decimal(bankroll_paise)
        allocations = [
            int(
                (bankroll * Decimal(str(max(fraction, 0.0)))).to_integral_value(rounding=ROUND_DOWN)
            )
            for fraction in fractions
        ]
        limit = int(
            (bankroll * Decimal(str(max_total_fraction))).to_integral_value(rounding=ROUND_DOWN)
        )

    limit = min(limit, bankroll_paise)
    total = sum(allocations)
    shaved = 0

    while total > limit and total > 0:
        excess = total - limit
        largest = max(range(len(allocations)), key=lambda i: allocations[i])
        if allocations[largest] == 0:  # pragma: no cover - total would be 0
            break
        take = min(excess, allocations[largest])
        allocations[largest] -= take
        shaved += take
        total -= take

    if shaved:
        _log.warning(
            "allocation.integer_shave",
            shaved_paise=shaved,
            limit_paise=limit,
            final_total_paise=total,
            reason="float-to-integer rounding pushed the total above the exposure limit",
        )

    return tuple(allocations), shaved


def solve_portfolio_kelly(
    candidates: Sequence[BetCandidate],
    bankroll_inr: Decimal,
    config: PortfolioConfig | None = None,
    correlation_lookup: CorrelationLookup | None = None,
) -> PortfolioAllocation:
    """Growth-optimal simultaneous allocation across N bets.

    Pipeline: cheap pre-filter, scenario construction, exact concave solve,
    fractional-Kelly post-scale, exact-integer paise allocation with
    re-verification.

    Args:
        candidates: Candidate bets, already devigged upstream.
        bankroll_inr: Total bankroll as an exact ``Decimal``.
        config: Allocation policy.
        correlation_lookup: Sparse pairwise correlations for the pre-filter.

    Returns:
        A :class:`PortfolioAllocation`. Non-actionable verdicts allocate
        exactly zero rather than raising, so a caller can log and move on
        without exception handling in the hot path.

    Raises:
        SolverFailureError: Every configured solver failed on a problem that
            constructed successfully. This is a genuine defect, not a market
            condition, so it raises rather than returning a verdict.
    """
    cfg = config or PortfolioConfig()
    bankroll_paise = to_paise(bankroll_inr, label="bankroll_inr")
    if bankroll_paise <= 0:
        msg = f"bankroll must be at least one paisa, got {bankroll_inr}"
        raise ValueError(msg)

    prefilter = prefilter_candidates(candidates, cfg, correlation_lookup)

    def _empty(
        verdict: PortfolioVerdict, reason: str, solver: str | None = None
    ) -> PortfolioAllocation:
        return PortfolioAllocation(
            verdict=verdict,
            allocations=(),
            bankroll_paise=bankroll_paise,
            total_allocated_paise=0,
            objective_value=0.0,
            expected_log_growth=0.0,
            fractional_kelly=cfg.fractional_kelly,
            solver=solver,
            solve_seconds=0.0,
            scenario_count=0,
            scenario_source=ScenarioSource.EXHAUSTIVE,
            prefilter_evaluated=prefilter.evaluated_count,
            prefilter_survivors=prefilter.survivor_count,
            prefilter_rejections=prefilter.rejections,
            integer_shave_paise=0,
            rejection_reason=reason,
        )

    survivors = prefilter.survivors
    if not survivors:
        _log.info(
            "portfolio.no_candidates",
            evaluated=prefilter.evaluated_count,
            rejected=len(prefilter.rejections),
        )
        return _empty(
            PortfolioVerdict.NO_CANDIDATES_SURVIVED,
            "no candidate cleared the tier-1 pre-filter",
        )

    scenarios = build_scenarios(survivors, cfg)
    n = len(survivors)

    x = cp.Variable(n, nonneg=True)
    wealth = 1.0 + scenarios.returns @ x
    objective = cp.Maximize(cp.sum(cp.multiply(scenarios.probabilities, cp.log(wealth))))
    constraints = [
        cp.sum(x) <= cfg.max_total_exposure_fraction,
        x <= cfg.per_bet_cap_fraction,
    ]
    problem = cp.Problem(objective, constraints)

    solver_used: str | None = None
    solve_seconds = 0.0
    failures: list[str] = []

    for solver_name in cfg.solvers:
        if solver_name not in cp.installed_solvers():
            failures.append(f"{solver_name}: not installed")
            continue
        started = time.perf_counter()
        try:
            problem.solve(solver=solver_name, verbose=False)
        except (cp.error.SolverError, ValueError, ArithmeticError) as exc:
            failures.append(f"{solver_name}: {exc!r}")
            _log.warning(
                "portfolio.solver_error",
                solver=solver_name,
                error=repr(exc),
                candidate_count=n,
                scenario_count=scenarios.scenario_count,
            )
            continue
        solve_seconds = time.perf_counter() - started

        if problem.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE) and x.value is not None:
            solver_used = solver_name
            if problem.status == cp.OPTIMAL_INACCURATE:
                _log.warning(
                    "portfolio.solver_inaccurate",
                    solver=solver_name,
                    status=problem.status,
                    detail="accepting an inaccurate optimum; verify the shave logic held",
                )
            break

        failures.append(f"{solver_name}: status {problem.status}")
        if problem.status in (cp.INFEASIBLE, cp.INFEASIBLE_INACCURATE):
            _log.error(
                "portfolio.infeasible",
                solver=solver_name,
                status=problem.status,
                candidate_count=n,
            )
            return _empty(
                PortfolioVerdict.INFEASIBLE,
                f"problem is infeasible under the configured caps ({problem.status})",
                solver_name,
            )

    if solver_used is None or x.value is None:
        _log.error(
            "portfolio.solver_failed",
            candidate_count=n,
            scenario_count=scenarios.scenario_count,
            attempts=failures,
        )
        msg = "every configured convex solver failed"
        raise SolverFailureError(msg, attempts=failures, candidate_count=n)

    raw = np.clip(np.asarray(x.value, dtype=np.float64), 0.0, None)
    optimal_growth = scenarios.expected_log_growth(raw)

    # Fractional Kelly as a post-scale. Preserves relative allocation.
    scaled = raw * cfg.fractional_kelly
    scaled_growth = scenarios.expected_log_growth(scaled)

    paise, shaved = _allocate_paise(
        tuple(float(value) for value in scaled),
        bankroll_paise,
        cfg.max_total_exposure_fraction,
    )

    allocations: list[BetAllocation] = []
    for candidate, fraction, stake in zip(survivors, scaled, paise, strict=True):
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            ev = (
                Decimal(str(candidate.fair_probability)) * Decimal(str(candidate.decimal_odds))
                - Decimal(1)
            ).quantize(_EV_QUANT)
        allocations.append(
            BetAllocation(
                bet_id=candidate.bet_id,
                market_key=candidate.market_key,
                outcome_key=candidate.outcome_key,
                fraction=min(float(fraction), 1.0),
                stake_paise=stake,
                decimal_odds=candidate.decimal_odds,
                fair_probability=candidate.fair_probability,
                single_kelly_fraction=candidate.single_kelly_fraction,
                ev_per_unit=ev,
            )
        )

    total = sum(paise)
    verdict = PortfolioVerdict.OPTIMAL_REDUCED if shaved else PortfolioVerdict.OPTIMAL

    _log.info(
        "portfolio.allocated",
        verdict=verdict.value,
        solver=solver_used,
        solve_seconds=solve_seconds,
        candidate_count=n,
        scenario_count=scenarios.scenario_count,
        scenario_source=scenarios.source.value,
        optimal_log_growth=optimal_growth,
        scaled_log_growth=scaled_growth,
        total_allocated_paise=total,
        exposure_fraction=total / bankroll_paise,
        integer_shave_paise=shaved,
    )

    return PortfolioAllocation(
        verdict=verdict,
        allocations=tuple(allocations),
        bankroll_paise=bankroll_paise,
        total_allocated_paise=total,
        objective_value=float(problem.value),
        expected_log_growth=scaled_growth,
        fractional_kelly=cfg.fractional_kelly,
        solver=solver_used,
        solve_seconds=solve_seconds,
        scenario_count=scenarios.scenario_count,
        scenario_source=scenarios.source,
        prefilter_evaluated=prefilter.evaluated_count,
        prefilter_survivors=prefilter.survivor_count,
        prefilter_rejections=prefilter.rejections,
        integer_shave_paise=shaved,
        rejection_reason=None,
    )
