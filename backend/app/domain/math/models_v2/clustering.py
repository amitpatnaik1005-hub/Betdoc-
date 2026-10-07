"""K-Means Clustering and Gaussian Mixture Models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["ClusteringConfig", "ClusteringModel"]


class ClusteringConfig(MathModelConfig):
    algorithm: Literal["kmeans", "gmm"] = "kmeans"
    n_clusters: int = Field(default=8, gt=0)
    max_iter: int = Field(default=300, gt=0)


class ClusteringModel(BaseMathModel):
    """Unsupervised grouping of market regimes or similar entities."""

    config: ClusteringConfig

    def __init__(self, config: ClusteringConfig | None = None) -> None:
        super().__init__(resolve_config(config, ClusteringConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        features = as_feature_matrix(X, name="X", min_samples=self.config.n_clusters)

        if self.config.algorithm == "kmeans":
            model = KMeans(
                n_clusters=self.config.n_clusters,
                max_iter=self.config.max_iter,
                random_state=self.config.random_state,
                n_init="auto",
            )
        else:
            model = GaussianMixture(
                n_components=self.config.n_clusters,
                max_iter=self.config.max_iter,
                random_state=self.config.random_state,
            )

        model.fit(features)
        self._publish_state(model)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Returns the cluster index for each sample."""
        model = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict(features))

    def predict_proba(self, X: FloatArray) -> FloatArray:
        """Returns the posterior probabilities (GMM only)."""
        if self.config.algorithm != "gmm":
            raise ValueError("predict_proba is only supported for GMM clustering.")
        model: GaussianMixture = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=model.n_features_in_)
        return sanitize(model.predict_proba(features))
