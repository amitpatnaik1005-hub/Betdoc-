"""Tennis analytics: service-game win probability and surface-adjusted serve probability."""

import math

OUTPUT_PRECISION = 4


def _require_finite(**values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")


def _require_probability(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1.")


def calculate_game_win_probability(p: float) -> float:
    """P(hold) = p^4 (1 + 4q + 10q^2) + 20 p^3 q^3 p^2 / (1 - 2pq), q = 1 - p.

    1 - 2pq >= 0.5 for every p in [0, 1]; the zero guard below is defensive only.
    """
    _require_finite(p=p)
    _require_probability("p", p)

    q = 1.0 - p
    denominator = 1.0 - (2.0 * p * q)
    if denominator == 0:
        raise ValueError("Deuce denominator evaluated to zero.")

    probability = (p**4) * (1.0 + (4.0 * q) + (10.0 * q**2)) + ((20.0 * p**3 * q**3 * p**2) / denominator)
    return round(min(max(probability, 0.0), 1.0), OUTPUT_PRECISION) + 0.0


def _elo_probability(player_surface_elo: float, opponent_surface_elo: float, elo_scale: float) -> float:
    exponent = (opponent_surface_elo - player_surface_elo) / elo_scale
    try:
        power = 10.0**exponent
    except OverflowError:
        power = math.inf
    return 1.0 / (1.0 + power)


def calculate_elo_win_probability(player_surface_elo: float, opponent_surface_elo: float, elo_scale: float) -> float:
    _require_finite(
        player_surface_elo=player_surface_elo, opponent_surface_elo=opponent_surface_elo, elo_scale=elo_scale
    )
    if elo_scale <= 0:
        raise ValueError("elo_scale must be greater than zero.")
    return round(_elo_probability(player_surface_elo, opponent_surface_elo, elo_scale), OUTPUT_PRECISION) + 0.0


def calculate_surface_adjusted_serve_prob(
    base_serve_prob: float,
    player_surface_elo: float,
    opponent_surface_elo: float,
    elo_scale: float,
) -> float:
    """Adjusted serve probability = (base_serve_prob + elo_win_prob) / 2."""
    _require_finite(
        base_serve_prob=base_serve_prob,
        player_surface_elo=player_surface_elo,
        opponent_surface_elo=opponent_surface_elo,
        elo_scale=elo_scale,
    )
    _require_probability("base_serve_prob", base_serve_prob)
    if elo_scale <= 0:
        raise ValueError("elo_scale must be greater than zero.")

    elo_prob = _elo_probability(player_surface_elo, opponent_surface_elo, elo_scale)
    adjusted = (base_serve_prob + elo_prob) / 2.0
    return round(min(max(adjusted, 0.0), 1.0), OUTPUT_PRECISION) + 0.0
