"""Black-Scholes-Merton pricing and Greeks with continuous dividend yield."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import norm

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["BlackScholesConfig", "BlackScholesModel"]


class BlackScholesConfig(MathModelConfig):
    option_type: Literal["call", "put"] = "call"
    limit_epsilon: float = Field(default=1e-8, gt=0.0)
    vega_scale: float = Field(default=1.0, gt=0.0)
    theta_scale: float = Field(default=1.0, gt=0.0)
    rho_scale: float = Field(default=1.0, gt=0.0)


@dataclass(frozen=True, slots=True)
class _BSState:
    option_type: str


class BlackScholesModel(BaseMathModel):
    r"""European option pricing under GBM with dividend yield :math:`q`.

    .. math:: d_1 = \frac{\ln(S/K) + (r - q + \sigma^2/2)T}{\sigma\sqrt T},\qquad d_2 = d_1 - \sigma\sqrt T

    .. math:: C = Se^{-qT}\Phi(d_1) - Ke^{-rT}\Phi(d_2),\qquad P = Ke^{-rT}\Phi(-d_2) - Se^{-qT}\Phi(-d_1)

    Greeks: :math:`\Delta_C = e^{-qT}\Phi(d_1)`, :math:`\Delta_P = -e^{-qT}\Phi(-d_1)`,
    :math:`\Gamma = e^{-qT}\phi(d_1)/(S\sigma\sqrt T)`, :math:`\mathcal V = Se^{-qT}\phi(d_1)\sqrt T`,

    .. math:: \Theta_C = -\frac{S\sigma e^{-qT}\phi(d_1)}{2\sqrt T} - rKe^{-rT}\Phi(d_2) + qSe^{-qT}\Phi(d_1)

    .. math:: \Theta_P = -\frac{S\sigma e^{-qT}\phi(d_1)}{2\sqrt T} + rKe^{-rT}\Phi(-d_2) - qSe^{-qT}\Phi(-d_1)

    :math:`\rho_C = KTe^{-rT}\Phi(d_2)`, :math:`\rho_P = -KTe^{-rT}\Phi(-d_2)`.

    Limit case (:math:`T \le \epsilon` or :math:`\sigma \le \epsilon`): price = discounted intrinsic
    :math:`\max(\pm(Se^{-qT} - Ke^{-rT}), 0)`, :math:`\Gamma = \mathcal V = 0`, deterministic
    :math:`\Delta, \Theta, \rho` (no division performed). ``X`` columns: ``[S, K, T, r, sigma, q]``.
    Output columns: ``[price, delta, gamma, vega, theta, rho]``, shape ``(N, 6)``.
    """

    config: BlackScholesConfig

    def __init__(self, config: BlackScholesConfig | None = None) -> None:
        super().__init__(resolve_config(config, BlackScholesConfig))
        self._publish_state(_BSState(self.config.option_type))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(_BSState(self.config.option_type))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        cfg = self.config
        state: _BSState = self._current_state()
        data = as_feature_matrix(X, name="X", n_features=6)
        S, K, T, r, sigma, q = (data[:, j] for j in range(6))
        if np.any(S <= 0.0) or np.any(K <= 0.0) or np.any(T < 0.0) or np.any(sigma < 0.0):
            raise ValueError("Require S > 0, K > 0, T >= 0, sigma >= 0.")
        eps = cfg.limit_epsilon
        regular = (T > eps) & (sigma > eps)
        Ts = np.where(regular, T, 1.0)
        ss = np.where(regular, sigma, 1.0)
        sqrt_t = np.sqrt(Ts)
        d1 = (np.log(S / K) + (r - q + 0.5 * ss**2) * Ts) / (ss * sqrt_t)
        d2 = d1 - ss * sqrt_t
        dq, dr = np.exp(-q * T), np.exp(-r * T)
        pdf_d1 = norm.pdf(d1)
        gamma = dq * pdf_d1 / (S * ss * sqrt_t)
        vega = S * dq * pdf_d1 * sqrt_t
        decay = -S * ss * dq * pdf_d1 / (2.0 * sqrt_t)
        forward = S * dq - K * dr
        if state.option_type == "call":
            price = S * dq * norm.cdf(d1) - K * dr * norm.cdf(d2)
            delta = dq * norm.cdf(d1)
            theta = decay - r * K * dr * norm.cdf(d2) + q * S * dq * norm.cdf(d1)
            rho = K * Ts * dr * norm.cdf(d2)
            itm = (forward > 0.0).astype(np.float64)
            lim_price, lim_delta = np.maximum(forward, 0.0), dq * itm
            lim_theta, lim_rho = (q * S * dq - r * K * dr) * itm, K * T * dr * itm
        else:
            price = K * dr * norm.cdf(-d2) - S * dq * norm.cdf(-d1)
            delta = -dq * norm.cdf(-d1)
            theta = decay + r * K * dr * norm.cdf(-d2) - q * S * dq * norm.cdf(-d1)
            rho = -K * Ts * dr * norm.cdf(-d2)
            itm = (forward < 0.0).astype(np.float64)
            lim_price, lim_delta = np.maximum(-forward, 0.0), -dq * itm
            lim_theta, lim_rho = (r * K * dr - q * S * dq) * itm, -K * T * dr * itm
        out = np.column_stack([
            np.where(regular, price, lim_price),
            np.where(regular, delta, lim_delta),
            np.where(regular, gamma, 0.0),
            np.where(regular, vega, 0.0) * cfg.vega_scale,
            np.where(regular, theta, lim_theta) * cfg.theta_scale,
            np.where(regular, rho, lim_rho) * cfg.rho_scale,
        ])
        return sanitize(out.reshape(data.shape[0], 6))
