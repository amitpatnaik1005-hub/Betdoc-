"""Tests for the Correlated Parlay Escalation gate."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from numpy.typing import NDArray

from domain.math.errors import (
    ExposureConcentrationError,
    InvalidCorrelationMatrixError,
)
from domain.math.parlay_correlation import (
    MAX_VOID_ENUMERATION_LEGS,
    CopulaMethod,
    ExposureMatrix,
    LegState,
    Parlay,
    ParlayVerdict,
    check_exposure_limit,
    exact_gaussian_copula_joint_probability,
    expected_value_with_void_risk,
    frechet_hoeffding_bounds,
    gaussian_copula_joint_probability,
    parlay_ev,
    recalculate_on_settlement,
    validate_correlation_matrix,
)
from tests.unit.domain.math.conftest import (
    make_leg,
    psd_correlation_matrices,
    rough_correlation_matrices,
)

_SEED = 20240101


def _identity(n: int) -> NDArray[np.float64]:
    return np.eye(n, dtype=np.float64)


def _uniform_correlation(n: int, rho: float) -> NDArray[np.float64]:
    matrix = np.full((n, n), rho, dtype=np.float64)
    np.fill_diagonal(matrix, 1.0)
    return matrix


# --------------------------------------------------------------------------- #
# Property: Frechet-Hoeffding containment
# --------------------------------------------------------------------------- #


@given(
    marginals=st.lists(
        st.floats(0.05, 0.95, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=3,
    ),
    rho=st.floats(-0.5, 0.95, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=120)
def test_copula_respects_frechet_hoeffding_bounds(
    marginals: list[float], rho: float
) -> None:
    """The strongest available invariant on any joint-probability estimator.

    These bounds hold for **every** dependence structure, so a violation means
    the estimator or the matrix is broken, never that the market is unusual.
    """
    tuple_marginals = tuple(marginals)
    n = len(tuple_marginals)
    estimate = gaussian_copula_joint_probability(
        tuple_marginals, _uniform_correlation(n, rho), n_samples=20_000, seed=_SEED
    )
    lower, upper = frechet_hoeffding_bounds(tuple_marginals)

    assert lower - 1e-9 <= estimate.joint_probability <= upper + 1e-9
    assert estimate.frechet_lower == pytest.approx(lower)
    assert estimate.frechet_upper == pytest.approx(upper)


@given(
    marginals=st.lists(
        st.floats(0.1, 0.9, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=4,
    )
)
def test_frechet_bounds_are_ordered_and_in_unit_interval(
    marginals: list[float],
) -> None:
    lower, upper = frechet_hoeffding_bounds(tuple(marginals))
    assert 0.0 <= lower <= upper <= 1.0


# --------------------------------------------------------------------------- #
# Correctness: Monte Carlo against the exact MVN oracle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rho", [-0.4, 0.0, 0.3, 0.7])
def test_monte_carlo_converges_to_the_exact_mvn_value(rho: float) -> None:
    """The correctness proof for the copula sampler.

    ``scipy``'s multivariate-normal CDF is an independent implementation of the
    same orthant probability. If the sampler agrees with it inside three
    standard errors across a spread of correlations, the Cholesky factoring,
    the threshold mapping and the counting logic are all correct.
    """
    marginals = (0.50, 0.58, 0.55)
    matrix = _uniform_correlation(3, rho)

    exact = exact_gaussian_copula_joint_probability(marginals, matrix)
    sampled = gaussian_copula_joint_probability(
        marginals, matrix, n_samples=400_000, seed=_SEED
    )

    assert exact.method is CopulaMethod.EXACT_MVN
    assert sampled.method is CopulaMethod.MONTE_CARLO
    assert sampled.standard_error > 0.0
    assert abs(sampled.joint_probability - exact.joint_probability) < (
        3.0 * sampled.standard_error + 1e-4
    )
    assert sampled.ci_low <= exact.joint_probability <= sampled.ci_high


def test_zero_correlation_reproduces_the_independent_product() -> None:
    """The book's implicit assumption, recovered as a special case."""
    marginals = (0.50, 0.58, 0.55)
    estimate = gaussian_copula_joint_probability(
        marginals, _identity(3), n_samples=400_000, seed=_SEED
    )
    product = 0.50 * 0.58 * 0.55

    assert estimate.independent_product == pytest.approx(product, rel=1e-12)
    assert estimate.joint_probability == pytest.approx(
        product, abs=3.0 * estimate.standard_error + 1e-4
    )
    assert estimate.dependence_ratio == pytest.approx(1.0, abs=0.02)


