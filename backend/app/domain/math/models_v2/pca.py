"""Principal Component Analysis (PCA) for dimensionality reduction."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from sklearn.decomposition import PCA

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["PCAConfig", "PCAModel"]


class PCAConfig(MathModelConfig):
    n_components: int | float | str | None = Field(default=None)
    whiten: bool = False


class PCAModel(BaseMathModel):
    """Linear dimensionality reduction via SVD."""

    config: PCAConfig

    def __init__(self, config: PCAConfig | None = None) -> None:
        super().__init__(resolve_config(config, PCAConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X", min_samples=2)
        model = PCA(
            n_components=self.config.n_components,
            whiten=self.config.whiten,
            random_state=self.config.random_state,
        )
        model.fit(features)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Transforms the data into the principal component space."""
        model: PCA = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.transform(features))

    def inverse_transform(self, X: FloatArray) -> FloatArray:
        model: PCA = self._current_state()
        features = as_feature_matrix(X, name="X")
        return sanitize(model.inverse_transform(features))
