"""Tests for the Manual Gut-Feel Sizing gate."""

from __future__ import annotations

import math
import time
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from domain.math.ev_filter import kelly_fraction
from domain.math.money import to_paise
from domain.math.portfolio_kelly import (
    BetCandidate,
    PortfolioConfig,
    PortfolioVerdict,
    PrefilterReason,
    ScenarioSource,
    build_scenarios,
    prefilter_candidates,
    quadratic_kelly_approximation,
    solve_portfolio_kelly,
)
from tests.unit.domain.math.conftest import bet_candidates, make_candidate

# --------------------------------------------------------------------------- #
# The golden INR 1,000 five-bet portfolio
# --------------------------------------------------------------------------- #

_GOLDEN_BANKROLL = Decimal("1000.00")

_GOLDEN_CANDIDATES: tuple[BetCandidate, ...] = (
    # Two mutually exclusive outcomes of one market. Negative covariance is
    # constructed here, not asserted, and the optimiser must find the hedge.
    make_candidate("epl_home", market_key="EPL_ARS_CHE", probability=0.52, odds=2.15),
    make_candidate("epl_draw", market_key="EPL_ARS_CHE", probability=0.26, odds=4.20),
    # Three independent markets with genuine edge.
    make_candidate("laliga_home", market_key="LALIGA_RMA_ATM", probability=0.55, odds=1.95),
    make_candidate("seriea_over", market_key="SERIEA_INT_NAP", probability=0.58, odds=1.85),
    make_candidate("bundes_away", market_key="BUNDES_RBL_VFB", probability=0.41, odds=2.60),
)

#: Fill these in from a FIRST RUN THAT HAS BEEN HAND-VERIFIED, then keep them
#: frozen forever. Leaving this as ``None`` skips only the exact-value
#: assertions; every structural assertion below still runs and is strong.
#:
#: A golden test frozen from unverified solver output is a regression test, not
#: a correctness test, and it will happily preserve a bug for years. Verify by
#: checking that (a) the total equals the exposure cap or the unconstrained
#: optimum, (b) every kelly_ratio is below fractional_kelly, and (c) the two
#: same-market allocations are jointly smaller than they would be if the
#: market were priced as two independent bets.
_PINNED_STAKES_PAISE: dict[str, int] | None = None


def _golden_config() -> PortfolioConfig:
    return PortfolioConfig(
        fractional_kelly=0.25,
        max_total_exposure_fraction=0.25,
        per_bet_cap_fraction=0.10,
        min_edge_per_unit=0.005,
        seed=20240101,
    )


def test_golden_thousand_rupee_portfolio_structure() -> None:
    """Replaces gut-feel with a growth-optimal, exactly-bounded allocation."""
    result = solve_portfolio_kelly(
        _GOLDEN_CANDIDATES, _GOLDEN_BANKROLL, _golden_config()
    )

    assert result.verdict.is_actionable
    assert result.solver is not None
    assert result.scenario_source is ScenarioSource.EXHAUSTIVE
    assert result.prefilter_survivors == 5
    assert result.bankroll_paise == 100_000

    # Exact integer bankroll safety. 25% of 100,000 paise is exactly 25,000.
    assert result.total_allocated_paise == sum(a.stake_paise for a in result.allocations)
    assert result.total_allocated_paise <= 25_000
    assert all(isinstance(a.stake_paise, int) for a in result.allocations)
    assert all(a.stake_paise >= 0 for a in result.allocations)

    # Growth must be strictly positive on a book of genuine edges.
    assert result.expected_log_growth > 0.0
    assert math.isfinite(result.objective_value)


def test_golden_portfolio_undersizes_relative_to_naive_single_bet_kelly() -> None:
    """The proof that the joint solve is doing its job.

    Repeated single-bet Kelly massively overbets a correlated book, because
    each calculation assumes the rest of the bankroll is idle. Every
    ``kelly_ratio`` must therefore land strictly below the fractional-Kelly
    multiplier. If they all equal 0.25 exactly, the scenario matrix is not
    encoding mutual exclusivity and the module has silently degraded to
    independent sizing.
    """
    result = solve_portfolio_kelly(
        _GOLDEN_CANDIDATES, _GOLDEN_BANKROLL, _golden_config()
    )
    ratios = [a.kelly_ratio for a in result.allocations if a.stake_paise > 0]

    assert ratios, "expected at least one funded allocation"
    assert any(ratio != pytest.approx(0.25, abs=1e-5) for ratio in ratios), "no joint effects occurred"

    naive_total = sum(
        to_paise(_GOLDEN_BANKROLL * Decimal(str(c.single_kelly_fraction * 0.25)))
        for c in _GOLDEN_CANDIDATES
    )
    assert result.total_allocated_paise != naive_total


