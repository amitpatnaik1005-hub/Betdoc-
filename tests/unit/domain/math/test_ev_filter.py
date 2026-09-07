"""Tests for the Blind Confidence gate."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from betdoc.domain.math.errors import (
    DevigDisagreementError,
    NegativeExpectedValueError,
)
from betdoc.domain.math.ev_filter import (
    EvFilterConfig,
    EvFilterRequest,
    EvVerdict,
    MarketOutcome,
    MarketQuote,
    evaluate_ev,
    kelly_fraction,
    poisson_scoreline_probability,
    power_devig,
    shin_devig,
)
from betdoc.domain.math.money import from_paise, to_paise
from tests.unit.domain.math.conftest import (
    complete_books,
    decimal_odds,
    probabilities,
    zero_vig_books,
)

# --------------------------------------------------------------------------- #
# Property: Kelly is always a valid fraction
# --------------------------------------------------------------------------- #


@given(probability=probabilities, odds=decimal_odds)
@example(probability=0.5, odds=1.01)
@example(probability=0.9999, odds=1000.0)
@example(probability=1e-4, odds=1.0101)
def test_kelly_fraction_always_in_unit_interval(probability: float, odds: float) -> None:
    """The single most important invariant in the vault.

    A fraction above 1.0 means betting more than the bankroll. A negative
    fraction means the caller has to interpret a sign, which is how people
    accidentally bet the wrong side.
    """
    fraction = kelly_fraction(probability, odds)
    assert 0.0 <= fraction <= 1.0
    assert math.isfinite(fraction)


@given(probability=probabilities, odds=decimal_odds)
def test_kelly_is_zero_exactly_when_edge_is_non_positive(
    probability: float, odds: float
) -> None:
    edge = probability * odds - 1.0
    fraction = kelly_fraction(probability, odds)
    if edge <= 0.0:
        assert fraction == 0.0
    else:
        assert fraction > 0.0


@given(probability=probabilities, odds=decimal_odds)
def test_kelly_matches_closed_form_when_positive(
    probability: float, odds: float
) -> None:
    assume(probability * odds - 1.0 > 1e-9)
    b = odds - 1.0
    expected = (probability * b - (1.0 - probability)) / b
    assert kelly_fraction(probability, odds) == pytest.approx(
        min(expected, 1.0), rel=1e-12
    )


# --------------------------------------------------------------------------- #
# Property: both devig methods produce a probability vector
# --------------------------------------------------------------------------- #


@given(market=complete_books())
def test_shin_probabilities_sum_to_one(market: MarketQuote) -> None:
    result = shin_devig(market)
    assert math.isclose(math.fsum(result.probabilities), 1.0, abs_tol=1e-9)
    assert all(0.0 < p < 1.0 for p in result.probabilities)


@given(market=complete_books())
def test_power_probabilities_sum_to_one(market: MarketQuote) -> None:
    result = power_devig(market)
    assert math.isclose(math.fsum(result.probabilities), 1.0, abs_tol=1e-9)
    assert all(0.0 < p < 1.0 for p in result.probabilities)


@given(market=complete_books())
def test_devig_preserves_ordering(market: MarketQuote) -> None:
    """Devigging removes margin. It must never reorder the favourites.

    If a shorter price ends up with a lower fair probability than a longer
    price, the estimator is broken, and no amount of downstream Kelly maths
    can recover from it.
    """
    shin = shin_devig(market)
    raw_order = sorted(range(len(market.outcomes)), key=lambda i: market.raw_implied[i])
    shin_order = sorted(range(len(market.outcomes)), key=lambda i: shin.probabilities[i])
    assert raw_order == shin_order


@given(market=complete_books())
def test_shin_z_is_a_valid_probability(market: MarketQuote) -> None:
    """Shin's ``z`` is the insider-money fraction, so it must lie in [0, 1)."""
    result = shin_devig(market)
    assert 0.0 <= result.parameter < 1.0


@given(market=complete_books())
def test_power_exponent_exceeds_one_for_a_book_with_margin(
    market: MarketQuote,
) -> None:
    result = power_devig(market)
    assert result.parameter >= 1.0


