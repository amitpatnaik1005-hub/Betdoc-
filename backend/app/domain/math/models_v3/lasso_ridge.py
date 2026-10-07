"""Scale-invariant L1 (Lasso) / L2 (Ridge) regularised least squares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from sklearn.linear_model import Lasso, Ridge
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["LassoRidgeConfig", "LassoRidgeModel"]

PositiveInt = Annotated[int, Field(ge=1)]


class LassoRidgeConfig(MathModelConfig):
    penalty: Literal["l1", "l2"] = "l2"
    alpha: float = Field(default=1.0, gt=0.0)
    fit_intercept: bool = True
    max_iter: PositiveInt | None = 10_000
    tol: float = Field(default=1e-6, gt=0.0)
    positive: bool = False
    lasso_selection: Literal["cyclic", "random"] = "cyclic"
    ridge_solver: Literal["auto", "svd", "cholesky", "lsqr", "sparse_cg", "sag", "saga", "lbfgs"] = "auto"
    min_target_std: float = Field(default=0.0, ge=0.0)
    rng_stream: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def _check_solver(self) -> Self:
        if self.penalty == "l2" and self.positive and self.ridge_solver not in {"auto", "lbfgs"}:
            raise ValueError("Ridge with positive=True requires ridge_solver 'lbfgs' (or 'auto').")
        if self.penalty == "l1" and self.max_iter is None:
            raise ValueError("Lasso requires a finite max_iter.")
        return self


@dataclass(frozen=True, slots=True)
class _LRState:
    pipeline: Pipeline
    coefficients: FloatArray
    intercept: float
    n_features: int


class LassoRidgeModel(BaseMathModel):
    r"""Penalised least squares on standardised features :math:`z = (x - m)/s`.

    .. math:: \text{Lasso: } \min_w \frac{1}{2n}\lVert y - Zw - b\rVert_2^2 + \alpha\lVert w\rVert_1,\qquad
              \text{Ridge: } \min_w \lVert y - Zw - b\rVert_2^2 + \alpha\lVert w\rVert_2^2

    ``make_pipeline(StandardScaler(), estimator)`` makes the penalty scale invariant without
    mutating ``X``. Coefficients reported in original units: :math:`w = \tilde w/s`,
    :math:`b = \tilde b - w^\top m`. Constant targets raise ``NumericalStabilityError``.
    """

    config: LassoRidgeConfig

    def __init__(self, config: LassoRidgeConfig | None = None) -> None:
        super().__init__(resolve_config(config, LassoRidgeConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        if y is None:
            raise ValueError("y is required for regularised regression.")
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_vector(y, name="y", length=features.shape[0])
        if float(target.std()) <= cfg.min_target_std:
            raise NumericalStabilityError("y has zero variance; regression is undefined.")
        estimator: Lasso | Ridge
        if cfg.penalty == "l1":
            estimator = Lasso(
                alpha=cfg.alpha, fit_intercept=cfg.fit_intercept, max_iter=cfg.max_iter, tol=cfg.tol,
                positive=cfg.positive, selection=cfg.lasso_selection, random_state=cfg.rng_stream,
            )
        else:
            solver = "lbfgs" if cfg.positive and cfg.ridge_solver == "auto" else cfg.ridge_solver
            estimator = Ridge(
                alpha=cfg.alpha, fit_intercept=cfg.fit_intercept, max_iter=cfg.max_iter, tol=cfg.tol,
                positive=cfg.positive, solver=solver, random_state=cfg.rng_stream,
            )
        pipeline = make_pipeline(StandardScaler(), estimator).fit(features, target)
        scaler: StandardScaler = pipeline.steps[0][1]
        w = np.asarray(estimator.coef_, dtype=np.float64).ravel() / np.asarray(scaler.scale_, dtype=np.float64)
        b = float(np.asarray(estimator.intercept_, dtype=np.float64).ravel()[0]) if cfg.fit_intercept else 0.0
        b -= float(w @ np.asarray(scaler.mean_, dtype=np.float64))
        self._publish_state(
            _LRState(pipeline, freeze(check_finite_array(w, name="coefficients")), check_finite_scalar(b, name="intercept"), features.shape[1])
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _LRState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return sanitize(np.asarray(state.pipeline.predict(features), dtype=np.float64))

    @property
    def coefficients(self) -> FloatArray:
        return self._current_state().coefficients

    @property
    def intercept(self) -> float:
        return self._current_state().intercept
