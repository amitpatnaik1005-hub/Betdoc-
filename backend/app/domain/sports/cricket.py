"""Cricket analytics: DLS par score and first-innings projection."""

import math

OUTPUT_PRECISION = 4


def _require_finite(**values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")


def _require_int(name: str, value: int, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}.")


def calculate_dls_par_score(resources_left_pct: float, original_target: float, dls_weight_factor: float) -> float:
    """Par score = target * (1 - resources_left * weight), bounded to [0, target].

    resources_left_pct is a fraction in [0, 1] (0.4 means 40% of resources remain).
    """
    _require_finite(
        resources_left_pct=resources_left_pct,
        original_target=original_target,
        dls_weight_factor=dls_weight_factor,
    )
    if not 0.0 <= resources_left_pct <= 1.0:
        raise ValueError("resources_left_pct must be a fraction between 0 and 1.")
    if original_target < 0:
        raise ValueError("original_target must not be negative.")
    if dls_weight_factor <= 0:
        raise ValueError("dls_weight_factor must be greater than zero.")

    raw = original_target * (1.0 - (resources_left_pct * dls_weight_factor))
    bounded = min(max(raw, 0.0), original_target)
    return round(bounded, OUTPUT_PRECISION) + 0.0


def calculate_first_innings_expectation(
    current_score: float,
    overs_bowled: float,
    wickets_lost: int,
    pitch_degradation_factor: float,
    total_overs: float,
    wickets_per_innings: int,
) -> float:
    """Projected total = score + run_rate * overs_left * pitch * exp(-wickets / wickets_per_innings)."""
    _require_finite(
        current_score=current_score,
        overs_bowled=overs_bowled,
        pitch_degradation_factor=pitch_degradation_factor,
        total_overs=total_overs,
    )
    _require_int("wickets_lost", wickets_lost, 0)
    _require_int("wickets_per_innings", wickets_per_innings, 1)
    if current_score < 0:
        raise ValueError("current_score must not be negative.")
    if overs_bowled < 0:
        raise ValueError("overs_bowled must not be negative.")
    if pitch_degradation_factor < 0:
        raise ValueError("pitch_degradation_factor must not be negative.")
    if total_overs <= 0:
        raise ValueError("total_overs must be greater than zero.")

    if wickets_lost >= wickets_per_innings or overs_bowled >= total_overs:
        return float(current_score)

    run_rate = current_score / max(overs_bowled, 1.0)
    remaining_overs = total_overs - overs_bowled
    projection = current_score + (
        run_rate * remaining_overs * pitch_degradation_factor * math.exp(-wickets_lost / wickets_per_innings)
    )
    return round(projection, OUTPUT_PRECISION) + 0.0
