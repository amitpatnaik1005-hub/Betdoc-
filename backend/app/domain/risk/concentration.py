"""Herfindahl-Hirschman concentration index."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array

_FAILSAFE = (ArithmeticError, ValueError, TypeError, MemoryError)


def calculate_hhi(weights: Any) -> float:
    r""":math:`\mathrm{HHI} = \sum_i (w'_i)^2` with :math:`w'_i = |w_i| / \sum_j |w_j|` (gross-exposure normalised)."""
    try:
        w = np.abs(to_finite_array(weights))
        if w.size == 0:
            return 0.0
        gross = float(w.sum())
        if not math.isfinite(gross) or gross == 0.0:
            return 0.0
        hhi = float(np.sum((w / gross) ** 2))
        return hhi if math.isfinite(hhi) else 0.0
    except _FAILSAFE:
        return 0.0