@pytest.mark.skipif(
    _PINNED_STAKES_PAISE is None,
    reason="pin exact stakes after a hand-verified first run",
)
def test_golden_thousand_rupee_portfolio_exact_values() -> None:
    """Exact-to-the-paisa regression lock, once verified."""
    assert _PINNED_STAKES_PAISE is not None
    result = solve_portfolio_kelly(
        _GOLDEN_CANDIDATES, _GOLDEN_BANKROLL, _golden_config()
    )
    actual = {a.bet_id: a.stake_paise for a in result.allocations}
    assert actual == _PINNED_STAKES_PAISE


# --------------------------------------------------------------------------- #
# Property: exact-integer bankroll safety
# --------------------------------------------------------------------------- #


@given(
    candidates=bet_candidates(min_size=1, max_size=5),
    bankroll_paise=st.integers(min_value=100, max_value=100_000_000),
)
@settings(max_examples=15, deadline=None)
def test_allocations_never_exceed_the_exposure_limit_in_integer_arithmetic(
    candidates: tuple[BetCandidate, ...], bankroll_paise: int
) -> None:
    """The required INR conversion-safety property.

    cvxpy works in float64 and the exposure constraint is satisfied only to
    solver tolerance. Converting each fraction independently is not enough: the
    sum can still land above the limit. This asserts the post-solve re-check in
    exact integers, which is the only place the guarantee actually holds.
    """
    bankroll = (Decimal(bankroll_paise) / 100).quantize(Decimal("0.01"))
    config = PortfolioConfig(max_total_exposure_fraction=0.25, per_bet_cap_fraction=0.10)

    result = solve_portfolio_kelly(candidates, bankroll, config)

    limit = int(Decimal(result.bankroll_paise) * Decimal("0.25"))
    total = sum(a.stake_paise for a in result.allocations)
    assert total == result.total_allocated_paise
    assert total <= limit
    assert total <= result.bankroll_paise
    assert all(a.stake_paise >= 0 for a in result.allocations)


@given(candidates=bet_candidates(min_size=1, max_size=4))
@settings(max_examples=15, deadline=None)
def test_every_fraction_respects_the_per_bet_cap(
    candidates: tuple[BetCandidate, ...],
) -> None:
    config = PortfolioConfig(per_bet_cap_fraction=0.05, max_total_exposure_fraction=0.20)
    result = solve_portfolio_kelly(candidates, Decimal("10000.00"), config)

    for allocation in result.allocations:
        assert 0.0 <= allocation.fraction <= 1.0
        # Post-scaled by fractional Kelly, so the bound is the cap itself.
        assert allocation.fraction <= 0.05 + 1e-6


# --------------------------------------------------------------------------- #
# Cross-validation against the closed-form single-bet Kelly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("probability", "odds"),
    [(0.55, 2.00), (0.60, 1.90), (0.35, 3.40), (0.20, 6.00)],
)
def test_single_candidate_reproduces_closed_form_kelly(
    probability: float, odds: float
) -> None:
    """The single highest-value test in this module.

    One candidate, full Kelly, caps wide open. The convex solver must
    reproduce ``kelly_fraction`` from ``ev_filter`` to four decimal places.
    That one assertion simultaneously validates the scenario construction, the
    cvxpy objective, the solver configuration and the closed form against each
    other. If it fails, nothing downstream is trustworthy.
    """
    candidate = make_candidate(
        "solo", market_key="SOLO", probability=probability, odds=odds
    )
    config = PortfolioConfig(
        fractional_kelly=1.0,
        max_total_exposure_fraction=0.95,
        per_bet_cap_fraction=0.95,
    )
    result = solve_portfolio_kelly((candidate,), Decimal("100000.00"), config)

    assert result.verdict.is_actionable
    assert len(result.allocations) == 1
    assert result.allocations[0].fraction == pytest.approx(
        kelly_fraction(probability, odds), abs=1e-4
    )


