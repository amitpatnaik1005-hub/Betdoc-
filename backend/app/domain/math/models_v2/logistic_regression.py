"""Logistic Regression using L-BFGS-B optimization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy import optimize

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["LogisticRegressionConfig", "LogisticRegressionModel"]


class LogisticRegressionConfig(MathModelConfig):
    l2_penalty: float = Field(default=1.0, ge=0.0)
    fit_intercept: bool = True
    max_iter: int = Field(default=1000, gt=0)
    tolerance: float = Field(default=1e-4, gt=0.0)


@dataclass(frozen=True, slots=True)
class _LogisticState:
    weights: FloatArray
    intercept: float
    classes: FloatArray


class LogisticRegressionModel(BaseMathModel):
    """L2-regularized binary Logistic Regression implemented via scipy optimize."""

    config: LogisticRegressionConfig

    def __init__(self, config: LogisticRegressionConfig | None = None) -> None:
        super().__init__(resolve_config(config, LogisticRegressionConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X")
        n_samples, n_features = features.shape
        target = as_classification_target(y, length=n_samples, name="y")

        classes, y_idx = np.unique(target, return_inverse=True)
        if len(classes) != 2:
            raise ValueError("LogisticRegressionModel only supports binary classification.")

        # x0 = [intercept, w1, w2, ...]
        dim = n_features + 1 if self.config.fit_intercept else n_features
        x0 = np.zeros(dim, dtype=np.float64)

        def loss_and_grad(w_ext: FloatArray) -> tuple[float, FloatArray]:
            if self.config.fit_intercept:
                b = w_ext[0]
                w = w_ext[1:]
            else:
                b = 0.0
                w = w_ext

            z = features @ w + b
            # log-loss: sum_i -y_i * z_i + log(1 + exp(z_i))
            # stable log-sum-exp:
            loss = np.sum(np.maximum(z, 0) - y_idx * z + np.log1p(np.exp(-np.abs(z))))
            reg = 0.5 * self.config.l2_penalty * np.dot(w, w)

            p = 1.0 / (1.0 + np.exp(-z))
            err = p - y_idx

            grad = np.empty_like(w_ext)
            if self.config.fit_intercept:
                grad[0] = np.sum(err)
                grad[1:] = features.T @ err + self.config.l2_penalty * w
            else:
                grad[:] = features.T @ err + self.config.l2_penalty * w

            return float(loss + reg), grad

        res = optimize.minimize(
            loss_and_grad,
            x0,
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": self.config.max_iter, "ftol": self.config.tolerance},
        )

        w_opt = res.x
        if self.config.fit_intercept:
            intercept = float(w_opt[0])
            weights = w_opt[1:]
        else:
            intercept = 0.0
            weights = w_opt

        self._publish_state(_LogisticState(freeze(weights), intercept, freeze(classes)))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        state: _LogisticState = self._current_state()
        features = as_feature_matrix(X, name="X")
        if features.shape[1] != state.weights.shape[0]:
            raise ValueError(f"Expected {state.weights.shape[0]} features.")
        
        z = features @ state.weights + state.intercept
        p1 = 1.0 / (1.0 + np.exp(-z))
        p0 = 1.0 - p1
        return sanitize(np.column_stack([p0, p1]))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _LogisticState = self._current_state()
        proba = self.predict_proba(X)
        idx = np.argmax(proba, axis=1)
        return state.classes[idx]
