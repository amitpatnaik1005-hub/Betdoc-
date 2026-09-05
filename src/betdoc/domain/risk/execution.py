"""Execution risk for arbitrage: the EV of an attempt, not the EV of a quote.

Why "risk-free arbitrage" is a category error
---------------------------------------------
A two-leg arb is risk-free only if both legs fill in full at the quoted price.
In practice three things intervene:

#. **Latency.** Between detection and acknowledgement the price can move. The
   longer the round trip, the more of your edge is a free option written to the
   bookmaker.
#. **In-play delay.** Live soccer markets impose a 5 to 8 second acceptance
   countdown. You are not placing a bet: you are granting the book a free
   option to reject you if the market moves in your favour during the window,
   and to fill you if it moves against you. That asymmetry is a real cost and
   this module prices it.
#. **Partial fills.** A 500 unit stake matched at 100 leaves you 400 short on
   one leg, which converts a hedged position into a directional one. The
   correct object is therefore a *distribution over fill pairs*, not a point
   estimate.

What this module computes
-------------------------
Given planned stakes, quoted odds, a fair probability for leg 1, and a fill
distribution (supplied or derived from latency and delay), it returns the true
expected value, the probability of an actual loss, the tail loss, the naked
exposure probability, and a strict verdict enum.

The default distribution is a mixture of an independent coupling and a
comonotone coupling, weighted by ``steam_correlation``. Independence alone is
optimistic: a steam move usually hits both books at once, so leg failures are
positively correlated exactly when it hurts most. The comonotone component is
the maximally correlated coupling consistent with the marginals, so the mixture
spans the honest range between the two.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, localcontext
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ArbExecutionScenario",
    "ExecutionEvReport",
    "ExecutionRiskError",
    "ExecutionVerdict",
    "FillDistribution",
    "FillOutcome",
    "LegFillModel",
    "build_fill_distribution",
    "calculate_execution_ev",
]

_PROB_TOLERANCE: Final[float] = 1e-9
"""Tolerance for probability-mass identities. Never compare masses with ``==``."""

_RATIO_QUANT: Final[Decimal] = Decimal("0.000001")
_MONEY: Final[Decimal] = Decimal("0.01")
_INTERNAL_PRECISION: Final[int] = 28

DEFAULT_LINE_HAZARD_PER_SECOND: Final[float] = 0.08
"""Hazard rate of a material line move, per second of exposure.

Calibrate this from your own tick history: it is the rate at which the quoted
price you detected ceases to be available. 0.08/s implies roughly a 45% chance
of losing the price across a 7 second in-play countdown, which matches typical
observed live-soccer rejection rates. Do not ship the default unmeasured.
"""

DEFAULT_GOAL_HAZARD_PER_SECOND: Final[float] = 0.0005
"""Hazard of a goal in open play, per second (about 2.7 goals per 90 minutes).

During an in-play acceptance delay a goal is the dominant void-or-reject cause,
so the delay window is priced with this hazard on top of the line hazard.
"""


class ExecutionRiskError(ValueError):
    """Base class for every rejected execution-risk calculation."""


# --------------------------------------------------------------------------- #
# Strict result types
# --------------------------------------------------------------------------- #


class FillOutcome(StrEnum):
    """Classification of a single leg's fill, derived from its ratio."""

    FULL = "full"
    PARTIAL = "partial"
    REJECTED = "rejected"

    @classmethod
    def from_ratio(cls, ratio: float) -> FillOutcome:
        """Classify a fill ratio using explicit tolerances, never bare equality."""
        if math.isclose(ratio, 0.0, rel_tol=0.0, abs_tol=_PROB_TOLERANCE):
            return cls.REJECTED
        if math.isclose(ratio, 1.0, rel_tol=0.0, abs_tol=_PROB_TOLERANCE):
            return cls.FULL
        return cls.PARTIAL


