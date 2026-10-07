"""Equal-risk-contribution (risk parity) weights via SLSQP with inverse-variance fallback."""

import math
import numpy as np
from typing import Any
from scipy.optimize import minimize
from app.domain.risk.common import safe_float

_FAILSAFE = (ArithmeticError, ValueError, TypeError, np.linalg.LinAlgError, MemoryError)


def _inverse_variance(cov: np.ndarray, variance_floor: float) -> np.ndarray:
    diag = np.diag(cov)
    inv = np.where(diag > variance_floor, 1.0 / np.where(diag > variance_floor, diag, 1.0), 0.0)
    total = float(inv.sum())
    if not math.isfinite(total) or total <= 0.0:
        return np.full(cov.shape[0], 1.0 / cov.shape[0])
    return inv / total


def risk_parity_weights(
    cov_matrix: Any,
    tolerance: float = 1e-6,
    max_iter: int = 1000,
    variance_floor: float = 1e-12,
    nan_fill: float = 0.0,
    posinf_fill: float = 0.0,
    neginf_fill: float = 0.0,
) -> np.ndarray:
    r"""Minimise the dimensionless objective

    Falls back to inverse-variance weights if SLSQP fails. Invalid input returns zeros.
    """
    n = 0
    try:
        cov = np.nan_to_num(np.asarray(cov_matrix, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        if cov.ndim != 2 or cov.shape[0] != cov.shape[1] or cov.shape[0] == 0:
            return np.zeros(cov.shape[0] if cov.ndim >= 1 else 0)
        n = cov.shape[0]
        cov = 0.5 * (cov + cov.T)
        floor = max(safe_float(variance_floor), 0.0)
        fallback = _inverse_variance(cov, floor)
        target = 1.0 / n

        def objective(w: np.ndarray) -> float:
            sigma_w = cov @ w
            port = max(float(w @ sigma_w), floor)
            return float(np.sum((w * sigma_w / port - target) ** 2))

        res = minimize(
            objective,
            fallback,
            method="SLSQP",
            bounds=[(0.0, 1.0)] * n,
            constraints=[{"type": "eq", "fun": lambda w: float(w.sum() - 1.0)}],
            tol=safe_float(tolerance),
            options={"maxiter": max(int(max_iter), 1)},
        )
        w = np.clip(np.nan_to_num(np.asarray(res.x, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill), 0.0, None)
        total = float(w.sum())
        if not res.success or total <= 0.0 or not math.isfinite(total):
            return fallback
        w = w / total
        if float(w @ cov @ w) <= floor:
            return fallback
        return w
    except _FAILSAFE:
        return np.zeros(n)
