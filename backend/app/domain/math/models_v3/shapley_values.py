"""Shapley attribution of ensemble signals: exact enumeration or antithetic Monte Carlo."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from scipy.special import gammaln

from app.domain.math.models_v2.base import (
    FLOAT_TINY,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    as_vector,
    check_finite_scalar,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["ShapleyConfig", "ShapleyValuesModel"]


class ShapleyConfig(MathModelConfig):
    method: Literal["auto", "exact", "monte_carlo"] = "auto"
    exact_max_players: int = Field(default=12, ge=1, le=20)
    n_permutations: int = Field(default=256, ge=1)
    antithetic: bool = True
    metric: Literal["neg_mse", "correlation", "hit_rate"] = "neg_mse"
    empty_prediction: Literal["zero", "target_mean"] = "zero"
    weight_floor: float = Field(default=0.0, ge=0.0)
    rng_stream: int = Field(default=46, ge=0)


@dataclass(frozen=True, slots=True)
class _ShapleyState:
    shapley_values: FloatArray
    ensemble_weights: FloatArray
    value_full: float
    value_empty: float
    method_used: str
    n_evaluations: int


class ShapleyValuesModel(BaseMathModel):
    r"""Cooperative-game attribution over :math:`F` ensemble signals.

    Coalition value :math:`v(S) = m\bigl(y, \tfrac{1}{|S|}\sum_{j\in S} x_j\bigr)` for metric :math:`m`.

    Exact (:math:`O(2^F)`):

    .. math:: \phi_i = \sum_{S \subseteq N\setminus\{i\}} \frac{|S|!\,(F-|S|-1)!}{F!}\bigl[v(S\cup\{i\}) - v(S)\bigr]

    Monte Carlo permutation estimator with antithetic pairs: for each permutation :math:`\pi`
    the reversed permutation is also evaluated, so each predecessor coalition :math:`S`
    is paired with its complement :math:`N\setminus(S\cup\{i\})`, which reduces variance.

    Efficiency: :math:`\sum_i\phi_i = v(N) - v(\varnothing)`. ``predict`` returns the Shapley-weighted
    ensemble :math:`X w`, where :math:`w \propto \max(\phi, \text{floor})`.
    """

    config: ShapleyConfig

    def __init__(self, config: ShapleyConfig | None = None) -> None:
        super().__init__(resolve_config(config, ShapleyConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        signals = as_feature_matrix(X, name="X", min_samples=2)
        if y is None:
            raise ValueError("y is required to evaluate coalition values.")
        target = as_vector(y, name="y", length=signals.shape[0])
        n_players = signals.shape[1]
        baseline = np.zeros_like(target) if cfg.empty_prediction == "zero" else np.full_like(target, target.mean())
        cache: dict[int, float] = {}

        def value(mask: int) -> float:
            if mask in cache:
                return cache[mask]
            cols = [j for j in range(n_players) if (mask >> j) & 1]
            pred = signals[:, cols].mean(axis=1) if cols else baseline
            cache[mask] = self._metric(target, pred)
            return cache[mask]

        use_exact = cfg.method == "exact" or (cfg.method == "auto" and n_players <= cfg.exact_max_players)
        if use_exact:
            if n_players > cfg.exact_max_players:
                raise ValueError("Exact Shapley requested beyond exact_max_players.")
            phi = self._exact(n_players, value)
            method_used = "exact"
        else:
            phi = self._monte_carlo(n_players, value)
            method_used = "monte_carlo_antithetic" if cfg.antithetic else "monte_carlo"

        full_mask = (1 << n_players) - 1
        positive = np.maximum(phi, cfg.weight_floor)
        total = float(positive.sum())
        weights = positive / total if total > FLOAT_TINY else np.full(n_players, 1.0 / n_players)
        self._publish_state(
            _ShapleyState(
                shapley_values=freeze(sanitize(phi)),
                ensemble_weights=freeze(weights),
                value_full=check_finite_scalar(value(full_mask), name="value_full"),
                value_empty=check_finite_scalar(value(0), name="value_empty"),
                method_used=method_used,
                n_evaluations=len(cache),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _ShapleyState = self._current_state()
        signals = as_feature_matrix(X, name="X", n_features=state.ensemble_weights.size)
        return sanitize(signals @ state.ensemble_weights)

    @property
    def shapley_values(self) -> FloatArray:
        return self._current_state().shapley_values

    def _exact(self, n_players: int, value: Any) -> FloatArray:
        masks = np.arange(1 << n_players, dtype=np.int64)
        sizes = np.zeros(masks.size, dtype=np.int64)
        for j in range(n_players):
            sizes += (masks >> j) & 1
        values = np.array([value(int(m)) for m in masks], dtype=np.float64)
        s = np.arange(n_players, dtype=np.float64)
        log_w = gammaln(s + 1.0) + gammaln(n_players - s) - gammaln(n_players + 1.0)
        weights = np.exp(log_w)
        phi = np.zeros(n_players, dtype=np.float64)
        for i in range(n_players):
            without = masks[((masks >> i) & 1) == 0]
            phi[i] = float(weights[sizes[without]] @ (values[without | (1 << i)] - values[without]))
        return phi

    def _monte_carlo(self, n_players: int, value: Any) -> FloatArray:
        cfg = self.config
        rng = self._rng(cfg.rng_stream)
        n_draws = math.ceil(cfg.n_permutations / 2) if cfg.antithetic else cfg.n_permutations
        phi = np.zeros(n_players, dtype=np.float64)
        count = 0
        for _ in range(n_draws):
            perm = rng.permutation(n_players)
            for order in ((perm, perm[::-1]) if cfg.antithetic else (perm,)):
                mask, previous = 0, value(0)
                for j in order:
                    mask |= 1 << int(j)
                    current = value(mask)
                    phi[j] += current - previous
                    previous = current
                count += 1
        return phi / count

    def _metric(self, target: FloatArray, pred: FloatArray) -> float:
        metric = self.config.metric
        if metric == "neg_mse":
            return -float(np.mean((pred - target) ** 2))
        if metric == "hit_rate":
            return float(np.mean(np.sign(pred) == np.sign(target)))
        sp, st = float(pred.std()), float(target.std())
        if sp <= FLOAT_TINY or st <= FLOAT_TINY:
            return 0.0
        return float(np.mean((pred - pred.mean()) * (target - target.mean())) / (sp * st))
