"""Information Theory metrics: Entropy, Mutual Information, and KL Divergence."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    as_vector,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["InformationTheoryConfig", "InformationTheoryModel"]


class InformationTheoryConfig(MathModelConfig):
    bins: int = Field(default=10, gt=1, description="Number of bins for continuous data.")
    base: float = Field(default=2.0, gt=0.0, description="Logarithm base (2=bits, e=nats).")
    laplace_alpha: float = Field(default=1e-5, ge=0.0)


class InformationTheoryModel(BaseMathModel):
    """Stateless computation of empirical information-theoretic quantities.

    If inputs are float arrays, they are uniformly binned to compute discrete
    probabilities. ``fit`` is a no-op.
    """

    config: InformationTheoryConfig

    def __init__(self, config: InformationTheoryConfig | None = None) -> None:
        super().__init__(resolve_config(config, InformationTheoryConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(True)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        raise NotImplementedError("InformationTheoryModel provides analytic methods, not predict().")

    def entropy(self, X: FloatArray) -> float:
        """Shannon entropy H(X)."""
        x_vec = as_vector(X, name="X")
        p = self._compute_pmf(x_vec)
        return self._entropy_from_pmf(p)

    def mutual_information(self, X: FloatArray, y: FloatArray) -> float:
        """Mutual Information I(X; Y)."""
        x_vec = as_vector(X, name="X")
        y_vec = as_vector(y, name="y", length=len(x_vec))
        p_xy = self._compute_joint_pmf(x_vec, y_vec)
        p_x = p_xy.sum(axis=1)
        p_y = p_xy.sum(axis=0)

        h_xy = self._entropy_from_pmf(p_xy.ravel())
        h_x = self._entropy_from_pmf(p_x)
        h_y = self._entropy_from_pmf(p_y)

        mi = h_x + h_y - h_xy
        return max(float(mi), 0.0)

    def kl_divergence(self, p_samples: FloatArray, q_samples: FloatArray) -> float:
        """Kullback-Leibler divergence D_{KL}(P || Q) estimated from samples."""
        p_vec = as_vector(p_samples, name="p_samples")
        q_vec = as_vector(q_samples, name="q_samples")

        global_min = min(p_vec.min(), q_vec.min())
        global_max = max(p_vec.max(), q_vec.max())
        bins = np.linspace(global_min, global_max, self.config.bins + 1)

        p_counts, _ = np.histogram(p_vec, bins=bins)
        q_counts, _ = np.histogram(q_vec, bins=bins)

        alpha = self.config.laplace_alpha
        p_prob = safe_array_divide(p_counts + alpha, len(p_vec) + len(bins) * alpha)
        q_prob = safe_array_divide(q_counts + alpha, len(q_vec) + len(bins) * alpha)

        mask = (p_prob > 0) & (q_prob > 0)
        div = np.sum(p_prob[mask] * np.log(p_prob[mask] / q_prob[mask]))
        return max(float(div / np.log(self.config.base)), 0.0)

    def conditional_entropy(self, X: FloatArray, y: FloatArray) -> float:
        """Conditional Entropy H(X | Y)."""
        return max(self.entropy(X) - self.mutual_information(X, y), 0.0)

    def _compute_pmf(self, x: FloatArray) -> FloatArray:
        counts, _ = np.histogram(x, bins=self.config.bins)
        alpha = self.config.laplace_alpha
        smoothed = counts + alpha
        return safe_array_divide(smoothed, smoothed.sum())

    def _compute_joint_pmf(self, x: FloatArray, y: FloatArray) -> FloatArray:
        hist, _, _ = np.histogram2d(x, y, bins=self.config.bins)
        alpha = self.config.laplace_alpha
        smoothed = hist + alpha
        return safe_array_divide(smoothed, smoothed.sum())

    def _entropy_from_pmf(self, pmf: FloatArray) -> float:
        mask = pmf > 0.0
        e = -np.sum(pmf[mask] * np.log(pmf[mask]))
        return float(e / np.log(self.config.base))
