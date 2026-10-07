"""Ornstein-Uhlenbeck mean-reverting SDE with exact-transition MLE."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import optimize

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_float_array,
    as_vector,
    check_finite_scalar,
    resolve_config,
    sanitize,
)

__all__ = ["OrnsteinUhlenbeckConfig", "OrnsteinUhlenbeckModel"]


def _positive_int_kwarg(kwargs: dict[str, Any], name: str, default: int) -> int:
    value = kwargs.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


class OrnsteinUhlenbeckConfig(MathModelConfig):
    dt: float = Field(default=1.0, gt=0.0)
    theta_bounds: tuple[float, float] = (1e-8, 1e4)
    sigma_bounds: tuple[float, float] = (1e-10, 1e6)
    refine_mle: bool = True
    forecast_horizon: int = Field(default=10, ge=1)
    n_steps: int = Field(default=100, ge=1)
    n_paths: int = Field(default=1000, ge=1)
    max_elements: int = Field(default=50_000_000, ge=1)
    min_observations: int = Field(default=10, ge=3)
    max_iter: int = Field(default=1000, ge=1)
    tol: float = Field(default=1e-10, gt=0.0)
    penalty_value: float = Field(default=1e12, gt=0.0)
    rng_stream: int = Field(default=44, ge=0)

    @model_validator(mode="after")
    def _enforce_positive_theta(self) -> Self:
        for name in ("theta_bounds", "sigma_bounds"):
            low, high = getattr(self, name)
            if not 0.0 < low < high:
                raise ValueError(f"{name} must satisfy 0 < low < high (theta > 0, sigma > 0).")
        return self


@dataclass(frozen=True, slots=True)
class _OUState:
    theta: float
    mu: float
    sigma: float
    log_likelihood: float
    last_value: float


class OrnsteinUhlenbeckModel(BaseMathModel):
    r"""Mean-reverting diffusion :math:`dx_t = \theta(\mu - x_t)\,dt + \sigma\,dW_t`, :math:`\theta > 0`.

    Exact AR(1) transition with :math:`b = e^{-\theta\Delta}`:

    .. math:: x_{t+\Delta}\mid x_t \sim \mathcal N\!\Bigl(\mu + (x_t - \mu)\,b,\ \frac{\sigma^2}{2\theta}\bigl(1 - e^{-2\theta\Delta}\bigr)\Bigr)

    Closed-form MLE from OLS :math:`x_{t+1} = a + b\,x_t + \varepsilon`:
    :math:`\hat\theta = -\ln\hat b/\Delta`, :math:`\hat\mu = \hat a/(1-\hat b)`,
    :math:`\hat\sigma^2 = 2\hat\theta\hat s^2/(1 - \hat b^2)`, optionally refined by bounded
    L-BFGS-B on the exact negative log-likelihood (never the Euler variance :math:`\sigma^2\Delta`).

    ``predict(X)`` conditions on history ``X`` and returns
    :math:`\mathbb E[x_{T+h}] = \mu + (x_T - \mu)e^{-\theta h\Delta}` for :math:`h = 1..H`.
    """

    config: OrnsteinUhlenbeckConfig

    def __init__(self, config: OrnsteinUhlenbeckConfig | None = None) -> None:
        super().__init__(resolve_config(config, OrnsteinUhlenbeckConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        x = as_vector(X, name="X")
        if x.size < cfg.min_observations:
            raise ValueError(f"OU calibration requires at least {cfg.min_observations} observations.")
        x0, x1 = x[:-1], x[1:]
        design = np.column_stack([np.ones(x0.size), x0])
        (a, b), *_ = np.linalg.lstsq(design, x1, rcond=None)
        if not 0.0 < b < 1.0:
            raise NumericalStabilityError(f"AR(1) coefficient b={b:.6g} implies no mean reversion (theta <= 0).")
        resid = x1 - a - b * x0
        s2 = float(resid @ resid) / x0.size
        theta = float(np.clip(-math.log(b) / cfg.dt, *cfg.theta_bounds))
        mu = float(a / (1.0 - b))
        sigma = float(np.clip(math.sqrt(max(2.0 * theta * s2 / (1.0 - b * b), 0.0)), *cfg.sigma_bounds))
        params = np.array([theta, mu, sigma], dtype=np.float64)
        if cfg.refine_mle:
            result = optimize.minimize(
                self._negative_log_likelihood, params, args=(x0, x1), method="L-BFGS-B",
                bounds=[cfg.theta_bounds, (None, None), cfg.sigma_bounds],
                options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
            )
            if result.success and float(result.fun) <= self._negative_log_likelihood(params, x0, x1):
                params = np.asarray(result.x, dtype=np.float64)
        nll = self._negative_log_likelihood(params, x0, x1)
        self._publish_state(
            _OUState(
                theta=check_finite_scalar(float(params[0]), name="theta"),
                mu=check_finite_scalar(float(params[1]), name="mu"),
                sigma=check_finite_scalar(float(params[2]), name="sigma"),
                log_likelihood=check_finite_scalar(-nll, name="log_likelihood"),
                last_value=float(x[-1]),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _OUState = self._current_state()
        horizon = _positive_int_kwarg(kwargs, "horizon", self.config.forecast_horizon)
        history = as_float_array(X, name="X", ndim=(1, 2), allow_empty=True).ravel()
        last = float(history[-1]) if history.size else state.last_value
        h = np.arange(1, horizon + 1, dtype=np.float64)
        return sanitize(state.mu + (last - state.mu) * np.exp(-state.theta * h * self.config.dt))

    def forecast_variance(self, horizon: int | None = None) -> FloatArray:
        state: _OUState = self._current_state()
        steps = horizon if horizon is not None else self.config.forecast_horizon
        h = np.arange(1, steps + 1, dtype=np.float64)
        return state.sigma**2 / (2.0 * state.theta) * -np.expm1(-2.0 * state.theta * h * self.config.dt)

    def simulate(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Exact-discretisation paths, shape ``(n_series, n_paths, n_steps)``."""
        cfg = self.config
        state: _OUState = self._current_state()
        n_steps = _positive_int_kwarg(kwargs, "n_steps", cfg.n_steps)
        n_paths = _positive_int_kwarg(kwargs, "n_paths", cfg.n_paths)
        x0 = as_float_array(X, name="X", ndim=(0, 1)).ravel()
        if x0.size * n_paths * n_steps > cfg.max_elements:
            raise ValueError("Requested simulation exceeds max_elements.")
        rng = self._rng(cfg.rng_stream)
        b = math.exp(-state.theta * cfg.dt)
        sd = math.sqrt(state.sigma**2 / (2.0 * state.theta) * -math.expm1(-2.0 * state.theta * cfg.dt))
        shocks = rng.standard_normal((x0.size, n_paths, n_steps)) * sd
        paths = np.empty_like(shocks)
        current = np.broadcast_to(x0[:, None], (x0.size, n_paths)).astype(np.float64)
        for t in range(n_steps):
            current = state.mu + (current - state.mu) * b + shocks[:, :, t]
            paths[:, :, t] = current
        return sanitize(paths)

    @property
    def half_life(self) -> float:
        return math.log(2.0) / self._current_state().theta

    def _negative_log_likelihood(self, params: FloatArray, x0: FloatArray, x1: FloatArray) -> float:
        theta, mu, sigma = (float(v) for v in params)
        if theta <= 0.0 or sigma <= 0.0:
            return self.config.penalty_value
        dt = self.config.dt
        mean = mu + (x0 - mu) * math.exp(-theta * dt)
        var = sigma**2 / (2.0 * theta) * -math.expm1(-2.0 * theta * dt)
        if var <= 0.0 or not math.isfinite(var):
            return self.config.penalty_value
        value = 0.5 * float(np.sum(math.log(2.0 * math.pi * var) + (x1 - mean) ** 2 / var))
        return value if math.isfinite(value) else self.config.penalty_value
