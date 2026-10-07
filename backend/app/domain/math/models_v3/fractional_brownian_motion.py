"""Fractional Brownian motion via Davies-Harte circulant embedding with Cholesky fallback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy.linalg import cholesky, toeplitz

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_float_array,
    as_vector,
    check_finite_scalar,
    resolve_config,
    sanitize,
)

__all__ = ["FractionalBrownianMotionConfig", "FractionalBrownianMotionModel"]


def _positive_int_kwarg(kwargs: dict[str, Any], name: str, default: int) -> int:
    value = kwargs.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


class FractionalBrownianMotionConfig(MathModelConfig):
    hurst: float = Field(default=0.7, gt=0.0, lt=1.0)
    sigma: float = Field(default=1.0, gt=0.0)
    horizon: float = Field(default=1.0, gt=0.0)
    n_steps: int = Field(default=256, ge=2)
    n_paths: int = Field(default=100, ge=1)
    estimate_parameters: bool = True
    sample_dt: float = Field(default=1.0, gt=0.0)
    max_variogram_lag: int = Field(default=20, ge=2)
    hurst_clip: float = Field(default=1e-3, gt=0.0, lt=0.5)
    eigenvalue_tolerance: float = Field(default=1e-10, ge=0.0)
    cholesky_jitter: float = Field(default=1e-12, gt=0.0)
    max_elements: int = Field(default=50_000_000, ge=1)
    rng_stream: int = Field(default=47, ge=0)


@dataclass(frozen=True, slots=True)
class _FBMState:
    hurst: float
    sigma: float
    estimated: bool


class FractionalBrownianMotionModel(BaseMathModel):
    r"""Fractional Brownian motion :math:`B_H(t)` with :math:`\mathrm{Cov}(B_H(t), B_H(s)) = \tfrac12(t^{2H} + s^{2H} - |t-s|^{2H})`.

    Exact fGn autocovariance:

    .. math:: \gamma(k) = \tfrac12\bigl(|k+1|^{2H} - 2|k|^{2H} + |k-1|^{2H}\bigr)

    Davies-Harte: embed :math:`\gamma` into the circulant first row
    :math:`c = [\gamma_0,\dots,\gamma_n,\gamma_{n-1},\dots,\gamma_1]` of size :math:`m = 2n`,
    compute eigenvalues :math:`\lambda = \mathrm{FFT}(c)`, build Hermitian coefficients
    :math:`a_0 = \sqrt{\lambda_0/m}Z_0`, :math:`a_n = \sqrt{\lambda_n/m}Z_n`,
    :math:`a_k = \sqrt{\lambda_k/(2m)}(Z_k + iZ'_k)`, :math:`a_{m-k} = \bar a_k`; then
    :math:`\mathrm{fGn} = \Re\,\mathrm{FFT}(a)_{0:n}`. If :math:`\min\lambda < 0`, it falls back to exact
    Cholesky of :math:`\Gamma_{ij} = \gamma(|i-j|)`.

    ``fit`` estimates :math:`H` from the variogram slope :math:`\log\mathbb E|x_{t+k}-x_t|^2 = 2H\log k + c`.
    ``predict(X)`` returns :math:`x_0 + \sigma B_H` paths with shape ``(n_series, n_paths, n_steps)``.
    """

    config: FractionalBrownianMotionConfig

    def __init__(self, config: FractionalBrownianMotionConfig | None = None) -> None:
        super().__init__(resolve_config(config, FractionalBrownianMotionConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        series = as_vector(X, name="X")
        if not cfg.estimate_parameters:
            self._publish_state(_FBMState(cfg.hurst, cfg.sigma, False))
            return
        max_lag = min(cfg.max_variogram_lag, series.size // 2)
        if max_lag < 2:
            raise ValueError("Series too short for variogram-based Hurst estimation.")
        lags = np.arange(1, max_lag + 1)
        vario = np.array([np.mean((series[k:] - series[:-k]) ** 2) for k in lags], dtype=np.float64)
        if np.any(vario <= 0.0):
            raise NumericalStabilityError("Variogram contains non-positive values (constant series?).")
        slope, intercept = np.polyfit(np.log(lags), np.log(vario), 1)
        hurst = float(np.clip(slope / 2.0, cfg.hurst_clip, 1.0 - cfg.hurst_clip))
        sigma = math.sqrt(float(vario[0])) / cfg.sample_dt**hurst
        self._publish_state(_FBMState(check_finite_scalar(hurst, name="hurst"), check_finite_scalar(sigma, name="sigma"), True))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        cfg = self.config
        state: _FBMState = self._current_state()
        n_steps = _positive_int_kwarg(kwargs, "n_steps", cfg.n_steps)
        n_paths = _positive_int_kwarg(kwargs, "n_paths", cfg.n_paths)
        x0 = as_float_array(X, name="X", ndim=(0, 1)).ravel()
        if x0.size * n_paths * n_steps > cfg.max_elements:
            raise ValueError("Requested simulation exceeds max_elements.")
        rng = self._rng(cfg.rng_stream)
        fgn = self.fractional_gaussian_noise(rng, x0.size * n_paths, n_steps, state.hurst)
        dt = cfg.horizon / n_steps
        paths = state.sigma * dt**state.hurst * np.cumsum(fgn, axis=1)
        return sanitize(x0[:, None, None] + paths.reshape(x0.size, n_paths, n_steps))

    def fractional_gaussian_noise(self, rng: np.random.Generator, count: int, n: int, hurst: float) -> FloatArray:
        k = np.arange(n + 1, dtype=np.float64)
        two_h = 2.0 * hurst
        gamma = 0.5 * (np.abs(k + 1.0) ** two_h - 2.0 * np.abs(k) ** two_h + np.abs(k - 1.0) ** two_h)
        row = np.concatenate([gamma, gamma[n - 1 : 0 : -1]])
        eigenvalues = np.real(np.fft.fft(row))
        scale = max(1.0, float(np.abs(eigenvalues).max()))
        if float(eigenvalues.min()) < -self.config.eigenvalue_tolerance * scale:
            return self._cholesky_fgn(rng, count, gamma[:n])
        lam = np.clip(eigenvalues, 0.0, None)
        m = 2 * n
        z = rng.standard_normal((count, m))
        a = np.zeros((count, m), dtype=np.complex128)
        a[:, 0] = math.sqrt(lam[0] / m) * z[:, 0]
        a[:, n] = math.sqrt(lam[n] / m) * z[:, n]
        idx = np.arange(1, n)
        a[:, idx] = np.sqrt(lam[idx] / (2.0 * m)) * (z[:, idx] + 1j * z[:, n + idx])
        a[:, m - idx] = np.conj(a[:, idx])
        return np.real(np.fft.fft(a, axis=1))[:, :n]

    def _cholesky_fgn(self, rng: np.random.Generator, count: int, gamma: FloatArray) -> FloatArray:
        cov = toeplitz(gamma) + self.config.cholesky_jitter * np.eye(gamma.size)
        try:
            lower = cholesky(cov, lower=True)
        except np.linalg.LinAlgError as exc:
            raise NumericalStabilityError("fGn covariance is not positive definite.") from exc
        return rng.standard_normal((count, gamma.size)) @ lower.T

    @property
    def hurst(self) -> float:
        return self._current_state().hurst
