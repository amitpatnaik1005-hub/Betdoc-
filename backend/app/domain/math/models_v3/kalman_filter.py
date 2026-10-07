"""Linear Gaussian state-space Kalman filter (Joseph-form) for tracking the true line."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import optimize

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_vector,
    check_finite_scalar,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["KalmanFilterConfig", "KalmanFilterModel"]

_LOG_2PI: float = math.log(2.0 * math.pi)


class KalmanFilterConfig(MathModelConfig):
    model: Literal["local_level", "local_linear_trend"] = "local_level"
    process_noise: float = Field(default=1e-3, gt=0.0)
    trend_noise: float = Field(default=1e-5, gt=0.0)
    observation_noise: float = Field(default=1e-1, gt=0.0)
    initial_state_mean: float = 0.0
    initial_state_variance: float = Field(default=1e6, gt=0.0)
    init_from_first_observation: bool = True
    estimate_noise: bool = True
    log_noise_bounds: tuple[float, float] = (-20.0, 10.0)
    loglik_burn_in: int = Field(default=1, ge=0)
    min_observations: int = Field(default=5, ge=2)
    variance_floor: float = Field(default=1e-12, gt=0.0)
    max_iter: int = Field(default=500, ge=1)
    tol: float = Field(default=1e-9, gt=0.0)
    penalty_value: float = Field(default=1e12, gt=0.0)

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        low, high = self.log_noise_bounds
        if not low < high:
            raise ValueError("log_noise_bounds must satisfy low < high.")
        for name in ("process_noise", "trend_noise", "observation_noise"):
            if not low <= math.log(getattr(self, name)) <= high:
                raise ValueError(f"log({name}) must lie inside log_noise_bounds.")
        return self


@dataclass(frozen=True, slots=True)
class _KalmanState:
    transition: FloatArray
    observation: FloatArray
    process_cov: FloatArray
    observation_var: float
    log_likelihood: float


class KalmanFilterModel(BaseMathModel):
    r"""Kalman filter for :math:`x_t = F x_{t-1} + w_t`, :math:`z_t = H x_t + v_t`,
    :math:`w_t \sim \mathcal N(0, Q)`, :math:`v_t \sim \mathcal N(0, R)`.

    Predict: :math:`\hat x^-_t = F\hat x_{t-1}`, :math:`P^-_t = F P_{t-1} F^\top + Q`.

    Update:

    .. math:: S_t = H P^-_t H^\top + R,\qquad K_t = P^-_t H^\top S_t^{-1},\qquad
              \hat x_t = \hat x^-_t + K_t\,(z_t - H\hat x^-_t)

    Joseph-form covariance (guaranteed symmetric positive semi-definite):

    .. math:: P_t = (I - K_t H)\,P^-_t\,(I - K_t H)^\top + K_t R K_t^\top

    Noise variances are estimated by maximising the prediction-error log-likelihood
    :math:`\ell = -\tfrac12\sum_t[\log 2\pi S_t + \nu_t^2/S_t]` over log-parameters.
    ``predict`` returns filtered state means with shape ``(T, state_dim)``.
    """

    config: KalmanFilterConfig

    def __init__(self, config: KalmanFilterConfig | None = None) -> None:
        super().__init__(resolve_config(config, KalmanFilterConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        z = as_vector(X, name="X")
        if z.size < cfg.min_observations:
            raise ValueError(f"Kalman fitting requires at least {cfg.min_observations} observations.")
        trend = cfg.model == "local_linear_trend"
        noises = [cfg.process_noise] + ([cfg.trend_noise] if trend else []) + [cfg.observation_noise]
        if cfg.estimate_noise:
            result = optimize.minimize(
                self._objective,
                np.log(np.asarray(noises, dtype=np.float64)),
                args=(z,),
                method="L-BFGS-B",
                bounds=[cfg.log_noise_bounds] * len(noises),
                options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
            )
            noises = list(np.exp(np.asarray(result.x, dtype=np.float64)))
        F, H, Q, r = self._system(noises)
        _, _, ll = self._run(z, F, H, Q, r)
        self._publish_state(
            _KalmanState(freeze(F), freeze(H), freeze(Q), check_finite_scalar(r, name="R"), check_finite_scalar(ll, name="log_likelihood"))
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _KalmanState = self._current_state()
        means, _, _ = self._run(as_vector(X, name="X"), state.transition, state.observation, state.process_cov, state.observation_var)
        return means

    def filter(self, X: FloatArray) -> tuple[FloatArray, FloatArray, float]:
        """Return ``(means (T, n), covariances (T, n, n), log_likelihood)``."""
        state: _KalmanState = self._current_state()
        return self._run(as_vector(X, name="X"), state.transition, state.observation, state.process_cov, state.observation_var)

    def _system(self, noises: list[float]) -> tuple[FloatArray, FloatArray, FloatArray, float]:
        if self.config.model == "local_level":
            return np.array([[1.0]]), np.array([[1.0]]), np.array([[noises[0]]]), float(noises[1])
        return (
            np.array([[1.0, 1.0], [0.0, 1.0]]),
            np.array([[1.0, 0.0]]),
            np.diag([noises[0], noises[1]]).astype(np.float64),
            float(noises[2]),
        )

    def _objective(self, log_noises: FloatArray, z: FloatArray) -> float:
        F, H, Q, r = self._system(list(np.exp(log_noises)))
        _, _, ll = self._run(z, F, H, Q, r)
        return -ll if math.isfinite(ll) else self.config.penalty_value

    def _run(self, z: FloatArray, F: FloatArray, H: FloatArray, Q: FloatArray, r: float) -> tuple[FloatArray, FloatArray, float]:
        cfg = self.config
        n = F.shape[0]
        identity = np.eye(n)
        x = np.zeros(n, dtype=np.float64)
        x[0] = z[0] if cfg.init_from_first_observation else cfg.initial_state_mean
        P = cfg.initial_state_variance * identity
        means = np.empty((z.size, n), dtype=np.float64)
        covs = np.empty((z.size, n, n), dtype=np.float64)
        ll = 0.0
        for t, obs in enumerate(z):
            if t > 0:
                x = F @ x
                P = F @ P @ F.T + Q
            innovation = float(obs - (H @ x)[0])
            S = max(float((H @ P @ H.T)[0, 0]) + r, cfg.variance_floor)
            K = (P @ H.T) / S
            x = x + K[:, 0] * innovation
            IKH = identity - K @ H
            P = IKH @ P @ IKH.T + (K * r) @ K.T
            P = 0.5 * (P + P.T)
            means[t], covs[t] = x, P
            if t >= cfg.loglik_burn_in:
                ll -= 0.5 * (_LOG_2PI + math.log(S) + innovation**2 / S)
        return sanitize(means), sanitize(covs), ll
