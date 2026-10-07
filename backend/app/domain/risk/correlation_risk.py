"""Correlation-structure concentration (eigenvalue dispersion / absorption ratio)."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float

_FAILSAFE = (ArithmeticError, ValueError, TypeError, np.linalg.LinAlgError, MemoryError)


def eigen_dispersion(
    return_matrix: Any, 
    variance_floor: float = 1e-12,
    nan_fill: float = 0.0,
    posinf_fill: float = 0.0,
    neginf_fill: float = 0.0,
) -> float:
    r"""Share of total correlation variance explained by the dominant eigenvalue."""
    try:
        m = np.nan_to_num(np.asarray(return_matrix, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        if m.ndim != 2 or m.shape[0] < 2:
            return 0.0
        m = m[:, np.var(m, axis=0) >= safe_float(variance_floor)]
        if m.shape[1] < 2:
            return 0.0
        corr = np.nan_to_num(np.corrcoef(m, rowvar=False), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        eigvals = np.clip(np.linalg.eigh(0.5 * (corr + corr.T))[0], 0.0, None)
        total = float(eigvals.sum())
        if not math.isfinite(total) or total <= 0.0:
            return 0.0
        result = float(np.max(eigvals)) / total
        return result if math.isfinite(result) else 0.0
    except _FAILSAFE:
        return 0.0
