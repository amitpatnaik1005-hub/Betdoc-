"""Shared strategies, fixtures and Hypothesis profiles for the math vault.

Profile policy: the ``dev`` profile keeps the inner loop fast, the ``ci``
profile turns the volume up, and the ``solver`` profile exists because cvxpy
solves take tens of milliseconds, so a 1000-example property test against the
optimiser would take minutes. Solver-touching property tests use ``solver``
explicitly rather than silently inheriting a profile that makes CI crawl.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Final

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, settings
from hypothesis import strategies as st
from numpy.typing import NDArray

from betdoc.domain.math.ev_filter import MarketOutcome, MarketQuote
from betdoc.domain.math.parlay_correlation import LegState, Parlay, ParlayLeg
from betdoc.domain.math.portfolio_kelly import BetCandidate

# --------------------------------------------------------------------------- #
# Hypothesis profiles
# --------------------------------------------------------------------------- #

settings.register_profile(
    "dev",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=1_000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "solver",
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
settings.load_profile("dev")

MIN_TRADABLE_ODDS: Final[float] = 1.0101
MAX_TRADABLE_ODDS: Final[float] = 1_000.0


# --------------------------------------------------------------------------- #
# Primitive strategies
# --------------------------------------------------------------------------- #

decimal_odds = st.floats(
    min_value=MIN_TRADABLE_ODDS,
    max_value=MAX_TRADABLE_ODDS,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)

probabilities = st.floats(
    min_value=1e-4,
    max_value=1.0 - 1e-4,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)

bankrolls_inr = st.integers(min_value=1, max_value=50_000_000).map(
    lambda paise: (Decimal(paise) / 100).quantize(Decimal("0.01"))
)


# --------------------------------------------------------------------------- #
# Market strategies
# --------------------------------------------------------------------------- #


@st.composite
def complete_books(
    draw: st.DrawFn,
    *,
    min_outcomes: int = 2,
    max_outcomes: int = 10,
    min_margin: float = 0.005,
    max_margin: float = 0.25,
) -> MarketQuote:
    """A complete bookmaker book with a controlled overround.

    Constructed by drawing arbitrary positive weights, normalising them, then
    inflating to the target booksum. This guarantees ``booksum > 1``, which is
    the precondition every devig method requires.
    """
    n = draw(st.integers(min_value=min_outcomes, max_value=max_outcomes))
    weights = draw(
        st.lists(
            st.floats(0.05, 1.0, allow_nan=False, allow_infinity=False),
            min_size=n,
            max_size=n,
        )
    )
    margin = draw(
        st.floats(min_margin, max_margin, allow_nan=False, allow_infinity=False)
    )
    total = math.fsum(weights)
    assume(total > 0.0)

    booksum = 1.0 + margin
    implied = [weight / total * booksum for weight in weights]
    assume(all(1e-4 <= value <= 0.99 for value in implied))

    outcomes = tuple(
        MarketOutcome(outcome_key=f"outcome_{index}", decimal_odds=1.0 / value)
        for index, value in enumerate(implied)
    )
    return MarketQuote(
        bookmaker="hypothesis_book",
        outcomes=outcomes,
        is_sharp=draw(st.booleans()),
    )


@st.composite
def zero_vig_books(draw: st.DrawFn, *, n_outcomes: int = 3) -> MarketQuote:
    """A book whose implied probabilities sum to exactly 1.0.

    Shin's ``z`` must solve to approximately zero here, and Shin and the power
    method must agree to machine tolerance. This is the analytic fixed point of
    both estimators and therefore the strongest single correctness check on
    the devig code.
    """
    weights = draw(
        st.lists(
            st.floats(0.1, 1.0, allow_nan=False, allow_infinity=False),
            min_size=n_outcomes,
            max_size=n_outcomes,
        )
    )
    total = math.fsum(weights)
    assume(total > 0.0)
    implied = [weight / total for weight in weights]
    assume(all(1e-4 <= value <= 0.99 for value in implied))
    outcomes = tuple(
        MarketOutcome(outcome_key=f"fair_{index}", decimal_odds=1.0 / value)
        for index, value in enumerate(implied)
    )
    return MarketQuote(bookmaker="fair_book", outcomes=outcomes, is_sharp=True)


# --------------------------------------------------------------------------- #
# Correlation matrix strategies
# --------------------------------------------------------------------------- #


@st.composite
def psd_correlation_matrices(draw: st.DrawFn, n: int) -> NDArray[np.float64]:
    """Guaranteed-PSD correlation matrix via random factor loadings.

    Built as ``normalise(A A')`` for a random loading matrix ``A``, which is
    PSD by construction. Use where the test is about the copula, not about
    matrix repair.
    """
    if n == 1:
        return np.ones((1, 1), dtype=np.float64)
    loadings = draw(
        st.lists(
            st.lists(
                st.floats(-1.5, 1.5, allow_nan=False, allow_infinity=False),
                min_size=n,
                max_size=n,
            ),
            min_size=n,
            max_size=n,
        )
    )
    array = np.asarray(loadings, dtype=np.float64)
    covariance = array @ array.T + 1e-6 * np.eye(n)
    scale = np.sqrt(np.diag(covariance))
    assume(np.all(scale > 1e-8))
    matrix = covariance / np.outer(scale, scale)
    np.fill_diagonal(matrix, 1.0)
    return (matrix + matrix.T) / 2.0


@st.composite
def rough_correlation_matrices(draw: st.DrawFn, n: int) -> NDArray[np.float64]:
    """Arbitrary symmetric unit-diagonal matrix, frequently NOT PSD.

    Deliberately invalid input, used to exercise the PSD repair path. Real
    correlation matrices elicited pairwise from judgement look exactly like
    this, so repair is a production path and not a defensive nicety.
    """
    matrix = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            value = draw(
                st.floats(-0.99, 0.99, allow_nan=False, allow_infinity=False)
            )
            matrix[i, j] = matrix[j, i] = value
    return matrix


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_leg(
    leg_id: str,
    *,
    probability: float,
    odds: float,
    risk_factor: str,
    void_probability: float = 0.0,
    state: LegState = LegState.PENDING,
) -> ParlayLeg:
    return ParlayLeg(
        leg_id=leg_id,
        market_key=f"market_{leg_id}",
        marginal_probability=probability,
        decimal_odds=odds,
        void_probability=void_probability,
        state=state,
        risk_factor_key=risk_factor,
    )


def make_candidate(
    bet_id: str,
    *,
    market_key: str,
    probability: float,
    odds: float,
    outcome_key: str | None = None,
) -> BetCandidate:
    return BetCandidate(
        bet_id=bet_id,
        market_key=market_key,
        outcome_key=outcome_key or bet_id,
        fair_probability=probability,
        decimal_odds=odds,
    )


@st.composite
def bet_candidates(draw: st.DrawFn, *, min_size: int = 1, max_size: int = 6) -> tuple[BetCandidate, ...]:
    """Candidate sets spanning several markets, with a mix of edges."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    candidates: list[BetCandidate] = []
    for index in range(n):
        probability = draw(st.floats(0.05, 0.85, allow_nan=False, allow_infinity=False))
        # Draw the price as a multiple of the fair price so edge is controllable
        # in both directions, including the negative-EV cases the prefilter must
        # discard.
        multiplier = draw(st.floats(0.85, 1.6, allow_nan=False, allow_infinity=False))
        odds = (1.0 / probability) * multiplier
        assume(MIN_TRADABLE_ODDS < odds <= MAX_TRADABLE_ODDS)
        candidates.append(
            make_candidate(
                f"bet_{index}",
                market_key=f"market_{draw(st.integers(0, 2))}",
                probability=probability,
                odds=odds,
            )
        )
    # Reject sets where one market's probabilities exceed 1.0, which
    # build_scenarios legitimately refuses.
    by_market: dict[str, float] = {}
    for candidate in candidates:
        by_market[candidate.market_key] = (
            by_market.get(candidate.market_key, 0.0) + candidate.fair_probability
        )
    assume(all(total <= 1.0 for total in by_market.values()))
    return tuple(candidates)


# --------------------------------------------------------------------------- #
# Fixtures: the golden Real Madrid book
# --------------------------------------------------------------------------- #


@pytest.fixture
def real_madrid_correct_score_book() -> MarketQuote:
    """A realistic correct-score book carrying roughly 18% overround.

    Correct-score markets are the canonical high-variance trap: eleven-plus
    outcomes, a fat margin, and tiny per-outcome probabilities whose relative
    estimation error is enormous. This is the market from Scenario 1.
    """
    prices: tuple[tuple[str, float], ...] = (
        ("0-0", 13.00),
        ("1-0", 8.50),
        ("2-0", 9.50),
        ("3-0", 15.00),
        ("0-1", 15.00),
        ("1-1", 8.00),
        ("2-1", 9.00),
        ("0-2", 26.00),
        ("1-2", 15.00),
        ("2-2", 17.00),
        ("ANY_OTHER", 2.80),
    )
    market = MarketQuote(
        bookmaker="pinnacle",
        outcomes=tuple(
            MarketOutcome(outcome_key=key, decimal_odds=odds) for key, odds in prices
        ),
        is_sharp=True,
    )
    # Guard the fixture itself. If someone edits a price and breaks the
    # overround, the failure should point here, not at the module under test.
    assert 1.10 < market.booksum < 1.30, f"fixture booksum drifted: {market.booksum}"
    return market


@pytest.fixture
def bankroll_two_lakh_inr() -> Decimal:
    """INR 200,000, so the 2% high-variance cap is exactly 400,000 paise."""
    return Decimal("200000.00")


@pytest.fixture
def three_leg_correlated_parlay() -> Parlay:
    """Team A win + Over 2.5 + BTTS yes, on a INR 1,000 stake.

    Prices chosen to multiply exactly in ``Decimal``: 2.10 * 1.80 * 1.90.
    """
    return Parlay(
        parlay_id="parlay_golden_1",
        legs=(
            make_leg("leg_win", probability=0.50, odds=2.10, risk_factor="TEAM_A_LOSES"),
            make_leg("leg_over", probability=0.58, odds=1.80, risk_factor="UNDER_2_5"),
            make_leg("leg_btts", probability=0.55, odds=1.90, risk_factor="NO_BTTS"),
        ),
        stake_paise=100_000,
    )


@pytest.fixture
def positive_correlation_matrix() -> NDArray[np.float64]:
    """Team A winning, Over 2.5 and BTTS are all positively linked."""
    return np.array(
        [
            [1.00, 0.25, 0.20],
            [0.25, 1.00, 0.55],
            [0.20, 0.55, 1.00],
        ],
        dtype=np.float64,
    )
