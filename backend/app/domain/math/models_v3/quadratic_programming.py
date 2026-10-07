"""Markowitz mean-variance optimisation via SLSQP on an SPD-regularised covariance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy import optimize

from app.domain.math.models_v2.base import (
    FLOAT_EPS,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_feature_matrix,
    check_finite_scalar,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["MarkowitzConfig", "MarkowitzModel"]


class MarkowitzConfig(MathModelConfig):
    risk_aversion: float = Field(default=1.0, gt=0.0)
    max_weight: float = Field(default=1.0, gt=0.0, le=1.0)
    ridge_epsilon: float = Field(default=1e-8, gt=0.0)
    shrinkage: float = Field(default=0.0, ge=0.0, le=1.0)
    ddof: int = Field(default=1, ge=0)
    max_iter: int = Field(default=1000, ge=1)
    tol: float = Field(default=1e-12, gt=0.0)
    constraint_tolerance: float = Field(default=1e-6, gt=0.0)


@dataclass(frozen=True, slots=True)
class _MarkowitzState:
    weights: FloatArray
    mean_returns: FloatArray
    covariance: FloatArray
    expected_return: float
    variance: float
    utility: float
    converged: bool


class MarkowitzModel(BaseMathModel):
    r"""Mean-variance utility maximisation.

    .. math:: \max_w\ w^\top\mu - \frac{\lambda}{2} w^\top Q w \quad\Longleftrightarrow\quad
              \min_w\ -w^\top\mu + \frac{\lambda}{2} w^\top Q w

    .. math:: \text{s.t.}\quad \sum_i w_i = 1,\qquad 0 \le w_i \le w_{\max}

    SPD guarantee: :math:`Q \leftarrow \tfrac12(Q + Q^\top) + \epsilon I` after optional shrinkage
    :math:`Q \leftarrow (1-s)Q + s\,\mathrm{diag}(Q)`; verified by Cholesky. Analytic gradient
    :math:`\nabla f = -\mu + \lambda Q w`. ``fit`` consumes a ``(T, n_assets)`` return matrix;
    ``predict`` returns portfolio returns :math:`X w^\star`.
    """

    config: MarkowitzConfig

    def __init__(self, config: MarkowitzConfig | None = None) -> None:
        super().__init__(resolve_config(config, MarkowitzConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        returns = as_feature_matrix(X, name="X", min_samples=cfg.ddof + 2)
        n_assets = returns.shape[1]
        if n_assets * cfg.max_weight < 1.0 - FLOAT_EPS:
            raise ValueError("max_weight * n_assets must be >= 1 for a feasible fully invested portfolio.")
        mu = returns.mean(axis=0)
        Q = np.atleast_2d(np.cov(returns, rowvar=False, ddof=cfg.ddof))
        Q = (1.0 - cfg.shrinkage) * Q + cfg.shrinkage * np.diag(np.diag(Q))
        Q = 0.5 * (Q + Q.T) + cfg.ridge_epsilon * np.eye(n_assets)
        try:
            np.linalg.cholesky(Q)
        except np.linalg.LinAlgError as exc:
            raise NumericalStabilityError("Covariance is not SPD after regularisation.") from exc
        lam = cfg.risk_aversion

        def objective(w: FloatArray) -> tuple[float, FloatArray]:
            qw = Q @ w
            return float(-w @ mu + 0.5 * lam * w @ qw), -mu + lam * qw

        result = optimize.minimize(
            objective,
            np.full(n_assets, 1.0 / n_assets),
            jac=True,
            method="SLSQP",
            bounds=[(0.0, cfg.max_weight)] * n_assets,
            constraints=[{"type": "eq", "fun": lambda w: float(w.sum() - 1.0), "jac": lambda w: np.ones_like(w)}],
            options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
        )
        weights = np.clip(np.asarray(result.x, dtype=np.float64), 0.0, cfg.max_weight)
        weights = weights / weights.sum()
        if abs(float(weights.sum()) - 1.0) > cfg.constraint_tolerance or np.any(weights > cfg.max_weight + cfg.constraint_tolerance):
            raise NumericalStabilityError("SLSQP solution violates portfolio constraints.")
        exp_ret = float(weights @ mu)
        variance = float(weights @ Q @ weights)
        self._publish_state(
            _MarkowitzState(
                weights=freeze(weights),
                mean_returns=freeze(mu),
                covariance=freeze(Q),
                expected_return=check_finite_scalar(exp_ret, name="expected_return"),
                variance=check_finite_scalar(variance, name="variance"),
                utility=check_finite_scalar(exp_ret - 0.5 * lam * variance, name="utility"),
                converged=bool(result.success),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _MarkowitzState = self._current_state()
        returns = as_feature_matrix(X, name="X", n_features=state.weights.size)
        return sanitize(returns @ state.weights)

    @property
    def weights(self) -> FloatArray:
        return self._current_state().weights

    @property
    def summary(self) -> dict[str, float | bool]:
        s: _MarkowitzState = self._current_state()
        return {"expected_return": s.expected_return, "variance": s.variance, "utility": s.utility, "converged": s.converged}
