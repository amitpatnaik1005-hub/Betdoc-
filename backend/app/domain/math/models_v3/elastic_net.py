"""Elastic-net regularised linear regression."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from sklearn.linear_model import ElasticNet
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

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

__all__ = ["ElasticNetConfig", "ElasticNetModel"]


class ElasticNetConfig(MathModelConfig):
    alpha: float = Field(default=1.0, gt=0.0)
    l1_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    max_iter: int = Field(default=10_000, ge=1)
    tol: float = Field(default=1e-6, gt=0.0)
    fit_intercept: bool = True
    selection: Literal["cyclic", "random"] = "cyclic"
    positive: bool = False
    standardize: bool = True


@dataclass(frozen=True, slots=True)
class _ENState:
    pipeline: Pipeline | ElasticNet
    coefficients: FloatArray
    intercept: float
    n_features: int
    n_iter: int
    dual_gap: float


class ElasticNetModel(BaseMathModel):
    r"""Convex combination of L1 and L2 penalties (coordinate descent).

    .. math:: \min_{w, b}\ \frac{1}{2n}\lVert y - Xw - b\rVert_2^2
              + \alpha\rho\lVert w\rVert_1 + \frac{\alpha(1-\rho)}{2}\lVert w\rVert_2^2

    With standardisation :math:`z = (x - m)/s`, coefficients are mapped back to
    original units: :math:`w = \tilde w / s`, :math:`b = \tilde b - w^\top m`.
    """

    config: ElasticNetConfig

    def __init__(self, config: ElasticNetConfig | None = None) -> None:
        super().__init__(resolve_config(config, ElasticNetConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        if y is None:
            raise ValueError("y is required for Elastic-Net regression.")
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_vector(y, name="y", length=features.shape[0])
        estimator = ElasticNet(
            alpha=cfg.alpha,
            l1_ratio=cfg.l1_ratio,
            max_iter=cfg.max_iter,
            tol=cfg.tol,
            fit_intercept=cfg.fit_intercept,
            selection=cfg.selection,
            positive=cfg.positive,
            random_state=cfg.random_state,
        )
        pipeline: Pipeline | ElasticNet = make_pipeline(StandardScaler(), estimator) if cfg.standardize else estimator
        pipeline.fit(features, target)
        w = np.asarray(estimator.coef_, dtype=np.float64).ravel()
        b = float(np.asarray(estimator.intercept_, dtype=np.float64).ravel()[0]) if cfg.fit_intercept else 0.0
        if isinstance(pipeline, Pipeline):
            scaler: StandardScaler = pipeline.steps[0][1]
            w = w / np.asarray(scaler.scale_, dtype=np.float64)
            b = b - float(w @ np.asarray(scaler.mean_, dtype=np.float64))
        self._publish_state(
            _ENState(
                pipeline=pipeline,
                coefficients=freeze(sanitize(w)),
                intercept=check_finite_scalar(b, name="intercept"),
                n_features=features.shape[1],
                n_iter=int(np.max(np.atleast_1d(estimator.n_iter_))),
                dual_gap=float(np.max(np.atleast_1d(estimator.dual_gap_))),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _ENState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return sanitize(np.asarray(state.pipeline.predict(features), dtype=np.float64))

    @property
    def coefficients(self) -> FloatArray:
        return self._current_state().coefficients

    @property
    def intercept(self) -> float:
        return self._current_state().intercept
