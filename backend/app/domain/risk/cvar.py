import math

import numpy as np

from app.domain.risk.common import safe_float, to_finite_array


def calculate_cvar(returns: list[float], confidence: float = 0.95) -> float:
    """Expected Shortfall: mean of the worst (1 - confidence) tail, as a positive loss magnitude."""
    arr = to_finite_array(returns)
    if arr.size == 0 or not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        return 0.0
    ordered = np.sort(arr)
    idx = min(max(int(math.floor((1.0 - confidence) * ordered.size)), 0), ordered.size - 1)
    tail = ordered[: idx + 1]
    if tail.size == 0:
        return 0.0
    return safe_float(max(0.0, -float(np.mean(tail))))
