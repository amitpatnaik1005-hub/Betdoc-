"""Exponentially Weighted Moving Averages (EWMA) and MACD filters."""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    resolve_config,
    sanitize,
)

__all__ = ["MovingAverageConfig", "MovingAverageModel"]


class MovingAverageConfig(MathModelConfig):
    span: float = Field(default=10.0, gt=0.0)
    adjust: bool = True
    macd_fast: float = Field(default=12.0, gt=0.0)
    macd_slow: float = Field(default=26.0, gt=0.0)
    macd_signal: float = Field(default=9.0, gt=0.0)


class MovingAverageModel(BaseMathModel):
    """Stateless EWMA and MACD filters for 1-D time series.

    .. math:: y_t = (1-\alpha) y_{t-1} + \alpha x_t

    where :math:`\alpha = 2 / (\mathrm{span} + 1)`.

    ``fit`` is a no-op. ``predict`` runs the filter over the sequential input matrix.
    """

    config: MovingAverageConfig

    def __init__(self, config: MovingAverageConfig | None = None) -> None:
        super().__init__(resolve_config(config, MovingAverageConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(True)

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Compute the EWMA for each column in ``X``."""
        matrix = as_feature_matrix(X, name="X")
        alpha = 2.0 / (self.config.span + 1.0)
        return self._ewma(matrix, alpha, self.config.adjust)

    def macd(self, X: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray]:
        """Return ``(MACD_line, Signal_line, Histogram)``."""
        matrix = as_feature_matrix(X, name="X")
        fast_alpha = 2.0 / (self.config.macd_fast + 1.0)
        slow_alpha = 2.0 / (self.config.macd_slow + 1.0)
        sig_alpha = 2.0 / (self.config.macd_signal + 1.0)

        fast_ewma = self._ewma(matrix, fast_alpha, self.config.adjust)
        slow_ewma = self._ewma(matrix, slow_alpha, self.config.adjust)
        macd_line = fast_ewma - slow_ewma
        signal_line = self._ewma(macd_line, sig_alpha, self.config.adjust)
        hist = macd_line - signal_line
        return macd_line, signal_line, hist

    @staticmethod
    def _ewma(matrix: FloatArray, alpha: float, adjust: bool) -> FloatArray:
        n_rows, n_cols = matrix.shape
        out = np.empty_like(matrix)
        if n_rows == 0:
            return out

        out[0] = matrix[0]
        if adjust:
            w = 1.0
            sum_w = 1.0
            for t in range(1, n_rows):
                w *= 1.0 - alpha
                sum_w += w
                out[t] = ((out[t - 1] * (sum_w - w)) + (matrix[t] * w)) / sum_w
        else:
            for t in range(1, n_rows):
                out[t] = out[t - 1] * (1.0 - alpha) + matrix[t] * alpha

        return sanitize(out)