class ExecutionVerdict(StrEnum):
    """Terminal decision returned to the executor. Exhaustive by construction.

    A strict enum prevents the classic silent failure of returning a bare float
    EV that a caller then compares against the wrong threshold.
    """

    EXECUTE = "execute"
    """Positive EV, tolerable tail, both legs worth firing at full size."""

    EXECUTE_REDUCED = "execute_reduced"
    """Positive EV but the loss tail breaches policy. Size down or pre-position."""

    ABORT_NEGATIVE_EV = "abort_negative_ev"
    """Execution friction consumes the entire quoted edge."""

    ABORT_EXCESSIVE_TAIL = "abort_excessive_tail"
    """Worst-case loss breaches the per-attempt loss cap regardless of EV."""

    ABORT_STALE = "abort_stale"
    """Total exposure window exceeds policy. The quote cannot be trusted."""

    @property
    def should_execute(self) -> bool:
        """True only for verdicts that permit sending an order."""
        return self in (ExecutionVerdict.EXECUTE, ExecutionVerdict.EXECUTE_REDUCED)


# --------------------------------------------------------------------------- #
# Scenario and distribution
# --------------------------------------------------------------------------- #

_STRICT: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_default=True,
    revalidate_instances="never",
)


class ArbExecutionScenario(BaseModel):
    """One realisable fill state of a two-leg arbitrage attempt.

    A scenario is a *joint* state: both legs' fill ratios together, plus the
    probability of that joint state. Modelling the legs separately is the error
    that makes correlated leg failure invisible.

    Attributes:
        leg_1_fill_ratio: Fraction of the planned leg 1 stake matched, ``[0, 1]``.
        leg_2_fill_ratio: Fraction of the planned leg 2 stake matched, ``[0, 1]``.
        leg_1_odds: Decimal odds actually struck on leg 1. Pass
            commission-adjusted odds for exchange legs.
        leg_2_odds: Decimal odds actually struck on leg 2.
        probability: Probability of this joint state, ``[0, 1]``.
    """

    model_config = _STRICT

    leg_1_fill_ratio: float = Field(ge=0.0, le=1.0)
    leg_2_fill_ratio: float = Field(ge=0.0, le=1.0)
    leg_1_odds: float = Field(gt=1.0, le=10_000.0)
    leg_2_odds: float = Field(gt=1.0, le=10_000.0)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)

    # ------------------------------ classification ---------------------- #

    @property
    def leg_1_outcome(self) -> FillOutcome:
        return FillOutcome.from_ratio(self.leg_1_fill_ratio)

    @property
    def leg_2_outcome(self) -> FillOutcome:
        return FillOutcome.from_ratio(self.leg_2_fill_ratio)

    @property
    def is_complete(self) -> bool:
        """Both legs filled in full: the only genuinely hedged state."""
        return (
            self.leg_1_outcome is FillOutcome.FULL and self.leg_2_outcome is FillOutcome.FULL
        )

    @property
    def is_naked(self) -> bool:
        """Exactly one leg filled at all: a directional position we never wanted."""
        filled_1 = self.leg_1_outcome is not FillOutcome.REJECTED
        filled_2 = self.leg_2_outcome is not FillOutcome.REJECTED
        return filled_1 is not filled_2

    @property
    def is_no_fill(self) -> bool:
        """Neither leg matched: zero P&L, but quota and latency were spent."""
        return (
            self.leg_1_outcome is FillOutcome.REJECTED
            and self.leg_2_outcome is FillOutcome.REJECTED
        )

    # --------------------------------- P&L ------------------------------ #

    def matched_stakes(
        self, planned_leg_1: Decimal, planned_leg_2: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Actual matched stakes for this scenario, rounded DOWN to the cent."""
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            r1 = Decimal(str(self.leg_1_fill_ratio)).quantize(_RATIO_QUANT)
            r2 = Decimal(str(self.leg_2_fill_ratio)).quantize(_RATIO_QUANT)
            return (
                (planned_leg_1 * r1).quantize(_MONEY, rounding=ROUND_DOWN),
                (planned_leg_2 * r2).quantize(_MONEY, rounding=ROUND_DOWN),
            )

    def profit_if_leg_1_wins(
        self, planned_leg_1: Decimal, planned_leg_2: Decimal
    ) -> Decimal:
        """Net P&L when outcome 1 settles: leg 1 pays, leg 2 is lost."""
        s1, s2 = self.matched_stakes(planned_leg_1, planned_leg_2)
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            payout = s1 * Decimal(str(self.leg_1_odds))
            return (payout - s1 - s2).quantize(_MONEY, rounding=ROUND_DOWN)

    def profit_if_leg_2_wins(
        self, planned_leg_1: Decimal, planned_leg_2: Decimal
    ) -> Decimal:
        """Net P&L when outcome 2 settles: leg 2 pays, leg 1 is lost."""
        s1, s2 = self.matched_stakes(planned_leg_1, planned_leg_2)
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            payout = s2 * Decimal(str(self.leg_2_odds))
            return (payout - s1 - s2).quantize(_MONEY, rounding=ROUND_DOWN)

    def worst_case_profit(
        self, planned_leg_1: Decimal, planned_leg_2: Decimal
    ) -> Decimal:
        """Minimum P&L across both settlement outcomes. Positive means locked."""
        return min(
            self.profit_if_leg_1_wins(planned_leg_1, planned_leg_2),
            self.profit_if_leg_2_wins(planned_leg_1, planned_leg_2),
        )

    def expected_profit(
        self,
        planned_leg_1: Decimal,
        planned_leg_2: Decimal,
        leg_1_true_probability: float,
    ) -> Decimal:
        """Probability-weighted P&L of this scenario under the fair probability."""
        p1 = Decimal(str(leg_1_true_probability))
        with localcontext() as ctx:
            ctx.prec = _INTERNAL_PRECISION
            value = p1 * self.profit_if_leg_1_wins(planned_leg_1, planned_leg_2) + (
                Decimal(1) - p1
            ) * self.profit_if_leg_2_wins(planned_leg_1, planned_leg_2)
            return value.quantize(_MONEY, rounding=ROUND_DOWN)


class FillDistribution(BaseModel):
    """A complete, normalised probability distribution over fill scenarios.

    The validator enforces total mass 1 within :data:`_PROB_TOLERANCE` and
    rejects duplicate fill pairs. An unnormalised distribution silently scales
    every EV downstream, which is precisely the class of bug that makes a
    negative-EV strategy look profitable in a backtest.
    """

    model_config = _STRICT

    scenarios: tuple[ArbExecutionScenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_mass_and_uniqueness(self) -> Self:
        total = math.fsum(s.probability for s in self.scenarios)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_PROB_TOLERANCE):
            msg = f"fill probabilities must sum to 1.0 within {_PROB_TOLERANCE}, got {total!r}"
            raise ValueError(msg)

        keys = [
            (
                Decimal(str(s.leg_1_fill_ratio)).quantize(_RATIO_QUANT),
                Decimal(str(s.leg_2_fill_ratio)).quantize(_RATIO_QUANT),
            )
            for s in self.scenarios
        ]
        if len(set(keys)) != len(keys):
            msg = "duplicate (leg_1_fill_ratio, leg_2_fill_ratio) pairs in distribution"
            raise ValueError(msg)

        odds_pairs = {(s.leg_1_odds, s.leg_2_odds) for s in self.scenarios}
        if len(odds_pairs) != 1:
            msg = "all scenarios in a distribution must share the same odds pair"
            raise ValueError(msg)
        return self

    @property
    def complete_fill_probability(self) -> float:
        return math.fsum(s.probability for s in self.scenarios if s.is_complete)

    @property
    def naked_exposure_probability(self) -> float:
        """Probability of ending with a single-sided, directional position."""
        return math.fsum(s.probability for s in self.scenarios if s.is_naked)

    @property
    def no_fill_probability(self) -> float:
        return math.fsum(s.probability for s in self.scenarios if s.is_no_fill)


class LegFillModel(BaseModel):
    """Marginal fill model for one leg, driven by its exposure window.

    Two hazards are superimposed:

    * **Line hazard** over the full exposure window (latency plus in-play
      delay): the price we detected stops being available.
    * **Goal hazard** over the in-play delay only: a material match event
      occurs mid-countdown, which voids or rejects the bet outright.

    Survival of the quote is ``exp(-(lambda_line * t_total + lambda_goal * t_delay))``.
    The complementary mass is split between a partial fill and an outright
    rejection by ``partial_share``, since a moving line often leaves residual
    liquidity at the old price rather than none at all.

    Attributes:
        latency_ms: Round-trip time from decision to venue acknowledgement.
        in_play_delay_seconds: Venue acceptance countdown, typically 5 to 8 for
            live soccer, 0 for pre-match.
        line_hazard_per_second: Calibrated rate of losing the quoted price.
        goal_hazard_per_second: Rate of a material match event, delay window only.
        partial_share: Of the mass where the price is lost, the fraction that
            still fills partially rather than being rejected, ``[0, 1]``.
        partial_ratio: Expected fill ratio conditional on a partial fill.
    """

    model_config = _STRICT

    latency_ms: float = Field(ge=0.0, le=600_000.0)
    in_play_delay_seconds: float = Field(default=0.0, ge=0.0, le=120.0)
    line_hazard_per_second: float = Field(
        default=DEFAULT_LINE_HAZARD_PER_SECOND, ge=0.0, le=10.0
    )
    goal_hazard_per_second: float = Field(
        default=DEFAULT_GOAL_HAZARD_PER_SECOND, ge=0.0, le=1.0
    )
    partial_share: float = Field(default=0.35, ge=0.0, le=1.0)
    partial_ratio: float = Field(default=0.4, gt=0.0, lt=1.0)

    @property
    def exposure_seconds(self) -> float:
        """Total window during which the quote can move against us."""
        return self.latency_ms / 1_000.0 + self.in_play_delay_seconds

    @property
    def material_event_probability(self) -> float:
        """Probability of a goal landing inside the acceptance countdown.

        Small in absolute terms (well under 1% for an 8 second delay) but it is
        the dominant *catastrophic* branch, because it voids the leg while the
        other leg stays live.
        """
        return -math.expm1(-self.goal_hazard_per_second * self.in_play_delay_seconds)

    @property
    def quote_survival_probability(self) -> float:
        """Probability the quoted price is still there when we are acknowledged."""
        exponent = (
            self.line_hazard_per_second * self.exposure_seconds
            + self.goal_hazard_per_second * self.in_play_delay_seconds
        )
        return math.exp(-exponent)

    def marginal(self) -> tuple[tuple[float, float], ...]:
        """Marginal distribution as ``((ratio, probability), ...)``.

        Returned in descending ratio order, which the comonotone coupling
        depends on. Masses below the tolerance are dropped and the remainder is
        renormalised so the result is always a valid distribution.
        """
        survive = self.quote_survival_probability
        lost = max(1.0 - survive, 0.0)
        partial = lost * self.partial_share
        rejected = lost - partial

        candidates = (
            (1.0, survive),
            (self.partial_ratio, partial),
            (0.0, rejected),
        )
        kept = [(r, p) for r, p in candidates if p > _PROB_TOLERANCE]
        if not kept:  # pragma: no cover - survive + lost == 1 by construction
            return ((0.0, 1.0),)

        total = math.fsum(p for _, p in kept)
        return tuple((r, p / total) for r, p in kept)


# --------------------------------------------------------------------------- #
# Couplings
# --------------------------------------------------------------------------- #


def _independent_join(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> dict[tuple[float, float], float]:
    """Product coupling: leg failures are unrelated. The optimistic bound."""
    joint: dict[tuple[float, float], float] = {}
    for ra, pa in a:
        for rb, pb in b:
            joint[ra, rb] = joint.get((ra, rb), 0.0) + pa * pb
    return joint


def _comonotone_join(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> dict[tuple[float, float], float]:
    """Maximally correlated coupling consistent with both marginals.

    Both marginals are sorted descending by fill ratio and driven by a single
    shared uniform draw, so the best fills on both legs occur together and the
    rejections occur together. This is the "one steam move kills both legs"
    world and is the pessimistic bound on execution risk.

    Implemented as an interval-intersection sweep of the two CDFs, which is
    exact and linear in the number of atoms.
    """
    joint: dict[tuple[float, float], float] = {}
    i = j = 0
    cum_a = a[0][1]
    cum_b = b[0][1]
    cursor = 0.0

    while i < len(a) and j < len(b):
        edge = min(cum_a, cum_b)
        segment = edge - cursor
        if segment > _PROB_TOLERANCE:
            key = (a[i][0], b[j][0])
            joint[key] = joint.get(key, 0.0) + segment
        cursor = edge
        if cursor >= 1.0 - _PROB_TOLERANCE:
            break
        if math.isclose(cum_a, cursor, rel_tol=0.0, abs_tol=_PROB_TOLERANCE):
            i += 1
            if i >= len(a):
                break
            cum_a += a[i][1]
        if math.isclose(cum_b, cursor, rel_tol=0.0, abs_tol=_PROB_TOLERANCE):
            j += 1
            if j >= len(b):
                break
            cum_b += b[j][1]
    return joint


def build_fill_distribution(
    *,
    leg_1_model: LegFillModel,
    leg_2_model: LegFillModel,
    leg_1_odds: float,
    leg_2_odds: float,
    steam_correlation: float = 0.35,
) -> FillDistribution:
    """Derive a joint fill distribution from per-leg latency and delay models.

    The result is the mixture
    ``(1 - rho) * independent + rho * comonotone``.

    Args:
        leg_1_model: Marginal model for leg 1.
        leg_2_model: Marginal model for leg 2.
        leg_1_odds: Decimal odds struck on leg 1.
        leg_2_odds: Decimal odds struck on leg 2.
        steam_correlation: ``rho`` in ``[0, 1]``. Zero assumes the legs fail
            independently, one assumes a common cause. Calibrate from the
            observed joint rejection rate of your own order log; 0.35 is a
            deliberately conservative starting point for liquid soccer markets.

    Returns:
        A normalised :class:`FillDistribution`.

    Raises:
        ExecutionRiskError: ``steam_correlation`` is outside ``[0, 1]``.
    """
    if not math.isfinite(steam_correlation) or not 0.0 <= steam_correlation <= 1.0:
        msg = f"steam_correlation must be in [0, 1], got {steam_correlation!r}"
        raise ExecutionRiskError(msg)

    marginal_1 = leg_1_model.marginal()
    marginal_2 = leg_2_model.marginal()

    independent = _independent_join(marginal_1, marginal_2)
    comonotone = _comonotone_join(marginal_1, marginal_2)

    blended: dict[tuple[float, float], float] = {}
    for key, mass in independent.items():
        blended[key] = blended.get(key, 0.0) + (1.0 - steam_correlation) * mass
    for key, mass in comonotone.items():
        blended[key] = blended.get(key, 0.0) + steam_correlation * mass

    kept = {k: v for k, v in blended.items() if v > _PROB_TOLERANCE}
    total = math.fsum(kept.values())
    if total <= 0.0:  # pragma: no cover - marginals are valid distributions
        msg = "fill distribution collapsed to zero mass"
        raise ExecutionRiskError(msg)

    scenarios = tuple(
        ArbExecutionScenario(
            leg_1_fill_ratio=r1,
            leg_2_fill_ratio=r2,
            leg_1_odds=leg_1_odds,
            leg_2_odds=leg_2_odds,
            probability=mass / total,
        )
        for (r1, r2), mass in sorted(kept.items(), reverse=True)
    )
    return FillDistribution(scenarios=scenarios)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


class ExecutionEvReport(BaseModel):
    """Full execution-risk assessment of one arbitrage attempt."""

    model_config = _STRICT

    verdict: ExecutionVerdict

    quoted_edge: float = Field(description="1 - sum of implied probabilities, as quoted.")
    leg_1_true_probability: float = Field(ge=0.0, le=1.0)

    expected_profit: Decimal
    expected_turnover: Decimal
    ev_per_unit_turnover: float

    guaranteed_profit_probability: float = Field(ge=0.0, le=1.0)
    loss_probability: float = Field(ge=0.0, le=1.0)
    naked_exposure_probability: float = Field(ge=0.0, le=1.0)
    complete_fill_probability: float = Field(ge=0.0, le=1.0)

    worst_case_loss: Decimal = Field(description="Most negative P&L across all states.")
    expected_shortfall: Decimal = Field(description="Mean loss conditional on a loss.")

    exposure_seconds: float = Field(ge=0.0)
    material_event_probability: float = Field(ge=0.0, le=1.0)

    scenario_count: int = Field(gt=0)

    @property
    def is_true_arbitrage(self) -> bool:
        """True only when no reachable state produces a loss.

        This is almost never true once latency and in-play delay are priced,
        which is the entire point of the module.
        """
        return self.loss_probability <= _PROB_TOLERANCE


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def calculate_execution_ev(
    *,
    leg_1_odds: float,
    leg_2_odds: float,
    planned_leg_1_stake: Decimal | float | int | str,
    planned_leg_2_stake: Decimal | float | int | str,
    latency_ms: float,
    in_play_delay_seconds: float = 0.0,
    leg_1_true_probability: float | None = None,
    distribution: FillDistribution | None = None,
    line_hazard_per_second: float = DEFAULT_LINE_HAZARD_PER_SECOND,
    goal_hazard_per_second: float = DEFAULT_GOAL_HAZARD_PER_SECOND,
    steam_correlation: float = 0.35,
    min_ev_per_turnover: float = 0.002,
    max_loss_probability: float = 0.10,
    max_worst_case_loss: Decimal | float | int | str | None = None,
    max_exposure_seconds: float = 12.0,
) -> ExecutionEvReport:
    """Expected value of an arbitrage *attempt*, net of execution risk.

    Method:
        #. Build (or accept) a joint distribution over fill pairs. When derived,
           the exposure window is ``latency_ms / 1000 + in_play_delay_seconds``
           and the survival probability of each quote decays exponentially in
           that window, with an extra goal-hazard term applied to the in-play
           countdown only.
        #. For every scenario compute the P&L under both settlement outcomes,
           using integer-cent ``Decimal`` arithmetic.
        #. Weight by the fair probability of outcome 1 and by the scenario
           probability, and aggregate EV, loss probability, tail loss and naked
           exposure probability.
        #. Return a strict :class:`ExecutionVerdict` rather than a bare number.

    Args:
        leg_1_odds: Decimal odds on outcome 1. Pass commission-adjusted odds
            for exchange legs, from ``domain.arbitrage.exchange``.
        leg_2_odds: Decimal odds on outcome 2, the complement of outcome 1.
        planned_leg_1_stake: Stake to send to venue 1.
        planned_leg_2_stake: Stake to send to venue 2.
        latency_ms: Round-trip to venue acknowledgement.
        in_play_delay_seconds: Acceptance countdown, typically 5-8 for live soccer.
        leg_1_true_probability: True probability outcome 1 hits. If None, it
            is derived from the quoted odds assuming balanced margins.
        distribution: An explicit ``FillDistribution``. If None, one is built
            using the hazard models and steam correlation.
        min_ev_per_turnover: Minimum acceptable EV / expected_turnover ratio.
        max_loss_probability: Hard cap on the chance of losing money.
        max_worst_case_loss: Hard cap on the absolute worst-case scenario.
        max_exposure_seconds: Hard cap on the time the quote is held open.

    Returns:
        An ``ExecutionEvReport`` containing the verdict and the risk profile.
    """
    pass
