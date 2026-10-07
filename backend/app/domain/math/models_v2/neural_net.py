"""Multi-Layer Perceptron (MLP) Classifier."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from sklearn.neural_network import MLPClassifier

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["NeuralNetConfig", "NeuralNetModel"]


class NeuralNetConfig(MathModelConfig):
    hidden_layer_sizes: tuple[int, ...] = Field(default=(100,))
    activation: str = Field(default="relu")
    alpha: float = Field(default=0.0001, ge=0.0)
    learning_rate_init: float = Field(default=0.001, gt=0.0)
    max_iter: int = Field(default=200, gt=0)
    early_stopping: bool = True


class NeuralNetModel(BaseMathModel):
    """Deep Neural Network classifier via scikit-learn."""

    config: NeuralNetConfig

    def __init__(self, config: NeuralNetConfig | None = None) -> None:
        super().__init__(resolve_config(config, NeuralNetConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_classification_target(y, length=features.shape[0], name="y")

        model = MLPClassifier(
            hidden_layer_sizes=self.config.hidden_layer_sizes,
            activation=self.config.activation,  # type: ignore
            alpha=self.config.alpha,
            learning_rate_init=self.config.learning_rate_init,
            max_iter=self.config.max_iter,
            early_stopping=self.config.early_stopping,
            random_state=self.config.random_state,
        )
        model.fit(features, target)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        model: MLPClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict(features))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        model: MLPClassifier = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict_proba(features))
