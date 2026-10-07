"""Gaussian and Student-t Copula models for multivariate dependence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from scipy import linalg, optimize, stats

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["CopulaConfig", "CopulaModel"]


class CopulaConfig(MathModelConfig):
    family: Literal["gaussian", "student_t"] = "gaussian"
    df: float = Field(default=4.0, gt=2.0, description="Degrees of freedom for Student-t.")
    estimate_df: bool = False
    regularization: float = Field(default=1e-6, ge=0.0)


@dataclass(frozen=True, slots=True)
class _CopulaState:
    correlation: FloatArray
    cholesky: FloatArray
    df: float | None


class CopulaModel(BaseMathModel):
    """Multivariate dependence modelling via Copulas.

    ``fit`` computes the empirical rank correlation (Spearman's rho) and derives the
    underlying Pearson correlation matrix via the exact Gaussian transformation:
    :math:`r = 2 \sin(\pi \rho / 6)`.

    ``predict`` simulates correlated uniform variables :math:`U \in [0,1]^d` using
    the fitted correlation structure.
    """

    config: CopulaConfig

    def __init__(self, config: CopulaConfig | None = None) -> None:
        super().__init__(resolve_config(config, CopulaConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        data = as_feature_matrix(X, name="X", min_samples=3)
        n_rows, n_cols = data.shape

        ranks = np.argsort(np.argsort(data, axis=0), axis=0)
        spearman = np.corrcoef(ranks, rowvar=False) if n_cols > 1 else np.eye(1)
        pearson = 2.0 * np.sin(np.pi / 6.0 * spearman)
        pearson.flat[:: n_cols + 1] = 1.0

        if self.config.regularization > 0.0:
            pearson = pearson + np.eye(n_cols) * self.config.regularization
            inv_diag = 1.0 / np.sqrt(np.diag(pearson))
            pearson = pearson * np.outer(inv_diag, inv_diag)

        df = None
        if self.config.family == "student_t" and self.config.estimate_df:
            u_data = (ranks + 0.5) / n_rows
            df = self._estimate_t_df(u_data, pearson)
        elif self.config.family == "student_t":
            df = self.config.df

        try:
            chol = linalg.cholesky(pearson, lower=True)
        except linalg.LinAlgError:
            eigvals, eigvecs = linalg.eigh(pearson)
            eigvals = np.maximum(eigvals, 1e-12)
            reconstructed = (eigvecs * eigvals) @ eigvecs.T
            inv_diag = 1.0 / np.sqrt(np.diag(reconstructed))
            pearson = reconstructed * np.outer(inv_diag, inv_diag)
            chol = linalg.cholesky(pearson, lower=True)

        self._publish_state(_CopulaState(freeze(pearson), freeze(chol), df))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Simulate ``n_samples`` rows of uniform dependencies.

        The input ``X`` is ignored for values; only its shape ``(n_samples, n_features)``
        is used. If ``X`` is a scalar integer, it is interpreted as ``n_samples``.
        """
        state: _CopulaState = self._current_state()
        try:
            shape_req = int(np.asarray(X).item())
            n_samples, n_features = shape_req, state.correlation.shape[0]
        except (ValueError, TypeError):
            matrix = as_feature_matrix(X, name="X")
            n_samples, n_features = matrix.shape

        if n_features != state.correlation.shape[0]:
            raise ValueError(f"Expected {state.correlation.shape[0]} features, got {n_features}.")

        rng = self._rng()
        z = rng.standard_normal((n_samples, n_features))
        correlated_z = z @ state.cholesky.T

        if self.config.family == "gaussian":
            return sanitize(stats.norm.cdf(correlated_z))

        df = state.df if state.df is not None else self.config.df
        chi2 = rng.chisquare(df, size=(n_samples, 1))
        correlated_t = correlated_z * np.sqrt(df / chi2)
        return sanitize(stats.t.cdf(correlated_t, df=df))

    def _estimate_t_df(self, u: FloatArray, corr: FloatArray) -> float:
        """Maximum Pseudo-Likelihood estimation for the degrees of freedom."""
        n_samples, n_features = u.shape
        inv_corr = linalg.inv(corr)
        sign, logdet = linalg.slogdet(corr)
        if sign <= 0:
            return self.config.df

        def neg_log_likelihood(df_val: float) -> float:
            df = df_val.item()
            t_quantiles = stats.t.ppf(u, df=df)
            t_quantiles = np.clip(t_quantiles, -10.0, 10.0)
            inner = np.sum(t_quantiles @ (inv_corr - np.eye(n_features)) * t_quantiles, axis=1)
            pdf_joint = stats.t.logpdf(inner, df=df) - 0.5 * logdet
            pdf_margins = np.sum(stats.t.logpdf(t_quantiles, df=df), axis=1)
            return -float(np.sum(pdf_joint - pdf_margins))

        res = optimize.minimize_scalar(neg_log_likelihood, bounds=(2.1, 100.0), method="bounded")
        return float(res.x) if res.success else self.config.df
