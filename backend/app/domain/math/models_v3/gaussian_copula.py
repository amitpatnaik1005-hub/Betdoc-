"""Gaussian copula for joint probabilities of correlated events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import multivariate_normal, norm, rankdata

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["GaussianCopulaConfig", "GaussianCopulaModel"]


class GaussianCopulaConfig(MathModelConfig):
    rank_method: Literal["average"] = "average"
    eigenvalue_floor: float = Field(default=1e-8, gt=0.0)
    clip_epsilon: float = Field(default=1e-10, gt=0.0, lt=0.5)
    mvn_maxpts: int = Field(default=1_000_000, ge=1)
    mvn_abseps: float = Field(default=1e-6, gt=0.0)
    mvn_releps: float = Field(default=1e-6, gt=0.0)
    min_samples: int = Field(default=3, ge=3)
    rng_stream: int = Field(default=42, ge=0)


@dataclass(frozen=True, slots=True)
class _CopulaState:
    correlation: FloatArray
    dimension: int


class GaussianCopulaModel(BaseMathModel):
    r"""Gaussian copula :math:`C_\Sigma(u) = \Phi_\Sigma\bigl(\Phi^{-1}(u_1),\dots,\Phi^{-1}(u_d)\bigr)`.

    Pseudo-observations via average ranks :math:`U = \mathrm{rank}/(N+1)` (strictly inside (0,1)),
    :math:`Z = \Phi^{-1}(U)`, :math:`\Sigma = \mathrm{corr}(Z)`. PSD repair: eigen-decompose,
    clip :math:`\lambda_i \ge 10^{-8}`, reconstruct and rescale to unit diagonal.
    ``predict`` maps marginal probabilities ``(n, d)`` to :math:`P(U_1 \le u_1,\dots,U_d \le u_d)`.
    """

    config: GaussianCopulaConfig

    def __init__(self, config: GaussianCopulaConfig | None = None) -> None:
        super().__init__(resolve_config(config, GaussianCopulaConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        data = as_feature_matrix(X, name="X", min_samples=cfg.min_samples)
        n_obs, dim = data.shape
        if dim < 2:
            raise ValueError("Gaussian copula requires at least two dimensions.")
        ranks = np.column_stack([rankdata(data[:, j], method=cfg.rank_method) for j in range(dim)])
        U = ranks / (n_obs + 1.0)
        Z = norm.ppf(U)
        raw = check_finite_array(np.corrcoef(Z, rowvar=False), name="correlation (constant column?)")
        sym = 0.5 * (raw + raw.T)
        eigvals, eigvecs = np.linalg.eigh(sym)
        rebuilt = (eigvecs * np.clip(eigvals, cfg.eigenvalue_floor, None)) @ eigvecs.T
        d = np.sqrt(np.diag(rebuilt))
        corr = rebuilt / np.outer(d, d)
        np.fill_diagonal(corr, 1.0)
        self._publish_state(_CopulaState(freeze(0.5 * (corr + corr.T)), dim))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        cfg = self.config
        state: _CopulaState = self._current_state()
        u = as_feature_matrix(X, name="X", n_features=state.dimension)
        if np.any((u < 0.0) | (u > 1.0)):
            raise ValueError("Marginal probabilities must lie in [0, 1].")
        z = norm.ppf(np.clip(u, cfg.clip_epsilon, 1.0 - cfg.clip_epsilon))
        dist = multivariate_normal(
            mean=np.zeros(state.dimension), cov=state.correlation, seed=self._rng(cfg.rng_stream),
            maxpts=cfg.mvn_maxpts, abseps=cfg.mvn_abseps, releps=cfg.mvn_releps,
        )
        joint = np.atleast_1d(np.asarray(dist.cdf(z), dtype=np.float64)).reshape(u.shape[0])
        return np.clip(sanitize(joint), 0.0, 1.0)

    @property
    def correlation(self) -> FloatArray:
        return self._current_state().correlation
