"""Scenario 2: the Correlated Parlay Escalation trap.

Three problems, each of which silently destroys naive parlay systems:

#. **Books price parlays as independent.** The offered price is the product of
   the leg prices. When legs are positively correlated the true joint
   probability *exceeds* that product, which is the structural mispricing this
   module hunts (:attr:`ParlayVerdict.CORRELATION_UNDERPRICED`). Conversely, a
   negatively correlated parlay that looks cheap is a trap.
#. **Legs have three terminal states, not two.** WIN, LOSE, VOID. A void leg
   re-prices at odds 1.0 and the parlay survives on the remaining legs. That
   makes the settlement space ``3^n``, not ``2^n``, and any EV model built on
   two states understates both the variance and the exposure.
#. **Concentration is invisible per-bet.** Twenty parlays each risking 0.5% of
   bankroll look prudent until you notice all twenty contain "Team A to win".
   The :class:`ExposureMatrix` aggregates by underlying failure driver and
   enforces a hard single-point drawdown limit.

Correlation modelling
---------------------
Joint win probabilities use a **Gaussian copula**: map each leg's marginal
through the inverse normal CDF to a latent threshold, draw correlated latent
normals, and count the orthant where every leg clears its threshold. Exact
orthant probabilities are available analytically for small dimensions via
``scipy.stats.multivariate_normal.cdf``, which this module uses both as the
production path for n <= 3 and as the correctness oracle for the Monte Carlo
estimator in tests.

Every estimate is validated against the **Frechet-Hoeffding bounds**::

    max(0, sum(p_i) - (n - 1))  <=  P(all win)  <=  min(p_i)

Those bounds hold for *any* dependence structure whatsoever, so a violation
means the estimator or the correlation matrix is broken, not that the market
is unusual.

Void independence assumption
----------------------------
Void events are modelled as independent across legs. This is defensible for
idiosyncratic causes (a single player prop voiding on a late team-sheet
change) and **wrong** for common causes (a weather abandonment voiding every
leg of a same-game parlay simultaneously). Where common-cause voids matter,
supply a void correlation explicitly rather than trusting the default.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal, localcontext
from enum import StrEnum
from itertools import product as iter_product
from typing import Final, Self

import numpy as np
import structlog
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.linalg import cholesky
from scipy.stats import multivariate_normal, norm

from betdoc.domain.math.errors import (
    ExposureConcentrationError,
    InvalidCorrelationMatrixError,
)
from betdoc.domain.math.money import from_paise

__all__ = [
    "MAX_VOID_ENUMERATION_LEGS",
    "CopulaEstimate",
    "CopulaMethod",
    "ExposureMatrix",
    "ExposureRow",
    "LegState",
    "Parlay",
    "ParlayLeg",
    "ParlayVerdict",
    "SettlementSnapshot",
    "check_exposure_limit",
    "exact_gaussian_copula_joint_probability",
    "expected_value_with_void_risk",
    "frechet_hoeffding_bounds",
    "gaussian_copula_joint_probability",
    "parlay_ev",
    "recalculate_on_settlement",
    "validate_correlation_matrix",
]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="domain.math.parlay_correlation"
)

_PROB_TOLERANCE: Final[float] = 1e-9
_MATRIX_TOLERANCE: Final[float] = 1e-8
_MIN_EIGENVALUE: Final[float] = 1e-10
_EXACT_CDF_MAX_DIM: Final[int] = 3
MAX_VOID_ENUMERATION_LEGS: Final[int] = 6
"""Void enumeration costs ``2^n`` copula evaluations. Refuse beyond this."""

_INTERNAL_PRECISION: Final[int] = 34
_EV_QUANT: Final[Decimal] = Decimal("0.00000001")

_STRICT: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_default=True,
    revalidate_instances="never",
    str_strip_whitespace=True,
)


# --------------------------------------------------------------------------- #
# Strict types
# --------------------------------------------------------------------------- #


class LegState(StrEnum):
    """Terminal and non-terminal states of a single parlay leg."""

    PENDING = "pending"
    WIN = "win"
    LOSE = "lose"
    VOID = "void"

    @property
    def is_terminal(self) -> bool:
        return self is not LegState.PENDING

    @property
    def kills_parlay(self) -> bool:
        """A single losing leg zeroes the entire parlay payout."""
        return self is LegState.LOSE

    @property
    def contributes_odds(self) -> bool:
        """VOID legs re-price to 1.0 and drop out of the payout product."""
        return self is not LegState.VOID


class CopulaMethod(StrEnum):
    """How a joint probability was obtained."""

    EXACT_MVN = "exact_mvn"
    MONTE_CARLO = "monte_carlo"
    DEGENERATE_SINGLE_LEG = "degenerate_single_leg"


class ParlayVerdict(StrEnum):
    """Assessment of a parlay against the portfolio."""

    ACCEPTED = "accepted"
    CORRELATION_UNDERPRICED = "correlation_underpriced"
    """Joint probability materially exceeds the independent product.

    This is the alpha. The book multiplied the leg prices as if the legs were
    independent; they are not, so the true probability is higher than the
    price implies.
    """

    CORRELATION_OVERPRICED = "correlation_overpriced"
    """Negatively correlated legs. Looks cheap, is not. Decline."""

    REJECTED_NEGATIVE_EV = "rejected_negative_ev"
    VOID_COLLAPSED = "void_collapsed"
    """Every leg voided. Stake is returned, EV is exactly zero."""

    DEAD = "dead"
    """At least one leg has lost. Payout is zero and cannot recover."""


# --------------------------------------------------------------------------- #
# Correlation matrix handling
# --------------------------------------------------------------------------- #


def validate_correlation_matrix(
    matrix: NDArray[np.float64], *, repair: bool = True
) -> NDArray[np.float64]:
    """Validate and, if needed, repair a correlation matrix to nearest PSD.

    A correlation matrix elicited from judgement or estimated pairwise is
    routinely not positive semi-definite, and a non-PSD matrix has no valid
    Cholesky factor, so sampling from it is undefined. Repair projects onto the
    PSD cone by clipping negative eigenvalues, then renormalises the diagonal
    back to unity.

    Args:
        matrix: Square, symmetric, unit-diagonal candidate matrix.
        repair: When False, a non-PSD matrix raises instead of being repaired.

    Returns:
        A validated, PSD, unit-diagonal ``float64`` matrix.

    Raises:
        InvalidCorrelationMatrixError: Not square, not symmetric, off-diagonal
            entries outside ``[-1, 1]``, diagonal not unity, or non-PSD with
            ``repair=False``.
    """
    array = np.asarray(matrix, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != array.shape[1]:
        msg = f"correlation matrix must be square, got shape {array.shape!r}"
        raise InvalidCorrelationMatrixError(msg, shape=array.shape)
    if not np.all(np.isfinite(array)):
        msg = "correlation matrix contains non-finite entries"
        raise InvalidCorrelationMatrixError(msg)
    if not np.allclose(array, array.T, atol=_MATRIX_TOLERANCE):
        msg = "correlation matrix must be symmetric"
        raise InvalidCorrelationMatrixError(msg)
    if not np.allclose(np.diag(array), 1.0, atol=_MATRIX_TOLERANCE):
        msg = f"correlation matrix diagonal must be unity, got {np.diag(array)!r}"
        raise InvalidCorrelationMatrixError(msg)
    if np.any(np.abs(array) > 1.0 + _MATRIX_TOLERANCE):
        msg = "correlation entries must lie in [-1, 1]"
        raise InvalidCorrelationMatrixError(msg)

    eigenvalues = np.linalg.eigvalsh(array)
    minimum = float(eigenvalues.min())
    if minimum >= -_MATRIX_TOLERANCE:
        # Already PSD. Symmetrise defensively against accumulated asymmetry.
        return (array + array.T) / 2.0

    if not repair:
        msg = f"correlation matrix is not positive semi-definite (min eig {minimum!r})"
        raise InvalidCorrelationMatrixError(msg, min_eigenvalue=minimum)

    values, vectors = np.linalg.eigh((array + array.T) / 2.0)
    clipped = np.clip(values, _MIN_EIGENVALUE, None)
    reconstructed = vectors @ np.diag(clipped) @ vectors.T
    scale = np.sqrt(np.diag(reconstructed))
    if np.any(scale <= 0.0):  # pragma: no cover - clipping guarantees positivity
        msg = "PSD repair produced a non-positive variance"
        raise InvalidCorrelationMatrixError(msg)
    repaired = reconstructed / np.outer(scale, scale)
    np.fill_diagonal(repaired, 1.0)

    _log.warning(
        "correlation_matrix.repaired",
        min_eigenvalue_before=minimum,
        min_eigenvalue_after=float(np.linalg.eigvalsh(repaired).min()),
        max_absolute_change=float(np.max(np.abs(repaired - array))),
        dimension=int(array.shape[0]),
        reason="input matrix was not positive semi-definite",
    )
    return (repaired + repaired.T) / 2.0


def frechet_hoeffding_bounds(marginals: tuple[float, ...]) -> tuple[float, float]:
    """Distribution-free bounds on ``P(all events occur)``.

    Hold for **every** dependence structure, so they are the strongest
    available sanity check on any joint-probability estimator::

        lower = max(0, sum(p_i) - (n - 1))     # maximal positive dependence
        upper = min(p_i)                        # comonotone limit
    """
    if not marginals:
        msg = "marginals must be non-empty"
        raise ValueError(msg)
    n = len(marginals)
    lower = max(0.0, math.fsum(marginals) - (n - 1))
    upper = min(marginals)
    return lower, min(upper, 1.0)


# --------------------------------------------------------------------------- #
# Copula estimation
# --------------------------------------------------------------------------- #


class CopulaEstimate(BaseModel):
    """A joint probability estimate with its precision and its bounds."""

    model_config = _STRICT

    joint_probability: float = Field(ge=0.0, le=1.0)
    independent_product: float = Field(ge=0.0, le=1.0)
    method: CopulaMethod
    standard_error: float = Field(ge=0.0)
    ci_low: float = Field(ge=0.0, le=1.0)
    ci_high: float = Field(ge=0.0, le=1.0)
    n_samples: int = Field(ge=0)
    frechet_lower: float = Field(ge=0.0, le=1.0)
    frechet_upper: float = Field(ge=0.0, le=1.0)
    was_clipped_to_bounds: bool = False

    @property
    def dependence_ratio(self) -> float:
        """Joint over independent product. Above 1.0 means positive dependence."""
        if self.independent_product <= _PROB_TOLERANCE:
            return 1.0
        return self.joint_probability / self.independent_product

    @model_validator(mode="after")
    def _within_bounds(self) -> Self:
        if self.joint_probability < self.frechet_lower - 1e-6:
            msg = "joint probability violates the Frechet-Hoeffding lower bound"
            raise ValueError(msg)
        if self.joint_probability > self.frechet_upper + 1e-6:
            msg = "joint probability violates the Frechet-Hoeffding upper bound"
            raise ValueError(msg)
        return self


def _thresholds(marginals: tuple[float, ...]) -> NDArray[np.float64]:
    clipped = np.clip(np.asarray(marginals, dtype=np.float64), 1e-12, 1.0 - 1e-12)
    return np.asarray(norm.ppf(clipped), dtype=np.float64)


def exact_gaussian_copula_joint_probability(
    marginals: tuple[float, ...], correlation: NDArray[np.float64]
) -> CopulaEstimate:
    """Analytic Gaussian-copula joint probability via the multivariate normal CDF.

    Exact up to the numerical tolerance of ``scipy``'s MVN integrator, which is
    excellent for dimension 3 and below. Used as the production path for short
    parlays and as the correctness oracle for the Monte Carlo estimator.

    Raises:
        ValueError: Dimension mismatch, or dimension above
            :data:`_EXACT_CDF_MAX_DIM`.
    """
    n = len(marginals)
    matrix = validate_correlation_matrix(correlation)
    if matrix.shape[0] != n:
        msg = f"correlation matrix dimension {matrix.shape[0]} != {n} marginals"
        raise ValueError(msg)
    if n > _EXACT_CDF_MAX_DIM:
        msg = (
            f"exact MVN CDF is only offered up to dimension {_EXACT_CDF_MAX_DIM}; "
            "use gaussian_copula_joint_probability for larger parlays"
        )
        raise ValueError(msg)

    independent = float(np.prod(np.asarray(marginals, dtype=np.float64)))
    lower, upper = frechet_hoeffding_bounds(marginals)

    if n == 1:
        return CopulaEstimate(
            joint_probability=marginals[0],
            independent_product=independent,
            method=CopulaMethod.DEGENERATE_SINGLE_LEG,
            standard_error=0.0,
            ci_low=marginals[0],
            ci_high=marginals[0],
            n_samples=0,
            frechet_lower=lower,
            frechet_upper=upper,
        )

    raw = float(
        multivariate_normal(mean=np.zeros(n), cov=matrix, allow_singular=True).cdf(
            _thresholds(marginals)
        )
    )
    joint = min(max(raw, lower), upper)
    return CopulaEstimate(
        joint_probability=joint,
        independent_product=independent,
        method=CopulaMethod.EXACT_MVN,
        standard_error=0.0,
        ci_low=joint,
        ci_high=joint,
        n_samples=0,
        frechet_lower=lower,
        frechet_upper=upper,
        was_clipped_to_bounds=not math.isclose(raw, joint, abs_tol=1e-9),
    )


def gaussian_copula_joint_probability(
    marginals: tuple[float, ...],
    correlation: NDArray[np.float64],
    *,
    n_samples: int = 200_000,
    seed: int = 20240101,
) -> CopulaEstimate:
    """Monte Carlo Gaussian-copula joint probability with an explicit CI.

    The seed is a **required, explicit** parameter rather than a default global
    draw, because a risk figure that changes between two runs of the same
    inputs is not auditable.

    Args:
        marginals: Fair (devigged) win probability of each leg.
        correlation: Latent-normal correlation matrix. Repaired to PSD if needed.
        n_samples: Draw count. Standard error scales as ``1/sqrt(n)``, so
            resolving a joint probability of 0.05 to +/- 0.001 needs roughly
            200k draws.
        seed: PRNG seed. Fixed for determinism.

    Returns:
        A :class:`CopulaEstimate` carrying the point estimate, its standard
        error, a 95% interval, and the Frechet-Hoeffding bounds it was checked
        against.
    """
    n = len(marginals)
    if n == 0:
        msg = "marginals must be non-empty"
        raise ValueError(msg)
    if n_samples < 1_000:
        msg = f"n_samples must be at least 1000 for a usable CI, got {n_samples}"
        raise ValueError(msg)

    matrix = validate_correlation_matrix(correlation)
    if matrix.shape[0] != n:
        msg = f"correlation matrix dimension {matrix.shape[0]} != {n} marginals"
        raise ValueError(msg)

    independent = float(np.prod(np.asarray(marginals, dtype=np.float64)))
    lower, upper = frechet_hoeffding_bounds(marginals)

    if n == 1:
        return CopulaEstimate(
            joint_probability=marginals[0],
            independent_product=independent,
            method=CopulaMethod.DEGENERATE_SINGLE_LEG,
            standard_error=0.0,
            ci_low=marginals[0],
            ci_high=marginals[0],
            n_samples=0,
            frechet_lower=lower,
            frechet_upper=upper,
        )

    try:
        factor = cholesky(matrix, lower=True)
    except np.linalg.LinAlgError:
        # Semi-definite after repair: fall back to the eigen square root.
        values, vectors = np.linalg.eigh(matrix)
        factor = vectors @ np.diag(np.sqrt(np.clip(values, 0.0, None)))
        _log.info("copula.cholesky_fallback", dimension=n, reason="matrix is singular")

    rng = np.random.default_rng(seed)
    latent = rng.standard_normal(size=(n_samples, n)) @ factor.T
    hits = int(np.count_nonzero(np.all(latent <= _thresholds(marginals), axis=1)))

    raw = hits / n_samples
    standard_error = math.sqrt(max(raw * (1.0 - raw), 0.0) / n_samples)
    joint = min(max(raw, lower), upper)
    was_clipped = not math.isclose(raw, joint, abs_tol=1e-12)
    if was_clipped:
        _log.warning(
            "copula.clipped_to_frechet_bounds",
            raw_estimate=raw,
            clipped_to=joint,
            frechet_lower=lower,
            frechet_upper=upper,
            n_samples=n_samples,
            reason="monte carlo noise pushed the estimate outside distribution-free bounds",
        )

    return CopulaEstimate(
        joint_probability=joint,
        independent_product=independent,
        method=CopulaMethod.MONTE_CARLO,
        standard_error=standard_error,
        ci_low=min(max(raw - 1.96 * standard_error, lower), upper),
        ci_high=min(max(raw + 1.96 * standard_error, lower), upper),
        n_samples=n_samples,
        frechet_lower=lower,
        frechet_upper=upper,
        was_clipped_to_bounds=was_clipped,
    )


# --------------------------------------------------------------------------- #
# Parlay model
# --------------------------------------------------------------------------- #


class ParlayLeg(BaseModel):
    """One leg of a parlay, with its own settlement state and failure driver."""

    model_config = _STRICT

    leg_id: str = Field(min_length=1, max_length=64)
    market_key: str = Field(min_length=1, max_length=128)
    marginal_probability: float = Field(gt=0.0, lt=1.0)
    decimal_odds: float = Field(gt=1.0, le=10_000.0)
    void_probability: float = Field(
        default=0.005,
        ge=0.0,
        lt=1.0,
        description="Postponement, abandonment or rule-based void risk.",
    )
    state: LegState = LegState.PENDING
    risk_factor_key: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "The underlying adverse event that makes this leg lose, e.g. "
            "'TEAM_A_LOSES'. Legs sharing a key share a single failure point, "
            "which is what the exposure matrix aggregates on."
        ),
    )

    @property
    def implied_probability(self) -> float:
        """Gross probability implied by the leg price, margin included."""
        return 1.0 / self.decimal_odds

    def with_state(self, state: LegState) -> ParlayLeg:
        """Return a new leg with the given state. The original is untouched."""
        return self.model_copy(update={"state": state})


class Parlay(BaseModel):
    """An immutable multi-leg bet. Settlement produces a new instance."""

    model_config = _STRICT

    parlay_id: str = Field(min_length=1, max_length=64)
    legs: tuple[ParlayLeg, ...] = Field(min_length=1, max_length=12)
    stake_paise: int = Field(gt=0)

    @model_validator(mode="after")
    def _unique_leg_ids(self) -> Self:
        ids = [leg.leg_id for leg in self.legs]
        if len(set(ids)) != len(ids):
            msg = f"duplicate leg_id in parlay {self.parlay_id!r}"
            raise ValueError(msg)
        return self

    # ------------------------------- state ------------------------------ #

    @property
    def is_dead(self) -> bool:
        return any(leg.state.kills_parlay for leg in self.legs)

    @property
    def pending_legs(self) -> tuple[ParlayLeg, ...]:
        return tuple(leg for leg in self.legs if leg.state is LegState.PENDING)

    @property
    def void_legs(self) -> tuple[ParlayLeg, ...]:
        return tuple(leg for leg in self.legs if leg.state is LegState.VOID)

    @property
    def is_fully_void(self) -> bool:
        return len(self.void_legs) == len(self.legs)

    @property
    def is_settled(self) -> bool:
        return self.is_dead or all(leg.state.is_terminal for leg in self.legs)

    # -------------------------------- pricing --------------------------- #

    @property
    def current_offered_odds_exact(self) -> Decimal:
        """Payout multiplier as an exact ``Decimal``.

        Computed by multiplying ``Decimal(str(...))`` per leg rather than
        multiplying floats and converting afterwards. Float multiplication of
        2.10 * 1.80 * 1.90 yields 7.181999999999999, which floors to one paisa
        short of the true payout. Money must never inherit that error.
        """
        odds = Decimal(1)
        for leg in self.legs:
            if leg.state.contributes_odds:
                odds *= Decimal(str(leg.decimal_odds))
        return odds

    @property
    def current_offered_odds(self) -> float:
        """Float view of :attr:`current_offered_odds_exact`, for the copula only."""
        return float(self.current_offered_odds_exact)

    @property
    def naive_independent_probability(self) -> float:
        """Product of pending marginals. What the book implicitly assumed."""
        pending = self.pending_legs
        if not pending:
            return 1.0
        return float(np.prod([leg.marginal_probability for leg in pending]))

    @property
    def potential_payout_paise(self) -> int:
        """Gross return if every surviving leg wins. Rounds DOWN."""
        if self.is_dead:
            return 0
        if self.is_fully_void:
            return self.stake_paise
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            payout = Decimal(self.stake_paise) * self.current_offered_odds_exact
        return int(payout.to_integral_value(rounding=ROUND_DOWN))

    @property
    def capital_at_risk_paise(self) -> int:
        """Stake still exposed. Zero once the parlay is dead or fully void."""
        if self.is_dead or self.is_fully_void:
            return 0
        return self.stake_paise

    def leg(self, leg_id: str) -> ParlayLeg:
        for candidate in self.legs:
            if candidate.leg_id == leg_id:
                return candidate
        msg = f"leg_id {leg_id!r} not present in parlay {self.parlay_id!r}"
        raise KeyError(msg)

    def pending_correlation_submatrix(
        self, correlation: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        """Extract the correlation block for the still-pending legs only."""
        matrix = np.asarray(correlation, dtype=np.float64)
        if matrix.shape[0] != len(self.legs):
            msg = (
                f"correlation matrix dimension {matrix.shape[0]} does not match "
                f"{len(self.legs)} legs"
            )
            raise ValueError(msg)
        indices = [i for i, leg in enumerate(self.legs) if leg.state is LegState.PENDING]
        if not indices:
            return np.ones((0, 0), dtype=np.float64)
        return matrix[np.ix_(indices, indices)]


# --------------------------------------------------------------------------- #
# EV and settlement
# --------------------------------------------------------------------------- #


def parlay_ev(parlay: Parlay, joint_probability: float) -> Decimal:
    """EV per unit staked, conditional on the legs already settled.

    Args:
        parlay: The parlay in its current state.
        joint_probability: ``P(all pending legs win)``, from a copula estimate.

    Returns:
        Exact ``Decimal`` EV per unit staked. Zero for a fully void parlay
        (stake returned), ``-1`` for a dead parlay (stake lost).
    """
    if parlay.is_dead:
        return Decimal(-1)
    if parlay.is_fully_void:
        return Decimal(0)
    if not 0.0 <= joint_probability <= 1.0:
        msg = f"joint_probability must be in [0, 1], got {joint_probability!r}"
        raise ValueError(msg)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        p = Decimal(str(joint_probability))
        o = Decimal(str(parlay.current_offered_odds))
        return (p * o - Decimal(1)).quantize(_EV_QUANT)


def expected_value_with_void_risk(
    parlay: Parlay,
    correlation: NDArray[np.float64],
    *,
    n_samples: int = 100_000,
    seed: int = 20240101,
) -> Decimal:
    """Full EV per unit staked, enumerating every void pattern.

    Decomposition: condition on which legs void (``2^k`` patterns over the
    ``k`` pending legs, independent per the module docstring), then within each
    pattern compute ``P(all surviving legs win)`` via the copula on the
    corresponding correlation sub-block, and re-price the payout on the
    surviving legs only::

        EV = sum over void patterns  P(pattern) * P_copula(survivors all win)
                                     * prod(odds of survivors)   -   1

    This is the calculation that a two-state parlay model cannot express, and
    it is materially different from the naive figure whenever void probability
    is non-trivial (player props, weather-exposed fixtures, cup ties).

    Raises:
        ValueError: More pending legs than :data:`MAX_VOID_ENUMERATION_LEGS`,
            which would make the enumeration cost explode.
    """
    if parlay.is_dead:
        return Decimal(-1)

    pending = parlay.pending_legs
    if not pending:
        return parlay_ev(parlay, 1.0)
    if len(pending) > MAX_VOID_ENUMERATION_LEGS:
        msg = (
            f"void enumeration supports at most {MAX_VOID_ENUMERATION_LEGS} pending "
            f"legs, got {len(pending)}; sample the void pattern instead"
        )
        raise ValueError(msg)

    submatrix = parlay.pending_correlation_submatrix(correlation)
    # Odds already banked from legs that have won or that voided.
    settled_odds = 1.0
    for leg in parlay.legs:
        if leg.state is LegState.WIN:
            settled_odds *= leg.decimal_odds

    expected_return = 0.0
    for pattern in iter_product((False, True), repeat=len(pending)):
        pattern_probability = 1.0
        survivor_indices: list[int] = []
        survivor_marginals: list[float] = []
        survivor_odds = 1.0
        for position, (leg, is_void) in enumerate(zip(pending, pattern, strict=True)):
            if is_void:
                pattern_probability *= leg.void_probability
            else:
                pattern_probability *= 1.0 - leg.void_probability
                survivor_indices.append(position)
                survivor_marginals.append(leg.marginal_probability)
                survivor_odds *= leg.decimal_odds

        if pattern_probability <= _PROB_TOLERANCE:
            continue

        if not survivor_indices:
            # Everything voided: stake is returned at odds 1.0.
            expected_return += pattern_probability * settled_odds
            continue

        block = submatrix[np.ix_(survivor_indices, survivor_indices)]
        marginals = tuple(survivor_marginals)
        estimate = (
            exact_gaussian_copula_joint_probability(marginals, block)
            if len(marginals) <= _EXACT_CDF_MAX_DIM
            else gaussian_copula_joint_probability(marginals, block, n_samples=n_samples, seed=seed)
        )
        expected_return += (
            pattern_probability * estimate.joint_probability * survivor_odds * settled_odds
        )

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        return (Decimal(str(expected_return)) - Decimal(1)).quantize(_EV_QUANT)


class SettlementSnapshot(BaseModel):
    """The recomputed risk picture after one leg settles."""

    model_config = _STRICT

    parlay: Parlay
    verdict: ParlayVerdict
    current_offered_odds: float = Field(gt=0.0)
    remaining_joint_probability: float = Field(ge=0.0, le=1.0)
    conditional_ev_per_unit: Decimal
    potential_payout_paise: int = Field(ge=0)
    capital_at_risk_paise: int = Field(ge=0)
    pending_leg_count: int = Field(ge=0)

    @property
    def conditional_ev_inr(self) -> Decimal:
        """EV in rupees on the actual stake, not per unit."""
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            return (from_paise(self.parlay.stake_paise) * self.conditional_ev_per_unit).quantize(
                Decimal("0.01")
            )


def recalculate_on_settlement(
    parlay: Parlay,
    leg_id: str,
    new_state: LegState,
    correlation: NDArray[np.float64],
    *,
    n_samples: int = 100_000,
    seed: int = 20240101,
) -> SettlementSnapshot:
    """Apply one leg settlement and recompute the parlay's entire risk profile.

    The spec's exact case works as follows. Leg 1 WIN, Leg 2 VOID, Leg 3
    PENDING gives ``current_offered_odds = o1 * 1.0 * o3`` (the void leg drops
    out), ``remaining_joint_probability = p3`` (only one leg still live), and
    ``conditional_ev_per_unit = p3 * o1 * o3 - 1``.

    **Idempotent**: settling a leg into the state it already holds returns an
    equal :class:`Parlay`, so a duplicated settlement message cannot corrupt
    state. This is a required property test.

    Args:
        parlay: Current parlay. Never mutated.
        leg_id: Leg being settled.
        new_state: Terminal state to apply. PENDING is rejected.
        correlation: Full-size correlation matrix over all legs.

    Returns:
        A :class:`SettlementSnapshot` holding a new frozen parlay plus the
        recomputed metrics.

    Raises:
        ValueError: ``new_state`` is PENDING, or the leg is already terminal in
            a different state (an illegal transition, which indicates a
            duplicate settlement from a different source and must be
            investigated rather than silently overwritten).
    """
    if new_state is LegState.PENDING:
        msg = "cannot settle a leg back into PENDING"
        raise ValueError(msg)

    existing = parlay.leg(leg_id)
    if existing.state.is_terminal and existing.state is not new_state:
        _log.error(
            "parlay.illegal_settlement_transition",
            parlay_id=parlay.parlay_id,
            leg_id=leg_id,
            current_state=existing.state.value,
            attempted_state=new_state.value,
            reason="conflicting terminal settlements for the same leg",
        )
        msg = (
            f"leg {leg_id!r} is already terminal in state {existing.state.value!r}; "
            f"refusing to overwrite with {new_state.value!r}"
        )
        raise ValueError(msg)

    updated = parlay.model_copy(
        update={
            "legs": tuple(
                leg.with_state(new_state) if leg.leg_id == leg_id else leg for leg in parlay.legs
            )
        }
    )

    pending = updated.pending_legs
    if updated.is_dead:
        verdict = ParlayVerdict.DEAD
        joint = 0.0
        ev = Decimal(-1)
    elif updated.is_fully_void:
        verdict = ParlayVerdict.VOID_COLLAPSED
        joint = 1.0
        ev = Decimal(0)
    elif not pending:
        verdict = ParlayVerdict.ACCEPTED
        joint = 1.0
        ev = parlay_ev(updated, 1.0)
    else:
        marginals = tuple(leg.marginal_probability for leg in pending)
        block = updated.pending_correlation_submatrix(correlation)
        estimate = (
            exact_gaussian_copula_joint_probability(marginals, block)
            if len(marginals) <= _EXACT_CDF_MAX_DIM
            else gaussian_copula_joint_probability(marginals, block, n_samples=n_samples, seed=seed)
        )
        joint = estimate.joint_probability
        ev = parlay_ev(updated, joint)
        if ev <= 0:
            verdict = ParlayVerdict.REJECTED_NEGATIVE_EV
        elif estimate.dependence_ratio > 1.05:
            verdict = ParlayVerdict.CORRELATION_UNDERPRICED
        elif estimate.dependence_ratio < 0.95:
            verdict = ParlayVerdict.CORRELATION_OVERPRICED
        else:
            verdict = ParlayVerdict.ACCEPTED

    _log.info(
        "parlay.settled",
        parlay_id=parlay.parlay_id,
        leg_id=leg_id,
        new_state=new_state.value,
        verdict=verdict.value,
        current_offered_odds=updated.current_offered_odds,
        remaining_joint_probability=joint,
        pending_leg_count=len(pending),
        capital_at_risk_paise=updated.capital_at_risk_paise,
    )

    return SettlementSnapshot(
        parlay=updated,
        verdict=verdict,
        current_offered_odds=updated.current_offered_odds,
        remaining_joint_probability=joint,
        conditional_ev_per_unit=ev,
        potential_payout_paise=updated.potential_payout_paise,
        capital_at_risk_paise=updated.capital_at_risk_paise,
        pending_leg_count=len(pending),
    )


# --------------------------------------------------------------------------- #
# Exposure concentration
# --------------------------------------------------------------------------- #


class ExposureRow(BaseModel):
    """Aggregate exposure to one underlying failure driver."""

    model_config = _STRICT

    risk_factor_key: str = Field(min_length=1)
    worst_case_loss_paise: int = Field(ge=0)
    parlay_ids: tuple[str, ...]
    exposure_fraction: float = Field(ge=0.0)

    @property
    def worst_case_loss_inr(self) -> Decimal:
        return from_paise(self.worst_case_loss_paise)


class ExposureMatrix(BaseModel):
    """Portfolio exposure aggregated by single point of failure.

    Twenty parlays at 0.5% of bankroll each look diversified. If every one of
    them contains "Team A to win", the portfolio is a single 10% bet wearing a
    disguise. This matrix is what makes that visible.
    """

    model_config = _STRICT

    bankroll_paise: int = Field(gt=0)
    rows: tuple[ExposureRow, ...]

    @property
    def worst_row(self) -> ExposureRow | None:
        return max(self.rows, key=lambda r: r.worst_case_loss_paise, default=None)

    def row(self, risk_factor_key: str) -> ExposureRow | None:
        return next((r for r in self.rows if r.risk_factor_key == risk_factor_key), None)

    @classmethod
    def build(cls, parlays: tuple[Parlay, ...], bankroll_paise: int) -> ExposureMatrix:
        """Aggregate live parlays by risk factor.

        A parlay contributes its **entire** stake to every risk factor it
        touches through a still-live leg, because any one of those factors
        realising adversely kills the whole parlay. That is the correct
        worst-case treatment and it is deliberately not netted across factors.
        """
        if bankroll_paise <= 0:
            msg = f"bankroll_paise must be positive, got {bankroll_paise}"
            raise ValueError(msg)

        buckets: dict[str, list[Parlay]] = {}
        for parlay in parlays:
            if parlay.is_dead or parlay.is_fully_void:
                continue
            live_factors = {
                leg.risk_factor_key for leg in parlay.legs if leg.state is LegState.PENDING
            }
            for factor in live_factors:
                buckets.setdefault(factor, []).append(parlay)

        rows = tuple(
            sorted(
                (
                    ExposureRow(
                        risk_factor_key=factor,
                        worst_case_loss_paise=sum(p.capital_at_risk_paise for p in group),
                        parlay_ids=tuple(p.parlay_id for p in group),
                        exposure_fraction=(
                            sum(p.capital_at_risk_paise for p in group) / bankroll_paise
                        ),
                    )
                    for factor, group in buckets.items()
                ),
                key=lambda r: (-r.worst_case_loss_paise, r.risk_factor_key),
            )
        )
        return cls(bankroll_paise=bankroll_paise, rows=rows)


def check_exposure_limit(
    existing: tuple[Parlay, ...],
    candidate: Parlay,
    bankroll_paise: int,
    *,
    max_single_point_drawdown: float = 0.08,
) -> ExposureMatrix:
    """Admit or refuse a new parlay on single-point concentration grounds.

    Args:
        existing: All live parlays currently in the portfolio.
        candidate: The proposed new parlay.
        bankroll_paise: Total bankroll in integer paise.
        max_single_point_drawdown: Hard limit on the fraction of bankroll that
            any one risk factor may put at risk. Default 0.08 per the spec.

    Returns:
        The post-admission :class:`ExposureMatrix` when the candidate is safe.

    Raises:
        ExposureConcentrationError: Admitting the candidate would push at least
            one risk factor beyond the limit. The error names the offending
            factor, so the caller can tell the user *why*, not just "no".
    """
    if not 0.0 < max_single_point_drawdown <= 1.0:
        msg = f"max_single_point_drawdown must be in (0, 1], got {max_single_point_drawdown!r}"
        raise ValueError(msg)

    projected = ExposureMatrix.build((*existing, candidate), bankroll_paise)
    limit_paise = int(bankroll_paise * max_single_point_drawdown)

    breaches = [row for row in projected.rows if row.worst_case_loss_paise > limit_paise]
    if breaches:
        worst = max(breaches, key=lambda r: r.worst_case_loss_paise)
        _log.error(
            "parlay.rejected",
            reason="exposure_concentration",
            parlay_id=candidate.parlay_id,
            risk_factor_key=worst.risk_factor_key,
            worst_case_loss_paise=worst.worst_case_loss_paise,
            limit_paise=limit_paise,
            exposure_fraction=worst.exposure_fraction,
            limit_fraction=max_single_point_drawdown,
            contributing_parlays=list(worst.parlay_ids),
            breached_factor_count=len(breaches),
        )
        msg = (
            f"admitting parlay {candidate.parlay_id!r} would concentrate "
            f"{worst.exposure_fraction:.2%} of bankroll on a single failure point "
            f"({worst.risk_factor_key!r})"
        )
        raise ExposureConcentrationError(
            msg,
            risk_factor_key=worst.risk_factor_key,
            exposure_fraction=worst.exposure_fraction,
            limit_fraction=max_single_point_drawdown,
            worst_case_loss_paise=worst.worst_case_loss_paise,
        )

    _log.info(
        "parlay.exposure_accepted",
        parlay_id=candidate.parlay_id,
        risk_factor_count=len(projected.rows),
        worst_exposure_fraction=(
            projected.worst_row.exposure_fraction if projected.worst_row else 0.0
        ),
        limit_fraction=max_single_point_drawdown,
    )
    return projected