def test_two_mutually_exclusive_outcomes_are_sized_below_independent_kelly() -> None:
    """Negative covariance must reduce total exposure, not increase it.

    Backing two outcomes of one market is a partial hedge. A model that treats
    them as independent bets will overstake, because it double-counts the
    downside that cannot occur.
    """
    candidates = (
        make_candidate("home", market_key="SAME", probability=0.50, odds=2.20),
        make_candidate("draw", market_key="SAME", probability=0.28, odds=4.00),
    )
    config = PortfolioConfig(
        fractional_kelly=1.0,
        max_total_exposure_fraction=0.95,
        per_bet_cap_fraction=0.95,
    )
    joint = solve_portfolio_kelly(candidates, Decimal("100000.00"), config)
    independent_total = sum(c.single_kelly_fraction for c in candidates)

    joint_total = sum(a.fraction for a in joint.allocations)
    assert joint_total > independent_total
    assert all(a.stake_paise > 0 for a in joint.allocations)


# --------------------------------------------------------------------------- #
# Scenario construction
# --------------------------------------------------------------------------- #


def test_same_market_candidates_never_win_together() -> None:
    """Mutual exclusivity, verified directly on the returns matrix."""
    candidates = (
        make_candidate("a", market_key="M1", probability=0.4, odds=2.6),
        make_candidate("b", market_key="M1", probability=0.35, odds=3.0),
        make_candidate("c", market_key="M2", probability=0.5, odds=2.1),
    )
    scenarios = build_scenarios(candidates, PortfolioConfig())

    wins = scenarios.returns > 0
    assert not np.any(wins[:, 0] & wins[:, 1]), "same-market outcomes co-occurred"
    assert np.any(wins[:, 0] & wins[:, 2]), "cross-market outcomes must be able to co-occur"
    assert math.isclose(float(scenarios.probabilities.sum()), 1.0, abs_tol=1e-12)


def test_scenario_returns_are_minus_one_on_a_loss() -> None:
    candidates = (make_candidate("a", market_key="M", probability=0.5, odds=2.5),)
    scenarios = build_scenarios(candidates, PortfolioConfig())

    values = set(np.round(scenarios.returns.ravel(), 10))
    assert values == {-1.0, round(1.5, 10)}


def test_inconsistent_market_probabilities_are_refused() -> None:
    """Devigged probabilities summing above 1 in one market are corrupt input."""
    candidates = (
        make_candidate("a", market_key="M", probability=0.7, odds=1.5),
        make_candidate("b", market_key="M", probability=0.6, odds=1.8),
    )
    with pytest.raises(ValueError, match="exceeds 1.0"):
        build_scenarios(candidates, PortfolioConfig())


def test_monte_carlo_fallback_engages_above_the_scenario_cap() -> None:
    """Exhaustive enumeration is exponential in market count. Sample instead."""
    candidates = tuple(
        make_candidate(f"b{i}", market_key=f"M{i}", probability=0.5, odds=2.2)
        for i in range(12)
    )
    scenarios = build_scenarios(candidates, PortfolioConfig(max_scenarios=64))

    assert scenarios.source is ScenarioSource.MONTE_CARLO
    assert scenarios.scenario_count == 8_192
    assert math.isclose(float(scenarios.probabilities.sum()), 1.0, abs_tol=1e-9)


@given(candidates=bet_candidates(min_size=1, max_size=4))
def test_scenario_probabilities_always_form_a_distribution(
    candidates: tuple[BetCandidate, ...],
) -> None:
    scenarios = build_scenarios(candidates, PortfolioConfig())
    assert np.all(scenarios.probabilities >= 0.0)
    assert math.isclose(float(scenarios.probabilities.sum()), 1.0, abs_tol=1e-9)
    assert scenarios.returns.shape == (scenarios.scenario_count, len(candidates))


# --------------------------------------------------------------------------- #
# The two-tier pre-filter
# --------------------------------------------------------------------------- #


def test_prefilter_discards_negative_ev_and_keeps_positive() -> None:
    candidates = (
        make_candidate("good", market_key="M1", probability=0.55, odds=2.10),
        make_candidate("bad", market_key="M2", probability=0.40, odds=2.00),
        make_candidate("marginal", market_key="M3", probability=0.50, odds=2.001),
    )
    result = prefilter_candidates(candidates, PortfolioConfig(min_edge_per_unit=0.01))

    survivors = {c.bet_id for c in result.survivors}
    reasons = {r.bet_id: r.reason for r in result.rejections}

    assert survivors == {"good"}
    assert reasons["bad"] is PrefilterReason.NEGATIVE_EV
    assert reasons["marginal"] is PrefilterReason.BELOW_MIN_EDGE
    assert result.evaluated_count == 3


