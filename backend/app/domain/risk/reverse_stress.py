"""Reverse stress test: volatility that makes ruin a confidence-level event."""

import math
import numpy as np
from typing import Any
from scipy.stats import norm
from app.domain.risk.common import safe_float, to_finite_array


def implied_ruin_volatility(
    current_value: float,
    ruin_level: float,
    time_horizon: float,
    confidence: float = 0.99,
    min_z: float = 1e-6,
) -> float:
    r""":math:`\sigma = \dfrac{V_0 - V_{\text{ruin}}}{V_0\, Z\, \sqrt T}`, with :math:`Z = \Phi^{-1}(\text{confidence})`.

    Returns 0.0 if :math:`V_0 \le 0`, :math:`Z \le 10^{-6}`, :math:`V_{\text{ruin}} \ge V_0` or :math:`T \le 0`.
    """
    try:
        v0, ruin, t, conf = safe_float(current_value), safe_float(ruin_level), safe_float(time_horizon), safe_float(confidence)
        if not 0.0 < conf < 1.0:
            return 0.0
        z = float(norm.ppf(conf))
        if v0 <= 0.0 or not math.isfinite(z) or z <= safe_float(min_z) or ruin >= v0 or t <= 0.0:
            return 0.0
        sigma = (v0 - ruin) / (v0 * z * math.sqrt(t))
        return sigma if math.isfinite(sigma) else 0.0
    except (ArithmeticError, ValueError, TypeError):
        return 0.0
