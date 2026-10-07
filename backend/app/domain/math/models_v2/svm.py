"""Support Vector Machine (SVM) Classification wrapper."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from sklearn.svm import SVC

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["SVMConfig", "SVMModel"]


class SVMConfig(MathModelConfig):
    C: float = Field(default=1.0, gt=0.0)
    kernel: Literal["linear", "poly", "rbf", "sigmoid"] = "rbf"
    degree: int = Field(default=3, ge=1)
    gamma: Literal["scale", "auto"] | float = "scale"
    probability: bool = True
    max_iter: int = -1


class SVMModel(BaseMathModel):
    """Support Vector Classification via libsvm (scikit-learn)."""

    config: SVMConfig

    def __init__(self, config: SVMConfig | None = None) -> None:
        super().__init__(resolve_config(config, SVMConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X")
        target = as_classification_target(y, length=features.shape[0], name="y")

        model = SVC(
            C=self.config.C,
            kernel=self.config.kernel,
            degree=self.config.degree,
            gamma=self.config.gamma,
            probability=self.config.probability,
            max_iter=self.config.max_iter,
            random_state=self.config.random_state,
        )
        model.fit(features, target)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        model: SVC = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict(features))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        model: SVC = self._current_state()
        if not model.probability:
            raise ValueError("predict_proba requires probability=True in config.")
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict_proba(features))
