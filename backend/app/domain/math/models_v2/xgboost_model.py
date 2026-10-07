"""Gradient Boosting Classification (Histogram-based XGBoost equivalent)."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from sklearn.ensemble import HistGradientBoostingClassifier

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["GradientBoostingConfig", "GradientBoostingModel"]


class GradientBoostingConfig(MathModelConfig):
    learning_rate: float = Field(default=0.1, gt=0.0)
    max_iter: int = Field(default=100, gt=0)
    max_depth: int | None = Field(default=None, gt=0)
    l2_regularization: float = Field(default=0.0, ge=0.0)
    early_stopping: bool = True


class GradientBoostingModel(BaseMathModel):
    """Fast Histogram-based Gradient Boosting for classification.

    Native scikit-learn alternative to XGBoost / LightGBM, offering native
    NaN support and fast execution.
    """

    config: GradientBoostingConfig

    def __init__(self, config: GradientBoostingConfig | None = None) -> None:
        super().__init__(resolve_config(config, GradientBoostingConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_classification_target(y, length=features.shape[0], name="y")

        model = HistGradientBoostingClassifier(
            learning_rate=self.config.learning_rate,
            max_iter=self.config.max_iter,
            max_depth=self.config.max_depth,
            l2_regularization=self.config.l2_regularization,
            early_stopping=self.config.early_stopping,
            random_state=self.config.random_state,
        )
        model.fit(features, target)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        model: HistGradientBoostingClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict(features))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        model: HistGradientBoostingClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict_proba(features))
