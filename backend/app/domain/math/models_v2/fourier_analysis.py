"""Discrete Fourier Transform (DFT) for seasonality detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_vector,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["FourierAnalysisConfig", "FourierAnalysisModel"]


class FourierAnalysisConfig(MathModelConfig):
    top_k_frequencies: int = Field(default=3, gt=0)
    detrend: bool = True
    windowing: bool = True


@dataclass(frozen=True, slots=True)
class _FourierState:
    frequencies: FloatArray
    amplitudes: FloatArray
    phases: FloatArray
    mean: float
    trend: float


class FourierAnalysisModel(BaseMathModel):
    """Extracts dominant cyclic components from a time series via FFT.

    ``fit`` computes the power spectrum, finds the top K frequencies, and extracts
    their amplitudes and phases.
    ``predict`` reconstructs the signal or forecasts it forward in time using
    the sum of the dominant sine waves.
    """

    config: FourierAnalysisConfig

    def __init__(self, config: FourierAnalysisConfig | None = None) -> None:
        super().__init__(resolve_config(config, FourierAnalysisConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        series = as_vector(X, name="X")
        n = len(series)
        if n < 3:
            raise ValueError("At least 3 observations required for Fourier analysis.")

        time_idx = np.arange(n, dtype=np.float64)
        mean_val = float(np.mean(series))
        trend = 0.0

        if self.config.detrend:
            poly = np.polyfit(time_idx, series, 1)
            trend, mean_val = float(poly[0]), float(poly[1])
            series = series - (trend * time_idx + mean_val)
        else:
            series = series - mean_val

        if self.config.windowing:
            window = np.hanning(n)
            series = series * window

        fft_vals = np.fft.rfft(series)
        freqs = np.fft.rfftfreq(n)

        magnitudes = np.abs(fft_vals)
        magnitudes[0] = 0.0  # Ignore DC component since we demeaned/detrended

        top_indices = np.argsort(magnitudes)[-self.config.top_k_frequencies :][::-1]

        dom_freqs = freqs[top_indices]
        dom_amps = magnitudes[top_indices] / (n / 2.0)
        dom_phases = np.angle(fft_vals[top_indices])

        if self.config.windowing:
            dom_amps *= 2.0  # Hanning window energy correction factor approx

        self._publish_state(
            _FourierState(freeze(dom_freqs), freeze(dom_amps), freeze(dom_phases), mean_val, trend)
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Evaluate the fitted Fourier series at the given integer time indices."""
        t = as_vector(X, name="X")
        state: _FourierState = self._current_state()

        out = np.full_like(t, state.mean)
        out += state.trend * t

        for freq, amp, phase in zip(state.frequencies, state.amplitudes, state.phases):
            out += amp * np.cos(2.0 * np.pi * freq * t + phase)

        return sanitize(out)
