"""Kernel density estimation with robust Silverman bandwidth."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from sklearn.neighbors import KernelDensity

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["KernelDensityConfig", "KernelDensityModel"]

PositiveFloat = Annotated[float, Field(gt=0.0)]


class KernelDensityConfig(MathModelConfig):
    bandwidth: Literal["auto"] | PositiveFloat = "auto"
    kernel: Literal["gaussian", "tophat", "epanechnikov", "exponential", "linear", "cosine"] = "gaussian"
    algorithm: Literal["auto", "kd_tree", "ball_tree"] = "auto"
    silverman_factor: float = Field(default=1.06, gt=0.0)
    iqr_divisor: float = Field(default=1.34, gt=0.0)
    iqr_quantiles: tuple[float, float] = (25.0, 75.0)
    bandwidth_exponent: float = Field(default=-0.2, lt=0.0)
    bandwidth_aggregation: Literal["min", "mean", "median"] = "median"
    min_bandwidth: float = Field(default=1e-8, gt=0.0)
    ddof: int = Field(default=1, ge=0)
    atol: float = Field(default=0.0, ge=0.0)
    rtol: float = Field(default=0.0, ge=0.0)
    leaf_size: int = Field(default=40, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = self.iqr_quantiles
        if not 0.0 <= lo < hi <= 100.0:
            raise ValueError("iqr_quantiles must satisfy 0 <= low < high <= 100.")
        return self


@dataclass(frozen=True, slots=True)
class _KDEState:
    estimator: KernelDensity
    bandwidth: float
    n_features: int


class KernelDensityModel(BaseMathModel):
    r"""Non-parametric density :math:`\hat f_h(x) = \frac{1}{n h^d}\sum_{i=1}^{n} K\!\left(\frac{x - x_i}{h}\right)`.

    Robust Silverman rule per feature (aggregated across features):

    .. math:: h = 1.06\cdot\min\!\left(\hat\sigma,\ \frac{\mathrm{IQR}}{1.34}\right) n^{-1/5}

    The rule falls back to :math:`\hat\sigma` when IQR = 0 and is floored at
    :math:`h \ge 10^{-8}`. ``predict`` returns densities ``exp(score_samples(X))``.
    """

    config: KernelDensityConfig

    def __init__(self, config: KernelDensityConfig | None = None) -> None:
        super().__init__(resolve_config(config, KernelDensityConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        arr = as_float_array(X, name="X", ndim=(1, 2))
        data = arr.reshape(-1, 1) if arr.ndim == 1 else arr
        if data.shape[0] <= cfg.ddof:
            raise ValueError("Number of samples must exceed ddof.")
        bandwidth = self._silverman(data) if cfg.bandwidth == "auto" else float(cfg.bandwidth)
        bandwidth = max(check_finite_scalar(bandwidth, name="bandwidth"), cfg.min_bandwidth)
        estimator = KernelDensity(
            bandwidth=bandwidth, kernel=cfg.kernel, algorithm=cfg.algorithm,
            atol=cfg.atol, rtol=cfg.rtol, leaf_size=cfg.leaf_size,
        ).fit(data)
        self._publish_state(_KDEState(estimator, bandwidth, data.shape[1]))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _KDEState = self._current_state()
        arr = as_float_array(X, name="X", ndim=(1, 2))
        data = arr.reshape(-1, state.n_features) if arr.ndim == 1 else arr
        if data.shape[1] != state.n_features:
            raise ValueError(f"X must have {state.n_features} features.")
        return np.maximum(sanitize(np.exp(state.estimator.score_samples(data))), 0.0)

    def log_density(self, X: FloatArray) -> FloatArray:
        state: _KDEState = self._current_state()
        data = as_feature_matrix(X, name="X", n_features=state.n_features)
        return sanitize(state.estimator.score_samples(data))

    @property
    def bandwidth(self) -> float:
        return self._current_state().bandwidth

    def _silverman(self, data: FloatArray) -> float:
        cfg = self.config
        n = data.shape[0]
        sigma = data.std(axis=0, ddof=cfg.ddof)
        q_lo, q_hi = np.percentile(data, cfg.iqr_quantiles, axis=0)
        robust = (q_hi - q_lo) / cfg.iqr_divisor
        spread = np.where(robust > 0.0, np.minimum(sigma, robust), sigma)
        per_feature = cfg.silverman_factor * spread * n**cfg.bandwidth_exponent
        reducer = {"min": np.min, "mean": np.mean, "median": np.median}[cfg.bandwidth_aggregation]
        h = float(reducer(per_feature))
        return h if math.isfinite(h) and h > 0.0 else cfg.min_bandwidth
