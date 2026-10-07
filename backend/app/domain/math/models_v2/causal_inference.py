"""Instrumental Variables and Inverse Probability Weighting (IPW) for Causal Inference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy import linalg

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    freeze,
    resolve_config,
    safe_array_divide,
    sanitize,
)
from app.domain.math.models_v2.logistic_regression import (
    LogisticRegressionConfig,
    LogisticRegressionModel,
)

__all__ = ["CausalInferenceConfig", "CausalInferenceModel"]


class CausalInferenceConfig(MathModelConfig):
    method: str = Field(default="ipw", description="Either 'ipw' or 'iv'.")
    trim_propensity: float = Field(default=0.01, ge=0.0, lt=0.5)
    iv_regularization: float = Field(default=1e-5, ge=0.0)


@dataclass(frozen=True, slots=True)
class _CausalState:
    ate: float
    propensity_model: Any | None = None
    iv_coefficients: FloatArray | None = None


class CausalInferenceModel(BaseMathModel):
    """Causal inference via Inverse Probability Weighting (IPW) or 2SLS (IV)."""

    config: CausalInferenceConfig

    def __init__(self, config: CausalInferenceConfig | None = None) -> None:
        super().__init__(resolve_config(config, CausalInferenceConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        if y is None:
            raise ValueError("Target outcome y is required.")
        outcome = as_feature_matrix(y, name="y", n_features=1)[:, 0]

        if self.config.method == "ipw":
            self._fit_ipw(X, outcome, kwargs.get("treatment"))
        elif self.config.method == "iv":
            self._fit_iv(X, outcome, kwargs.get("treatment"), kwargs.get("instruments"))
        else:
            raise ValueError(f"Unknown method {self.config.method}")

    def _fit_ipw(self, X: FloatArray, outcome: FloatArray, treatment: Any) -> None:
        if treatment is None:
            raise ValueError("treatment array is required for IPW.")
        T = as_feature_matrix(treatment, name="treatment", n_features=1)[:, 0]
        covariates = as_feature_matrix(X, name="X")

        lr_cfg = LogisticRegressionConfig(random_state=self.config.random_state)
        propensity_model = LogisticRegressionModel(lr_cfg)
        propensity_model.fit(covariates, T)

        probs = propensity_model.predict_proba(covariates)[:, 1]
        trim = self.config.trim_propensity
        probs = np.clip(probs, trim, 1.0 - trim)

        weights = np.where(T == 1.0, 1.0 / probs, 1.0 / (1.0 - probs))
        ate = float(np.mean(outcome[T == 1.0] * weights[T == 1.0]) - np.mean(outcome[T == 0.0] * weights[T == 0.0]))

        self._publish_state(_CausalState(ate=ate, propensity_model=propensity_model))

    def _fit_iv(self, X: FloatArray, outcome: FloatArray, treatment: Any, instruments: Any) -> None:
        if treatment is None or instruments is None:
            raise ValueError("treatment and instruments arrays required for IV.")
        T = as_feature_matrix(treatment, name="treatment", n_features=1)
        Z = as_feature_matrix(instruments, name="instruments")
        covariates = as_feature_matrix(X, name="X")

        X_stage1 = np.hstack([Z, covariates])
        X_stage1 = np.hstack([np.ones((X_stage1.shape[0], 1)), X_stage1])
        beta1 = self._ols(X_stage1, T)
        T_hat = X_stage1 @ beta1

        X_stage2 = np.hstack([T_hat, covariates])
        X_stage2 = np.hstack([np.ones((X_stage2.shape[0], 1)), X_stage2])
        beta2 = self._ols(X_stage2, outcome.reshape(-1, 1)).ravel()

        self._publish_state(_CausalState(ate=float(beta2[1]), iv_coefficients=freeze(beta2)))

    def _ols(self, X_mat: FloatArray, y_vec: FloatArray) -> FloatArray:
        reg = self.config.iv_regularization
        XTX = X_mat.T @ X_mat + np.eye(X_mat.shape[1]) * reg
        XTY = X_mat.T @ y_vec
        return linalg.solve(XTX, XTY, assume_a="pos")

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Returns the Average Treatment Effect (ATE) broadcasted to ``X.shape[0]``."""
        state: _CausalState = self._current_state()
        n_rows = np.asarray(X).shape[0] if np.asarray(X).ndim > 0 else 1
        return np.full(n_rows, state.ate, dtype=np.float64)