# --------------------------------------------------------------------------- #
# Edge case: the zero-vig fixed point
# --------------------------------------------------------------------------- #


@given(market=zero_vig_books())
def test_zero_vig_book_is_the_analytic_fixed_point(market: MarketQuote) -> None:
    """Both estimators must be identity maps on a 100% book.

    Shin's ``z`` collapses to zero and the power exponent to one. Any deviation
    means the root-finder is being called where it should not be, or the
    booksum guard is mis-signed.
    """
    shin = shin_devig(market)
    power = power_devig(market)

    assert shin.parameter == pytest.approx(0.0, abs=1e-6)
    assert power.parameter == pytest.approx(1.0, abs=1e-6)
    for raw, shin_p, power_p in zip(
        market.raw_implied, shin.probabilities, power.probabilities, strict=True
    ):
        assert shin_p == pytest.approx(raw, abs=1e-8)
        assert power_p == pytest.approx(raw, abs=1e-8)


def test_odds_of_exactly_one_point_zero_one_is_accepted() -> None:
    """1.01 is the tradable minimum on every major venue. It must not round out."""
    market = MarketQuote(
        bookmaker="edge",
        outcomes=(
            MarketOutcome(outcome_key="heavy_favourite", decimal_odds=1.01),
            MarketOutcome(outcome_key="longshot", decimal_odds=50.0),
        ),
        is_sharp=True,
    )
    result = shin_devig(market)
    assert math.isclose(math.fsum(result.probabilities), 1.0, abs_tol=1e-9)
    assert kelly_fraction(0.999, 1.01) >= 0.0


def test_incomplete_book_is_rejected_at_construction() -> None:
    """A sub-100% book is either incomplete or self-arbitraging. Neither is devig-able."""
    with pytest.raises(ValueError, match="below 1.0"):
        MarketQuote(
            bookmaker="broken",
            outcomes=(
                MarketOutcome(outcome_key="a", decimal_odds=3.0),
                MarketOutcome(outcome_key="b", decimal_odds=3.0),
            ),
        )


# --------------------------------------------------------------------------- #
# Golden: the Real Madrid 2-0 trap
# --------------------------------------------------------------------------- #


def test_real_madrid_2_0_stake_is_capped_to_the_high_variance_limit(
    real_madrid_correct_score_book: MarketQuote, bankroll_two_lakh_inr: Decimal
) -> None:
    """The headline scenario: INR 20,000 requested, INR 4,000 permitted.

    The cap figure is derived from policy, not from the solver, so it is
    hardcoded legitimately: a 2% high-variance cap on a INR 200,000 bankroll
    is exactly 400,000 paise. The book has 11 outcomes, comfortably above the
    6-outcome high-variance threshold, so the tight cap applies.

    ``devig_relative_tolerance`` is relaxed to 0.50 here deliberately. On an
    18% margin book Shin and the power method genuinely diverge, and the
    disagreement gate is exercised in its own test below. Mixing the two
    concerns in one test would make a sizing failure look like a devig failure.
    """
    offered = 2.0 * real_madrid_correct_score_book.outcomes[10].decimal_odds

    decision = evaluate_ev(
        EvFilterRequest(
            market=real_madrid_correct_score_book,
            target_outcome_key="ANY_OTHER",
            offered_odds=offered,
            requested_stake_inr=Decimal("20000.00"),
            bankroll_inr=bankroll_two_lakh_inr,
        ),
        EvFilterConfig(devig_relative_tolerance=0.50),
    )

    assert decision.verdict is EvVerdict.APPROVED_REDUCED
    assert decision.binding_constraint == "bankroll_cap"
    assert decision.approved_stake_paise == 400_000
    assert decision.approved_stake_inr == Decimal("4000.00")
    assert decision.requested_stake_paise == 2_000_000
    assert decision.reduction_ratio == pytest.approx(0.20, abs=1e-12)
    assert decision.low_confidence is True, "an 18% margin book must be flagged"


