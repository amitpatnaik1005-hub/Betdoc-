"""Autoregressive Integrated Moving Average (ARIMA) using exact OLS/Yule-Walker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field, model_validator
from scipy import linalg, optimize

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_vector,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["ARIMAConfig", "ARIMAModel"]


class ARIMAConfig(MathModelConfig):
    p: int = Field(default=1, ge=0, description="Auto-regressive order.")
    d: int = Field(default=0, ge=0, description="Differencing degree.")
    q: int = Field(default=0, ge=0, description="Moving-average order.")
    max_iter: int = Field(default=100, gt=0)
    tolerance: float = Field(default=1e-6, gt=0.0)

    @model_validator(mode="after")
    def _check_orders(self) -> Any:
        if self.p == 0 and self.q == 0:
            raise ValueError("At least one of p or q must be strictly positive.")
        return self


@dataclass(frozen=True, slots=True)
class _ARIMAState:
    ar_params: FloatArray
    ma_params: FloatArray
    intercept: float
    variance: float
    residuals: FloatArray
    last_observations: FloatArray


class ARIMAModel(BaseMathModel):
    """ARIMA(p, d, q) time series model implemented from scratch.

    If q=0, exact OLS is used for the AR(p) component.
    If q>0, conditional sum-of-squares (CSS) optimization is used via ``scipy.optimize``.
    """

    config: ARIMAConfig

    def __init__(self, config: ARIMAConfig | None = None) -> None:
        super().__init__(resolve_config(config, ARIMAConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        series = as_vector(X, name="X")
        diff_series = self._difference(series, self.config.d)
        n = len(diff_series)

        if n <= self.config.p + self.config.q:
            raise ValueError("Not enough observations to fit ARIMA(p,d,q).")

        mu = float(np.mean(diff_series))
        centred = diff_series - mu

        if self.config.q == 0:
            # Exact OLS for AR(p)
            ar_params, res = self._fit_ar(centred, self.config.p)
            ma_params = np.zeros(0, dtype=np.float64)
            var = float(np.var(res)) if len(res) > 0 else 1.0
        else:
            # Conditional Sum of Squares for ARMA(p, q)
            x0 = np.zeros(self.config.p + self.config.q, dtype=np.float64)
            if self.config.p > 0:
                x0[: self.config.p], _ = self._fit_ar(centred, self.config.p)

            def css(params: FloatArray) -> float:
                res = self._compute_residuals(centred, params[: self.config.p], params[self.config.p :])
                return float(np.sum(res**2))

            result = optimize.minimize(
                css,
                x0,
                method="L-BFGS-B",
                options={"maxiter": self.config.max_iter, "ftol": self.config.tolerance},
            )
            params = np.asarray(result.x, dtype=np.float64)
            ar_params = params[: self.config.p]
            ma_params = params[self.config.p :]
            res = self._compute_residuals(centred, ar_params, ma_params)
            var = float(np.var(res)) if len(res) > 0 else 1.0

        state = _ARIMAState(
            ar_params=freeze(ar_params),
            ma_params=freeze(ma_params),
            intercept=mu,
            variance=var,
            residuals=freeze(res[-self.config.q :] if self.config.q > 0 else np.zeros(0)),
            last_observations=freeze(series[-max(self.config.p + self.config.d, 1) :]),
        )
        self._publish_state(state)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Forecast ``steps`` ahead. ``X`` must be a scalar integer."""
        state: _ARIMAState = self._current_state()
        try:
            steps = int(np.asarray(X).item())
        except (ValueError, TypeError):
            raise ValueError("ARIMA predict requires a scalar integer number of steps.")

        if steps <= 0:
            return np.zeros(0, dtype=np.float64)

        forecast_diff = np.zeros(steps, dtype=np.float64)
        history = list(state.last_observations)
        if self.config.d > 0:
            diff_history = list(self._difference(np.array(history), self.config.d))
        else:
            diff_history = history.copy()

        diff_history = [x - state.intercept for x in diff_history]
        residuals = list(state.residuals)

        for i in range(steps):
            pred = 0.0
            for j, ar in enumerate(state.ar_params):
                if len(diff_history) > j:
                    pred += ar * diff_history[-(j + 1)]
            for j, ma in enumerate(state.ma_params):
                if len(residuals) > j:
                    pred += ma * residuals[-(j + 1)]

            forecast_diff[i] = pred + state.intercept
            diff_history.append(pred)
            residuals.append(0.0)

        out = np.empty(steps, dtype=np.float64)
        current = np.array(history)
        for i in range(steps):
            if self.config.d == 0:
                val = forecast_diff[i]
            else:
                val = forecast_diff[i]
                for d_idx in range(self.config.d):
                    val += current[-(d_idx + 1)]
            out[i] = val
            current = np.append(current, val)

        return sanitize(out)

    @staticmethod
    def _difference(series: FloatArray, d: int) -> FloatArray:
        out = series
        for _ in range(d):
            out = np.diff(out)
        return out

    @staticmethod
    def _fit_ar(series: FloatArray, p: int) -> tuple[FloatArray, FloatArray]:
        if p == 0:
            return np.zeros(0, dtype=np.float64), series
        n = len(series)
        X_mat = np.zeros((n - p, p), dtype=np.float64)
        for i in range(p):
            X_mat[:, i] = series[p - i - 1 : n - i - 1]
        y_vec = series[p:]
        try:
            params, _, _, _ = linalg.lstsq(X_mat, y_vec)
        except linalg.LinAlgError:
            params = np.zeros(p, dtype=np.float64)
        res = y_vec - X_mat @ params
        return params, res

    @staticmethod
    def _compute_residuals(series: FloatArray, ar: FloatArray, ma: FloatArray) -> FloatArray:
        p, q = len(ar), len(ma)
        n = len(series)
        res = np.zeros(n, dtype=np.float64)
        for i in range(n):
            pred = 0.0
            for j in range(p):
                if i - j - 1 >= 0:
                    pred += ar[j] * series[i - j - 1]
            for j in range(q):
                if i - j - 1 >= 0:
                    pred += ma[j] * res[i - j - 1]
            res[i] = series[i] - pred
        return res
