"""Scenario 1: the Blind Confidence trap.

Gates a proposed stake on a high-variance market (worked case: Real Madrid
2-0 correct score, INR 20,000 requested) through four independent hurdles:

#. **Complete-book validation.** Shin's method solves for a single insider
   parameter across an entire market. A single selection cannot be devigged,
   so an incomplete book is rejected outright rather than silently normalised.
#. **Dual devig with cross-check.** Shin *and* Clarke's power method are run
   independently. Correct-score books carry 15% to 25% margin, where Shin is
   numerically fragile, so material disagreement between the two estimators is
   treated as evidence the price is untrustworthy and the bet is rejected.
#. **Point-estimate EV gate.** Non-positive EV raises
   :class:`NegativeExpectedValueError`. No exceptions, no overrides.
#. **Robust Kelly sizing.** See the note below, which is the mathematically
   important part of this module.

Why robust Kelly and not "a Poisson tail penalty"
--------------------------------------------------
Expected log wealth for a binary bet is::

    g(f, p) = p * log(1 + f*b) + (1 - p) * log(1 - f)

This is **linear in p**. Consequently, integrating it over a Beta posterior on
``p`` returns the posterior *mean* and changes the optimal ``f`` by exactly
zero. Numerical quadrature over parameter uncertainty is, for this objective,
provably pointless. Any implementation that claims to shrink Kelly by
"averaging over the posterior" is either wrong or is secretly applying an
undocumented penalty.

The correct mechanism is a **robust quantile criterion**. Because ``g`` is
monotonically increasing in ``p`` for any fixed ``f``, the alpha-quantile of the
growth rate equals the growth rate evaluated at the alpha-quantile of ``p``::

    Q_alpha[g(f, p)] = g(f, Beta.ppf(alpha))

So we size on the lower confidence bound of the fair probability rather than
its mean. This down-weights exactly where the spec wants it down-weighted,
because a correct-score outcome carries a wide posterior (tiny ``p``, thin
information per outcome, high book margin) while a 1X2 line at a sharp book
carries a narrow one. The shrinkage is derived, not invented.

Poisson enters this module only as :func:`poisson_scoreline_probability`, an
*independent* second opinion on a scoreline price. It is never a penalty
multiplier.

Calibration warning
-------------------
``sharp_pseudo_count``, ``soft_pseudo_count`` and
``margin_information_penalty`` are the only unmeasured parameters here. They
set the width of the posterior and therefore the aggressiveness of the
shrinkage. Fit them against realised CLV before trusting the output at size.
Shipping the defaults unmeasured means the sizing is an opinion.
"""

from __future__ import annotations

import math
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Final, Self

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import brentq
from scipy.stats import beta as beta_dist
from scipy.stats import poisson

from betdoc.domain.math.errors import (
    DevigDisagreementError,
    IncompleteMarketError,
    NegativeExpectedValueError,
    NumericalSolutionError,
)
from betdoc.domain.math.money import from_paise, to_paise

__all__ = [
    "DevigMethod",
    "DevigResult",
    "EvFilterConfig",
    "EvFilterDecision",
    "EvFilterRequest",
    "EvVerdict",
    "MarketOutcome",
    "MarketQuote",
    "evaluate_ev",
    "kelly_fraction",
    "poisson_scoreline_probability",
    "power_devig",
    "shin_devig",
]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="domain.math.ev_filter"
)

MIN_DECIMAL_ODDS: Final[float] = 1.01
MAX_DECIMAL_ODDS: Final[float] = 10_000.0
_PROB_TOLERANCE: Final[float] = 1e-9
_ROOT_TOLERANCE: Final[float] = 1e-12
_INTERNAL_PRECISION: Final[int] = 34

_STRICT: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_default=True,
    revalidate_instances="never",
    str_strip_whitespace=True,
)


# --------------------------------------------------------------------------- #
# Strict result types
# --------------------------------------------------------------------------- #


class DevigMethod(StrEnum):
    """Which margin-removal estimator produced a probability vector."""

    SHIN = "shin"
    POWER = "power"


