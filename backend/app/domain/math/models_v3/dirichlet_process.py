"""Dirichlet Process Gaussian mixture via collapsed Gibbs sampling (CRP) with NIG conjugacy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.special import gammaln

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    freeze,
    resolve_config,
)

__all__ = ["DirichletProcessConfig", "DirichletProcessModel"]

PositiveFloat = Annotated[float, Field(gt=0.0)]


class DirichletProcessConfig(MathModelConfig):
    concentration: float = Field(default=1.0, gt=0.0)
    prior_mean: float | None = None
    prior_kappa: float = Field(default=0.01, gt=0.0)
    prior_shape: float = Field(default=2.0, gt=0.0)
    prior_scale: PositiveFloat | None = None
    n_iter: int = Field(default=100, ge=1)
    burn_in: int = Field(default=20, ge=0)
    standardize: bool = True
    min_std: float = Field(default=1e-12, gt=0.0)
    assign_new_clusters: bool = False
    new_cluster_label: float = -1.0
    rng_stream: int = Field(default=49, ge=0)

    @model_validator(mode="after")
    def _check_iterations(self) -> Self:
        if self.burn_in >= self.n_iter:
            raise ValueError("burn_in must be < n_iter.")
        return self


@dataclass(frozen=True, slots=True)
class _DPState:
    counts: FloatArray
    sums: FloatArray
    sumsq: FloatArray
    prior_mean: FloatArray
    prior_scale: FloatArray
    x_mean: FloatArray
    x_scale: FloatArray
    labels: FloatArray
    posterior_means: FloatArray


class DirichletProcessModel(BaseMathModel):
    r"""Infinite Gaussian mixture, :math:`G \sim \mathrm{DP}(\alpha, G_0)`, with independent
    Normal-Inverse-Gamma base measure per dimension: :math:`\sigma^2 \sim \mathrm{IG}(a_0, b_0)`,
    :math:`\mu\mid\sigma^2 \sim \mathcal N(m_0, \sigma^2/\kappa_0)`.

    CRP conditional (collapsed Gibbs, Neal's Algorithm 3):

    .. math:: P(z_i = k \mid z_{-i}, x) \propto \begin{cases} n_{k,-i}\; p(x_i \mid x_{k,-i}) & \text{existing } k \\
              \alpha\; p(x_i \mid G_0) & \text{new cluster}\end{cases}

    Closed-form posterior predictive (Student-t per dimension):

    .. math:: \kappa_n = \kappa_0 + n,\quad m_n = \frac{\kappa_0 m_0 + \sum x}{\kappa_n},\quad a_n = a_0 + \tfrac n2,\quad
              b_n = b_0 + \tfrac12\sum(x-\bar x)^2 + \frac{\kappa_0 n(\bar x - m_0)^2}{2\kappa_n}

    .. math:: p(x^\ast\mid\cdot) = t_{2a_n}\!\left(x^\ast;\ m_n,\ \frac{b_n(\kappa_n+1)}{a_n\kappa_n}\right)

    All sampling uses the injected seeded generator. ``predict`` returns MAP cluster labels.
    """

    config: DirichletProcessConfig

    def __init__(self, config: DirichletProcessConfig | None = None) -> None:
        super().__init__(resolve_config(config, DirichletProcessConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        data = as_feature_matrix(X, name="X", min_samples=2)
        n_obs, dim = data.shape
        x_mean = data.mean(axis=0) if cfg.standardize else np.zeros(dim)
        std = data.std(axis=0)
        x_scale = np.where(std > cfg.min_std, std, 1.0) if cfg.standardize else np.ones(dim)
        Z = (data - x_mean) / x_scale
        m0 = np.full(dim, cfg.prior_mean) if cfg.prior_mean is not None else Z.mean(axis=0)
        b0 = np.full(dim, cfg.prior_scale) if cfg.prior_scale is not None else np.maximum(Z.var(axis=0), cfg.min_std) * cfg.prior_shape
        rng = self._rng(cfg.rng_stream)

        labels = np.zeros(n_obs, dtype=np.int64)
        counts = np.zeros(n_obs, dtype=np.float64)
        sums = np.zeros((n_obs, dim), dtype=np.float64)
        sumsq = np.zeros((n_obs, dim), dtype=np.float64)
        counts[0], sums[0], sumsq[0] = n_obs, Z.sum(axis=0), (Z**2).sum(axis=0)
        n_clusters = 1
        log_alpha = math.log(cfg.concentration)
        zero_n, zero_d = np.zeros(1), np.zeros((1, dim))

        for _ in range(cfg.n_iter):
            for i in rng.permutation(n_obs):
                x, k = Z[i], labels[i]
                counts[k] -= 1.0
                sums[k] -= x
                sumsq[k] -= x * x
                if counts[k] <= 0.0:
                    last = n_clusters - 1
                    if k != last:
                        counts[k], sums[k], sumsq[k] = counts[last], sums[last], sumsq[last]
                        labels[labels == last] = k
                    counts[last], sums[last], sumsq[last] = 0.0, 0.0, 0.0
                    n_clusters -= 1
                log_existing = np.log(counts[:n_clusters]) + self._log_predictive(
                    x, counts[:n_clusters], sums[:n_clusters], sumsq[:n_clusters], m0, b0
                )
                log_new = log_alpha + self._log_predictive(x, zero_n, zero_d, zero_d, m0, b0)
                logits = np.append(log_existing, log_new)
                probs = np.exp(logits - logits.max())
                choice = int(rng.choice(n_clusters + 1, p=probs / probs.sum()))
                if choice == n_clusters:
                    n_clusters += 1
                labels[i] = choice
                counts[choice] += 1.0
                sums[choice] += x
                sumsq[choice] += x * x

        order = np.argsort(-counts[:n_clusters], kind="stable")
        remap = np.empty(n_clusters, dtype=np.int64)
        remap[order] = np.arange(n_clusters)
        c, s, ss = counts[:n_clusters][order], sums[:n_clusters][order], sumsq[:n_clusters][order]
        post_means = (cfg.prior_kappa * m0 + s) / (cfg.prior_kappa + c[:, None])
        self._publish_state(
            _DPState(
                counts=freeze(c), sums=freeze(s), sumsq=freeze(ss),
                prior_mean=freeze(m0), prior_scale=freeze(b0),
                x_mean=freeze(x_mean), x_scale=freeze(x_scale),
                labels=freeze(remap[labels].astype(np.float64)),
                posterior_means=freeze(post_means * x_scale + x_mean),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _DPState = self._current_state()
        log_post = self._log_assignment(X, state)
        labels = np.argmax(log_post, axis=1).astype(np.float64)
        if self.config.assign_new_clusters:
            labels[labels == state.counts.size] = self.config.new_cluster_label
        return labels

    def predict_proba(self, X: FloatArray) -> FloatArray:
        log_post = self._log_assignment(X, self._current_state())
        probs = np.exp(log_post - log_post.max(axis=1, keepdims=True))
        return probs / probs.sum(axis=1, keepdims=True)

    @property
    def n_clusters(self) -> int:
        return int(self._current_state().counts.size)

    @property
    def cluster_means(self) -> FloatArray:
        return self._current_state().posterior_means

    def _log_assignment(self, X: FloatArray, state: _DPState) -> FloatArray:
        data = as_feature_matrix(X, name="X", n_features=state.x_mean.size)
        Z = (data - state.x_mean) / state.x_scale
        dim = Z.shape[1]
        rows = []
        for x in Z:
            lp = np.log(state.counts) + self._log_predictive(x, state.counts, state.sums, state.sumsq, state.prior_mean, state.prior_scale)
            if self.config.assign_new_clusters:
                lp = np.append(lp, math.log(self.config.concentration) + self._log_predictive(
                    x, np.zeros(1), np.zeros((1, dim)), np.zeros((1, dim)), state.prior_mean, state.prior_scale
                ))
            rows.append(lp)
        return np.vstack(rows)

    def _log_predictive(self, x: FloatArray, n: FloatArray, s: FloatArray, ss: FloatArray, m0: FloatArray, b0: FloatArray) -> FloatArray:
        k0, a0 = self.config.prior_kappa, self.config.prior_shape
        nn = n[:, None]
        xbar = np.where(nn > 0.0, s / np.maximum(nn, 1.0), 0.0)
        kn = k0 + nn
        mn = (k0 * m0 + s) / kn
        an = a0 + 0.5 * nn
        scatter = np.maximum(ss - nn * xbar**2, 0.0)
        bn = b0 + 0.5 * scatter + k0 * nn * (xbar - m0) ** 2 / (2.0 * kn)
        nu = 2.0 * an
        scale2 = bn * (kn + 1.0) / (an * kn)
        z = (x[None, :] - mn) ** 2 / (nu * scale2)
        logp = gammaln(0.5 * (nu + 1.0)) - gammaln(0.5 * nu) - 0.5 * np.log(nu * math.pi * scale2) - 0.5 * (nu + 1.0) * np.log1p(z)
        return logp.sum(axis=1)
