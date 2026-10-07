"""Exponential spectral risk measure."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array

_FAILSAFE = (ArithmeticError, ValueError, TypeError, np.linalg.LinAlgError, MemoryError)


def exponential_spectral_risk(returns: Any, gamma: float = 1.0) -> float:
    r"""Spectral risk with exponential risk-aversion weights.

    Returns sorted ascending (worst first), :math:`W_i = e^{-\gamma i/N}`, normalised to
    :math:`\sum W = 1`; :math:`\text{Risk} = -\sum_i W_i R_i` (positive for losses).
    Weights are computed in log space (max-shifted) so large :math:`\gamma` cannot underflow to 0/0.
    """
    try:
        arr = np.sort(to_finite_array(returns))
        n = int(arr.size)
        g = safe_float(gamma)
        if n == 0 or not math.isfinite(g) or g < 0.0:
            return 0.0
        log_w = -g * np.arange(1, n + 1, dtype=np.float64) / n
        w = np.exp(log_w - log_w.max())
        total = float(w.sum())
        if not math.isfinite(total) or total <= 0.0:
            return 0.0
        risk = -float((w / total) @ arr)
        return risk if math.isfinite(risk) else 0.0
    except _FAILSAFE:
        return 0.0