class EvVerdict(StrEnum):
    """Terminal decision. Exhaustive, so an unhandled branch fails loudly."""

    APPROVED = "approved"
    """Requested stake is at or below the robust Kelly allocation."""

    APPROVED_REDUCED = "approved_reduced"
    """Positive robust EV, but the stake was cut to the sizing limit."""

    REJECTED_FAILS_ROBUST_EV = "rejected_fails_robust_ev"
    """Positive EV at the point estimate, negative at the lower bound.

    The most important verdict in this module. It is the state a correct-score
    bet lands in when the edge exists only if your probability estimate is
    exactly right, which for a 15% margin book it is not.
    """

    REJECTED_BELOW_MIN_EDGE = "rejected_below_min_edge"
    """Edge is positive but too small to survive execution friction."""

    @property
    def is_approved(self) -> bool:
        return self in (EvVerdict.APPROVED, EvVerdict.APPROVED_REDUCED)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class MarketOutcome(BaseModel):
    """One priceable outcome in a complete book."""

    model_config = _STRICT

    outcome_key: str = Field(min_length=1, max_length=64)
    decimal_odds: float = Field(gt=MIN_DECIMAL_ODDS - 1e-9, le=MAX_DECIMAL_ODDS)

    @property
    def raw_implied_probability(self) -> float:
        """Gross implied probability. Still contains the bookmaker margin."""
        return 1.0 / self.decimal_odds


class MarketQuote(BaseModel):
    """A COMPLETE book from one bookmaker.

    For a correct-score market this must include an explicit
    "any other score" bucket, otherwise the book does not sum to a
    probability and no devig method is defined on it.
    """

    model_config = _STRICT

    bookmaker: str = Field(min_length=1, max_length=64)
    outcomes: tuple[MarketOutcome, ...] = Field(min_length=2)
    is_sharp: bool = Field(
        default=False,
        description="True for Pinnacle-class books. Drives posterior width.",
    )

    @model_validator(mode="after")
    def _validate_book(self) -> Self:
        keys = [o.outcome_key for o in self.outcomes]
        if len(set(keys)) != len(keys):
            msg = "duplicate outcome_key in market"
            raise ValueError(msg)
        if self.booksum <= 1.0 - _PROB_TOLERANCE:
            # Sub-100% is either an incomplete book or an arbitrage against
            # this single venue. Either way it is not devig-able.
            msg = (
                f"booksum {self.booksum!r} is below 1.0: the market is "
                "incomplete or internally arbitrageable"
            )
            raise ValueError(msg)
        return self

    @property
    def raw_implied(self) -> tuple[float, ...]:
        return tuple(o.raw_implied_probability for o in self.outcomes)

    @property
    def booksum(self) -> float:
        return math.fsum(self.raw_implied)

    @property
    def margin(self) -> float:
        """Overround. Correct-score books typically sit at 0.15 to 0.25."""
        return self.booksum - 1.0

    def index_of(self, outcome_key: str) -> int:
        for index, outcome in enumerate(self.outcomes):
            if outcome.outcome_key == outcome_key:
                return index
        msg = f"outcome_key {outcome_key!r} is not present in this market"
        raise KeyError(msg)


class EvFilterConfig(BaseModel):
    """Sizing and safety policy. Every threshold is explicit and auditable."""

    model_config = _STRICT

    fractional_kelly: float = Field(default=0.25, gt=0.0, le=1.0)
    robust_quantile: float = Field(
        default=0.25,
        gt=0.0,
        lt=0.5,
        description="Posterior quantile used for sizing. Lower is more cautious.",
    )
    high_variance_cap_fraction: float = Field(default=0.02, gt=0.0, le=1.0)
    standard_cap_fraction: float = Field(default=0.05, gt=0.0, le=1.0)
    high_variance_outcome_threshold: int = Field(
        default=6,
        ge=2,
        description="Books with at least this many outcomes are high variance.",
    )
    devig_relative_tolerance: float = Field(default=0.15, gt=0.0, le=1.0)
    high_margin_threshold: float = Field(default=0.12, gt=0.0, lt=1.0)
    sharp_pseudo_count: float = Field(default=200.0, gt=0.0)
    soft_pseudo_count: float = Field(default=40.0, gt=0.0)
    margin_information_penalty: float = Field(default=5.0, ge=0.0)
    min_edge_per_unit: float = Field(
        default=0.005,
        ge=0.0,
        description="Minimum EV per unit staked. Below this, friction wins.",
    )


