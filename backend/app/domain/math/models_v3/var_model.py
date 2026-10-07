"""Vector Autoregression VAR(p) with regularised OLS and companion-matrix stationarity gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_feature_matrix,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["VARConfig", "VARModel"]


def _positive_int_kwarg(kwargs: dict[str, Any], name: str, default: int) -> int:
    value = kwargs.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


class VARConfig(MathModelConfig):
    lags: int = Field(default=1, ge=1)
    include_intercept: bool = True
    ridge_alpha: float = Field(default=0.0, ge=0.0)
    stationarity_tolerance: float = Field(default=0.0, ge=0.0, lt=1.0)
    forecast_horizon: int = Field(default=5, ge=1)


@dataclass(frozen=True, slots=True)
class _VARState:
    intercept: FloatArray
    coefficients: FloatArray  # (k, k*p) = [A_1 ... A_p]
    companion_eigenvalues: FloatArray
    residual_covariance: FloatArray
    n_series: int


class VARModel(BaseMathModel):
    r"""VAR(p): :math:`y_t = c + \sum_{j=1}^{p} A_j y_{t-j} + u_t`, :math:`u_t \sim (0, \Sigma_u)`.

    Stacked regression :math:`Y = Z B + U` with :math:`Z_t = [1, y_{t-1}^\top, \dots, y_{t-p}^\top]`:

    .. math:: \hat B = (Z^\top Z + \lambda D)^{-1} Z^\top Y \quad (\lambda > 0,\ D = I \text{ with } D_{00}=0),\qquad
              \hat B = (Z^\top Z)^{+} Z^\top Y \quad (\lambda = 0)

    Companion matrix and stationarity gate:

    .. math:: \mathbf F = \begin{bmatrix} A_1 & A_2 & \cdots & A_p \\ I_k & 0 & \cdots & 0 \\ & \ddots & & \vdots \\ 0 & \cdots & I_k & 0\end{bmatrix},
              \qquad \max_i \lvert\lambda_i(\mathbf F)\rvert < 1 - \epsilon

    A violation raises ``NumericalStabilityError``. ``predict(X)`` iterates the recursion from the
    last :math:`p` rows of ``X`` and returns ``(horizon, k)``.
    """

    config: VARConfig

    def __init__(self, config: VARConfig | None = None) -> None:
        super().__init__(resolve_config(config, VARConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        p = cfg.lags
        Y = as_feature_matrix(X, name="X", min_samples=p + 2)
        n_obs, k = Y.shape
        target = Y[p:]
        lagged = np.hstack([Y[p - j : n_obs - j] for j in range(1, p + 1)])
        Z = np.hstack([np.ones((lagged.shape[0], 1)), lagged]) if cfg.include_intercept else lagged
        gram = Z.T @ Z
        if cfg.ridge_alpha > 0.0:
            penalty = cfg.ridge_alpha * np.eye(Z.shape[1])
            if cfg.include_intercept:
                penalty[0, 0] = 0.0
            B = np.linalg.solve(gram + penalty, Z.T @ target)
        else:
            B = np.linalg.pinv(gram) @ (Z.T @ target)
        if not np.all(np.isfinite(B)):
            raise NumericalStabilityError("VAR coefficient estimate is not finite.")
        intercept = B[0] if cfg.include_intercept else np.zeros(k)
        coefficients = (B[1:] if cfg.include_intercept else B).T
        companion = np.zeros((k * p, k * p), dtype=np.float64)
        companion[:k, :] = coefficients
        if p > 1:
            companion[k:, : k * (p - 1)] = np.eye(k * (p - 1))
        eigenvalues = np.linalg.eigvals(companion)
        modulus = float(np.max(np.abs(eigenvalues)))
        if modulus >= 1.0 - cfg.stationarity_tolerance:
            raise NumericalStabilityError(f"VAR is non-stationary: max |lambda| = {modulus:.6f} >= 1.")
        residuals = target - Z @ B
        dof = residuals.shape[0] - Z.shape[1]
        if dof <= 0:
            raise ValueError("Insufficient observations for the requested lag order.")
        self._publish_state(
            _VARState(
                intercept=freeze(intercept),
                coefficients=freeze(coefficients),
                companion_eigenvalues=freeze(np.abs(eigenvalues)),
                residual_covariance=freeze(residuals.T @ residuals / dof),
                n_series=k,
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _VARState = self._current_state()
        p = self.config.lags
        horizon = _positive_int_kwarg(kwargs, "horizon", self.config.forecast_horizon)
        history = as_feature_matrix(X, name="X", n_features=state.n_series, min_samples=p)
        window = [row for row in history[-p:]]
        out = np.empty((horizon, state.n_series), dtype=np.float64)
        for h in range(horizon):
            lag_vector = np.concatenate(window[::-1])
            nxt = state.intercept + state.coefficients @ lag_vector
            out[h] = nxt
            window = window[1:] + [nxt]
        return sanitize(out)

    @property
    def coefficient_matrices(self) -> FloatArray:
        """:math:`A_j` stacked with shape ``(p, k, k)``."""
        s: _VARState = self._current_state()
        k = s.n_series
        return np.stack([s.coefficients[:, j * k : (j + 1) * k] for j in range(self.config.lags)])

    @property
    def residual_covariance(self) -> FloatArray:
        return self._current_state().residual_covariance
