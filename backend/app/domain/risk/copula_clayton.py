"""Clayton copula lower-tail dependence."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array


def clayton_lower_tail_dependence(theta: float) -> float:
    r""":math:`\lambda_L = 2^{-1/\theta}` for :math:`\theta > 0`; otherwise 0.0."""
    try:
        th = safe_float(theta)
        if not math.isfinite(th) or th <= 0.0:
            return 0.0
        result = 2.0 ** (-1.0 / th)
        return result if math.isfinite(result) else 0.0
    except (ArithmeticError, ValueError, TypeError):
        return 0.0
