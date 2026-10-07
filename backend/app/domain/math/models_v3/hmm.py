"""Gaussian Hidden Markov Model: scaled Forward-Backward (Baum-Welch EM) and log-space Viterbi."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator
from sklearn.cluster import KMeans

from app.domain.math.models_v2.base import (
    FLOAT_TINY,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_float_array,
    check_finite_scalar,
    freeze,
    resolve_config,
    safe_array_divide,
)

__all__ = ["HMMConfig", "HMMModel"]

_LOG_2PI: float = math.log(2.0 * math.pi)


class HMMConfig(MathModelConfig):
    n_states: int = Field(default=2, ge=2)
    n_iter: int = Field(default=200, ge=1)
    tol: float = Field(default=1e-6, gt=0.0)
    min_variance: float = Field(default=1e-6, gt=0.0)
    transition_pseudocount: float = Field(default=0.0, ge=0.0)
    self_transition_init: float = Field(default=0.9, gt=0.0, lt=1.0)
    initial_transition: tuple[tuple[float, ...], ...] | None = None
    initial_start_probability: tuple[float, ...] | None = None
    row_sum_tolerance: float = Field(default=1e-8, gt=0.0)
    kmeans_n_init: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _validate_stochastic(self) -> Self:
        k, tol = self.n_states, self.row_sum_tolerance
        if self.initial_transition is not None:
            try:
                matrix = np.asarray(self.initial_transition, dtype=np.float64)
            except ValueError as exc:
                raise ValueError("initial_transition must be a rectangular matrix.") from exc
            if matrix.shape != (k, k):
                raise ValueError(f"initial_transition must have shape ({k}, {k}).")
            if not np.all(np.isfinite(matrix)) or np.any(matrix < 0.0):
                raise ValueError("initial_transition entries must be finite and non-negative.")
            if np.any(np.abs(matrix.sum(axis=1) - 1.0) > tol):
                raise ValueError("Every row of initial_transition must sum to 1.0.")
        if self.initial_start_probability is not None:
            start = np.asarray(self.initial_start_probability, dtype=np.float64)
            if start.shape != (k,) or not np.all(np.isfinite(start)) or np.any(start < 0.0):
                raise ValueError(f"initial_start_probability must be {k} finite non-negative values.")
            if abs(float(start.sum()) - 1.0) > tol:
                raise ValueError("initial_start_probability must sum to 1.0.")
        return self


@dataclass(frozen=True, slots=True)
class _HMMState:
    start: FloatArray
    transition: FloatArray
    means: FloatArray
    variances: FloatArray
    log_likelihood: float
    n_iter: int
    converged: bool
    n_features: int


class HMMModel(BaseMathModel):
    r"""Latent market-regime detection with diagonal-Gaussian emissions.

    .. math:: P(O, Z) = \pi_{z_1} b_{z_1}(o_1)\prod_{t=2}^{T} A_{z_{t-1} z_t}\, b_{z_t}(o_t),\qquad
              b_k(o) = \prod_d \mathcal N(o_d;\mu_{kd}, \sigma^2_{kd})

    Scaled forward recursion (no underflow), with per-step scale :math:`c_t`:

    .. math:: \hat\alpha_t(j) = \frac{b_j(o_t)\sum_i \hat\alpha_{t-1}(i) A_{ij}}{c_t},\qquad
              \log P(O) = \sum_t \log c_t

    Emissions are additionally shifted by :math:`\max_k \log b_k(o_t)` (log-sum-exp trick).
    Scaled backward: :math:`\hat\beta_t(i) = \sum_j A_{ij} b_j(o_{t+1})\hat\beta_{t+1}(j) / c_{t+1}`.

    Viterbi in log space:

    .. math:: \delta_t(j) = \max_i\bigl[\delta_{t-1}(i) + \log A_{ij}\bigr] + \log b_j(o_t)

    ``predict`` returns the Viterbi most-likely hidden-state sequence.
    """

    config: HMMConfig

    def __init__(self, config: HMMConfig | None = None) -> None:
        super().__init__(resolve_config(config, HMMConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        obs = self._as_observations(X, n_features=None)
        if obs.shape[0] <= cfg.n_states:
            raise ValueError("Number of observations must exceed n_states.")
        means, variances = self._initial_emissions(obs)
        transition = self._initial_transition()
        start = self._initial_start()
        previous: float | None = None
        converged = False
        n_iter = 0
        log_likelihood = -math.inf
        for iteration in range(cfg.n_iter):
            gamma, xi_sum, log_likelihood = self._forward_backward(
                self._log_emission(obs, means, variances), start, transition
            )
            n_iter = iteration + 1
            if previous is not None and abs(log_likelihood - previous) < cfg.tol:
                converged = True
                break
            previous = log_likelihood
            start, transition, means, variances = self._m_step(obs, gamma, xi_sum, means)
        if not converged:
            _, _, log_likelihood = self._forward_backward(self._log_emission(obs, means, variances), start, transition)
        self._publish_state(
            _HMMState(
                start=freeze(start),
                transition=freeze(transition),
                means=freeze(means),
                variances=freeze(variances),
                log_likelihood=check_finite_scalar(log_likelihood, name="log_likelihood"),
                n_iter=n_iter,
                converged=converged,
                n_features=obs.shape[1],
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _HMMState = self._current_state()
        obs = self._as_observations(X, n_features=state.n_features)
        log_b = self._log_emission(obs, state.means, state.variances)
        return self._viterbi(log_b, state.start, state.transition).astype(np.float64)

    def predict_proba(self, X: FloatArray) -> FloatArray:
        state: _HMMState = self._current_state()
        obs = self._as_observations(X, n_features=state.n_features)
        gamma, _, _ = self._forward_backward(self._log_emission(obs, state.means, state.variances), state.start, state.transition)
        return gamma

    def score(self, X: FloatArray) -> float:
        state: _HMMState = self._current_state()
        obs = self._as_observations(X, n_features=state.n_features)
        _, _, ll = self._forward_backward(self._log_emission(obs, state.means, state.variances), state.start, state.transition)
        return ll

    @property
    def transition_matrix(self) -> FloatArray:
        return self._current_state().transition

    @staticmethod
    def _log_emission(obs: FloatArray, means: FloatArray, variances: FloatArray) -> FloatArray:
        diff = obs[:, None, :] - means[None, :, :]
        return -0.5 * (np.log(2.0 * np.pi * variances).sum(axis=1)[None, :] + (diff**2 / variances[None, :, :]).sum(axis=2))

    @staticmethod
    def _forward_backward(log_b: FloatArray, start: FloatArray, transition: FloatArray) -> tuple[FloatArray, FloatArray, float]:
        n_obs, n_states = log_b.shape
        shift = log_b.max(axis=1, keepdims=True)
        b = np.exp(log_b - shift)
        alpha = np.empty((n_obs, n_states), dtype=np.float64)
        scale = np.empty(n_obs, dtype=np.float64)
        current = start * b[0]
        for t in range(n_obs):
            if t > 0:
                current = (alpha[t - 1] @ transition) * b[t]
            scale[t] = float(current.sum())
            if scale[t] <= FLOAT_TINY or not math.isfinite(scale[t]):
                raise NumericalStabilityError(f"Zero likelihood at t={t}; model cannot explain the observation.")
            alpha[t] = current / scale[t]
        beta = np.empty_like(alpha)
        beta[-1] = 1.0
        for t in range(n_obs - 2, -1, -1):
            beta[t] = (transition @ (b[t + 1] * beta[t + 1])) / scale[t + 1]
        gamma = alpha * beta
        gamma = safe_array_divide(gamma, gamma.sum(axis=1, keepdims=True), default=1.0 / n_states)
        weighted = b[1:] * beta[1:] / scale[1:, None]
        xi_sum = transition * (alpha[:-1].T @ weighted)
        log_likelihood = float(np.log(scale).sum() + shift.sum())
        return gamma, xi_sum, log_likelihood

    def _m_step(
        self, obs: FloatArray, gamma: FloatArray, xi_sum: FloatArray, previous_means: FloatArray
    ) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
        cfg = self.config
        k = cfg.n_states
        start = safe_array_divide(gamma[0], gamma[0].sum(), default=1.0 / k)
        counts = xi_sum + cfg.transition_pseudocount
        transition = safe_array_divide(counts, counts.sum(axis=1, keepdims=True), default=1.0 / k)
        weights = gamma.sum(axis=0)
        occupied = weights > FLOAT_TINY
        means = np.where(occupied[:, None], safe_array_divide(gamma.T @ obs, weights[:, None]), previous_means)
        sq = (obs[:, None, :] - means[None, :, :]) ** 2
        variances = np.maximum(safe_array_divide(np.einsum("tk,tkd->kd", gamma, sq), weights[:, None]), cfg.min_variance)
        return start, transition, means, variances

    @staticmethod
    def _viterbi(log_b: FloatArray, start: FloatArray, transition: FloatArray) -> np.ndarray:
        n_obs, n_states = log_b.shape
        log_a = np.log(np.maximum(transition, FLOAT_TINY))
        delta = np.log(np.maximum(start, FLOAT_TINY)) + log_b[0]
        backptr = np.zeros((n_obs, n_states), dtype=np.int64)
        cols = np.arange(n_states)
        for t in range(1, n_obs):
            scores = delta[:, None] + log_a
            backptr[t] = np.argmax(scores, axis=0)
            delta = scores[backptr[t], cols] + log_b[t]
        path = np.empty(n_obs, dtype=np.int64)
        path[-1] = int(np.argmax(delta))
        for t in range(n_obs - 1, 0, -1):
            path[t - 1] = backptr[t, path[t]]
        return path

    def _initial_emissions(self, obs: FloatArray) -> tuple[FloatArray, FloatArray]:
        cfg = self.config
        km = KMeans(n_clusters=cfg.n_states, n_init=cfg.kmeans_n_init, random_state=cfg.random_state).fit(obs)
        order = np.argsort(km.cluster_centers_[:, 0], kind="stable")
        means = np.asarray(km.cluster_centers_[order], dtype=np.float64)
        remap = np.empty_like(order)
        remap[order] = np.arange(order.size)
        labels = remap[km.labels_]
        global_var = np.maximum(obs.var(axis=0), cfg.min_variance)
        variances = np.vstack(
            [np.maximum(obs[labels == k].var(axis=0), cfg.min_variance) if np.sum(labels == k) > 1 else global_var for k in range(cfg.n_states)]
        )
        return means, variances

    def _initial_transition(self) -> FloatArray:
        cfg = self.config
        if cfg.initial_transition is not None:
            matrix = np.asarray(cfg.initial_transition, dtype=np.float64)
            return matrix / matrix.sum(axis=1, keepdims=True)
        k = cfg.n_states
        matrix = np.full((k, k), (1.0 - cfg.self_transition_init) / (k - 1), dtype=np.float64)
        np.fill_diagonal(matrix, cfg.self_transition_init)
        return matrix

    def _initial_start(self) -> FloatArray:
        cfg = self.config
        if cfg.initial_start_probability is not None:
            start = np.asarray(cfg.initial_start_probability, dtype=np.float64)
            return start / start.sum()
        return np.full(cfg.n_states, 1.0 / cfg.n_states, dtype=np.float64)

    @staticmethod
    def _as_observations(X: FloatArray, *, n_features: int | None) -> FloatArray:
        arr = as_float_array(X, name="X", ndim=(1, 2))
        obs = arr.reshape(-1, 1) if arr.ndim == 1 else arr
        if n_features is not None and obs.shape[1] != n_features:
            raise ValueError(f"X must have {n_features} feature columns.")
        return obs
