"""Entropic (exponential-utility) risk measure."""

import math
import numpy as np
from typing import Any
from scipy.special import logsumexp
from app.domain.risk.common import safe_float, to_finite_array

_FAILSAFE = (ArithmeticError, ValueError, TypeError, MemoryError)


def entropic_risk_measure(returns: Any, theta: float = 1.0) -> float:
    r""":math:`\rho(X) = \frac1\theta\ln\mathbb E[e^{-\theta X}] = \frac1\theta\bigl(\operatorname{LSE}(-\theta X) - \ln N\bigr)`.

    Log-sum-exp prevents overflow. Returns 0.0 for :math:`\theta \le 0` or empty input.
    """
    try:
        arr = to_finite_array(returns)
        th = safe_float(theta)
        if arr.size == 0 or not math.isfinite(th) or th <= 0.0:
            return 0.0
        result = (float(logsumexp(-th * arr)) - math.log(arr.size)) / th
        return result if math.isfinite(result) else 0.0
    except _FAILSAFE:
        return 0.0
