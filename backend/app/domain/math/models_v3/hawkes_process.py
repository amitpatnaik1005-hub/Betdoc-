"""Univariate exponential-kernel Hawkes process: O(N) MLE, intensity and Ogata simulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Self

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

__all__ = ["HawkesConfig", "HawkesProcessModel"]

PositiveFloat = Annotated[float, Field(gt=0.0)]


class HawkesConfig(MathModelConfig):
    initial_mu: float = Field(default=1.0, gt=0.0)
    initial_alpha: float = Field(default=0.5, gt=0.0)
    initial_beta: float = Field(default=1.0, gt=0.0)
    stationarity_margin: float = Field(default=1e-6, gt=0.0, lt=1.0)
    mu_bounds: tuple[float, float] = (1e-10, 1e6)
    beta_bounds: tuple[float, float] = (1e-8, 1e6)
    observation_end: PositiveFloat | None = None
    min_events: int = Field(default=5, ge=2)
    max_iter: int = Field(default=1000, ge=1)
    tol: float = Field(default=1e-10, gt=0.0)
    penalty_value: float = Field(default=1e12, gt=0.0)
    max_simulated_events: int = Field(default=1_000_000, ge=1)
    rng_stream: int = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _enforce_subcritical(self) -> Self:
        if self.initial_alpha / self.initial_beta >= 1.0 - self.stationarity_margin:
            raise ValueError("Hawkes stationarity requires alpha / beta < 1.")
        for name, value in (("mu_bounds", self.initial_mu), ("beta_bounds", self.initial_beta)):
            low, high = getattr(self, name)
            if not 0.0 < low < high or not low <= value <= high:
                raise ValueError(f"{name} must satisfy 0 < low < high and contain the initial value.")
        return self


@dataclass(frozen=True, slots=True)
class _HawkesState:
    mu: float
    alpha: float
    beta: float
    events: FloatArray
    excitation: FloatArray  # S_i = 1 + R_i = sum_{j<=i} exp(-beta (t_i - t_j))
    observation_end: float
    log_likelihood: float


class HawkesProcessModel(BaseMathModel):
    r"""Self-exciting point process :math:`\lambda(t) = \mu + \sum_{t_i < t}\alpha e^{-\beta(t - t_i)}`,
    with branching ratio :math:`\eta = \alpha/\beta < 1` (enforced via :math:`\alpha = \eta\beta`,
    :math:`\eta \in (0, 1-\epsilon)`).

    :math:`O(N)` recursion: :math:`R_1 = 0`, :math:`R_i = e^{-\beta(t_i - t_{i-1})}(1 + R_{i-1})`.

    Exact compensator and log-likelihood on :math:`[0, T]`:

    .. math:: \Lambda(T) = \mu T + \sum_i \frac{\alpha}{\beta}\bigl(1 - e^{-\beta(T - t_i)}\bigr),\qquad
              \ell = \sum_i \log(\mu + \alpha R_i) - \Lambda(T)

    ``predict(X)`` returns :math:`\lambda(t)` at query times ``X`` from the fitted history in
    :math:`O(N + M\log N)`. :meth:`simulate` uses Ogata thinning with the seeded generator.
    """

    config: HawkesConfig

    def __init__(self, config: HawkesConfig | None = None) -> None:
        super().__init__(resolve_config(config, HawkesConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        events = as_vector(X, name="X")
        if events.size < cfg.min_events:
            raise ValueError(f"At least {cfg.min_events} events are required.")
        if np.any(events < 0.0) or np.any(np.diff(events) < 0.0):
            raise ValueError("Event times must be non-negative and sorted ascending.")
        horizon = cfg.observation_end if cfg.observation_end is not None else float(events[-1])
        if horizon < float(events[-1]):
            raise ValueError("observation_end must be >= the last event time.")
        cap = 1.0 - cfg.stationarity_margin
        x0 = np.array([cfg.initial_mu, cfg.initial_alpha / cfg.initial_beta, cfg.initial_beta], dtype=np.float64)
        result = optimize.minimize(
            self._negative_log_likelihood, x0, args=(events, horizon), method="L-BFGS-B",
            bounds=[cfg.mu_bounds, (0.0, cap), cfg.beta_bounds],
            options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
        )
        mu, eta, beta = (float(v) for v in result.x)
        alpha = eta * beta
        R = self._recursion(events, beta)
        nll = self._negative_log_likelihood(np.asarray(result.x, dtype=np.float64), events, horizon)
        self._publish_state(
            _HawkesState(
                mu=check_finite_scalar(mu, name="mu"),
                alpha=check_finite_scalar(alpha, name="alpha"),
                beta=check_finite_scalar(beta, name="beta"),
                events=freeze(events),
                excitation=freeze(1.0 + R),
                observation_end=horizon,
                log_likelihood=check_finite_scalar(-nll, name="log_likelihood"),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _HawkesState = self._current_state()
        query = as_vector(X, name="X")
        idx = np.searchsorted(state.events, query, side="left") - 1
        safe_idx = np.maximum(idx, 0)
        decay = np.exp(-state.beta * np.maximum(query - state.events[safe_idx], 0.0))
        excitation = np.where(idx >= 0, decay * state.excitation[safe_idx], 0.0)
        return sanitize(state.mu + state.alpha * excitation)

    def simulate(self, horizon: float) -> FloatArray:
        """Ogata thinning on :math:`[0, T]` with the fitted parameters."""
        state: _HawkesState = self._current_state()
        if not math.isfinite(horizon) or horizon <= 0.0:
            raise ValueError("horizon must be a positive finite number.")
        rng = self._rng(self.config.rng_stream)
        t, excitation, out = 0.0, 0.0, []
        while len(out) < self.config.max_simulated_events:
            upper = state.mu + state.alpha * excitation
            wait = float(rng.exponential(1.0 / upper))
            excitation *= math.exp(-state.beta * wait)
            t += wait
            if t >= horizon:
                break
            if float(rng.random()) * upper <= state.mu + state.alpha * excitation:
                out.append(t)
                excitation += 1.0
        return np.asarray(out, dtype=np.float64)

    @property
    def parameters(self) -> dict[str, float]:
        s: _HawkesState = self._current_state()
        eta = s.alpha / s.beta
        return {
            "mu": s.mu, "alpha": s.alpha, "beta": s.beta, "branching_ratio": eta,
            "stationary_intensity": s.mu / (1.0 - eta), "log_likelihood": s.log_likelihood,
        }

    @staticmethod
    def _recursion(events: FloatArray, beta: float) -> FloatArray:
        R = np.zeros(events.size, dtype=np.float64)
        decay = np.exp(-beta * np.diff(events))
        for i in range(1, events.size):
            R[i] = decay[i - 1] * (1.0 + R[i - 1])
        return R

    def _negative_log_likelihood(self, theta: FloatArray, events: FloatArray, horizon: float) -> float:
        mu, eta, beta = (float(v) for v in theta)
        if mu <= 0.0 or beta <= 0.0 or not 0.0 <= eta < 1.0:
            return self.config.penalty_value
        alpha = eta * beta
        intensity = mu + alpha * self._recursion(events, beta)
        if np.any(intensity <= 0.0):
            return self.config.penalty_value
        compensator = mu * horizon + eta * float(np.sum(-np.expm1(-beta * (horizon - events))))
        value = compensator - float(np.sum(np.log(intensity)))
        return value if math.isfinite(value) else self.config.penalty_value
