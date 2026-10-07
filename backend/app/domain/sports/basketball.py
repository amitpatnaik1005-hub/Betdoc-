"""Basketball analytics: pace-adjusted spread and spread-to-win-probability."""

import math

OUTPUT_PRECISION = 4


def _require_finite(**values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")


def calculate_pace_adjusted_spread(
    home_rating: float,
    away_rating: float,
    home_pace: float,
    away_pace: float,
    league_avg_pace: float,
) -> float:
    """Spread = (home - away) * ((home_pace + away_pace) / (2 * league_avg_pace))."""
    _require_finite(
        home_rating=home_rating,
        away_rating=away_rating,
        home_pace=home_pace,
        away_pace=away_pace,
        league_avg_pace=league_avg_pace,
    )
    if league_avg_pace <= 0:
        raise ValueError("league_avg_pace must be greater than zero.")
    if home_pace <= 0 or away_pace <= 0:
        raise ValueError("home_pace and away_pace must be greater than zero.")

    spread = (home_rating - away_rating) * ((home_pace + away_pace) / (2.0 * league_avg_pace))
    if not math.isfinite(spread):
        raise ValueError("Spread overflowed; inputs are out of range.")
    return round(spread, OUTPUT_PRECISION) + 0.0


def calculate_win_probability_from_spread(spread: float, std_dev: float) -> float:
    """P(home win) = Phi(spread / std_dev) = 0.5 * (1 + erf(spread / (std_dev * sqrt(2))))."""
    _require_finite(spread=spread, std_dev=std_dev)
    if std_dev <= 0:
        raise ValueError("std_dev must be greater than zero.")

    probability = 0.5 * (1.0 + math.erf(spread / (std_dev * math.sqrt(2.0))))
    return round(min(max(probability, 0.0), 1.0), OUTPUT_PRECISION) + 0.0
