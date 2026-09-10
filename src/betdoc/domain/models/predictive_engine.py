from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

from betdoc.domain.math.errors import DomainMathError


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainMathError("Expected a real number", field=label, value=value)
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise DomainMathError("Invalid numeric input", field=label) from exc
    if not math.isfinite(result):
        raise DomainMathError("Expected a finite number", field=label, value=value)
    return result


@dataclass(frozen=True)
class TeamMetrics:
    team_id: str
    elo_rating: float
    form_last_5: List[float]
    days_rest: int
    home_advantage_weight: float
    offensive_efficiency: float
    defensive_efficiency: float
    injury_impact_score: float


@dataclass(frozen=True)
class PredictionResult:
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    confidence_score: float
    latent_fatigue_factor: float
    fair_odds_home: float
    fair_odds_draw: float
    fair_odds_away: float


class MultiFactorPredictiveEngine:
    """Three-way Elo softmax with explicit, heuristic feature coefficients.

    Form observations run oldest to newest and must share the same units.
    Higher offensive and defensive efficiencies both indicate stronger teams.
    Coefficients require historical fitting before use as calibrated forecasts.
    Confidence measures distribution concentration, not predictive accuracy.
    """

    def __init__(
        self,
        base_home_advantage: float = 65.0,
        fatigue_decay_rate: float = 0.08,
        draw_base_rate: float = 0.26,
    ) -> None:
        self.base_home_advantage = _finite(
            base_home_advantage, "base_home_advantage"
        )
        self.fatigue_decay_rate = _finite(
            fatigue_decay_rate, "fatigue_decay_rate"
        )
        self.draw_base_rate = _finite(draw_base_rate, "draw_base_rate")

        if not 0.0 <= self.base_home_advantage <= 500.0:
            raise DomainMathError("Home advantage must be between 0 and 500")
        if not 0.0 < self.fatigue_decay_rate <= 1.0:
            raise DomainMathError("Fatigue decay must be in (0, 1]")
        if not 0.000001 <= self.draw_base_rate <= 0.999999:
            raise DomainMathError("Draw base rate is outside supported bounds")

    def calculate_fatigue_penalty(self, days_rest: int) -> float:
        """Return a fatigue index in [0, 1] using nonlinear recovery."""
        if (
            isinstance(days_rest, bool)
            or not isinstance(days_rest, int)
            or days_rest < 0
        ):
            raise DomainMathError(
                "Rest days must be a non-negative integer",
                days_rest=days_rest,
            )
        if days_rest >= 1_000_000:
            return 0.0
        exponent = self.fatigue_decay_rate * days_rest * days_rest
        return math.exp(-min(exponent, 745.0))

    def calculate_form_momentum(self, form: List[float]) -> float:
        """Exponentially weighted mean; the last observation is most recent."""
        if len(form) > 5:
            raise DomainMathError(
                "Expected at most five form observations", count=len(form)
            )
        if not form:
            return 0.0

        values = [_finite(value, "form") for value in form]
        scale = max(abs(value) for value in values)
        if scale == 0.0:
            return 0.0

        weights = [
            math.exp(-0.5 * (len(values) - index - 1))
            for index in range(len(values))
        ]
        normalized = math.fsum(
            weight * (value / scale)
            for weight, value in zip(weights, values)
        ) / math.fsum(weights)
        return max(-1.0, min(1.0, normalized)) * scale

    def _validate_team(self, team: TeamMetrics) -> None:
        if not isinstance(team.team_id, str) or not team.team_id.strip():
            raise DomainMathError("Team ID must be non-empty")

        bounds = (
            ("elo_rating", team.elo_rating, 0.0, 4_000.0),
            ("home_advantage_weight", team.home_advantage_weight, 0.0, 2.0),
            ("offensive_efficiency", team.offensive_efficiency, 0.0, 10.0),
            ("defensive_efficiency", team.defensive_efficiency, 0.0, 10.0),
            ("injury_impact_score", team.injury_impact_score, 0.0, 1.0),
        )
        for label, value, lower, upper in bounds:
            number = _finite(value, label)
            if not lower <= number <= upper:
                raise DomainMathError(
                    "Team metric is outside supported bounds",
                    team_id=team.team_id,
                    field=label,
                    value=value,
                    lower=lower,
                    upper=upper,
                )
        self.calculate_fatigue_penalty(team.days_rest)
        self.calculate_form_momentum(team.form_last_5)

    def predict_match(
        self, home: TeamMetrics, away: TeamMetrics
    ) -> PredictionResult:
        self._validate_team(home)
        self._validate_team(away)
        if home.team_id == away.team_id:
            raise DomainMathError("A team cannot play itself", team_id=home.team_id)

        home_fatigue = self.calculate_fatigue_penalty(home.days_rest)
        away_fatigue = self.calculate_fatigue_penalty(away.days_rest)
        home_form = math.tanh(
            self.calculate_form_momentum(home.form_last_5) / 3.0
        )
        away_form = math.tanh(
            self.calculate_form_momentum(away.form_last_5) / 3.0
        )

        rating_difference = math.fsum(
            (
                home.elo_rating - away.elo_rating,
                self.base_home_advantage * home.home_advantage_weight,
                55.0 * (home_form - away_form),
                -100.0 * (home_fatigue - away_fatigue),
                -140.0 * (
                    home.injury_impact_score - away.injury_impact_score
                ),
                80.0 * (
                    math.log1p(home.offensive_efficiency)
                    - math.log1p(away.offensive_efficiency)
                ),
                80.0 * (
                    math.log1p(home.defensive_efficiency)
                    - math.log1p(away.defensive_efficiency)
                ),
            )
        )
        log_strength_ratio = rating_difference * math.log(10.0) / 400.0

        # At equal adjusted strengths, the draw probability equals its baseline.
        draw_logit = math.log(
            2.0 * self.draw_base_rate / (1.0 - self.draw_base_rate)
        )
        logits = [
            0.5 * log_strength_ratio,
            draw_logit,
            -0.5 * log_strength_ratio,
        ]
        offset = max(logits)
        weights = [math.exp(value - offset) for value in logits]
        denominator = math.fsum(weights)
        probabilities = [weight / denominator for weight in weights]

        largest = max(range(3), key=probabilities.__getitem__)
        probabilities[largest] += 1.0 - math.fsum(probabilities)

        if not all(0.0 < probability < 1.0 for probability in probabilities):
            raise DomainMathError(
                "Prediction exceeded numerical probability bounds",
                rating_difference=rating_difference,
            )

        entropy = -math.fsum(
            probability * math.log(probability)
            for probability in probabilities
        )
        confidence = max(0.0, min(1.0, 1.0 - entropy / math.log(3.0)))

        home_probability, draw_probability, away_probability = probabilities
        return PredictionResult(
            home_win_probability=home_probability,
            draw_probability=draw_probability,
            away_win_probability=away_probability,
            confidence_score=confidence,
            latent_fatigue_factor=home_fatigue - away_fatigue,
            fair_odds_home=1.0 / home_probability,
            fair_odds_draw=1.0 / draw_probability,
            fair_odds_away=1.0 / away_probability,
        )