def test_perfect_positive_correlation_equals_the_minimum_marginal() -> None:
    """The comonotone limit, and the Frechet upper bound, are the same point.

    Also exercises the singular-matrix path: ``rho = 1`` gives a rank-one
    matrix whose Cholesky fails, so the eigen square-root fallback must engage.
    """
    marginals = (0.40, 0.55, 0.70)
    estimate = gaussian_copula_joint_probability(
        marginals, _uniform_correlation(3, 1.0), n_samples=200_000, seed=_SEED
    )
    assert estimate.joint_probability == pytest.approx(min(marginals), abs=5e-3)
    assert estimate.dependence_ratio > 1.0, "comonotone must beat independence"


def test_mutually_exclusive_legs_have_zero_joint_probability() -> None:
    """Perfectly negatively correlated legs summing below 1 can never co-occur."""
    marginals = (0.40, 0.50)
    estimate = gaussian_copula_joint_probability(
        marginals, _uniform_correlation(2, -1.0), n_samples=200_000, seed=_SEED
    )
    lower, _ = frechet_hoeffding_bounds(marginals)

    assert lower == 0.0
    assert estimate.joint_probability == pytest.approx(0.0, abs=2e-3)


def test_positive_correlation_is_flagged_as_underpriced(
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """The actual alpha in same-game parlays.

    The book multiplies leg prices as if independent. Positively correlated
    legs therefore have a true joint probability above the priced one, and the
    dependence ratio is the size of the mispricing.
    """
    marginals = (0.50, 0.58, 0.55)
    estimate = gaussian_copula_joint_probability(
        marginals, positive_correlation_matrix, n_samples=400_000, seed=_SEED
    )
    assert estimate.joint_probability > estimate.independent_product
    assert estimate.dependence_ratio > 1.05


def test_copula_is_deterministic_for_a_fixed_seed() -> None:
    """A risk figure that changes between identical runs is not auditable."""
    marginals = (0.45, 0.62)
    matrix = _uniform_correlation(2, 0.3)
    first = gaussian_copula_joint_probability(
        marginals, matrix, n_samples=50_000, seed=7
    )
    second = gaussian_copula_joint_probability(
        marginals, matrix, n_samples=50_000, seed=7
    )
    assert first.joint_probability == second.joint_probability


# --------------------------------------------------------------------------- #
# Correlation matrix validation and repair
# --------------------------------------------------------------------------- #


@given(matrix=psd_correlation_matrices(3))
def test_psd_matrices_pass_through_unchanged(matrix: NDArray[np.float64]) -> None:
    validated = validate_correlation_matrix(matrix)
    assert np.allclose(validated, matrix, atol=1e-8)
    assert np.allclose(np.diag(validated), 1.0)


@given(matrix=rough_correlation_matrices(4))
@settings(max_examples=80)
def test_repair_always_yields_a_valid_psd_correlation_matrix(
    matrix: NDArray[np.float64],
) -> None:
    """Repair is a production path. Pairwise-elicited matrices are rarely PSD."""
    repaired = validate_correlation_matrix(matrix, repair=True)

    assert np.allclose(repaired, repaired.T, atol=1e-10)
    assert np.allclose(np.diag(repaired), 1.0, atol=1e-10)
    assert float(np.linalg.eigvalsh(repaired).min()) >= -1e-9
    assert np.all(np.abs(repaired) <= 1.0 + 1e-9)


def test_known_non_psd_matrix_is_repaired_not_rejected() -> None:
    """The classic inconsistent triple: A and B both love C, but hate each other."""
    matrix = np.array(
        [[1.0, 0.9, 0.9], [0.9, 1.0, -0.9], [0.9, -0.9, 1.0]], dtype=np.float64
    )
    assert float(np.linalg.eigvalsh(matrix).min()) < -1e-6

    repaired = validate_correlation_matrix(matrix, repair=True)
    assert float(np.linalg.eigvalsh(repaired).min()) >= -1e-9

    with pytest.raises(InvalidCorrelationMatrixError, match="positive semi-definite"):
        validate_correlation_matrix(matrix, repair=False)


@pytest.mark.parametrize(
    ("matrix", "match"),
    [
        (np.array([[1.0, 0.5]], dtype=np.float64), "square"),
        (np.array([[1.0, 0.5], [0.2, 1.0]], dtype=np.float64), "symmetric"),
        (np.array([[0.9, 0.0], [0.0, 1.0]], dtype=np.float64), "unity"),
        (np.array([[1.0, np.nan], [np.nan, 1.0]], dtype=np.float64), "non-finite"),
    ],
)
def test_malformed_matrices_are_rejected(
    matrix: NDArray[np.float64], match: str
) -> None:
    with pytest.raises(InvalidCorrelationMatrixError, match=match):
        validate_correlation_matrix(matrix)


# --------------------------------------------------------------------------- #
# Golden: exact payout arithmetic and the WIN / VOID / PENDING case
# --------------------------------------------------------------------------- #


def test_full_parlay_payout_is_exact_to_the_paisa(
    three_leg_correlated_parlay: Parlay,
) -> None:
    """The one-paisa test.

    ``1.0 * 2.10 * 1.80 * 1.90`` in float64 is ``7.181999999999999``, which
    floors a INR 1,000 stake to 718,199 paise. In ``Decimal`` it is exactly
    ``7.182``, giving 718,200. This assertion is the reason
    ``current_offered_odds_exact`` exists.
    """
    parlay = three_leg_correlated_parlay

    assert parlay.current_offered_odds_exact == Decimal("7.182")
    assert parlay.potential_payout_paise == 718_200
    assert parlay.capital_at_risk_paise == 100_000
    assert parlay.naive_independent_probability == pytest.approx(
        0.50 * 0.58 * 0.55, rel=1e-12
    )


def test_leg_win_then_void_then_pending_reprices_exactly(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """The spec's exact settlement scenario, asserted to the paisa.

    Leg 1 WIN, Leg 2 VOID, Leg 3 PENDING. The void leg re-prices to 1.0 and
    drops out of the product, so::

        offered odds  = 2.10 * 1.90                = 3.99   (exact in Decimal)
        remaining P   = p3                         = 0.55
        EV per unit   = 0.55 * 3.99 - 1            = 1.1945
        payout        = 100,000 paise * 3.99       = 399,000 paise
    """
    after_win = recalculate_on_settlement(
        three_leg_correlated_parlay,
        "leg_win",
        LegState.WIN,
        positive_correlation_matrix,
    )
    after_void = recalculate_on_settlement(
        after_win.parlay, "leg_over", LegState.VOID, positive_correlation_matrix
    )

    parlay = after_void.parlay
    assert parlay.leg("leg_win").state is LegState.WIN
    assert parlay.leg("leg_over").state is LegState.VOID
    assert parlay.leg("leg_btts").state is LegState.PENDING
    assert after_void.pending_leg_count == 1

    assert parlay.current_offered_odds_exact == Decimal("3.99")
    assert parlay.potential_payout_paise == 399_000
    assert parlay.capital_at_risk_paise == 100_000

    assert after_void.remaining_joint_probability == pytest.approx(0.55, abs=1e-12)
    assert after_void.conditional_ev_per_unit == pytest.approx(
        Decimal("1.1945"), abs=Decimal("0.0000001")
    )
    assert after_void.conditional_ev_inr == Decimal("1194.50")
    assert after_void.verdict is not ParlayVerdict.DEAD


def test_single_pending_leg_uses_the_exact_marginal_not_the_copula(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """With one leg live there is no dependence left to model.

    The joint probability must equal the marginal exactly, with zero Monte
    Carlo error, which is what :attr:`CopulaMethod.DEGENERATE_SINGLE_LEG` is for.
    """
    step = recalculate_on_settlement(
        three_leg_correlated_parlay, "leg_win", LegState.WIN, positive_correlation_matrix
    )
    step = recalculate_on_settlement(
        step.parlay, "leg_over", LegState.WIN, positive_correlation_matrix
    )
    assert step.remaining_joint_probability == pytest.approx(0.55, abs=1e-12)


# --------------------------------------------------------------------------- #
# Void semantics
# --------------------------------------------------------------------------- #


@given(
    state=st.sampled_from([LegState.WIN, LegState.LOSE, LegState.VOID]),
    leg_id=st.sampled_from(["leg_win", "leg_over", "leg_btts"]),
)
@settings(max_examples=10, suppress_health_check=list(HealthCheck), deadline=None)
def test_settlement_is_idempotent(
    state: LegState,
    leg_id: str,
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """A duplicated settlement message must not corrupt state.

    At-least-once delivery is the normal case on a message bus, so an
    exactly-once settlement handler is not available and idempotence has to be
    a property of the domain object instead.
    """
    first = recalculate_on_settlement(
        three_leg_correlated_parlay, leg_id, state, positive_correlation_matrix
    )
    second = recalculate_on_settlement(
        first.parlay, leg_id, state, positive_correlation_matrix
    )
    assert first.parlay == second.parlay
    assert first.parlay.current_offered_odds_exact == second.parlay.current_offered_odds_exact
    assert first.parlay.capital_at_risk_paise == second.parlay.capital_at_risk_paise


def test_conflicting_terminal_settlement_is_refused(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """Two sources disagreeing on a settlement is an incident, not a merge."""
    won = recalculate_on_settlement(
        three_leg_correlated_parlay, "leg_win", LegState.WIN, positive_correlation_matrix
    )
    with pytest.raises(ValueError, match="already terminal"):
        recalculate_on_settlement(
            won.parlay, "leg_win", LegState.LOSE, positive_correlation_matrix
        )


def test_settling_back_to_pending_is_refused(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    with pytest.raises(ValueError, match="back into PENDING"):
        recalculate_on_settlement(
            three_leg_correlated_parlay,
            "leg_win",
            LegState.PENDING,
            positive_correlation_matrix,
        )


def test_one_losing_leg_kills_the_parlay(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    step = recalculate_on_settlement(
        three_leg_correlated_parlay, "leg_over", LegState.LOSE, positive_correlation_matrix
    )
    assert step.verdict is ParlayVerdict.DEAD
    assert step.parlay.is_dead is True
    assert step.conditional_ev_per_unit == Decimal(-1)
    assert step.potential_payout_paise == 0
    assert step.capital_at_risk_paise == 0
    assert parlay_ev(step.parlay, 1.0) == Decimal(-1)


def test_all_legs_void_returns_the_stake_at_zero_ev(
    three_leg_correlated_parlay: Parlay,
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """A fully void parlay is a refund, which is EV zero, not a loss."""
    parlay = three_leg_correlated_parlay
    for leg_id in ("leg_win", "leg_over", "leg_btts"):
        parlay = recalculate_on_settlement(
            parlay, leg_id, LegState.VOID, positive_correlation_matrix
        ).parlay

    assert parlay.is_fully_void is True
    assert parlay.potential_payout_paise == 100_000
    assert parlay.capital_at_risk_paise == 0
    assert parlay_ev(parlay, 1.0) == Decimal(0)


def test_void_risk_ev_reduces_to_plain_ev_when_void_probability_is_zero(
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """Cross-validates the 2^n enumeration against the single-path formula.

    With zero void probability the enumeration collapses to one pattern, so it
    must reproduce ``parlay_ev`` exactly. This is the cheapest available check
    that the void decomposition is correctly normalised.
    """
    parlay = Parlay(
        parlay_id="no_void",
        legs=(
            make_leg("a", probability=0.50, odds=2.10, risk_factor="A", void_probability=0.0),
            make_leg("b", probability=0.58, odds=1.80, risk_factor="B", void_probability=0.0),
            make_leg("c", probability=0.55, odds=1.90, risk_factor="C", void_probability=0.0),
        ),
        stake_paise=100_000,
    )
    joint = exact_gaussian_copula_joint_probability(
        (0.50, 0.58, 0.55), positive_correlation_matrix
    ).joint_probability

    enumerated = expected_value_with_void_risk(parlay, positive_correlation_matrix)
    direct = parlay_ev(parlay, joint)
    assert float(enumerated) == pytest.approx(float(direct), abs=1e-4)


def test_void_risk_strictly_changes_ev_when_void_probability_is_material(
    positive_correlation_matrix: NDArray[np.float64],
) -> None:
    """Two-state parlay models cannot express this, and understate the EV.

    A void refunds the stake and re-prices the remainder, which is strictly
    better than a loss. Ignoring void probability therefore biases EV downward
    and, more dangerously, understates the variance of the payout.
    """
    with_void = Parlay(
        parlay_id="with_void",
        legs=(
            make_leg("a", probability=0.50, odds=2.10, risk_factor="A", void_probability=0.10),
            make_leg("b", probability=0.58, odds=1.80, risk_factor="B", void_probability=0.10),
            make_leg("c", probability=0.55, odds=1.90, risk_factor="C", void_probability=0.10),
        ),
        stake_paise=100_000,
    )
    without_void = with_void.model_copy(
        update={"legs": tuple(leg.model_copy(update={"void_probability": 0.0}) for leg in with_void.legs)}
    )

    assert expected_value_with_void_risk(
        with_void, positive_correlation_matrix
    ) != expected_value_with_void_risk(without_void, positive_correlation_matrix)


def test_void_enumeration_refuses_an_intractable_leg_count() -> None:
    """2^n copula evaluations. Refuse loudly rather than hang."""
    n = MAX_VOID_ENUMERATION_LEGS + 1
    parlay = Parlay(
        parlay_id="too_many",
        legs=tuple(
            make_leg(f"leg_{i}", probability=0.5, odds=2.0, risk_factor=f"R{i}")
            for i in range(n)
        ),
        stake_paise=100_000,
    )
    with pytest.raises(ValueError, match="at most"):
        expected_value_with_void_risk(parlay, _identity(n))


# --------------------------------------------------------------------------- #
# Golden: exposure concentration at the 8% limit
# --------------------------------------------------------------------------- #


def _parlay_on_factor(parlay_id: str, factor: str, stake_paise: int) -> Parlay:
    return Parlay(
        parlay_id=parlay_id,
        legs=(
            make_leg(f"{parlay_id}_l1", probability=0.55, odds=2.0, risk_factor=factor),
            make_leg(f"{parlay_id}_l2", probability=0.60, odds=1.8, risk_factor=f"OTHER_{parlay_id}"),
        ),
        stake_paise=stake_paise,
    )


def test_exposure_matrix_aggregates_by_failure_driver() -> None:
    """Twenty small parlays on one factor are one large bet in disguise."""
    bankroll = 10_000_000  # INR 100,000
    parlays = tuple(
        _parlay_on_factor(f"p{i}", "TEAM_A_LOSES", 300_000) for i in range(2)
    )
    matrix = ExposureMatrix.build(parlays, bankroll)

    row = matrix.row("TEAM_A_LOSES")
    assert row is not None
    assert row.exposure_fraction == pytest.approx(0.06, abs=1e-9)
    assert row.worst_case_loss_paise == 600_000


def test_concentration_violation_raises_error() -> None:
    """Three parlays crossing the 8% global limit."""
    bankroll = 10_000_000  # INR 100,000
    existing = tuple(
        _parlay_on_factor(f"p{i}", "TEAM_A_LOSES", 300_000) for i in range(2)
    )
    candidate = _parlay_on_factor("p2", "TEAM_A_LOSES", 300_000)

    with pytest.raises(ExposureConcentrationError) as excinfo:
        check_exposure_limit(existing, candidate, bankroll, max_single_point_drawdown=0.08)

    assert excinfo.value.risk_factor_key == "TEAM_A_LOSES"
    assert excinfo.value.exposure_fraction == pytest.approx(0.09, abs=1e-9)
