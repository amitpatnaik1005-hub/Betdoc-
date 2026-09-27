import logging
import math

import numpy as np
from scipy.optimize import minimize

from app.domain.risk.common import safe_float

kumbha = logging.getLogger("betdoc.kumbha")


def optimize_portfolio(
    expected_returns: list[float],
    cov_matrix: list[list[float]],
    risk_free_rate: float = 0.0,
) -> list[float]:
    """Long-only max-Sharpe weights via SLSQP. Equal weights on any failure."""
    n = len(expected_returns)
    if n == 0:
        return []
    equal = [1.0 / n] * n
    if n == 1:
        return [1.0]

    try:
        mu = np.asarray([float(x) for x in expected_returns], dtype=np.float64)
        cov = np.asarray(cov_matrix, dtype=np.float64)
        if cov.shape != (n, n) or not np.all(np.isfinite(mu)) or not np.all(np.isfinite(cov)):
            return equal
        cov = (cov + cov.T) / 2.0
        if float(np.min(np.linalg.eigvalsh(cov))) < -1e-10:
            return equal  # not positive semi-definite

        rf = safe_float(risk_free_rate)

        def neg_sharpe(w: np.ndarray) -> float:
            variance = float(w @ cov @ w)
            if variance <= 1e-12:
                return 1e6
            return -(float(w @ mu) - rf) / math.sqrt(variance)

        res = minimize(
            neg_sharpe,
            x0=np.asarray(equal),
            method="SLSQP",
            bounds=[(0.0, 1.0)] * n,
            constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w)) - 1.0}],
            options={"maxiter": 500, "ftol": 1e-10},
        )
        if not res.success or not np.all(np.isfinite(res.x)):
            return equal
        weights = np.clip(res.x, 0.0, 1.0)
        total = float(np.sum(weights))
        if total <= 0.0 or not math.isfinite(total):
            return equal
        return [safe_float(w) for w in weights / total]
    except (ValueError, TypeError, np.linalg.LinAlgError):
        kumbha.warning("MPT optimization failed; returning equal weights", exc_info=True)
        return equal
