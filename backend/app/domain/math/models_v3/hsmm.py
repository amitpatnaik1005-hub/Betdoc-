"""Explicit-duration Hidden Semi-Markov Model: log-space forward algorithm with Poisson durations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.special import logsumexp
from scipy.stats import poisson

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["HSMMConfig", "HSMMModel"]

_LOG_2PI: float = math.log(2.0 * math.pi)


class HSMMConfig(MathModelConfig):
    transition_matrix: tuple[tuple[float, ...], ...] = ((0.0, 1.0), (1.0, 0.0))
    duration_lambdas: tuple[float, ...] = (5.0, 5.0)
    emission_means: tuple[float, ...] = (0.0, 1.0)
    emission_variances: tuple[float, ...] = (1.0, 1.0)
    initial_probabilities: tuple[float, ...] | None = None
    max_duration: int = Field(default=50, ge=1)
    duration_offset: int = Field(default=1, ge=0)
    right_censored: bool = False
    row_sum_tolerance: float = Field(default=1e-8, gt=0.0)
    diagonal_tolerance: float = Field(default=0.0, ge=0.0)
    log_floor: float = Field(default=-1e12, lt=0.0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        try:
            A = np.asarray(self.transition_matrix, dtype=np.float64)
        except ValueError as exc:
            raise ValueError("transition_matrix must be rectangular.") from exc
        k = A.shape[0]
        if A.ndim != 2 or A.shape != (k, k) or k < 2:
            raise ValueError("transition_matrix must be square with at least 2 states.")
        if not np.all(np.isfinite(A)) or np.any(A < 0.0):
            raise ValueError("transition_matrix entries must be finite and non-negative.")
        if np.any(np.abs(A.sum(axis=1) - 1.0) > self.row_sum_tolerance):
            raise ValueError("Every transition row must sum to 1.0.")
        if np.any(np.abs(np.diag(A)) > self.diagonal_tolerance):
            raise ValueError("HSMM requires A_ii = 0 (self-transitions are modelled by durations).")
        for name in ("duration_lambdas", "emission_means", "emission_variances"):
            if len(getattr(self, name)) != k:
                raise ValueError(f"{name} must have length {k}.")
        if any(v <= 0.0 for v in self.duration_lambdas) or any(v <= 0.0 for v in self.emission_variances):
            raise ValueError("duration_lambdas and emission_variances must be strictly positive.")
        if self.initial_probabilities is not None:
            pi = np.asarray(self.initial_probabilities, dtype=np.float64)
            if pi.shape != (k,) or np.any(pi < 0.0) or abs(float(pi.sum()) - 1.0) > self.row_sum_tolerance:
                raise ValueError("initial_probabilities must be a length-K probability vector.")
        return self


@dataclass(frozen=True, slots=True)
class _HSMMState:
    log_transition: FloatArray
    log_initial: FloatArray
    log_duration: FloatArray  # (K, D)
    log_survival: FloatArray  # (K, D)
    means: FloatArray
    variances: FloatArray


class HSMMModel(BaseMathModel):
    r"""Explicit-duration HSMM: state :math:`j` persists :math:`d` steps with
    :math:`p_j(d) \propto \mathrm{Pois}(d - \delta;\lambda_j)`, :math:`d = 1..D`, then transitions with :math:`A_{ij}`
    (:math:`A_{ii} = 0`). Emissions :math:`b_j(o) = \mathcal N(o;\mu_j,\sigma_j^2)`.

    Log-space forward over segment ends, :math:`\alpha_t(j) = \log P(o_{1:t}, \text{segment } j \text{ ends at } t)`:

    .. math:: \alpha_t(j) = \operatorname{LSE}_{d=1}^{\min(D,t)}\Bigl[\log p_j(d) + \sum_{s=t-d+1}^{t}\log b_j(o_s) + \kappa_{t-d}(j)\Bigr]

    .. math:: \kappa_0(j) = \log\pi_j,\qquad \kappa_{t}(j) = \operatorname{LSE}_i\bigl[\alpha_t(i) + \log A_{ij}\bigr]

    :math:`\log L = \operatorname{LSE}_j\,\alpha_T(j)`. :math:`\log 0` is replaced by ``log_floor``; every
    log-space sum uses ``scipy.special.logsumexp``. ``predict`` returns the log-likelihood per sequence.
    """

    config: HSMMConfig

    def __init__(self, config: HSMMConfig | None = None) -> None:
        super().__init__(resolve_config(config, HSMMConfig))
        self._publish_state(self._build_state())

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(self._build_state())

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _HSMMState = self._current_state()
        arr = as_float_array(X, name="X", ndim=(1, 2))
        sequences = arr.reshape(1, -1) if arr.ndim == 1 else arr
        return np.array([self._log_likelihood(state, seq) for seq in sequences], dtype=np.float64)

    def _safe_log(self, values: FloatArray) -> FloatArray:
        out = np.full(values.shape, self.config.log_floor, dtype=np.float64)
        np.log(values, out=out, where=values > 0.0)
        return out

    def _build_state(self) -> _HSMMState:
        cfg = self.config
        A = np.asarray(cfg.transition_matrix, dtype=np.float64)
        k = A.shape[0]
        pi = np.full(k, 1.0 / k) if cfg.initial_probabilities is None else np.asarray(cfg.initial_probabilities, dtype=np.float64)
        d = np.arange(1, cfg.max_duration + 1, dtype=np.float64)
        lam = np.asarray(cfg.duration_lambdas, dtype=np.float64)
        raw = poisson.logpmf(d[None, :] - cfg.duration_offset, lam[:, None])
        raw = np.where(np.isfinite(raw), raw, cfg.log_floor)
        log_dur = raw - logsumexp(raw, axis=1, keepdims=True)
        log_surv = np.flip(np.logaddexp.accumulate(np.flip(log_dur, axis=1), axis=1), axis=1)
        return _HSMMState(
            log_transition=freeze(self._safe_log(A)),
            log_initial=freeze(self._safe_log(pi)),
            log_duration=freeze(check_finite_array(log_dur, name="log_duration")),
            log_survival=freeze(check_finite_array(log_surv, name="log_survival")),
            means=freeze(np.asarray(cfg.emission_means, dtype=np.float64)),
            variances=freeze(np.asarray(cfg.emission_variances, dtype=np.float64)),
        )

    def _log_likelihood(self, state: _HSMMState, obs: FloatArray) -> float:
        cfg = self.config
        T, k = obs.size, state.means.size
        log_b = -0.5 * (_LOG_2PI + np.log(state.variances)[None, :] + (obs[:, None] - state.means[None, :]) ** 2 / state.variances[None, :])
        C = np.vstack([np.zeros((1, k)), np.cumsum(log_b, axis=0)])
        kappa = np.full((T + 1, k), cfg.log_floor, dtype=np.float64)
        kappa[0] = state.log_initial
        alpha = np.full((T + 1, k), cfg.log_floor, dtype=np.float64)
        for t in range(1, T + 1):
            d = np.arange(1, min(cfg.max_duration, t) + 1)
            duration_table = state.log_survival if (cfg.right_censored and t == T) else state.log_duration
            terms = duration_table[:, d - 1].T + (C[t] - C[t - d]) + kappa[t - d]
            alpha[t] = logsumexp(terms, axis=0)
            if t < T:
                kappa[t] = logsumexp(alpha[t][:, None] + state.log_transition, axis=0)
        return check_finite_scalar(float(logsumexp(alpha[T])), name="log_likelihood")
