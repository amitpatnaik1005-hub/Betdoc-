"""Bootstrap particle filter (SMC) for non-linear implied-probability dynamics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import special

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

__all__ = ["ParticleFilterConfig", "ParticleFilterDiagnostics", "ParticleFilterModel"]


class ParticleFilterConfig(MathModelConfig):
    n_particles: int = Field(default=2000, ge=10)
    observation_space: Literal["probability", "decimal_odds"] = "probability"
    process_std: float = Field(default=0.05, gt=0.0)
    observation_std: float = Field(default=0.02, gt=0.0)
    initial_std: float = Field(default=0.5, gt=0.0)
    resample_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    estimate_process_std: bool = True
    process_std_bounds: tuple[float, float] = (1e-4, 2.0)
    init_from_first_observation: bool = True
    probability_clip: float = Field(default=1e-6, gt=0.0, lt=0.5)
    min_observations: int = Field(default=3, ge=2)

    @model_validator(mode="after")
    def _check(self) -> Self:
        low, high = self.process_std_bounds
        if not 0.0 < low < high:
            raise ValueError("process_std_bounds must satisfy 0 < low < high.")
        if not low <= self.process_std <= high:
            raise ValueError("process_std must lie inside process_std_bounds.")
        return self


@dataclass(frozen=True, slots=True)
class _PFState:
    process_std: float
    initial_logit: float


@dataclass(frozen=True, slots=True)
class ParticleFilterDiagnostics:
    means: FloatArray
    effective_sample_size: FloatArray
    n_resamples: int
    log_likelihood: float


class ParticleFilterModel(BaseMathModel):
    r"""Sequential Monte Carlo over a latent logit-probability random walk.

    .. math:: x_t = x_{t-1} + \eta_t,\ \eta_t \sim \mathcal N(0, \sigma_x^2),\qquad
              z_t = \operatorname{expit}(x_t) + \epsilon_t,\ \epsilon_t \sim \mathcal N(0, \sigma_z^2)

    Log-weight update and normalisation (log-sum-exp):

    .. math:: \log \tilde w_t^{(i)} = \log w_{t-1}^{(i)} + \log p(z_t \mid x_t^{(i)}),\qquad
              w_t^{(i)} = \exp\!\bigl(\log\tilde w_t^{(i)} - \operatorname{LSE}_j \log\tilde w_t^{(j)}\bigr)

    Effective sample size :math:`\mathrm{ESS}_t = 1/\sum_i (w_t^{(i)})^2`; systematic
    resampling with :math:`u_i = (U + i)/N` fires only when
    :math:`\mathrm{ESS}_t < \tau N`.

    ``predict`` returns the weighted particle mean per step in the observation space
    (probabilities, or decimal odds :math:`1/p`).
    """

    config: ParticleFilterConfig

    def __init__(self, config: ParticleFilterConfig | None = None) -> None:
        super().__init__(resolve_config(config, ParticleFilterConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        probs = self._to_probability(as_vector(X, name="X"))
        if probs.size < cfg.min_observations:
            raise ValueError(f"Particle filter calibration needs {cfg.min_observations} observations.")
        logits = special.logit(probs)
        process_std = cfg.process_std
        if cfg.estimate_process_std:
            increments = np.diff(logits)
            process_std = float(np.clip(increments.std(ddof=1), *cfg.process_std_bounds))
        self._publish_state(
            _PFState(
                process_std=check_finite_scalar(process_std, name="process_std"),
                initial_logit=check_finite_scalar(float(logits[0]), name="initial_logit"),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        return self.diagnostics(X).means

    def diagnostics(self, X: FloatArray) -> ParticleFilterDiagnostics:
        cfg = self.config
        state: _PFState = self._current_state()
        probs = self._to_probability(as_vector(X, name="X"))
        n = cfg.n_particles
        rng = self._rng(7)
        center = float(special.logit(probs[0])) if cfg.init_from_first_observation else state.initial_logit
        particles = rng.normal(center, cfg.initial_std, size=n)
        log_w = np.full(n, -math.log(n), dtype=np.float64)
        means = np.empty(probs.size, dtype=np.float64)
        ess_trace = np.empty(probs.size, dtype=np.float64)
        log_norm = -0.5 * math.log(2.0 * math.pi * cfg.observation_std**2)
        total_ll = 0.0
        n_resamples = 0
        for t, observation in enumerate(probs):
            if t > 0:
                particles = particles + rng.normal(0.0, state.process_std, size=n)
            predicted = special.expit(particles)
            log_lik = log_norm - 0.5 * ((observation - predicted) / cfg.observation_std) ** 2
            log_w = log_w + log_lik
            lse = float(special.logsumexp(log_w))
            total_ll += lse
            log_w = log_w - lse
            weights = np.exp(log_w)
            projected = predicted if cfg.observation_space == "probability" else 1.0 / np.maximum(predicted, cfg.probability_clip)
            means[t] = float(weights @ projected)
            ess = 1.0 / float(np.sum(weights**2))
            ess_trace[t] = ess
            if ess < cfg.resample_threshold * n:
                particles = particles[self._systematic_resample(weights, rng)]
                log_w = np.full(n, -math.log(n), dtype=np.float64)
                n_resamples += 1
        return ParticleFilterDiagnostics(
            means=sanitize(means),
            effective_sample_size=freeze(ess_trace),
            n_resamples=n_resamples,
            log_likelihood=check_finite_scalar(total_ll, name="log_likelihood"),
        )

    @staticmethod
    def _systematic_resample(weights: FloatArray, rng: np.random.Generator) -> np.ndarray:
        n = weights.size
        positions = (rng.random() + np.arange(n)) / n
        cumulative = np.cumsum(weights)
        cumulative[-1] = 1.0
        return np.minimum(np.searchsorted(cumulative, positions, side="right"), n - 1)

    def _to_probability(self, values: FloatArray) -> FloatArray:
        clip = self.config.probability_clip
        if self.config.observation_space == "decimal_odds":
            if np.any(values <= 1.0):
                raise ValueError("Decimal odds must be strictly greater than 1.0.")
            values = 1.0 / values
        elif np.any((values < 0.0) | (values > 1.0)):
            raise ValueError("Probabilities must lie in [0, 1].")
        return np.clip(values, clip, 1.0 - clip)
