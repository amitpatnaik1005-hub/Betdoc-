"""Exception hierarchy for the domain math vault.

Every error carries the numeric context that caused it. A bare exception
message is useless in a post-mortem six weeks after a bad settlement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

__all__ = [
    "DevigDisagreementError",
    "DomainMathError",
    "ExposureConcentrationError",
    "IncompleteMarketError",
    "InvalidCorrelationMatrixError",
    "NegativeExpectedValueError",
    "NumericalSolutionError",
    "SolverFailureError",
]


class DomainMathError(ValueError):
    """Base class for every rejected domain calculation."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        base = super().__str__()
        if not self.context:
            return base
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{base} [{detail}]"


class IncompleteMarketError(DomainMathError):
    """The market is not a complete book, so it cannot be devigged."""

    def __init__(self, message: str, *, booksum: float, outcome_count: int) -> None:
        super().__init__(message, booksum=booksum, outcome_count=outcome_count)
        self.booksum = booksum
        self.outcome_count = outcome_count


class NumericalSolutionError(DomainMathError):
    """A root-find or optimisation failed to converge within its bracket."""


class DevigDisagreementError(DomainMathError):
    """Shin and the power method disagree beyond tolerance on the target outcome.

    This is a safety control, not a nuisance. Disagreement means the book's
    margin structure is unusual, which is exactly when a devigged probability
    should not be trusted enough to stake against.
    """

    def __init__(
        self,
        message: str,
        *,
        shin_probability: float,
        power_probability: float,
        relative_difference: float,
        tolerance: float,
    ) -> None:
        super().__init__(
            message,
            shin_probability=shin_probability,
            power_probability=power_probability,
            relative_difference=relative_difference,
            tolerance=tolerance,
        )
        self.shin_probability = shin_probability
        self.power_probability = power_probability
        self.relative_difference = relative_difference
        self.tolerance = tolerance


class NegativeExpectedValueError(DomainMathError):
    """The bet has non-positive expected value against the fair probability."""

    def __init__(
        self,
        message: str,
        *,
        fair_probability: float,
        offered_odds: float,
        ev_per_unit: Decimal,
    ) -> None:
        super().__init__(
            message,
            fair_probability=fair_probability,
            offered_odds=offered_odds,
            ev_per_unit=str(ev_per_unit),
        )
        self.fair_probability = fair_probability
        self.offered_odds = offered_odds
        self.ev_per_unit = ev_per_unit


class ExposureConcentrationError(DomainMathError):
    """A single failure point would breach the max drawdown limit."""

    def __init__(
        self,
        message: str,
        *,
        risk_factor_key: str,
        exposure_fraction: float,
        limit_fraction: float,
        worst_case_loss_paise: int,
    ) -> None:
        super().__init__(
            message,
            risk_factor_key=risk_factor_key,
            exposure_fraction=exposure_fraction,
            limit_fraction=limit_fraction,
            worst_case_loss_paise=worst_case_loss_paise,
        )
        self.risk_factor_key = risk_factor_key
        self.exposure_fraction = exposure_fraction
        self.limit_fraction = limit_fraction
        self.worst_case_loss_paise = worst_case_loss_paise


class InvalidCorrelationMatrixError(DomainMathError):
    """The correlation matrix is malformed beyond automatic repair."""


class SolverFailureError(DomainMathError):
    """Every configured convex solver failed on a feasible-looking problem."""
