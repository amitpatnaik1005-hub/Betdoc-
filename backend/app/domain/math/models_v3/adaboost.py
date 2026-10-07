"""Adaptive Boosting classifier over depth-limited CART stumps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from sklearn.ensemble import AdaBoostClassifier
from sklearn.tree import DecisionTreeClassifier

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_classification_target,
    as_feature_matrix,
    freeze,
    resolve_config,
)

__all__ = ["AdaBoostConfig", "AdaBoostModel"]


class AdaBoostConfig(MathModelConfig):
    n_estimators: int = Field(default=200, ge=1)
    learning_rate: float = Field(default=0.5, gt=0.0)
    algorithm: Literal["SAMME", "SAMME.R"] = "SAMME"
    base_max_depth: int = Field(default=1, ge=1)
    base_min_samples_leaf: int = Field(default=1, ge=1)


@dataclass(frozen=True, slots=True)
class _AdaState:
    estimator: AdaBoostClassifier
    classes: FloatArray
    n_features: int
    estimator_weights: FloatArray
    estimator_errors: FloatArray
    feature_importances: FloatArray


class AdaBoostModel(BaseMathModel):
    r"""Multi-class AdaBoost (SAMME).

    .. math:: \mathrm{err}_m = \frac{\sum_i w_i\,\mathbb{1}[y_i \ne h_m(x_i)]}{\sum_i w_i},\qquad
              \alpha_m = \nu\left[\log\frac{1-\mathrm{err}_m}{\mathrm{err}_m} + \log(K-1)\right]

    .. math:: w_i \leftarrow w_i\,\exp\!\bigl(\alpha_m\,\mathbb{1}[y_i \ne h_m(x_i)]\bigr),\qquad
              H(x) = \arg\max_k \sum_m \alpha_m\,\mathbb{1}[h_m(x) = k]

    ``SAMME.R`` is forwarded only when the installed scikit-learn still exposes the
    ``algorithm`` parameter (removed in scikit-learn >= 1.8, SAMME.R rejected from 1.6).
    """

    config: AdaBoostConfig

    def __init__(self, config: AdaBoostConfig | None = None) -> None:
        super().__init__(resolve_config(config, AdaBoostConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        features = as_feature_matrix(X, name="X", min_samples=2)
        target = as_classification_target(y, length=features.shape[0])
        params: dict[str, Any] = {
            "estimator": DecisionTreeClassifier(
                max_depth=cfg.base_max_depth,
                min_samples_leaf=cfg.base_min_samples_leaf,
                random_state=cfg.random_state,
            ),
            "n_estimators": cfg.n_estimators,
            "learning_rate": cfg.learning_rate,
            "random_state": cfg.random_state,
        }
        if "algorithm" in AdaBoostClassifier().get_params():
            params["algorithm"] = cfg.algorithm
        elif cfg.algorithm != "SAMME":
            raise ValueError("SAMME.R is unavailable in the installed scikit-learn; use 'SAMME'.")
        estimator = AdaBoostClassifier(**params).fit(features, target)
        n_fitted = len(estimator.estimators_)
        self._publish_state(
            _AdaState(
                estimator=estimator,
                classes=freeze(np.asarray(estimator.classes_, dtype=np.float64)),
                n_features=features.shape[1],
                estimator_weights=freeze(np.asarray(estimator.estimator_weights_[:n_fitted], dtype=np.float64)),
                estimator_errors=freeze(np.asarray(estimator.estimator_errors_[:n_fitted], dtype=np.float64)),
                feature_importances=freeze(np.nan_to_num(np.asarray(estimator.feature_importances_, dtype=np.float64))),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _AdaState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return np.asarray(state.estimator.predict(features), dtype=np.float64)

    def predict_proba(self, X: FloatArray) -> FloatArray:
        state: _AdaState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.n_features)
        return np.clip(np.asarray(state.estimator.predict_proba(features), dtype=np.float64), 0.0, 1.0)

    @property
    def feature_importances(self) -> FloatArray:
        return self._current_state().feature_importances
