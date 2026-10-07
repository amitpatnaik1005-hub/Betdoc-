"""GARCH(1,1) conditional volatility via constrained Gaussian MLE."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import optimize, signal

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_float_array,
    as_vector,
    check_finite_scalar,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["GARCHConfig", "GARCHModel"]

_LOG_2PI: float = math.log(2.0 * math.pi)


def _positive_int_kwarg(kwargs: dict[str, Any], name: str, default: int) -> int:
    value = kwargs.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


class GARCHConfig(MathModelConfig):
    include_mean: bool = True
    initial_alpha: float = Field(default=0.05, ge=0.0, lt=1.0)
    initial_beta: float = Field(default=0.90, ge=0.0, lt=1.0)
    stationarity_margin: float = Field(default=1e-6, gt=0.0, lt=1.0)
    omega_floor: float = Field(default=1e-12, gt=0.0)
    variance_floor: float = Field(default=1e-12, gt=0.0)
    forecast_steps: int = Field(default=1, ge=1)
    min_observations: int = Field(default=30, ge=3)
    max_iter: int = Field(default=1000, ge=1)
    tol: float = Field(default=1e-10, gt=0.0)
    penalty_value: float = Field(default=1e12, gt=0.0)

    @model_validator(mode="after")
    def _enforce_stationarity(self) -> Self:
        if self.initial_alpha + self.initial_beta >= 1.0 - self.stationarity_margin:
            raise ValueError("GARCH(1,1) requires alpha + beta < 1 for wide-sense stationarity.")
        return self


@dataclass(frozen=True, slots=True)
class _GARCHState:
    mu: float
    omega: float
    alpha: float
    beta: float
    sample_variance: float
    log_likelihood: float
    converged: bool
    returns: FloatArray


class GARCHModel(BaseMathModel):
    r"""Generalised autoregressive conditional heteroskedasticity.

    .. math:: r_t = \mu + \varepsilon_t,\qquad \varepsilon_t = \sigma_t z_t,\quad z_t \sim \mathcal N(0,1)

    .. math:: \sigma_t^2 = \omega + \alpha\,\varepsilon_{t-1}^2 + \beta\,\sigma_{t-1}^2,\qquad
              \omega > 0,\ \alpha,\beta \ge 0,\ \alpha + \beta < 1

    Log-likelihood, with :math:`\sigma_0^2` initialised to the sample variance:

    .. math:: \ell(\theta) = -\tfrac12\sum_t\left[\log 2\pi + \log\sigma_t^2 + \frac{\varepsilon_t^2}{\sigma_t^2}\right]

    :math:`h`-step forecast with :math:`\bar\sigma^2 = \omega/(1-\alpha-\beta)`:

    .. math:: \mathbb{E}[\sigma_{T+h}^2] = \bar\sigma^2 + (\alpha+\beta)^{h-1}\bigl(\sigma_{T+1}^2 - \bar\sigma^2\bigr)

    ``predict(X, steps=h)`` conditions on returns ``X`` (empty uses training returns).
    """

    config: GARCHConfig

    def __init__(self, config: GARCHConfig | None = None) -> None:
        super().__init__(resolve_config(config, GARCHConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        returns = as_vector(X, name="X")
        if returns.size < cfg.min_observations:
            raise ValueError(f"GARCH requires at least {cfg.min_observations} returns.")
        sample_var = float(returns.var(ddof=1))
        if sample_var <= cfg.variance_floor:
            raise NumericalStabilityError("Return series has zero variance.")
        cap = 1.0 - cfg.stationarity_margin
        omega0 = max(sample_var * (1.0 - cfg.initial_alpha - cfg.initial_beta), cfg.omega_floor)
        x0 = np.array(
            ([float(returns.mean())] if cfg.include_mean else []) + [omega0, cfg.initial_alpha, cfg.initial_beta],
            dtype=np.float64,
        )
        bounds = ([(None, None)] if cfg.include_mean else []) + [(cfg.omega_floor, None), (0.0, cap), (0.0, cap)]
        constraint = {"type": "ineq", "fun": lambda th: cap - th[-2] - th[-1]}
        result = optimize.minimize(
            self._negative_log_likelihood,
            x0,
            args=(returns, sample_var),
            method="SLSQP",
            bounds=bounds,
            constraints=[constraint],
            options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
        )
        theta = np.asarray(result.x, dtype=np.float64)
        mu, omega, alpha, beta = self._unpack(theta)
        if alpha + beta >= 1.0 or omega <= 0.0 or alpha < 0.0 or beta < 0.0:
            raise NumericalStabilityError("MLE violated GARCH stationarity constraints.")
        nll = self._negative_log_likelihood(theta, returns, sample_var)
        self._publish_state(
            _GARCHState(
                mu=check_finite_scalar(mu, name="mu"),
                omega=check_finite_scalar(omega, name="omega"),
                alpha=check_finite_scalar(alpha, name="alpha"),
                beta=check_finite_scalar(beta, name="beta"),
                sample_variance=sample_var,
                log_likelihood=check_finite_scalar(-nll, name="log_likelihood"),
                converged=bool(result.success),
                returns=freeze(returns),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _GARCHState = self._current_state()
        steps = _positive_int_kwarg(kwargs, "steps", self.config.forecast_steps)
        history = as_float_array(X, name="X", ndim=(1, 2), allow_empty=True).ravel()
        if history.size == 0:
            history = np.array(state.returns, dtype=np.float64)
        eps = history - state.mu
        s2 = self._variance_path(eps, state.omega, state.alpha, state.beta, state.sample_variance)
        next_var = state.omega + state.alpha * eps[-1] ** 2 + state.beta * s2[-1]
        persistence = state.alpha + state.beta
        unconditional = state.omega / (1.0 - persistence)
        horizon = np.arange(steps, dtype=np.float64)
        forecast = unconditional + persistence**horizon * (next_var - unconditional)
        return np.maximum(sanitize(forecast), self.config.variance_floor)

    def conditional_variance(self, X: FloatArray) -> FloatArray:
        """In-sample conditional variance path :math:`\\sigma_t^2` for returns ``X``."""
        state: _GARCHState = self._current_state()
        eps = as_vector(X, name="X") - state.mu
        return self._variance_path(eps, state.omega, state.alpha, state.beta, state.sample_variance)

    @property
    def parameters(self) -> dict[str, float | bool]:
        s: _GARCHState = self._current_state()
        return {
            "mu": s.mu,
            "omega": s.omega,
            "alpha": s.alpha,
            "beta": s.beta,
            "persistence": s.alpha + s.beta,
            "unconditional_variance": s.omega / (1.0 - s.alpha - s.beta),
            "log_likelihood": s.log_likelihood,
            "converged": s.converged,
        }

    def _unpack(self, theta: FloatArray) -> tuple[float, float, float, float]:
        if self.config.include_mean:
            return float(theta[0]), float(theta[1]), float(theta[2]), float(theta[3])
        return 0.0, float(theta[0]), float(theta[1]), float(theta[2])

    def _variance_path(self, eps: FloatArray, omega: float, alpha: float, beta: float, s2_0: float) -> FloatArray:
        s2 = np.empty(eps.size, dtype=np.float64)
        s2[0] = s2_0
        if eps.size > 1:
            drive = omega + alpha * eps[:-1] ** 2
            s2[1:], _ = signal.lfilter([1.0], [1.0, -beta], drive, zi=np.array([beta * s2_0]))
        return np.maximum(sanitize(s2), self.config.variance_floor)

    def _negative_log_likelihood(self, theta: FloatArray, returns: FloatArray, s2_0: float) -> float:
        mu, omega, alpha, beta = self._unpack(theta)
        if omega <= 0.0 or alpha < 0.0 or beta < 0.0 or alpha + beta >= 1.0:
            return self.config.penalty_value
        eps = returns - mu
        s2 = self._variance_path(eps, omega, alpha, beta, s2_0)
        value = 0.5 * float(np.sum(_LOG_2PI + np.log(s2) + eps**2 / s2))
        return value if math.isfinite(value) else self.config.penalty_value