def test_real_madrid_robust_probability_is_strictly_below_the_point_estimate(
    real_madrid_correct_score_book: MarketQuote, bankroll_two_lakh_inr: Decimal
) -> None:
    """The whole mechanism in one assertion.

    Sizing happens on the posterior lower bound, so the robust probability must
    be below the point estimate and the robust Kelly below the nominal Kelly.
    If these are ever equal, the quantile shrinkage has been bypassed and the
    module has silently reverted to naive Kelly.
    """
    offered = 2.0 * real_madrid_correct_score_book.outcomes[10].decimal_odds
    decision = evaluate_ev(
        EvFilterRequest(
            market=real_madrid_correct_score_book,
            target_outcome_key="ANY_OTHER",
            offered_odds=offered,
            requested_stake_inr=Decimal("20000.00"),
            bankroll_inr=bankroll_two_lakh_inr,
        ),
        EvFilterConfig(devig_relative_tolerance=0.50),
    )

    assert decision.robust_probability < decision.fair_probability
    assert decision.robust_kelly_fraction < decision.kelly_fraction
    assert decision.robust_ev_per_unit < decision.ev_per_unit
    # 11 outcomes and a fat margin must thin the posterior substantially.
    assert decision.effective_sample_size < 100.0


def test_negative_ev_bet_raises(
    real_madrid_correct_score_book: MarketQuote, bankroll_two_lakh_inr: Decimal
) -> None:
    """Constraint 1: non-positive EV is a hard stop, not a warning."""
    offered = 0.5 * real_madrid_correct_score_book.outcomes[2].decimal_odds

    with pytest.raises(NegativeExpectedValueError) as excinfo:
        evaluate_ev(
            EvFilterRequest(
                market=real_madrid_correct_score_book,
                target_outcome_key="2-0",
                offered_odds=offered,
                requested_stake_inr=Decimal("20000.00"),
                bankroll_inr=bankroll_two_lakh_inr,
            ),
            EvFilterConfig(devig_relative_tolerance=0.50),
        )

    assert excinfo.value.ev_per_unit <= 0
    assert excinfo.value.offered_odds == pytest.approx(offered)
    assert 0.0 < excinfo.value.fair_probability < 1.0


def test_marginal_edge_is_rejected_by_the_robust_gate(
    real_madrid_correct_score_book: MarketQuote, bankroll_two_lakh_inr: Decimal
) -> None:
    """The verdict that saves the most money over a season.

    A price barely above fair is positive EV at the point estimate and negative
    at the posterior lower bound. Naive Kelly stakes it. This gate declines it,
    and returns zero rather than raising, so the scanner can log and continue.
    """
    fair_odds = 1.0 / shin_devig(
        real_madrid_correct_score_book, high_margin_threshold=0.12
    ).probabilities[real_madrid_correct_score_book.index_of("2-0")]
    offered = fair_odds * 1.02

    decision = evaluate_ev(
        EvFilterRequest(
            market=real_madrid_correct_score_book,
            target_outcome_key="2-0",
            offered_odds=offered,
            requested_stake_inr=Decimal("20000.00"),
            bankroll_inr=bankroll_two_lakh_inr,
        ),
        EvFilterConfig(devig_relative_tolerance=0.50),
    )

    assert decision.verdict in (
        EvVerdict.REJECTED_FAILS_ROBUST_EV,
        EvVerdict.REJECTED_BELOW_MIN_EDGE,
    )
    assert decision.approved_stake_paise == 0
    assert decision.rejection_reason is not None
    assert decision.ev_per_unit > 0, "the point estimate was positive by construction"