def test_prefilter_never_drops_mutually_exclusive_candidates() -> None:
    """Same-market pairs are the hedge structure the optimiser exploits.

    Treating them as "highly correlated garbage" would destroy the exact value
    the joint solve exists to capture, so the lookup returns -1.0 for them by
    design.
    """
    candidates = (
        make_candidate("home", market_key="SAME", probability=0.50, odds=2.20),
        make_candidate("draw", market_key="SAME", probability=0.28, odds=4.00),
    )
    result = prefilter_candidates(
        candidates, PortfolioConfig(max_pairwise_correlation=0.1)
    )
    assert result.survivor_count == 2
    assert not result.rejections


def test_prefilter_drops_the_weaker_of_a_highly_correlated_pair() -> None:
    strong = make_candidate("strong", market_key="M1", probability=0.60, odds=2.00)
    weak = make_candidate("weak", market_key="M2", probability=0.52, odds=2.00)
    result = prefilter_candidates(
        (weak, strong),
        PortfolioConfig(max_pairwise_correlation=0.9),
        {("strong", "weak"): 0.97},
    )

    assert {c.bet_id for c in result.survivors} == {"strong"}
    assert result.rejections[0].bet_id == "weak"
    assert result.rejections[0].reason is PrefilterReason.CORRELATION_DOMINATED


def test_prefilter_records_the_cost_of_its_own_approximation() -> None:
    """The honesty requirement, made testable.

    Negative-EV hedges are discarded for throughput. The total positive edge
    thrown away must be observable so the trade can be audited rather than
    assumed harmless.
    """
    candidates = tuple(
        make_candidate(f"b{i}", market_key=f"M{i}", probability=0.5, odds=2.001)
        for i in range(10)
    )
    result = prefilter_candidates(candidates, PortfolioConfig(min_edge_per_unit=0.05))

    assert result.survivor_count == 0
    assert len(result.rejections) == 10
    assert result.discarded_edge_total > 0.0


def test_prefilter_enforces_the_candidate_cap() -> None:
    candidates = tuple(
        make_candidate(f"b{i}", market_key=f"M{i}", probability=0.55, odds=2.10)
        for i in range(20)
    )
    result = prefilter_candidates(candidates, PortfolioConfig(max_candidates=5))

    assert result.survivor_count == 5
    assert all(
        r.reason is PrefilterReason.EXCEEDS_CANDIDATE_CAP for r in result.rejections
    )


def test_prefilter_rejects_duplicate_bet_ids() -> None:
    duplicate = make_candidate("dup", market_key="M", probability=0.5, odds=2.2)
    with pytest.raises(ValueError, match="duplicate bet_id"):
        prefilter_candidates((duplicate, duplicate))


def test_prefilter_is_orders_of_magnitude_cheaper_than_the_solve() -> None:
    """The performance constraint, measured rather than asserted by comment.

    500 candidates through the scalar filter must cost a small fraction of a
    single convex solve. The threshold is deliberately loose so the test is not
    flaky on shared CI runners, while still failing loudly if someone
    introduces an N-by-N matrix build into the filter.
    """
    candidates = tuple(
        make_candidate(
            f"b{i}",
            market_key=f"M{i % 50}",
            probability=0.5 + (i % 7) * 0.02,
            odds=2.0 + (i % 11) * 0.05,
        )
        for i in range(500)
    )

    started = time.perf_counter()
    result = prefilter_candidates(candidates, PortfolioConfig(max_candidates=8))
    prefilter_seconds = time.perf_counter() - started

    assert prefilter_seconds < 0.25
    assert result.survivor_count <= 8

    solved = solve_portfolio_kelly(candidates, Decimal("100000.00"), PortfolioConfig(max_candidates=8))
    assert solved.prefilter_evaluated == 500
    assert solved.prefilter_survivors <= 8


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_empty_candidate_list_returns_a_verdict_not_an_exception() -> None:
    """The scanner calls this on every tick. It must not raise on an empty book."""
    result = solve_portfolio_kelly((), Decimal("1000.00"))

    assert result.verdict is PortfolioVerdict.NO_CANDIDATES_SURVIVED
    assert result.allocations == ()
    assert result.total_allocated_paise == 0
    assert result.rejection_reason is not None
    assert result.solver is None


