"""Model-edge half-life decay."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array


def edge_decay_penalty(days_since_training: float, half_life_days: float) -> float:
    r"""Penalty :math:`= e^{-\ln 2\cdot t/h}` with :math:`t = \max(t, 0)`. If :math:`h \le 0`: 1.0 when
    :math:`t = 0`, else 0.0.
    """
    try:
        t = max(safe_float(days_since_training), 0.0)
        h = safe_float(half_life_days)
        if h <= 0.0:
            return 1.0 if t == 0.0 else 0.0
        result = math.exp(-math.log(2.0) * t / h)
        return result if math.isfinite(result) else 0.0
    except (ArithmeticError, ValueError, TypeError):
        return 0.0