class EvFilterRequest(BaseModel):
    """A user's proposed bet, plus the sharp market it is judged against."""

    model_config = _STRICT

    market: MarketQuote
    target_outcome_key: str = Field(min_length=1, max_length=64)
    offered_odds: float = Field(
        gt=MIN_DECIMAL_ODDS - 1e-9,
        le=MAX_DECIMAL_ODDS,
        description="Odds we can actually get, which may differ from the sharp book.",
    )
    requested_stake_inr: Decimal = Field(gt=Decimal(0))
    bankroll_inr: Decimal = Field(gt=Decimal(0))

    @model_validator(mode="after")
    def _target_exists(self) -> Self:
        self.market.index_of(self.target_outcome_key)
        return self

    @property
    def requested_stake_paise(self) -> int:
        return to_paise(self.requested_stake_inr, label="requested_stake_inr")

    @property
    def bankroll_paise(self) -> int:
        return to_paise(self.bankroll_inr, label="bankroll_inr")


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class DevigResult(BaseModel):
    """A margin-free probability vector plus the fitted parameter."""

    model_config = _STRICT

    method: DevigMethod
    probabilities: tuple[float, ...] = Field(min_length=2)
    parameter: float = Field(description="Shin's z, or the power method's k.")
    booksum: float
    margin: float
    low_confidence: bool = False

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        total = math.fsum(self.probabilities)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-8):
            msg = f"devigged probabilities must sum to 1.0, got {total!r}"
            raise ValueError(msg)
        return self


class EvFilterDecision(BaseModel):
    """The complete, auditable record of one sizing decision."""

    model_config = _STRICT

    verdict: EvVerdict
    requested_stake_paise: int = Field(ge=0)
    approved_stake_paise: int = Field(ge=0)
    bankroll_paise: int = Field(gt=0)

    fair_probability: float = Field(gt=0.0, lt=1.0)
    robust_probability: float = Field(gt=0.0, lt=1.0)
    offered_odds: float = Field(gt=1.0)

    ev_per_unit: Decimal
    robust_ev_per_unit: Decimal

    kelly_fraction: float = Field(ge=0.0, le=1.0)
    robust_kelly_fraction: float = Field(ge=0.0, le=1.0)
    final_fraction: float = Field(ge=0.0, le=1.0)
    binding_constraint: str

    posterior_alpha: float = Field(gt=0.0)
    posterior_beta: float = Field(gt=0.0)
    effective_sample_size: float = Field(gt=0.0)

    shin: DevigResult
    power: DevigResult
    low_confidence: bool
    rejection_reason: str | None = None

    @property
    def approved_stake_inr(self) -> Decimal:
        return from_paise(self.approved_stake_paise)

    @property
    def reduction_ratio(self) -> float:
        """How aggressively the request was cut. 1.0 means untouched."""
        if self.requested_stake_paise == 0:
            return 0.0
        return self.approved_stake_paise / self.requested_stake_paise

    @model_validator(mode="after")
    def _never_exceed_request_or_bankroll(self) -> Self:
        if self.approved_stake_paise > self.requested_stake_paise:
            msg = "approved stake may never exceed the requested stake"
            raise ValueError(msg)
        if self.approved_stake_paise > self.bankroll_paise:
            msg = "approved stake may never exceed the bankroll"
            raise ValueError(msg)
        if not self.verdict.is_approved and self.approved_stake_paise != 0:
            msg = "a rejected verdict must approve exactly zero"
            raise ValueError(msg)
        return self


# --------------------------------------------------------------------------- #
# Devig estimators
# --------------------------------------------------------------------------- #


