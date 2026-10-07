"""M/M/c queue metrics with overflow-safe log-space Erlang-C."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from scipy import special

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_feature_matrix,
    check_finite_scalar,
    require_integral,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["QueueingTheoryConfig", "QueueingTheoryModel"]


class QueueingTheoryConfig(MathModelConfig):
    n_servers: int = Field(default=1, ge=1)
    max_servers: int = Field(default=100_000, ge=1)
    min_service_rate: float = Field(default=1e-12, gt=0.0)
    saturation_policy: Literal["raise", "clamp"] = "clamp"
    saturated_wait_value: float = Field(default=1e12, gt=0.0)


@dataclass(frozen=True, slots=True)
class _QueueState:
    arrival_rate: float | None
    service_rate: float | None


class QueueingTheoryModel(BaseMathModel):
    r"""M/M/c liquidity queue for exchange order books.

    Offered load :math:`a = \lambda/\mu`, utilisation :math:`\rho = a/c < 1`.

    Erlang-C probability of waiting, evaluated in log space with
    :math:`\log k! = \operatorname{gammaln}(k+1)`:

    .. math:: C(c, a) = \frac{\dfrac{a^c}{c!}\dfrac{c}{c-a}}{\displaystyle\sum_{k=0}^{c-1}\frac{a^k}{k!} + \frac{a^c}{c!}\frac{c}{c-a}}

    .. math:: W_q = \frac{C(c, a)}{c\mu - \lambda},\qquad L_q = \lambda W_q,\qquad W = W_q + \frac1\mu

    ``predict`` takes rows ``[lambda, mu]`` or ``[lambda, mu, c]`` and returns :math:`W_q`.
    ``fit`` estimates :math:`\hat\lambda = 1/\overline{\Delta t}` and :math:`\hat\mu = 1/\overline{s}`
    from rows ``[inter_arrival_time, service_time]``.
    """

    config: QueueingTheoryConfig

    def __init__(self, config: QueueingTheoryConfig | None = None) -> None:
        super().__init__(resolve_config(config, QueueingTheoryConfig))
        self._publish_state(_QueueState(None, None))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        data = as_feature_matrix(X, name="X", n_features=2)
        if np.any(data <= 0.0):
            raise ValueError("Inter-arrival and service times must be strictly positive.")
        lam = check_finite_scalar(1.0 / float(data[:, 0].mean()), name="arrival_rate")
        mu = check_finite_scalar(1.0 / float(data[:, 1].mean()), name="service_rate")
        self._publish_state(_QueueState(lam, mu))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        self._current_state()
        return self.metrics(X)[:, 3]

    def predict_from_fitted(self) -> FloatArray:
        state: _QueueState = self._current_state()
        if state.arrival_rate is None or state.service_rate is None:
            raise ValueError("Call fit() before predict_from_fitted().")
        return self.metrics(np.array([[state.arrival_rate, state.service_rate]], dtype=np.float64))[0]

    def metrics(self, X: FloatArray) -> FloatArray:
        """Return ``[rho, P_wait, L_q, W_q, W]`` per row."""
        cfg = self.config
        data = as_feature_matrix(X, name="X")
        if data.shape[1] not in (2, 3):
            raise ValueError("X rows must be [arrival_rate, service_rate] or [arrival_rate, service_rate, servers].")
        lam, mu = data[:, 0], data[:, 1]
        if np.any(lam < 0.0) or np.any(mu < cfg.min_service_rate):
            raise ValueError("arrival_rate must be >= 0 and service_rate >= min_service_rate.")
        servers = (
            require_integral(data[:, 2], name="servers")
            if data.shape[1] == 3
            else np.full(data.shape[0], cfg.n_servers, dtype=np.int64)
        )
        if np.any(servers < 1) or np.any(servers > cfg.max_servers):
            raise ValueError("servers must lie in [1, max_servers].")
        offered = lam / mu
        rho = offered / servers
        stable = rho < 1.0
        if not np.all(stable) and cfg.saturation_policy == "raise":
            raise NumericalStabilityError("Queue is unstable (rho >= 1); waiting time is unbounded.")
        p_wait = np.ones(data.shape[0], dtype=np.float64)
        for c in np.unique(servers[stable]):
            rows = stable & (servers == c)
            p_wait[rows] = self.erlang_c(offered[rows], int(c))
        wq = np.where(stable, safe_array_divide(p_wait, servers * mu - lam), cfg.saturated_wait_value)
        lq = np.where(stable, lam * wq, cfg.saturated_wait_value)
        w = np.where(stable, wq + 1.0 / mu, cfg.saturated_wait_value)
        return sanitize(np.column_stack([rho, p_wait, lq, wq, w]))

    @staticmethod
    def erlang_c(offered_load: FloatArray, servers: int) -> FloatArray:
        load = np.asarray(offered_load, dtype=np.float64)
        out = np.zeros(load.size, dtype=np.float64)
        positive = load > 0.0
        if not np.any(positive):
            return out
        log_a = np.log(load[positive])
        k = np.arange(servers, dtype=np.float64)
        log_terms = k[None, :] * log_a[:, None] - special.gammaln(k + 1.0)[None, :]
        log_sum = special.logsumexp(log_terms, axis=1)
        log_top = servers * log_a - special.gammaln(servers + 1.0) + math.log(servers) - np.log(servers - load[positive])
        out[positive] = np.exp(log_top - np.logaddexp(log_sum, log_top))
        return np.clip(out, 0.0, 1.0)
