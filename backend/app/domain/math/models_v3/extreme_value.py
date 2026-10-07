"""Extreme Value Theory: Peaks-Over-Threshold with a Generalised Pareto tail."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import genpareto

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["ExtremeValueConfig", "ExtremeValueModel"]


class ExtremeValueConfig(MathModelConfig):
    threshold_quantile: float = Field(default=0.95, gt=0.0, lt=1.0)
    threshold: float | None = None
    min_samples_exceeding: int = Field(default=30, ge=3)
    tail: Literal["upper", "lower"] = "upper"


@dataclass(frozen=True, slots=True)
class _EVTState:
    shape: float
    loc: float
    scale: float
    threshold: float
    exceedance_rate: float
    n_exceedances: int
    sign: float


class ExtremeValueModel(BaseMathModel):
    r"""POT tail model: for threshold :math:`u`, excesses :math:`Y = X - u \mid X > u \sim \mathrm{GPD}(\xi, 0, \beta)`.

    .. math:: \bar G_{\xi,\beta}(y) = \begin{cases}(1 + \xi y/\beta)^{-1/\xi} & \xi \ne 0\\ e^{-y/\beta} & \xi = 0\end{cases}

    The fit uses ``genpareto.fit(excesses, floc=0)``, which locks the location to keep the
    POT assumption. ``predict`` returns the conditional exceedance
    :math:`P(X > x\mid X > u) =` ``genpareto.sf(X, c, loc=u, scale=beta)``.
    The unconditional tail is :math:`\zeta_u \bar G(x - u)`, with VaR
    :math:`u + \frac{\beta}{\xi}\bigl[(p/\zeta_u)^{-\xi} - 1\bigr]`.
    ``tail="lower"`` models losses by reflecting :math:`X \to -X`.
    """

    config: ExtremeValueConfig

    def __init__(self, config: ExtremeValueConfig | None = None) -> None:
        super().__init__(resolve_config(config, ExtremeValueConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        sign = 1.0 if cfg.tail == "upper" else -1.0
        data = sign * as_vector(X, name="X")
        u = float(np.quantile(data, cfg.threshold_quantile)) if cfg.threshold is None else sign * cfg.threshold
        X_tail = data[data > u] - u
        if X_tail.size < cfg.min_samples_exceeding:
            raise NumericalStabilityError(
                f"Only {X_tail.size} exceedances above threshold; need {cfg.min_samples_exceeding}."
            )
        c, loc, scale = genpareto.fit(X_tail, floc=0)
        if not scale > 0.0:
            raise NumericalStabilityError("GPD scale must be strictly positive.")
        self._publish_state(
            _EVTState(
                shape=check_finite_scalar(float(c), name="shape"),
                loc=float(loc),
                scale=check_finite_scalar(float(scale), name="scale"),
                threshold=check_finite_scalar(u, name="threshold"),
                exceedance_rate=X_tail.size / data.size,
                n_exceedances=int(X_tail.size),
                sign=sign,
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _EVTState = self._current_state()
        levels = state.sign * as_vector(X, name="X")
        sf = genpareto.sf(levels, state.shape, loc=state.threshold + state.loc, scale=state.scale)
        return np.clip(sanitize(sf), 0.0, 1.0)

    def tail_probability(self, X: FloatArray) -> FloatArray:
        """Unconditional :math:`P(X > x) \\approx \\zeta_u \\bar G(x - u)` for :math:`x \\ge u`."""
        state: _EVTState = self._current_state()
        return self.predict(X) * state.exceedance_rate

    def value_at_risk(self, probability: float) -> float:
        state: _EVTState = self._current_state()
        if not 0.0 < probability < state.exceedance_rate:
            raise ValueError("probability must lie in (0, exceedance_rate).")
        ratio = probability / state.exceedance_rate
        if abs(state.shape) <= np.finfo(np.float64).eps:
            var = state.threshold - state.scale * math.log(ratio)
        else:
            var = state.threshold + state.scale / state.shape * (ratio ** (-state.shape) - 1.0)
        return check_finite_scalar(state.sign * var, name="value_at_risk")

    @property
    def parameters(self) -> dict[str, float | int]:
        s: _EVTState = self._current_state()
        return {"shape": s.shape, "scale": s.scale, "threshold": s.sign * s.threshold,
                "exceedance_rate": s.exceedance_rate, "n_exceedances": s.n_exceedances}