def shin_devig(market: MarketQuote, *, high_margin_threshold: float = 0.12) -> DevigResult:
    """Remove margin using Shin's insider-trading model.

    Shin models the observed price as a bookmaker protecting against a fraction
    ``z`` of informed money::

        p_i = (sqrt(z^2 + 4(1-z) * pi_i^2 / sum(pi)) - z) / (2(1-z))

    where ``pi_i = 1 / o_i``. We solve for the unique ``z`` in ``[0, 1)`` that
    makes the probabilities sum to one, by Brent root-finding.

    Why Shin rather than proportional normalisation: proportional devigging
    spreads margin evenly, but books load margin onto longshots. On a
    correct-score market that bias is severe, and proportional devigging will
    manufacture phantom edges on unlikely scorelines. Shin corrects the
    direction of that bias.

    Args:
        market: A complete book.
        high_margin_threshold: Above this overround, flag low confidence.
            Shin is numerically fragile with high margin and many outcomes.

    Returns:
        A :class:`DevigResult` with method SHIN.

    Raises:
        IncompleteMarketError: The book does not sum above 1.0.
        NumericalSolutionError: No root exists in ``[0, 1)``.
    """
    booksum = market.booksum
    if booksum <= 1.0 + _PROB_TOLERANCE:
        _log.warning(
            "shin_devig.zero_margin_book",
            bookmaker=market.bookmaker,
            booksum=booksum,
        )
        # A book at exactly 100% needs no correction: z is identically zero.
        probabilities = tuple(pi / booksum for pi in market.raw_implied)
        return DevigResult(
            method=DevigMethod.SHIN,
            probabilities=probabilities,
            parameter=0.0,
            booksum=booksum,
            margin=booksum - 1.0,
            low_confidence=False,
        )

    pi = np.asarray(market.raw_implied, dtype=np.float64)
    k = pi**2 / booksum

    def _probabilities(z: float) -> np.ndarray:
        if z >= 1.0 - 1e-12:
            # Analytic limit as z -> 1, avoiding the 0/0 form.
            return k
        root = np.sqrt(z * z + 4.0 * (1.0 - z) * k)
        return (root - z) / (2.0 * (1.0 - z))

    def _residual(z: float) -> float:
        return float(np.sum(_probabilities(z))) - 1.0

    lo, hi = 0.0, 1.0 - 1e-12
    residual_lo, residual_hi = _residual(lo), _residual(hi)
    if residual_lo < 0.0:
        msg = "shin residual is negative at z=0, which contradicts booksum > 1"
        raise NumericalSolutionError(msg, booksum=booksum, residual=residual_lo)
    if residual_hi > 0.0:
        msg = (
            "no Shin solution in [0, 1): the book's squared-probability mass "
            "exceeds its booksum, which indicates corrupted odds"
        )
        _log.error(
            "shin_devig.no_root",
            bookmaker=market.bookmaker,
            booksum=booksum,
            residual_at_one=residual_hi,
        )
        raise NumericalSolutionError(msg, booksum=booksum, residual=residual_hi)

    z = float(brentq(_residual, lo, hi, xtol=_ROOT_TOLERANCE, maxiter=200))
    raw = _probabilities(z)
    total = float(np.sum(raw))
    probabilities = tuple(float(p / total) for p in raw)

    low_confidence = market.margin > high_margin_threshold
    if low_confidence:
        _log.warning(
            "shin_devig.high_margin",
            bookmaker=market.bookmaker,
            margin=market.margin,
            threshold=high_margin_threshold,
            outcome_count=len(market.outcomes),
            reason="shin is numerically fragile at high margin with many outcomes",
        )

    return DevigResult(
        method=DevigMethod.SHIN,
        probabilities=probabilities,
        parameter=z,
        booksum=booksum,
        margin=market.margin,
        low_confidence=low_confidence,
    )


