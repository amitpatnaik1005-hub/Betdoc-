"""Gradient boosting regression machine with bounded tree complexity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from sklearn.ensemble import GradientBoostingRegressor

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["GradientBoostingRegressionConfig", "GradientBoostingRegressionModel"]

Fraction = Annotated[float, Field(gt=0.0, le=1.0)]
PositiveInt = Annotated[int, Field(ge=1)]


class GradientBoostingRegressionConfig(MathModelConfig):
    loss: Literal["squared_error", "absolute_error", "huber", "quantile"] = "squared_error"
    learning_rate: float = Field(default=0.05, gt=0.0, le=1.0)
    n_estimators: int = Field(default=300, ge=1)
    max_depth: int = Field(default=3, ge=1, le=10)
    min_samples_split: int = Field(default=2, ge=2)
    min_samples_leaf: int = Field(default=1, ge=1)
    subsample: float = Field(default=1.0, gt=0.0, le=1.0)
    max_features: Literal["sqrt", "log2"] | Fraction | None = None
    quantile_alpha: float = Field(default=0.9, gt=0.0, lt=1.0)
    validation_fraction: float = Field(default=0.1, gt=0.0, lt=1.0)
    n_iter_no_change: PositiveInt | None = None
    tol: float = Field(default=1e-4, gt=0.0)
    rng_stream: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def _check_leaf(self) -> Self:
        if 2 * self.min_samples_leaf > self.min_samples_split and self.min_samples_split > 2:
            raise ValueError("min_samples_split must allow two leaves of min_samples_leaf.")
        return self


@dataclass(frozen=True, slots=True)
class _GBRState:
    estimator: GradientBoostingRegressor
    n_features: int
    feature_importances: FloatArray
    train_loss: FloatArray


class GradientBoostingRegressionModel(BaseMathModel):
    r"""Stage-wise functional gradient descent.

    .. math:: \hat y_i = F_0 + \sum_{m=1}^{M}\gamma_m h_m(x_i),\qquad
              r_{im} = -\left.\frac{\partial L(y_i, F)}{\partial F}\right|_{F_{m-1}},\qquad
              F_m = F_{m-1} + \nu\,\gamma_m h_m

    with depth-bounded CART learners :math:`h_m` (``max_depth`` in [1, 10]) and shrinkage
    :math:`\nu \in (0, 1]`. ``random_state`` is bound to ``rng_stream``.
    """

    config: GradientBoostingRegressionConfig

    def __init__(self, config: GradientBoostingRegressionConfig | None = None) -> None:
        super().__init__(resolve_config(config, GradientBoostingRegressionConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        if y is None:
            raise ValueError("y is required for gradient boosting.")
        features = as_feature_matrix(X, name="X")
        if features.shape[0] < cfg.min_samples_split:
            raise ValueError(f"X has {features.shape[0]} rows; min_samples_split={cfg.min_samples_split}.")
        target = as_vector(y, name="y", length=features.shape[0])
        estimator = GradientBoostingRegressor(
            loss=cfg.loss, learning_rate=cfg.learning_rate, n_estimators=cfg.n_estimators, max_depth=cfg.max_depth,
            min_samples_split=cfg.min_samples_split, min_samples_leaf=cfg.min_samples_leaf, subsample=cfg.subsample,
            max_features=cfg.max_features, alpha=cfg.quantile_alpha, validation_fraction=cfg.validation_fraction,
            n_iter_no_change=cfg.n_iter_no_change, tol=cfg.tol, random_state=cfg.rng_stream,
        ).fit(features, target)
        self._publish_state(
            _GBRState(
                estimator=estimator,
                n_features=features.shape[1],
                feature_importances=freeze(np.nan_to_num(np.asarray(estimator.feature_importances_, dtype=np.float64))),
                train_loss=freeze(check_finite_array(estimator.train_score_, name="train_loss")),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _GBRState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return sanitize(np.asarray(state.estimator.predict(features), dtype=np.float64))

    @property
    def feature_importances(self) -> FloatArray:
        return self._current_state().feature_importances
