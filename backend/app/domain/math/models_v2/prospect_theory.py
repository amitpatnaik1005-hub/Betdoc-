"""Cumulative Prospect Theory (CPT) for modelling irrational market pricing."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["ProspectTheoryConfig", "ProspectTheoryModel"]


class ProspectTheoryConfig(MathModelConfig):
    alpha: float = Field(default=0.88, gt=0.0, le=1.0, description="Value exponent for gains.")
    beta: float = Field(default=0.88, gt=0.0, le=1.0, description="Value exponent for losses.")
    lam: float = Field(default=2.25, ge=1.0, description="Loss aversion multiplier.")
    gamma: float = Field(default=0.61, gt=0.0, le=1.0, description="Probability weighting (gains).")
    delta: float = Field(default=0.69, gt=0.0, le=1.0, description="Probability weighting (losses).")


class ProspectTheoryModel(BaseMathModel):
    """Tversky & Kahneman's Cumulative Prospect Theory (1992).

    Transforms raw financial outcomes and objective probabilities into subjective
    utility values to model retail market pricing anomalies (e.g. longshot bias).
    """

    config: ProspectTheoryConfig

    def __init__(self, config: ProspectTheoryConfig | None = None) -> None:
        super().__init__(resolve_config(config, ProspectTheoryConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(True)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Compute the subjective utility for a matrix of ``[outcomes, probabilities]``."""
        matrix = as_feature_matrix(X, name="X", n_features=2)
        outcomes = matrix[:, 0]
        probs = matrix[:, 1]
        if np.any((probs < 0.0) | (probs > 1.0)):
            raise ValueError("Probabilities must lie in [0, 1].")

        v = self.value_function(outcomes)
        w = self.probability_weighting(probs, outcomes)
        return sanitize(v * w)

    def value_function(self, outcomes: FloatArray) -> FloatArray:
        """V(x) = x^alpha if x >= 0 else -lam * (-x)^beta"""
        x = np.asarray(outcomes, dtype=np.float64)
        v = np.empty_like(x)
        gains = x >= 0
        losses = ~gains
        v[gains] = np.power(x[gains], self.config.alpha)
        v[losses] = -self.config.lam * np.power(-x[losses], self.config.beta)
        return sanitize(v)

    def probability_weighting(self, probs: FloatArray, outcomes: FloatArray) -> FloatArray:
        """w(p) = p^c / (p^c + (1-p)^c)^(1/c)"""
        p = np.asarray(probs, dtype=np.float64)
        x = np.asarray(outcomes, dtype=np.float64)
        w = np.empty_like(p)

        gains = x >= 0
        p_g = p[gains]
        if len(p_g) > 0:
            c = self.config.gamma
            denom = np.power(np.power(p_g, c) + np.power(1.0 - p_g, c), 1.0 / c)
            w[gains] = np.power(p_g, c) / denom

        losses = ~gains
        p_l = p[losses]
        if len(p_l) > 0:
            c = self.config.delta
            denom = np.power(np.power(p_l, c) + np.power(1.0 - p_l, c), 1.0 / c)
            w[losses] = np.power(p_l, c) / denom

        return sanitize(w)