def power_devig(market: MarketQuote) -> DevigResult:
    """Remove margin using Clarke's power (odds-ratio) method.

    Solves for the exponent ``k`` satisfying ``sum((1/o_i)^k) = 1``. Because
    every ``1/o_i`` is strictly below 1, the sum is monotonically decreasing in
    ``k``, so the root is unique and lies above 1 for any book with margin.

    This exists as an **independent** cross-check on Shin. Two estimators that
    agree give you evidence. One estimator gives you a number.

    Raises:
        NumericalSolutionError: The bracket search failed to converge.
    """
    booksum = market.booksum
    pi = np.asarray(market.raw_implied, dtype=np.float64)

    if booksum <= 1.0 + _PROB_TOLERANCE:
        probabilities = tuple(float(p / booksum) for p in pi)
        return DevigResult(
            method=DevigMethod.POWER,
            probabilities=probabilities,
            parameter=1.0,
            booksum=booksum,
            margin=booksum - 1.0,
            low_confidence=False,
        )

    def _residual(k: float) -> float:
        return float(np.sum(pi**k)) - 1.0

    lo, hi = 1.0, 2.0
    for _ in range(60):
        if _residual(hi) < 0.0:
            break
        hi *= 2.0
    else:  # pragma: no cover - unreachable for valid odds
        msg = "power devig bracket expansion failed"
        raise NumericalSolutionError(msg, booksum=booksum)

    k = float(brentq(_residual, lo, hi, xtol=_ROOT_TOLERANCE, maxiter=200))
    raw = pi**k
    total = float(np.sum(raw))
    probabilities = tuple(float(p / total) for p in raw)

    return DevigResult(
        method=DevigMethod.POWER,
        probabilities=probabilities,
        parameter=k,
        booksum=booksum,
        margin=market.margin,
        low_confidence=False,
    )


