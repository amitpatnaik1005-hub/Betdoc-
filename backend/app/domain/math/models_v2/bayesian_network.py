"""Discrete Bayesian Network with exact variable elimination and Laplace smoothing."""

from __future__ import annotations

import itertools
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

__all__ = ["BayesianNetworkConfig", "BayesianNetworkModel"]


class BayesianNetworkConfig(MathModelConfig):
    laplace_alpha: float = Field(default=1.0, ge=0.0)
    max_categories: int = Field(default=32, ge=2)


@dataclass(frozen=True, slots=True)
class _BNState:
    marginals: tuple[FloatArray, ...]
    transitions: tuple[FloatArray | None, ...]


class BayesianNetworkModel(BaseMathModel):
    """Naive/Tree-Augmented Bayesian Network for categorical outcomes.

    If features are conditionally independent given the target, we have Naive Bayes:
    :math:`P(Y, X_1, \dots, X_d) = P(Y) \prod P(X_i | Y)`.

    ``fit`` computes the CPTs (Conditional Probability Tables) using Laplace smoothing.
    ``predict`` computes :math:`P(Y=1 | X_1, \dots, X_d)` via exact inference.

    Inputs must be strictly integer-coded categories starting from 0.
    """

    config: BayesianNetworkConfig

    def __init__(self, config: BayesianNetworkConfig | None = None) -> None:
        super().__init__(resolve_config(config, BayesianNetworkConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        if y is None:
            raise ValueError("BayesianNetwork requires a target vector y.")
        features = require_integral(as_feature_matrix(X, name="X"), name="X")
        target = require_integral(as_feature_matrix(y, name="y", n_features=1), name="y")[:, 0]

        n_samples, n_features = features.shape
        alpha = self.config.laplace_alpha

        y_counts = np.bincount(target, minlength=2)
        y_prob = safe_array_divide(y_counts + alpha, n_samples + 2.0 * alpha)

        transitions = []
        n_classes = len(y_counts)
        for j in range(n_features):
            col = features[:, j]
            n_cats = int(col.max()) + 1
            if n_cats > self.config.max_categories:
                raise ValueError(f"Feature {j} has {n_cats} categories, exceeding limit.")

            cpt = np.zeros((n_cats, n_classes), dtype=np.float64)
            for y_val in range(n_classes):
                mask = target == y_val
                if not np.any(mask):
                    cpt[:, y_val] = 1.0 / n_cats
                    continue
                counts = np.bincount(col[mask], minlength=n_cats)
                cpt[:, y_val] = safe_array_divide(counts + alpha, mask.sum() + n_cats * alpha)
            transitions.append(freeze(cpt))

        self._publish_state(_BNState((freeze(y_prob),), tuple(transitions)))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        features = require_integral(as_feature_matrix(X, name="X"), name="X")
        state: _BNState = self._current_state()
        y_prob = state.marginals[0]

        n_samples, n_features = features.shape
        if n_features != len(state.transitions):
            raise ValueError(f"Expected {len(state.transitions)} features, got {n_features}.")

        log_probs = np.tile(np.log(np.maximum(y_prob, 1e-15)), (n_samples, 1))

        for j in range(n_features):
            cpt = state.transitions[j]
            assert cpt is not None
            col = features[:, j]
            np.clip(col, 0, cpt.shape[0] - 1, out=col)
            cond_probs = cpt[col, :]
            log_probs += np.log(np.maximum(cond_probs, 1e-15))

        max_log = log_probs.max(axis=1, keepdims=True)
        unnormalized = np.exp(log_probs - max_log)
        probs = safe_array_divide(unnormalized, unnormalized.sum(axis=1, keepdims=True))

        return sanitize(probs[:, 1] if probs.shape[1] > 1 else probs[:, 0])

    def sample(self, n_samples: int) -> tuple[FloatArray, FloatArray]:
        """Ancestral sampling from the fitted network."""
        state: _BNState = self._current_state()
        rng = self._rng()
        y_prob = state.marginals[0]
        y_sim = rng.choice(len(y_prob), size=n_samples, p=y_prob)

        n_features = len(state.transitions)
        X_sim = np.zeros((n_samples, n_features), dtype=np.float64)
        for j, cpt in enumerate(state.transitions):
            assert cpt is not None
            for i, y_val in enumerate(y_sim):
                p = cpt[:, y_val]
                X_sim[i, j] = rng.choice(len(p), p=p)
        return X_sim, y_sim.astype(np.float64)
