"""Gaussian point-spread model with dynamic (time and context) variance scaling."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator
from scipy import stats

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    as_float_array,
    as_vector,
    check_finite_scalar,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["NormalDistributionConfig", "NormalDistributionModel"]


class NormalDistributionConfig(MathModelConfig):
    sigma_prior: float = Field(default=13.5, gt=0.0, description="Prior margin std-dev (points).")
    min_sigma: float = Field(default=1e-6, gt=0.0)
    prior_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    time_exponent: float = Field(default=0.5, ge=0.0, description="0.5 = Brownian scaling.")
    ddof: int = Field(default=1, ge=0)
    estimate_bias: bool = True

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min_sigma > self.sigma_prior:
            raise ValueError("min_sigma must not exceed sigma_prior.")
        return self


@dataclass(frozen=True, slots=True)
class _NormalState:
    bias: float
    sigma: float
    n_observations: int


class NormalDistributionModel(BaseMathModel):
    r"""Point-spread cover probabilities under a Gaussian margin model.

    .. math:: f(x) = \frac{1}{\sigma_t\sqrt{2\pi}} \exp\!\left(-\frac{(x-\mu)^2}{2\sigma_t^2}\right),
              \qquad z = \frac{x-\mu}{\sigma_t}

    Dynamic variance scaling with remaining-time fraction :math:`\tau \in [0,1]`,
    exponent :math:`\gamma` and context multiplier :math:`m > 0`:

    .. math:: \sigma_t = \max\!\left(\sigma\,\sqrt{m}\,\tau^{\gamma},\ \sigma_{\min}\right)

    Shrinkage of the fitted variance towards the prior with weight :math:`w`:

    .. math:: \sigma^2 = w\,\sigma_0^2 + (1-w)\,\hat s^2

    Cover probability: :math:`P(M > L) = 1 - \Phi\left((L - \mu)/\sigma_t\right)`.

    ``predict`` input columns: ``[expected_margin, line, (time_fraction), (variance_multiplier)]``.
    If the model is unfitted, the prior :math:`\sigma_0` and zero bias are used.
    """

    config: NormalDistributionConfig

    def __init__(self, config: NormalDistributionConfig | None = None) -> None:
        super().__init__(resolve_config(config, NormalDistributionConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        if y is None:
            residuals = as_float_array(X, name="X", ndim=(1, 2)).ravel()
        else:
            predicted = as_feature_matrix(X, name="X")[:, 0]
            actual = as_vector(y, name="y", length=predicted.shape[0])
            residuals = actual - predicted
        n_obs = residuals.size
        if n_obs <= self.config.ddof:
            raise ValueError("Number of residuals must exceed ddof.")
        bias = float(residuals.mean()) if self.config.estimate_bias else 0.0
        centred = residuals - bias
        sample_var = float(centred @ centred) / (n_obs - self.config.ddof)
        w = self.config.prior_weight
        blended = w * self.config.sigma_prior**2 + (1.0 - w) * sample_var
        sigma = max(math.sqrt(max(blended, 0.0)), self.config.min_sigma)
        self._publish_state(
            _NormalState(check_finite_scalar(bias, name="bias"), check_finite_scalar(sigma, name="sigma"), n_obs)
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        mu, line, sigma_t = self.margin_distribution(X)
        return np.clip(sanitize(stats.norm.sf(line, loc=mu, scale=sigma_t)), 0.0, 1.0)

    def margin_distribution(self, X: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray]:
        """Return ``(mu, line, sigma_t)`` per row after bias and variance scaling."""
        matrix = as_feature_matrix(X, name="X")
        if matrix.shape[1] not in (2, 3, 4):
            raise ValueError("X must have 2 to 4 columns: margin, line, [time_fraction], [variance_mult].")
        state: _NormalState | None = self._optional_state()
        bias = state.bias if state is not None else 0.0
        sigma = state.sigma if state is not None else self.config.sigma_prior
        n_rows = matrix.shape[0]
        fraction = matrix[:, 2] if matrix.shape[1] >= 3 else np.ones(n_rows)
        multiplier = matrix[:, 3] if matrix.shape[1] == 4 else np.ones(n_rows)
        if np.any((fraction < 0.0) | (fraction > 1.0)):
            raise ValueError("time_fraction must lie in [0, 1].")
        if np.any(multiplier <= 0.0):
            raise ValueError("variance_multiplier must be strictly positive.")
        sigma_t = np.maximum(sigma * np.sqrt(multiplier) * fraction**self.config.time_exponent, self.config.min_sigma)
        return matrix[:, 0] + bias, matrix[:, 1], sigma_t

    def pdf(self, x: FloatArray, loc: FloatArray, scale: FloatArray) -> FloatArray:
        return sanitize(stats.norm.pdf(x, loc=loc, scale=self._validate_scale(scale)))

    def cdf(self, x: FloatArray, loc: FloatArray, scale: FloatArray) -> FloatArray:
        return np.clip(sanitize(stats.norm.cdf(x, loc=loc, scale=self._validate_scale(scale))), 0.0, 1.0)

    def z_score(self, x: FloatArray, loc: FloatArray, scale: FloatArray) -> FloatArray:
        return safe_array_divide(np.asarray(x, dtype=np.float64) - loc, self._validate_scale(scale))

    def _validate_scale(self, scale: FloatArray) -> FloatArray:
        arr = as_float_array(scale, name="scale")
        return np.maximum(arr, self.config.min_sigma)
