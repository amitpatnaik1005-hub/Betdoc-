"""Random Forest Classifier wrapping scikit-learn."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from sklearn.ensemble import RandomForestClassifier

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["RandomForestConfig", "RandomForestModel"]


class RandomForestConfig(MathModelConfig):
    n_estimators: int = Field(default=100, gt=0)
    max_depth: int | None = Field(default=None, gt=0)
    min_samples_split: int = Field(default=2, gt=1)
    n_jobs: int = Field(default=1)


class RandomForestModel(BaseMathModel):
    """Random Forest ensemble wrapper for robust non-linear classification."""

    config: RandomForestConfig

    def __init__(self, config: RandomForestConfig | None = None) -> None:
        super().__init__(resolve_config(config, RandomForestConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X")
        target = as_classification_target(y, length=features.shape[0], name="y")

        model = RandomForestClassifier(
            n_estimators=self.config.n_estimators,
            max_depth=self.config.max_depth,
            min_samples_split=self.config.min_samples_split,
            random_state=self.config.random_state,
            n_jobs=self.config.n_jobs,
        )
        model.fit(features, target)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        model: RandomForestClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict(features))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        model: RandomForestClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict_proba(features))
