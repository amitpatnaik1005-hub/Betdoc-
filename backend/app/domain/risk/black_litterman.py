import logging

import numpy as np

from app.domain.risk.common import safe_float

kumbha = logging.getLogger("betdoc.kumbha")

DEFAULT_TAU = 0.05


def black_litterman_adjust(
    market_weights: list[float],
    cov_matrix: list[list[float]],
    views: list[float],
    view_confidence: list[float],
    risk_aversion: float = 2.5,
    tau: float = DEFAULT_TAU,
) -> list[float]:
    """
    Absolute views (one per asset, P = I). Confidence in (0, 1) maps to
    Omega = diag((1 - c) / c * diag(tau * Sigma)). Uses np.linalg.solve only.
    Returns long-only posterior weights, or the original weights on failure.
    """
    original = [safe_float(w) for w in market_weights]
    n = len(original)
    if n == 0:
        return []

    try:
        w_mkt = np.asarray(original, dtype=np.float64)
        cov = np.asarray(cov_matrix, dtype=np.float64)
        q = np.asarray([float(v) for v in views], dtype=np.float64)
        conf = np.asarray([float(c) for c in view_confidence], dtype=np.float64)
        if cov.shape != (n, n) or q.shape != (n,) or conf.shape != (n,):
            return original
        if not all(np.all(np.isfinite(a)) for a in (cov, q, conf)):
            return original
        if risk_aversion <= 0.0 or tau <= 0.0:
            return original

        cov = (cov + cov.T) / 2.0
        p = np.eye(n)
        tau_sigma = tau * cov
        pi = risk_aversion * cov @ w_mkt                      # implied equilibrium returns

        conf = np.clip(conf, 1e-6, 1.0 - 1e-6)
        omega = np.diag(((1.0 - conf) / conf) * np.diag(p @ tau_sigma @ p.T))

        m = p @ tau_sigma @ p.T + omega
        adjustment = np.linalg.solve(m, q - p @ pi)
        mu_bl = pi + tau_sigma @ p.T @ adjustment             # posterior returns

        w_bl = np.linalg.solve(risk_aversion * cov, mu_bl)
        if not np.all(np.isfinite(w_bl)):
            return original
        w_bl = np.clip(w_bl, 0.0, None)
        total = float(np.sum(w_bl))
        if total <= 0.0:
            return original
        return [safe_float(w) for w in w_bl / total]
    except (ValueError, TypeError, np.linalg.LinAlgError):
        kumbha.warning("Black-Litterman failed; returning market weights", exc_info=True)
        return original