def test_devig_disagreement_gate_fires_on_a_fat_margin_book(
    real_madrid_correct_score_book: MarketQuote, bankroll_two_lakh_inr: Decimal
) -> None:
    """A deliberately tight tolerance must trip the cross-check.

    This proves the gate is wired, and it documents why the sizing tests relax
    the tolerance explicitly rather than by accident.
    """
    offered = 2.0 * real_madrid_correct_score_book.outcomes[2].decimal_odds

    with pytest.raises(DevigDisagreementError) as excinfo:
        evaluate_ev(
            EvFilterRequest(
                market=real_madrid_correct_score_book,
                target_outcome_key="2-0",
                offered_odds=offered,
                requested_stake_inr=Decimal("20000.00"),
                bankroll_inr=bankroll_two_lakh_inr,
            ),
            EvFilterConfig(devig_relative_tolerance=1e-6),
        )

    assert excinfo.value.relative_difference > excinfo.value.tolerance
    assert excinfo.value.shin_probability > 0.0
    assert excinfo.value.power_probability > 0.0


# --------------------------------------------------------------------------- #
# Property: money never exceeds its bounds, in exact integer arithmetic
# --------------------------------------------------------------------------- #


@given(market=complete_books(min_outcomes=2, max_outcomes=5))
@settings(max_examples=60)
def test_approved_stake_never_exceeds_request_or_bankroll(
    market: MarketQuote,
) -> None:
    """Asserted in integer paise, so no float tolerance can hide a breach."""
    target = market.outcomes[0].outcome_key
    fair = shin_devig(market).probabilities[0]
    offered = (1.0 / fair) * 1.5
    assume(1.0101 < offered <= 10_000.0)

    try:
        decision = evaluate_ev(
            EvFilterRequest(
                market=market,
                target_outcome_key=target,
                offered_odds=offered,
                requested_stake_inr=Decimal("5000.00"),
                bankroll_inr=Decimal("100000.00"),
            ),
            EvFilterConfig(devig_relative_tolerance=1.0),
        )
    except NegativeExpectedValueError:
        return

    assert decision.approved_stake_paise <= decision.requested_stake_paise
    assert decision.approved_stake_paise <= decision.bankroll_paise
    assert isinstance(decision.approved_stake_paise, int)
    if not decision.verdict.is_approved:
        assert decision.approved_stake_paise == 0


@given(rupees=st.integers(min_value=0, max_value=10_000_000))
def test_inr_paise_round_trip_is_lossless(rupees: int) -> None:
    """INR integer conversion safety, both directions."""
    amount = (Decimal(rupees) / 100).quantize(Decimal("0.01"))
    paise = to_paise(amount)
    assert paise == rupees
    assert from_paise(paise) == amount


@given(
    amount=st.decimals(
        min_value=Decimal("0.00"),
        max_value=Decimal("1000000.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_to_paise_never_rounds_up(amount: Decimal) -> None:
    """Allocation rounds DOWN so a bankroll constraint cannot be breached."""
    paise = to_paise(amount)
    assert Decimal(paise) <= amount * 100
    assert paise == int(amount * 100)


# --------------------------------------------------------------------------- #
# Poisson cross-check
# --------------------------------------------------------------------------- #


def test_poisson_scoreline_probability_is_a_valid_probability() -> None:
    value = poisson_scoreline_probability(
        home_xg=2.1, away_xg=0.9, home_goals=2, away_goals=0
    )
    assert 0.0 < value < 1.0


def test_poisson_scoreline_distribution_sums_towards_one() -> None:
    """Summing the joint grid must approach 1 as the grid widens.

    This validates the independence factorisation, and it is the reason this
    function is only a cross-check: real scorelines show low-score dependence
    that independent Poissons cannot express.
    """
    total = math.fsum(
        poisson_scoreline_probability(
            home_xg=1.6, away_xg=1.2, home_goals=h, away_goals=a
        )
        for h in range(15)
        for a in range(15)
    )
    assert total == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize(
    ("home_xg", "away_xg", "home_goals", "away_goals"),
    [(0.0, 1.0, 1, 1), (-1.0, 1.0, 1, 1), (1.0, 1.0, -1, 1), (1.0, 1.0, 1, -1)],
)
def test_poisson_rejects_invalid_inputs(
    home_xg: float, away_xg: float, home_goals: int, away_goals: int
) -> None:
    with pytest.raises(ValueError):
        poisson_scoreline_probability(
            home_xg=home_xg,
            away_xg=away_xg,
            home_goals=home_goals,
            away_goals=away_goals,
        )
