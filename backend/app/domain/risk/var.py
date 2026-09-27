import math

import numpy as np
from scipy.stats import norm

from app.domain.risk.common import safe_float, to_finite_array


def _valid_confidence(confidence: float) -> bool:
    return math.isfinite(confidence) and 0.0 < confidence < 1.0


def calculate_parametric_var(returns: list[float], confidence: float = 0.95) -> float:
    """Gaussian VaR as a positive loss magnitude: -(mean - z * std)."""
    arr = to_finite_array(returns)
    if arr.size < 2 or not _valid_confidence(confidence):
        return 0.0
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if not math.isfinite(std) or std == 0.0:
        return safe_float(max(0.0, -mean))
    z = float(norm.ppf(confidence))
    return safe_float(max(0.0, -(mean - z * std)))


def calculate_historical_var(returns: list[float], confidence: float = 0.95) -> float:
    """Empirical VaR from the sorted return distribution, as a positive loss magnitude."""
    arr = to_finite_array(returns)
    if arr.size == 0 or not _valid_confidence(confidence):
        return 0.0
    ordered = np.sort(arr)
    idx = min(max(int(math.floor((1.0 - confidence) * ordered.size)), 0), ordered.size - 1)
    return safe_float(max(0.0, -float(ordered[idx])))
