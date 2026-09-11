from __future__ import annotations

import math
from dataclasses import dataclass

from betdoc.domain.math.errors import (
    DomainMathError,
    InvalidCorrelationMatrixError,
    NumericalSolutionError,
)


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainMathError("Expected a real number", field=label, value=value)
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise DomainMathError("Invalid numeric input", field=label) from exc
    if not math.isfinite(result):
        raise DomainMathError("Expected a finite number", field=label, value=value)
    return result


@dataclass(frozen=True)
class ParlayLeg:
    leg_id: str
    event_id: str
    fair_probability: float
    offered_odds: float


@dataclass(frozen=True)
class ParlayAnalysis:
    legs_count: int
    uncorrelated_joint_probability: float
    correlated_joint_probability: float
    sportsbook_offered_parlay_odds: float
    true_fair_parlay_odds: float
    edge_percentage: float
    is_positive_ev: bool
    compound_vig_percentage: float


class CopulaParlayEvaluator:
    """Bernoulli-correlation approximation, not a Gaussian copula.

    For two legs, rho is the correlation of the two outcome indicators.
    For more legs, rho is explicitly interpreted as the correlation between
    the accumulated intersection and the next leg in supplied order. It is
    NOT a common pairwise correlation matrix or an identified multivariate
    copula. Each aggregation must satisfy its own Frechet bounds.

    Callers must exclude mutually exclusive selections. Event IDs alone do
    not describe settlement compatibility.

    Compound vig is the payout discount relative to model fair odds, not an
    independently measured sportsbook overround.
    """

    @staticmethod
    def _combine(p: float, q: float, rho: float) -> float:
        lower = max(0.0, p + q - 1.0)
        upper = min(p, q)
        deviation = (
            math.sqrt(p)
            * math.sqrt(1.0 - p)
            * math.sqrt(q)
            * math.sqrt(1.0 - q)
        )
        joint = math.fsum((p * q, rho * deviation))
        tolerance = 8.0 * max(
            math.ulp(lower), math.ulp(upper), math.ulp(joint)
        )
        if joint < lower - tolerance or joint > upper + tolerance:
            raise InvalidCorrelationMatrixError(
                "Correlation is unattainable for these marginal probabilities",
                p=p,
                q=q,
                rho=rho,
                requested_joint=joint,
                lower_bound=lower,
                upper_bound=upper,
            )
        return max(lower, min(upper, joint))

    def evaluate_parlay(
        self,
        legs: list[ParlayLeg],
        correlation_coefficient: float = 0.0,
        *,
        offered_parlay_odds: float | None = None,
    ) -> ParlayAnalysis:
        if not legs:
            raise DomainMathError("A parlay requires at least one leg")

        rho = _finite(correlation_coefficient, "correlation_coefficient")
        if not -1.0 <= rho <= 1.0:
            raise InvalidCorrelationMatrixError(
                "Correlation must be in [-1, 1]", rho=rho
            )

        probabilities: list[float] = []
        odds: list[float] = []
        seen: set[str] = set()

        for leg in legs:
            if (
                not isinstance(leg.leg_id, str)
                or not leg.leg_id.strip()
                or not isinstance(leg.event_id, str)
                or not leg.event_id.strip()
            ):
                raise DomainMathError("Leg and event IDs must be non-empty")
            if leg.leg_id in seen:
                raise DomainMathError("Duplicate parlay leg", leg_id=leg.leg_id)
            seen.add(leg.leg_id)

            probability = _finite(leg.fair_probability, "fair_probability")
            price = _finite(leg.offered_odds, "offered_odds")
            if not 0.0 < probability < 1.0:
                raise DomainMathError(
                    "Leg probabilities must be strictly between 0 and 1",
                    leg_id=leg.leg_id,
                    probability=probability,
                )
            if price <= 1.0:
                raise DomainMathError(
                    "Decimal odds must exceed 1", leg_id=leg.leg_id, odds=price
                )
            probabilities.append(probability)
            odds.append(price)

        independent = math.exp(
            math.fsum(math.log(probability) for probability in probabilities)
        )
        if independent == 0.0:
            raise NumericalSolutionError("Independent joint probability underflow")

        joint = probabilities[0]
        for probability in probabilities[1:]:
            joint = self._combine(joint, probability, rho)

        # Do not manufacture positive EV by flooring a genuine tiny probability.
        # Values requiring a material clamp need a wider-range pricing interface.
        if not 0.0001 <= joint <= 0.9999:
            raise NumericalSolutionError(
                "Joint probability is outside the supported pricing interval",
                joint_probability=joint,
                lower=0.0001,
                upper=0.9999,
            )
        joint = max(0.0001, min(0.9999, joint))

        if offered_parlay_odds is None:
            try:
                offered = math.exp(math.fsum(math.log(price) for price in odds))
            except OverflowError as exc:
                raise NumericalSolutionError("Parlay odds overflow") from exc
        else:
            offered = _finite(offered_parlay_odds, "offered_parlay_odds")

        if not math.isfinite(offered) or offered <= 1.0:
            raise DomainMathError("Offered parlay odds must be finite and exceed 1")

        fair_odds = 1.0 / joint
        ev_per_unit = math.fsum((joint * offered, -1.0))
        edge_percentage = 100.0 * ev_per_unit
        if not math.isfinite(edge_percentage):
            raise NumericalSolutionError("Parlay edge exceeds numeric range")

        return ParlayAnalysis(
            legs_count=len(legs),
            uncorrelated_joint_probability=independent,
            correlated_joint_probability=joint,
            sportsbook_offered_parlay_odds=offered,
            true_fair_parlay_odds=fair_odds,
            edge_percentage=edge_percentage,
            is_positive_ev=ev_per_unit > 0.0,
            compound_vig_percentage=-edge_percentage,
        )
