"""Dynamic Time Warping with Sakoe-Chiba band over NaN-padded sequences."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["DynamicTimeWarpingConfig", "DynamicTimeWarpingModel"]


class DynamicTimeWarpingConfig(MathModelConfig):
    window: int = Field(default=10, ge=0)
    adaptive_window: bool = True
    local_cost: Literal["absolute", "squared"] = "absolute"
    normalize: Literal["none", "sum_length"] = "none"
    infeasible_distance: float = Field(default=1e12, gt=0.0)
    max_sequence_length: int = Field(default=10_000, ge=1)


@dataclass(frozen=True, slots=True)
class _DTWState:
    templates: tuple[FloatArray, ...]


class DynamicTimeWarpingModel(BaseMathModel):
    r"""DTW distance between sequences :math:`a_{1:n}` and :math:`b_{1:m}`.

    .. math:: D_{0,0} = 0,\quad D_{i,j} = \infty \text{ otherwise};\qquad
              D_{i,j} = c(a_i, b_j) + \min\{D_{i-1,j-1},\ D_{i-1,j},\ D_{i,j-1}\}\quad \text{for } |i - j| \le w

    Sakoe-Chiba band :math:`w` (widened to :math:`|n - m|` if ``adaptive_window``). NaN entries
    (padding) are masked out before traversal. Local cost :math:`|a-b|` or :math:`(a-b)^2` (the latter
    returns :math:`\sqrt{D_{n,m}}`). ``fit`` stores templates (rows of a NaN-padded matrix);
    ``predict`` returns the ``(n_queries, n_templates)`` distance matrix.
    """

    config: DynamicTimeWarpingConfig

    def __init__(self, config: DynamicTimeWarpingConfig | None = None) -> None:
        super().__init__(resolve_config(config, DynamicTimeWarpingConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(_DTWState(tuple(freeze(s) for s in self._as_masked_sequences(X))))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _DTWState = self._current_state()
        queries = self._as_masked_sequences(X)
        out = np.empty((len(queries), len(state.templates)), dtype=np.float64)
        for i, q in enumerate(queries):
            for j, t in enumerate(state.templates):
                out[i, j] = self.distance(q, t)
        return sanitize(out)

    def distance(self, a: FloatArray, b: FloatArray) -> float:
        cfg = self.config
        a = np.asarray(a, dtype=np.float64)[~np.isnan(np.asarray(a, dtype=np.float64))]
        b = np.asarray(b, dtype=np.float64)[~np.isnan(np.asarray(b, dtype=np.float64))]
        n, m = a.size, b.size
        if n == 0 or m == 0:
            raise ValueError("Sequences must contain at least one non-NaN value.")
        w = max(cfg.window, abs(n - m)) if cfg.adaptive_window else cfg.window
        D = np.full((n + 1, m + 1), np.inf, dtype=np.float64)
        D[0, 0] = 0.0
        for i in range(1, n + 1):
            j_lo, j_hi = max(1, i - w), min(m, i + w)
            if j_lo > j_hi:
                continue
            diff = a[i - 1] - b[j_lo - 1 : j_hi]
            cost = np.abs(diff) if cfg.local_cost == "absolute" else diff * diff
            diag_up = np.minimum(D[i - 1, j_lo - 1 : j_hi], D[i - 1, j_lo : j_hi + 1])
            row = D[i]
            for k, j in enumerate(range(j_lo, j_hi + 1)):
                row[j] = cost[k] + min(diag_up[k], row[j - 1])
        total = float(D[n, m])
        if not math.isfinite(total):
            return cfg.infeasible_distance
        if cfg.local_cost == "squared":
            total = math.sqrt(total)
        return total / (n + m) if cfg.normalize == "sum_length" else total

    def _as_masked_sequences(self, X: FloatArray) -> list[FloatArray]:
        try:
            arr = np.array(X, dtype=np.float64, copy=True)
        except (TypeError, ValueError) as exc:
            raise TypeError("X must be convertible to a float64 array.") from exc
        if arr.ndim not in (1, 2) or arr.size == 0:
            raise ValueError("X must be a non-empty 1-D sequence or 2-D NaN-padded matrix.")
        if np.any(np.isinf(arr)):
            raise NumericalStabilityError("X contains infinite values (only NaN padding is permitted).")
        rows = arr.reshape(1, -1) if arr.ndim == 1 else arr
        if rows.shape[1] > self.config.max_sequence_length:
            raise ValueError("Sequence length exceeds max_sequence_length.")
        sequences = [row[~np.isnan(row)] for row in rows]
        if any(s.size == 0 for s in sequences):
            raise ValueError("Every row must contain at least one non-NaN value.")
        return sequences
