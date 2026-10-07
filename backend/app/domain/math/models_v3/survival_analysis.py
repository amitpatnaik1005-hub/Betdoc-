"""Cox Proportional Hazards with overflow-safe partial likelihood and Breslow baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy import optimize

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_feature_matrix,
    as_float_array,
    check_finite_scalar,
    freeze,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["CoxPHConfig", "CoxPHModel"]


class CoxPHConfig(MathModelConfig):
    l2_penalty: float = Field(default=1e-4, ge=0.0)
    eta_clip: float = Field(default=50.0, gt=0.0)
    standardize: bool = True
    min_std: float = Field(default=1e-12, gt=0.0)
    max_iter: int = Field(default=500, ge=1)
    tol: float = Field(default=1e-9, gt=0.0)


@dataclass(frozen=True, slots=True)
class _CoxState:
    beta: FloatArray
    beta_original: FloatArray
    x_mean: FloatArray
    x_scale: FloatArray
    event_times: FloatArray
    cumulative_baseline_hazard: FloatArray
    log_partial_likelihood: float


class CoxPHModel(BaseMathModel):
    r"""Semi-parametric hazard :math:`h(t\mid x) = h_0(t)\exp(\beta^\top x)` for time-to-goal.

    Breslow partial log-likelihood, :math:`\eta_i = \operatorname{clip}(\beta^\top x_i, \pm c)`:

    .. math:: \ell(\beta) = \sum_{i:\delta_i = 1}\Bigl[\eta_i - \log\sum_{j: t_j \ge t_i} e^{\eta_j}\Bigr] - \tfrac{\lambda}{2}\lVert\beta\rVert^2

    With data sorted by time **descending**, :math:`\sum_{j: t_j\ge t_i} e^{\eta_j}` is an
    :math:`O(N)` ``np.cumsum`` read at the end of each tie group; :math:`\max\eta` is factored out
    to avoid overflow. Breslow baseline hazard and survival:

    .. math:: \hat h_0(t_k) = \frac{d_k}{\sum_{j\in R(t_k)} e^{\eta_j}},\qquad
              \hat S(t\mid x) = \exp\!\bigl(-\hat H_0(t)\,e^{\beta^\top x}\bigr)

    ``y`` columns: ``[time, event_observed]``. ``predict`` returns the partial hazard :math:`e^{\eta}`.
    """

    config: CoxPHConfig

    def __init__(self, config: CoxPHConfig | None = None) -> None:
        super().__init__(resolve_config(config, CoxPHConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        features = as_feature_matrix(X, name="X", min_samples=2)
        if y is None:
            raise ValueError("y = [time, event_observed] is required.")
        outcome = as_feature_matrix(y, name="y", n_features=2)
        if outcome.shape[0] != features.shape[0]:
            raise ValueError("X and y must have the same number of rows.")
        time, event = outcome[:, 0], outcome[:, 1]
        if np.any(time < 0.0) or not np.all((event == 0.0) | (event == 1.0)):
            raise ValueError("time must be >= 0 and event_observed binary.")
        if event.sum() < 1.0:
            raise ValueError("At least one observed event is required.")

        x_mean = features.mean(axis=0) if cfg.standardize else np.zeros(features.shape[1])
        std = features.std(axis=0)
        x_scale = np.where(std > cfg.min_std, std, 1.0) if cfg.standardize else np.ones(features.shape[1])
        order = np.argsort(-time, kind="stable")
        Z = ((features - x_mean) / x_scale)[order]
        t_desc, e_desc = time[order], event[order]
        group_end = np.searchsorted(-t_desc, -t_desc, side="right") - 1

        def objective(beta: FloatArray) -> tuple[float, FloatArray]:
            eta = np.clip(Z @ beta, -cfg.eta_clip, cfg.eta_clip)
            shift = float(eta.max())
            w = np.exp(eta - shift)
            risk = np.cumsum(w)[group_end]
            risk_x = np.cumsum(w[:, None] * Z, axis=0)[group_end]
            ll = float(e_desc @ (eta - shift - np.log(risk))) - 0.5 * cfg.l2_penalty * float(beta @ beta)
            grad = (e_desc[:, None] * (Z - risk_x / risk[:, None])).sum(axis=0) - cfg.l2_penalty * beta
            return -ll, -grad

        result = optimize.minimize(
            objective, np.zeros(Z.shape[1]), jac=True, method="L-BFGS-B",
            options={"maxiter": cfg.max_iter, "ftol": cfg.tol},
        )
        beta = np.asarray(result.x, dtype=np.float64)
        if not np.all(np.isfinite(beta)):
            raise NumericalStabilityError("Cox coefficients are not finite.")
        eta = np.clip(Z @ beta, -cfg.eta_clip, cfg.eta_clip)
        cum_w = np.cumsum(np.exp(eta))
        uniq, deaths = np.unique(t_desc[e_desc == 1.0], return_counts=True)
        risk_idx = np.searchsorted(-t_desc, -uniq, side="right") - 1
        hazard = safe_array_divide(deaths.astype(np.float64), cum_w[risk_idx])
        self._publish_state(
            _CoxState(
                beta=freeze(beta),
                beta_original=freeze(beta / x_scale),
                x_mean=freeze(x_mean),
                x_scale=freeze(x_scale),
                event_times=freeze(uniq.astype(np.float64)),
                cumulative_baseline_hazard=freeze(np.cumsum(hazard)),
                log_partial_likelihood=check_finite_scalar(-float(result.fun), name="log_partial_likelihood"),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        return np.exp(self._linear_predictor(X))

    def survival_function(self, X: FloatArray, times: FloatArray) -> FloatArray:
        """:math:`S(t\\mid x)` with shape ``(n_samples, n_times)``."""
        state: _CoxState = self._current_state()
        grid = as_float_array(times, name="times", ndim=1)
        idx = np.searchsorted(state.event_times, grid, side="right") - 1
        H0 = np.where(idx >= 0, state.cumulative_baseline_hazard[np.maximum(idx, 0)], 0.0)
        return np.clip(sanitize(np.exp(-np.outer(np.exp(self._linear_predictor(X)), H0))), 0.0, 1.0)

    @property
    def coefficients(self) -> FloatArray:
        return self._current_state().beta_original

    def _linear_predictor(self, X: FloatArray) -> FloatArray:
        state: _CoxState = self._current_state()
        features = as_feature_matrix(X, name="X", n_features=state.beta.size)
        eta = ((features - state.x_mean) / state.x_scale) @ state.beta
        return np.clip(eta, -self.config.eta_clip, self.config.eta_clip)
