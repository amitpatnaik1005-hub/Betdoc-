"""Discrete-time Markov Chains for sequence modelling and state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    freeze,
    require_integral,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["MarkovChainConfig", "MarkovChainModel"]


class MarkovChainConfig(MathModelConfig):
    n_states: int = Field(default=2, ge=2)
    laplace_alpha: float = Field(default=0.1, ge=0.0)
    ergodic_eps: float = Field(default=1e-8, ge=0.0)


@dataclass(frozen=True, slots=True)
class _MarkovState:
    transition_matrix: FloatArray
    stationary_distribution: FloatArray


class MarkovChainModel(BaseMathModel):
    """Homogeneous Discrete-Time Markov Chain.

    ``fit`` estimates the row-stochastic transition matrix :math:`P` from sequential data.
    ``predict`` propagates an initial state distribution :math:`k` steps forward:
    :math:`\pi_k = \pi_0 P^k`.
    """

    config: MarkovChainConfig

    def __init__(self, config: MarkovChainConfig | None = None) -> None:
        super().__init__(resolve_config(config, MarkovChainConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        sequence = require_integral(as_feature_matrix(X, name="X", n_features=1), name="X")[:, 0]
        n = self.config.n_states
        if sequence.max() >= n or sequence.min() < 0:
            raise ValueError(f"States must be in [0, {n-1}].")

        counts = np.zeros((n, n), dtype=np.float64)
        for t in range(len(sequence) - 1):
            counts[sequence[t], sequence[t + 1]] += 1.0

        alpha = self.config.laplace_alpha
        smoothed = counts + alpha
        P = safe_array_divide(smoothed, smoothed.sum(axis=1, keepdims=True), default=1.0 / n)

        if self.config.ergodic_eps > 0:
            P = (1.0 - self.config.ergodic_eps) * P + self.config.ergodic_eps / n

        eigenvalues, eigenvectors = np.linalg.eig(P.T)
        idx = np.argmin(np.abs(eigenvalues - 1.0))
        stat = np.abs(eigenvectors[:, idx])
        stationary = safe_array_divide(stat, stat.sum(), default=1.0 / n)

        self._publish_state(_MarkovState(freeze(P), freeze(stationary)))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Predict state distribution ``k`` steps ahead.

        ``X`` must have shape ``(n_samples, n_states + 1)``.
        Columns ``0..n_states-1`` are the initial distribution :math:`\pi_0`.
        Column ``n_states`` is the integer number of steps :math:`k`.
        """
        matrix = as_feature_matrix(X, name="X")
        n = self.config.n_states
        if matrix.shape[1] != n + 1:
            raise ValueError(f"X must have {n + 1} columns (initial dist + steps).")

        pi_0 = matrix[:, :n]
        steps = require_integral(matrix[:, n : n + 1], name="steps")[:, 0]
        if np.any(steps < 0):
            raise ValueError("Steps must be non-negative.")

        state: _MarkovState = self._current_state()
        P = state.transition_matrix

        out = np.empty_like(pi_0)
        unique_steps = np.unique(steps)
        for k in unique_steps:
            mask = steps == k
            P_k = np.linalg.matrix_power(P, int(k))
            out[mask] = pi_0[mask] @ P_k

        return sanitize(out)

    def viterbi(self, observations: FloatArray, emission_matrix: FloatArray) -> npt.NDArray[np.int64]:
        """Decode the most likely state sequence via dynamic programming."""
        state: _MarkovState = self._current_state()
        P = np.log(np.maximum(state.transition_matrix, 1e-15))
        E = np.log(np.maximum(emission_matrix, 1e-15))
        pi = np.log(np.maximum(state.stationary_distribution, 1e-15))

        obs = require_integral(as_feature_matrix(observations, name="obs", n_features=1), name="obs")[:, 0]
        n_steps = len(obs)
        n_states = self.config.n_states

        T1 = np.empty((n_states, n_steps), dtype=np.float64)
        T2 = np.empty((n_states, n_steps), dtype=np.int64)

        T1[:, 0] = pi + E[:, obs[0]]
        T2[:, 0] = 0

        for i in range(1, n_steps):
            for j in range(n_states):
                transitions = T1[:, i - 1] + P[:, j] + E[j, obs[i]]
                best_prev = int(np.argmax(transitions))
                T1[j, i] = transitions[best_prev]
                T2[j, i] = best_prev

        path = np.empty(n_steps, dtype=np.int64)
        path[-1] = int(np.argmax(T1[:, -1]))
        for i in range(n_steps - 1, 0, -1):
            path[i - 1] = T2[path[i], i]

        return path