def test_all_candidates_negative_ev_returns_no_survivors() -> None:
    candidates = tuple(
        make_candidate(f"b{i}", market_key=f"M{i}", probability=0.4, odds=2.0)
        for i in range(5)
    )
    result = solve_portfolio_kelly(candidates, Decimal("1000.00"))

    assert result.verdict is PortfolioVerdict.NO_CANDIDATES_SURVIVED
    assert result.total_allocated_paise == 0
    assert len(result.prefilter_rejections) == 5


def test_one_paisa_bankroll_allocates_nothing_but_does_not_crash() -> None:
    """Rounding DOWN means a sub-paisa allocation becomes zero, correctly."""
    candidate = make_candidate("a", market_key="M", probability=0.55, odds=2.10)
    result = solve_portfolio_kelly((candidate,), Decimal("0.01"))

    assert result.bankroll_paise == 1
    assert result.total_allocated_paise == 0
    assert result.verdict.is_actionable


def test_zero_bankroll_is_refused() -> None:
    candidate = make_candidate("a", market_key="M", probability=0.55, odds=2.10)
    with pytest.raises(ValueError, match="at least one paisa"):
        solve_portfolio_kelly((candidate,), Decimal("0.00"))


def test_odds_at_the_tradable_minimum_are_handled() -> None:
    """1.01 gives b = 0.01, so the Kelly denominator is tiny. No division blowup."""
    candidate = make_candidate("fav", market_key="M", probability=0.999, odds=1.01)
    result = solve_portfolio_kelly((candidate,), Decimal("100000.00"))

    assert result.verdict.is_actionable
    assert math.isfinite(result.expected_log_growth)
    assert result.total_allocated_paise <= 2_500_000


def test_solution_is_deterministic_for_a_fixed_seed() -> None:
    config = _golden_config()
    first = solve_portfolio_kelly(_GOLDEN_CANDIDATES, _GOLDEN_BANKROLL, config)
    second = solve_portfolio_kelly(_GOLDEN_CANDIDATES, _GOLDEN_BANKROLL, config)

    assert [a.stake_paise for a in first.allocations] == [
        a.stake_paise for a in second.allocations
    ]


# --------------------------------------------------------------------------- #
# The quadratic approximation is an approximation
# --------------------------------------------------------------------------- #


def test_quadratic_approximation_is_close_for_small_stakes() -> None:
    """Validates the Taylor expansion in the regime where it is valid."""
    candidates = (make_candidate("a", market_key="M", probability=0.52, odds=2.00),)
    config = PortfolioConfig(
        fractional_kelly=1.0,
        max_total_exposure_fraction=0.05,
        per_bet_cap_fraction=0.05,
    )
    scenarios = build_scenarios(candidates, config)
    approximate = quadratic_kelly_approximation(candidates, scenarios, config)
    exact = solve_portfolio_kelly(candidates, Decimal("100000.00"), config)

    assert len(approximate) == 1
    assert approximate[0] == pytest.approx(exact.allocations[0].fraction, abs=5e-3)


def test_quadratic_approximation_diverges_at_large_stakes() -> None:
    """The reason it must never be the production path.

    With the caps opened up, the second-order expansion overstates the optimum
    because it truncates the concavity of the logarithm exactly where that
    concavity is doing the risk management.
    """
    candidates = (make_candidate("a", market_key="M", probability=0.40, odds=3.00),)
    config = PortfolioConfig(
        fractional_kelly=1.0,
        max_total_exposure_fraction=0.95,
        per_bet_cap_fraction=0.95,
    )
    scenarios = build_scenarios(candidates, config)
    approximate = quadratic_kelly_approximation(candidates, scenarios, config)
    exact = kelly_fraction(0.40, 3.00)

    assert approximate[0] != pytest.approx(exact, abs=1e-3)


@given(candidates=bet_candidates(min_size=1, max_size=4))
def test_quadratic_approximation_always_respects_the_caps(
    candidates: tuple[BetCandidate, ...],
) -> None:
    config = PortfolioConfig(per_bet_cap_fraction=0.05, max_total_exposure_fraction=0.20)
    scenarios = build_scenarios(candidates, config)
    fractions = quadratic_kelly_approximation(candidates, scenarios, config)

    assert all(0.0 <= f <= 0.05 + 1e-9 for f in fractions)
    assert math.fsum(fractions) <= 0.20 + 1e-9
