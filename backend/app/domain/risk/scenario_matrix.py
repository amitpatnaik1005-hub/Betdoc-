"""Deterministic scenario stress testing."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float

_FAILSAFE = (ArithmeticError, ValueError, TypeError, MemoryError)


def stress_test_portfolio(
    weights: Any, 
    shock_matrix: Any,
    nan_fill: float = 0.0,
    posinf_fill: float = 0.0,
    neginf_fill: float = 0.0,
) -> np.ndarray:
    r"""Scenario P&L :math:`\Delta V = S w` for shock matrix :math:`S \in \mathbb R^{M\times N}`."""
    m = 0
    try:
        shocks = np.nan_to_num(np.asarray(shock_matrix, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        if shocks.ndim == 1:
            shocks = shocks.reshape(1, -1)
        if shocks.ndim != 2:
            return np.zeros(0)
        m = shocks.shape[0]
        w = np.nan_to_num(np.asarray(weights, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        if w.size == 0 or w.size != shocks.shape[1]:
            return np.zeros(m)
        return np.nan_to_num(shocks @ w, nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
    except _FAILSAFE:
        return np.zeros(m)
