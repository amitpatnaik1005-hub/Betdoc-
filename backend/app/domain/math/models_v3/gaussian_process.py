"""Gaussian Process regression with a validated Constant x RBF + White kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    as_vector,
    check_finite_scalar,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["GaussianProcessConfig", "GaussianProcessModel"]

Bounds = tuple[float, float]


def _check_bounds(name: str, value: float, bounds: Bounds) -> None:
    low, high = bounds
    if not 0.0 < low < high:
        raise ValueError(f"{name}_bounds must satisfy 0 < low < high.")
    if not low <= value <= high:
        raise ValueError(f"{name} must lie inside {name}_bounds.")


class GaussianProcessConfig(MathModelConfig):
    length_scale: float = Field(default=1.0, gt=0.0)
    length_scale_bounds: Bounds = (1e-3, 1e3)
    constant_value: float = Field(default=1.0, gt=0.0)
    constant_value_bounds: Bounds = (1e-3, 1e3)
    noise_level: float = Field(default=1e-2, gt=0.0)
    noise_level_bounds: Bounds = (1e-8, 1e1)
    alpha: float = Field(default=1e-10, ge=0.0)
    n_restarts_optimizer: int = Field(default=3, ge=0)
    normalize_y: bool = True
    optimizer: Literal["fmin_l_bfgs_b"] | None = "fmin_l_bfgs_b"

    @model_validator(mode="after")
    def _validate_kernel(self) -> Self:
        _check_bounds("length_scale", self.length_scale, self.length_scale_bounds)
        _check_bounds("constant_value", self.constant_value, self.constant_value_bounds)
        _check_bounds("noise_level", self.noise_level, self.noise_level_bounds)
        return self


@dataclass(frozen=True, slots=True)
class _GPState:
    estimator: GaussianProcessRegressor
    n_features: int
    log_marginal_likelihood: float
    kernel_parameters: FloatArray


class GaussianProcessModel(BaseMathModel):
    r"""Bayesian non-parametric regression.

    .. math:: k(x, x') = c\,\exp\!\left(-\frac{\lVert x - x'\rVert^2}{2\ell^2}\right) + \sigma_n^2\,\delta_{x x'}

    .. math:: \mu_* = K_*^\top (K + \alpha I)^{-1} y,\qquad
              \Sigma_* = K_{**} - K_*^\top (K + \alpha I)^{-1} K_*

    Hyper-parameters maximise the log marginal likelihood

    .. math:: \log p(y \mid X, \theta) = -\tfrac12 y^\top K_y^{-1} y - \tfrac12 \log\lvert K_y\rvert - \tfrac n2 \log 2\pi

    with ``n_restarts_optimizer`` seeded restarts inside the configured bounds.
    """

    config: GaussianProcessConfig

    def __init__(self, config: GaussianProcessConfig | None = None) -> None:
        super().__init__(resolve_config(config, GaussianProcessConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        if y is None:
            raise ValueError("y is required for Gaussian Process regression.")
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_vector(y, name="y", length=features.shape[0])
        kernel = ConstantKernel(cfg.constant_value, cfg.constant_value_bounds) * RBF(
            cfg.length_scale, cfg.length_scale_bounds
        ) + WhiteKernel(cfg.noise_level, cfg.noise_level_bounds)
        estimator = GaussianProcessRegressor(
            kernel=kernel,
            alpha=cfg.alpha,
            optimizer=cfg.optimizer,
            n_restarts_optimizer=cfg.n_restarts_optimizer,
            normalize_y=cfg.normalize_y,
            random_state=cfg.random_state,
        ).fit(features, target)
        lml = check_finite_scalar(float(estimator.log_marginal_likelihood_value_), name="log_marginal_likelihood")
        self._publish_state(
            _GPState(
                estimator=estimator,
                n_features=features.shape[1],
                log_marginal_likelihood=lml,
                kernel_parameters=freeze(np.exp(np.asarray(estimator.kernel_.theta, dtype=np.float64))),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _GPState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return sanitize(np.asarray(state.estimator.predict(features), dtype=np.float64))

    def predict_with_std(self, X: FloatArray) -> tuple[FloatArray, FloatArray]:
        state: _GPState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        mean, std = state.estimator.predict(features, return_std=True)
        return sanitize(np.asarray(mean, dtype=np.float64)), np.maximum(sanitize(np.asarray(std, dtype=np.float64)), 0.0)

    @property
    def log_marginal_likelihood(self) -> float:
        return self._current_state().log_marginal_likelihood
