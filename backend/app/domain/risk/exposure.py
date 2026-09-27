import numpy as np

from app.domain.risk.common import safe_float, to_finite_array


def calculate_exposure(active_stakes: list[float], bankroll: float) -> tuple[float, float]:
    """Returns (total_exposure, exposure_pct) where pct is 0-100."""
    arr = to_finite_array(active_stakes)
    total = safe_float(float(np.sum(arr[arr > 0.0]))) if arr.size else 0.0
    bank = safe_float(bankroll)
    if bank <= 0.0:
        return total, 0.0
    return total, safe_float(total / bank * 100.0)
