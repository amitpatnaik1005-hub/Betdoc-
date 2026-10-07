"""Merton jump-diffusion: MLE calibration and Euler-Maruyama path simulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import optimize, special

from app.domain.math.models_v2.base import (
    FLOAT_MAX,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_float_array,
    as_vector,
    check_finite_scalar,
    resolve_config,
    sanitize,
)

__all__ = ["JumpDiffusionConfig", "JumpDiffusionModel"]

_LOG_FLOAT_MAX: float = math.log(FLOAT_MAX)


def _positive_int_kwarg(kwargs: dict[str, Any], name: str, default: int) -> int:
    value = kwargs.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


def _check_positive_bounds(name: str, bounds: tuple[float, float], value: float) -> None:
    low, high = bounds
    if not 0.0 < low < high:
        raise ValueError(f"{name}_bounds must satisfy 0 < low < high.")
    if not low <= value <= high:
        raise ValueError(f"{name} must lie inside {name}_bounds.")


class JumpDiffusionConfig(MathModelConfig):
    drift: float = 0.05
    volatility: float = Field(default=0.2, gt=0.0)
    jump_intensity: float = Field(default=1.0, gt=0.0)
    jump_mean: float = -0.05
    jump_std: float = Field(default=0.1, gt=0.0)
    dt: float = Field(default=1.0 / 252.0, gt=0.0)
    n_steps: int = Field(default=252, ge=1)
    n_paths: int = Field(default=1000, ge=1)
    antithetic: bool = False
    max_elements: int = Field(default=50_000_000, ge=1)
    max_jumps_per_step: int = Field(default=10, ge=1)
    volatility_bounds: tuple[float, float] = (1e-4, 5.0)
    intensity_bounds: tuple[float, float] = (1e-6, 1e3)
    jump_std_bounds: tuple[float, float] = (1e-6, 2.0)
    min_observations: int = Field(default=30, ge=5)
    max_iter: int = Field(default=2000, ge=1)
    tol: float = Field(default=1e-10, gt=0.0)
    penalty_value: float = Field(default=1e12, gt=0.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_positive_bounds("volatility", self.volatility_bounds, self.volatility)
        _check_positive_bounds("intensity", self.intensity_bounds, self.jump_intensity)
        _check_positive_bounds("jump_std", self.jump_std_bounds, self.jump_std)
        return self


@dataclass(frozen=True, slots=True)
class _JDState:
    drift: float
    volatility: float
    intensity: float
    jump_mean: float
    jump_std: float
    log_likelihood: float | None
    calibrated: bool


class JumpDiffusionModel(BaseMathModel):
    r"""Merton (1976) jump-diffusion.

    .. math:: \frac{dS_t}{S_{t^-}} = (\mu - \lambda\kappa)\,dt + \sigma\,dW_t + (e^{Y} - 1)\,dN_t,\qquad
              Y \sim \mathcal N(\mu_J, \sigma_J^2),\ N_t \sim \mathrm{Poisson}(\lambda t),\
              \kappa = e^{\mu_J + \sigma_J^2/2} - 1

    Euler-Maruyama on :math:`X_t = \log S_t`:

    .. math:: X_{t+\Delta} = X_t + \bigl(\mu - \tfrac12\sigma^2 - \lambda\kappa\bigr)\Delta + \sigma\sqrt{\Delta}\,Z
              + N_\Delta\mu_J + \sqrt{N_\Delta}\,\sigma_J\,\xi

    Calibration maximises the Poisson-mixture likelihood truncated at :math:`K` jumps:

    .. math:: p(r) = \sum_{k=0}^{K} \frac{e^{-\lambda\Delta}(\lambda\Delta)^k}{k!}\,
              \mathcal N\!\bigl(r;\ (\mu - \tfrac12\sigma^2 - \lambda\kappa)\Delta + k\mu_J,\ \sigma^2\Delta + k\sigma_J^2\bigr)

    ``predict(X, n_steps=..., n_paths=...)`` takes initial prices ``X`` and returns an array of
    shape ``(n_assets, n_paths, n_steps)``. Un-fitted models simulate from config parameters.
    """

    config: JumpDiffusionConfig

    def __init__(self, config: JumpDiffusionConfig | None = None) -> None:
        super().__init__(resolve_config(config, JumpDiffusionConfig))
        cfg = self.config
        self._publish_state(_JDState(cfg.drift, cfg.volatility, cfg.jump_intensity, cfg.jump_mean, cfg.jump_std, None, False))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        returns = as_vector(X, name="X")
        if returns.size < cfg.min_observations:
            raise ValueError(f"Calibration requires at least {cfg.min_observations} log-returns.")
        sigma0 = float(np.clip(returns.std(ddof=1) / math.sqrt(cfg.dt), *cfg.volatility_bounds))
        x0 = np.array(
            [
                float(returns.mean()) / cfg.dt + 0.5 * sigma0**2,
                sigma0,
                float(np.clip(cfg.jump_intensity, *cfg.intensity_bounds)),
                cfg.jump_mean,
                float(np.clip(cfg.jump_std, *cfg.jump_std_bounds)),
            ],
            dtype=np.float64,
        )
        bounds = [(None, None), cfg.volatility_bounds, cfg.intensity_bounds, (None, None), cfg.jump_std_bounds]
        result = optimize.minimize(
            self._negative_log_likelihood,
            x0,
            args=(returns,),
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
        )
        mu, sigma, lam, mj, sj = (float(v) for v in result.x)
        nll = self._negative_log_likelihood(np.asarray(result.x, dtype=np.float64), returns)
        self._publish_state(
            _JDState(
                drift=check_finite_scalar(mu, name="drift"),
                volatility=check_finite_scalar(sigma, name="volatility"),
                intensity=check_finite_scalar(lam, name="intensity"),
                jump_mean=check_finite_scalar(mj, name="jump_mean"),
                jump_std=check_finite_scalar(sj, name="jump_std"),
                log_likelihood=check_finite_scalar(-nll, name="log_likelihood"),
                calibrated=True,
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        cfg = self.config
        state: _JDState = self._current_state()
        n_steps = _positive_int_kwarg(kwargs, "n_steps", cfg.n_steps)
        n_paths = _positive_int_kwarg(kwargs, "n_paths", cfg.n_paths)
        if cfg.antithetic and n_paths % 2 != 0:
            raise ValueError("n_paths must be even when antithetic sampling is enabled.")
        s0 = as_float_array(X, name="X", ndim=(0, 1, 2)).ravel()
        if s0.size == 0 or np.any(s0 <= 0.0):
            raise ValueError("Initial prices must be non-empty and strictly positive.")
        shape = (s0.size, n_paths, n_steps)
        if s0.size * n_paths * n_steps > cfg.max_elements:
            raise ValueError("Requested simulation exceeds max_elements.")
        rng = self._rng(11)
        dt = cfg.dt
        kappa = math.expm1(state.jump_mean + 0.5 * state.jump_std**2)
        drift_term = (state.drift - 0.5 * state.volatility**2 - state.intensity * kappa) * dt
        if cfg.antithetic:
            half = rng.standard_normal((s0.size, n_paths // 2, n_steps))
            z = np.concatenate([half, -half], axis=1)
        else:
            z = rng.standard_normal(shape)
        jumps = rng.poisson(state.intensity * dt, size=shape).astype(np.float64)
        jump_sizes = jumps * state.jump_mean + np.sqrt(jumps) * state.jump_std * rng.standard_normal(shape)
        increments = drift_term + state.volatility * math.sqrt(dt) * z + jump_sizes
        log_paths = np.log(s0)[:, None, None] + np.cumsum(increments, axis=2)
        return sanitize(np.exp(np.minimum(log_paths, _LOG_FLOAT_MAX)))

    @property
    def parameters(self) -> dict[str, float | bool | None]:
        s: _JDState = self._current_state()
        return {
            "drift": s.drift,
            "volatility": s.volatility,
            "intensity": s.intensity,
            "jump_mean": s.jump_mean,
            "jump_std": s.jump_std,
            "log_likelihood": s.log_likelihood,
            "calibrated": s.calibrated,
        }

    def _negative_log_likelihood(self, theta: FloatArray, returns: FloatArray) -> float:
        cfg = self.config
        mu, sigma, lam, mj, sj = (float(v) for v in theta)
        if sigma <= 0.0 or lam <= 0.0 or sj <= 0.0:
            return cfg.penalty_value
        dt = cfg.dt
        kappa = math.expm1(mj + 0.5 * sj**2)
        k = np.arange(cfg.max_jumps_per_step + 1, dtype=np.float64)
        log_pk = k * math.log(lam * dt) - lam * dt - special.gammaln(k + 1.0)
        mean_k = (mu - 0.5 * sigma**2 - lam * kappa) * dt + k * mj
        var_k = sigma**2 * dt + k * sj**2
        log_density = log_pk[None, :] - 0.5 * (
            np.log(2.0 * math.pi * var_k)[None, :] + (returns[:, None] - mean_k[None, :]) ** 2 / var_k[None, :]
        )
        value = -float(special.logsumexp(log_density, axis=1).sum())
        return value if math.isfinite(value) else cfg.penalty_value
