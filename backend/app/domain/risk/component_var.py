"""Euler-allocated component Value-at-Risk."""

import math
import numpy as np
from typing import Any
from scipy.stats import norm
from app.domain.risk.common import safe_float

_FAILSAFE = (ArithmeticError, ValueError, TypeError, np.linalg.LinAlgError, MemoryError)


def component_var(
    weights: Any,
    cov_matrix: Any,
    confidence: float = 0.95,
    variance_floor: float = 1e-12,
    nan_fill: float = 0.0,
    posinf_fill: float = 0.0,
    neginf_fill: float = 0.0,
) -> np.ndarray:
    r"""Euler-allocated component VaR."""
    n = 0
    try:
        w = np.nan_to_num(np.asarray(weights, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        n = int(w.size)
        cov = np.nan_to_num(np.asarray(cov_matrix, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        conf = safe_float(confidence)
        if n == 0 or cov.ndim != 2 or cov.shape != (n, n) or not 0.0 < conf < 1.0:
            return np.zeros(n)
        cov = 0.5 * (cov + cov.T)
        sigma_w = cov @ w
        variance = max(float(w @ sigma_w), 0.0)
        if variance <= safe_float(variance_floor):
            return np.zeros(n)
        z = float(norm.ppf(conf))
        return np.nan_to_num(w * (z * sigma_w / math.sqrt(variance)), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
    except _FAILSAFE:
        return np.zeros(n)
