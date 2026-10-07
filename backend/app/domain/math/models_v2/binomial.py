"""Binomial and Poisson-Binomial tail probabilities for discrete sporting events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from scipy import stats

from app.domain.math.models_v2.base import (
    FLOAT_EPS,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    as_float_array,
    freeze,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["BinomialConfig", "BinomialModel"]


class BinomialConfig(MathModelConfig):
    k: int = Field(default=1, ge=0, description="Success threshold for P(S >= k).")
    method: Literal["exact", "poisson", "normal"] = "exact"
    laplace_alpha: float = Field(default=1.0, ge=0.0)
    continuity_correction: bool = True
    identical_tolerance: float = Field(default=1e-12, ge=0.0)


@dataclass(frozen=True, slots=True)
class _BinomialState:
    event_probabilities: FloatArray
    n_observations: int


class BinomialModel(BaseMathModel):
    r"""Exact and approximate tail probabilities :math:`P(S \ge k)`.

    Identical trials (Binomial):

    .. math:: P(X \ge k) = \sum_{i=k}^{n} \binom{n}{i} p^{i}(1-p)^{n-i}

    Non-identical independent trials (Poisson-Binomial), exact recursion:

    .. math:: f_j(s) = f_{j-1}(s)\,(1-p_j) + f_{j-1}(s-1)\,p_j, \qquad f_0(0) = 1

    Approximations with :math:`\mu = \sum_j p_j` and :math:`\sigma^2 = \sum_j p_j(1-p_j)`:

    .. math:: P_{\mathrm{Pois}}(S \ge k) = 1 - \sum_{i=0}^{k-1} \frac{e^{-\mu}\mu^{i}}{i!}

    .. math:: P_{\mathcal{N}}(S \ge k) = 1 - \Phi\!\left(\frac{k - c - \mu}{\sigma}\right)

    with :math:`c = 1/2` under continuity correction.

    ``fit`` estimates per-event probabilities from binary outcomes using Laplace smoothing
    :math:`\hat p_j = (\sum_i x_{ij} + a) / (n + 2a)`. ``predict`` accepts an
    ``(n_rows, m_events)`` probability matrix and does not require ``fit``.
    """

    config: BinomialConfig

    def __init__(self, config: BinomialConfig | None = None) -> None:
        super().__init__(resolve_config(config, BinomialConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        outcomes = as_feature_matrix(X, name="X")
        if not np.all((outcomes == 0.0) | (outcomes == 1.0)):
            raise ValueError("X must contain binary outcomes (0.0 or 1.0) for fit.")
        n_obs = outcomes.shape[0]
        alpha = self.config.laplace_alpha
        probabilities = safe_array_divide(outcomes.sum(axis=0) + alpha, n_obs + 2.0 * alpha)
        self._publish_state(_BinomialState(freeze(np.clip(probabilities, 0.0, 1.0)), n_obs))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        return self._tail(self._as_probability_matrix(X))

    def predict_from_fitted(self) -> float:
        """Tail probability using the fitted per-event probabilities."""
        state: _BinomialState = self._current_state()
        return float(self._tail(state.event_probabilities.reshape(1, -1))[0])

    def pmf(self, X: FloatArray) -> FloatArray:
        """Exact Poisson-Binomial PMF, shape ``(n_rows, m_events + 1)``."""
        return self._poisson_binomial_pmf(self._as_probability_matrix(X))

    @staticmethod
    def binomial_tail(n_trials: int, p: float, k: int) -> float:
        """Exact :math:`P(X \\ge k)` for :math:`X \\sim \\mathrm{Bin}(n, p)`."""
        if n_trials < 0 or k < 0:
            raise ValueError("n_trials and k must be non-negative.")
        if not 0.0 <= p <= 1.0:
            raise ValueError("p must lie in [0, 1].")
        return float(np.clip(stats.binom.sf(k - 1, n_trials, p), 0.0, 1.0))

    @staticmethod
    def _as_probability_matrix(X: FloatArray) -> FloatArray:
        array = as_float_array(X, name="X", ndim=(1, 2))
        matrix = array.reshape(1, -1) if array.ndim == 1 else array
        if np.any(matrix < 0.0) or np.any(matrix > 1.0):
            raise ValueError("Event probabilities must lie in [0, 1].")
        return matrix

    @staticmethod
    def _poisson_binomial_pmf(probabilities: FloatArray) -> FloatArray:
        n_rows, m_events = probabilities.shape
        pmf = np.zeros((n_rows, m_events + 1), dtype=np.float64)
        pmf[:, 0] = 1.0
        for j in range(m_events):
            p = probabilities[:, j : j + 1]
            shifted = np.zeros_like(pmf)
            shifted[:, 1:] = pmf[:, :-1]
            pmf = pmf * (1.0 - p) + shifted * p
        pmf = np.clip(pmf, 0.0, 1.0)
        return safe_array_divide(pmf, pmf.sum(axis=1, keepdims=True))

    def _tail(self, probabilities: FloatArray) -> FloatArray:
        k = self.config.k
        n_rows, m_events = probabilities.shape
        if k == 0:
            return np.ones(n_rows, dtype=np.float64)
        if k > m_events:
            return np.zeros(n_rows, dtype=np.float64)

        if self.config.method == "exact":
            out = np.empty(n_rows, dtype=np.float64)
            spread = probabilities.max(axis=1) - probabilities.min(axis=1)
            identical = spread <= self.config.identical_tolerance
            if np.any(identical):
                p_common = probabilities[identical].mean(axis=1)
                out[identical] = stats.binom.sf(k - 1, m_events, p_common)
            if np.any(~identical):
                pmf = self._poisson_binomial_pmf(probabilities[~identical])
                out[~identical] = pmf[:, k:].sum(axis=1)
        elif self.config.method == "poisson":
            out = stats.poisson.sf(k - 1, probabilities.sum(axis=1))
        else:
            mu = probabilities.sum(axis=1)
            sigma = np.sqrt((probabilities * (1.0 - probabilities)).sum(axis=1))
            shift = 0.5 if self.config.continuity_correction else 0.0
            z = safe_array_divide(k - shift - mu, sigma)
            out = stats.norm.sf(z)
            degenerate = sigma <= FLOAT_EPS
            out[degenerate] = (mu[degenerate] >= k).astype(np.float64)
        return np.clip(sanitize(out), 0.0, 1.0)