def poisson_scoreline_probability(
    *, home_xg: float, away_xg: float, home_goals: int, away_goals: int
) -> float:
    """Independent Poisson estimate of an exact scoreline probability.

    Provided as a *second opinion* on a correct-score price, never as a
    penalty multiplier. Independent goal Poissons are a known simplification:
    real scorelines exhibit low-score dependence, which is what the
    Dixon-Coles correction addresses. Treat a large divergence between this
    value and the devigged market as a reason to investigate, not as truth.

    Raises:
        ValueError: Any input is non-finite, negative, or a bool.
    """
    for label, value in (("home_xg", home_xg), ("away_xg", away_xg)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0.0:
            msg = f"{label} must be a finite positive number, got {value!r}"
            raise ValueError(msg)
    for label, goals in (("home_goals", home_goals), ("away_goals", away_goals)):
        if isinstance(goals, bool) or not isinstance(goals, int) or goals < 0:
            msg = f"{label} must be a non-negative int, got {goals!r}"
            raise ValueError(msg)

    return float(poisson.pmf(home_goals, home_xg) * poisson.pmf(away_goals, away_xg))


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def kelly_fraction(probability: float, decimal_odds: float) -> float:
    """Full-Kelly fraction for a single binary bet, clamped to ``[0, 1]``.

    ``f = (p*b - q) / b`` with ``b = odds - 1``. Returns exactly 0.0 for any
    non-positive edge, so the caller never has to interpret a negative
    fraction as "bet the other side".
    """
    if isinstance(probability, bool) or not math.isfinite(probability):
        msg = f"probability must be a finite number, got {probability!r}"
        raise ValueError(msg)
    if not 0.0 <= probability <= 1.0:
        msg = f"probability must be in [0, 1], got {probability!r}"
        raise ValueError(msg)
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        msg = f"decimal_odds must be finite and above 1.0, got {decimal_odds!r}"
        raise ValueError(msg)

    b = decimal_odds - 1.0
    fraction = (probability * b - (1.0 - probability)) / b
    if fraction <= 0.0:
        return 0.0
    return min(fraction, 1.0)


def _effective_sample_size(market: MarketQuote, config: EvFilterConfig) -> float:
    """Posterior concentration implied by the market's quality.

    Three effects, each with a documented direction:

    * A sharp book carries more information than a soft one.
    * More outcomes means thinner information per outcome, so we divide by
      ``sqrt(n)`` rather than ``n``, which would be too punitive.
    * Higher margin means the book is less willing to show its true opinion,
      so information decays in the overround.

    These are calibration parameters. Fit them against realised CLV.
    """
    base = config.sharp_pseudo_count if market.is_sharp else config.soft_pseudo_count
    outcome_penalty = math.sqrt(float(len(market.outcomes)))
    margin_penalty = 1.0 + config.margin_information_penalty * max(market.margin, 0.0)
    return max(base / (outcome_penalty * margin_penalty), 2.0)


def _ev_per_unit(probability: float, decimal_odds: float) -> Decimal:
    """Expected profit per unit staked, as an exact ``Decimal``."""
    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        p = Decimal(str(probability))
        o = Decimal(str(decimal_odds))
        return (p * o - Decimal(1)).quantize(Decimal("0.00000001"))


def evaluate_ev(
    request: EvFilterRequest, config: EvFilterConfig | None = None
) -> EvFilterDecision:
    """Gate a proposed stake through devig, EV and robust Kelly sizing.

    Args:
        request: The proposed bet and the sharp market it is judged against.
        config: Sizing and safety policy. Defaults are deliberately cautious.

    Returns:
        An :class:`EvFilterDecision` recording every intermediate value, so a
        decision can be re-audited months later without re-running the code.

    Raises:
        IncompleteMarketError: The book cannot be devigged.
        DevigDisagreementError: Shin and power disagree beyond tolerance.
        NegativeExpectedValueError: EV against the fair probability is not
            positive. This is the hard stop the spec requires.
    """
    cfg = config or EvFilterConfig()
    market = request.market
    index = market.index_of(request.target_outcome_key)

    # ---- Hurdle 1: complete book -------------------------------------- #
    if market.booksum <= 1.0 - _PROB_TOLERANCE:
        _log.error(
            "ev_filter.rejected",
            reason="incomplete_market",
            bookmaker=market.bookmaker,
            booksum=market.booksum,
            outcome_count=len(market.outcomes),
        )
        msg = "market is not a complete book and cannot be devigged"
        raise IncompleteMarketError(
            msg, booksum=market.booksum, outcome_count=len(market.outcomes)
        )

    # ---- Hurdle 2: dual devig with cross-check ------------------------- #
    shin = shin_devig(market, high_margin_threshold=cfg.high_margin_threshold)
    power = power_devig(market)

    p_shin = shin.probabilities[index]
    p_power = power.probabilities[index]
    denominator = max(p_shin, p_power, _PROB_TOLERANCE)
    relative_difference = abs(p_shin - p_power) / denominator

    if relative_difference > cfg.devig_relative_tolerance:
        _log.error(
            "ev_filter.rejected",
            reason="devig_disagreement",
            bookmaker=market.bookmaker,
            outcome_key=request.target_outcome_key,
            shin_probability=p_shin,
            power_probability=p_power,
            relative_difference=relative_difference,
            tolerance=cfg.devig_relative_tolerance,
            margin=market.margin,
        )
        msg = "Shin and power devig disagree beyond tolerance on the target outcome"
        raise DevigDisagreementError(
            msg,
            shin_probability=p_shin,
            power_probability=p_power,
            relative_difference=relative_difference,
            tolerance=cfg.devig_relative_tolerance,
        )

    # Shin is the primary estimator; power was the control.
    fair_probability = p_shin
    ev_per_unit = _ev_per_unit(fair_probability, request.offered_odds)

    # ---- Hurdle 3: point-estimate EV ---------------------------------- #
    if ev_per_unit <= 0:
        _log.error(
            "ev_filter.rejected",
            reason="negative_expected_value",
            outcome_key=request.target_outcome_key,
            fair_probability=fair_probability,
            offered_odds=request.offered_odds,
            ev_per_unit=str(ev_per_unit),
            requested_stake_paise=request.requested_stake_paise,
        )
        msg = "bet has non-positive expected value against the fair probability"
        raise NegativeExpectedValueError(
            msg,
            fair_probability=fair_probability,
            offered_odds=request.offered_odds,
            ev_per_unit=ev_per_unit,
        )

    # ---- Hurdle 4: robust Kelly sizing -------------------------------- #
    n_eff = _effective_sample_size(market, cfg)
    posterior_alpha = max(fair_probability * n_eff, 1e-6)
    posterior_beta = max((1.0 - fair_probability) * n_eff, 1e-6)
    robust_probability = float(
        beta_dist.ppf(cfg.robust_quantile, posterior_alpha, posterior_beta)
    )
    robust_probability = min(max(robust_probability, _PROB_TOLERANCE), 1.0 - _PROB_TOLERANCE)
    robust_ev_per_unit = _ev_per_unit(robust_probability, request.offered_odds)

    nominal_kelly = kelly_fraction(fair_probability, request.offered_odds)
    robust_kelly = kelly_fraction(robust_probability, request.offered_odds)

    is_high_variance = len(market.outcomes) >= cfg.high_variance_outcome_threshold
    cap_fraction = (
        cfg.high_variance_cap_fraction if is_high_variance else cfg.standard_cap_fraction
    )
    fractional = robust_kelly * cfg.fractional_kelly

    if fractional <= cap_fraction:
        final_fraction, binding = fractional, "fractional_robust_kelly"
    else:
        final_fraction, binding = cap_fraction, "bankroll_cap"

    common = {
        "requested_stake_paise": request.requested_stake_paise,
        "bankroll_paise": request.bankroll_paise,
        "fair_probability": fair_probability,
        "robust_probability": robust_probability,
        "offered_odds": request.offered_odds,
        "ev_per_unit": ev_per_unit,
        "robust_ev_per_unit": robust_ev_per_unit,
        "kelly_fraction": nominal_kelly,
        "robust_kelly_fraction": robust_kelly,
        "posterior_alpha": posterior_alpha,
        "posterior_beta": posterior_beta,
        "effective_sample_size": n_eff,
        "shin": shin,
        "power": power,
        "low_confidence": shin.low_confidence,
    }

    # The edge exists only if the point estimate is exactly right.
    if robust_ev_per_unit <= 0 or robust_kelly <= 0.0:
        _log.warning(
            "ev_filter.rejected",
            reason="fails_robust_ev",
            outcome_key=request.target_outcome_key,
            fair_probability=fair_probability,
            robust_probability=robust_probability,
            robust_ev_per_unit=str(robust_ev_per_unit),
            effective_sample_size=n_eff,
            detail="positive EV at the point estimate, non-positive at the lower bound",
        )
        return EvFilterDecision(
            verdict=EvVerdict.REJECTED_FAILS_ROBUST_EV,
            approved_stake_paise=0,
            final_fraction=0.0,
            binding_constraint="robust_ev_gate",
            rejection_reason=(
                "edge does not survive the "
                f"{cfg.robust_quantile:.0%} posterior lower bound"
            ),
            **common,
        )

    if float(robust_ev_per_unit) < cfg.min_edge_per_unit:
        _log.warning(
            "ev_filter.rejected",
            reason="below_min_edge",
            outcome_key=request.target_outcome_key,
            robust_ev_per_unit=str(robust_ev_per_unit),
            min_edge_per_unit=cfg.min_edge_per_unit,
        )
        return EvFilterDecision(
            verdict=EvVerdict.REJECTED_BELOW_MIN_EDGE,
            approved_stake_paise=0,
            final_fraction=0.0,
            binding_constraint="min_edge",
            rejection_reason=(
                f"robust edge {float(robust_ev_per_unit):.4f} is below the "
                f"{cfg.min_edge_per_unit:.4f} minimum"
            ),
            **common,
        )

    # Exact integer allocation. ROUND_DOWN, so the cap can never be breached.
    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        allowed_inr = request.bankroll_inr * Decimal(str(final_fraction))
    allowed_paise = to_paise(allowed_inr, label="allowed_stake")
    approved_paise = min(allowed_paise, request.requested_stake_paise)

    verdict = (
        EvVerdict.APPROVED
        if approved_paise == request.requested_stake_paise
        else EvVerdict.APPROVED_REDUCED
    )

    if verdict is EvVerdict.APPROVED_REDUCED:
        _log.info(
            "ev_filter.reduced",
            outcome_key=request.target_outcome_key,
            requested_stake_paise=request.requested_stake_paise,
            approved_stake_paise=approved_paise,
            binding_constraint=binding,
            final_fraction=final_fraction,
            robust_kelly_fraction=robust_kelly,
            low_confidence=shin.low_confidence,
        )

    return EvFilterDecision(
        verdict=verdict,
        approved_stake_paise=approved_paise,
        final_fraction=final_fraction,
        binding_constraint=binding,
        rejection_reason=None,
        **common,
    )
