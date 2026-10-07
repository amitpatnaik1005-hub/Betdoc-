"""Linear-programming capital allocation via HiGHS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator
from scipy.optimize import linprog

from app.domain.math.models_v2.base import (
    BaseMathModel, MathModelConfig, FloatArray, resolve_config,
    check_finite_scalar, check_finite_array, sanitize, as_feature_matrix,
    as_float_array, as_vector, NumericalStabilityError, ModelNotFittedError, freeze
)

__all__ = ["SimplexOptimizationConfig", "SimplexOptimizationModel"]

Matrix = tuple[tuple[float, ...], ...]
Vector = tuple[float, ...]


def _as_matrix(value: Matrix, name: str) -> FloatArray:
    try:
        arr = np.asarray(value, dtype=np.float64)
    except ValueError as exc:
        raise ValueError(f"{name} must be rectangular.") from exc
    if arr.ndim != 2 or arr.size == 0 or not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be a non-empty finite 2-D matrix.")
    return arr


class SimplexOptimizationConfig(MathModelConfig):
    A_ub: Matrix | None = None
    b_ub: Vector | None = None
    A_eq: Matrix | None = None
    b_eq: Vector | None = None
    min_alloc: float = 0.0
    max_alloc: float = 1.0
    enforce_budget: bool = True
    budget: float = Field(default=1.0, gt=0.0)
    method: Literal["highs", "highs-ds", "highs-ipm"] = "highs"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.min_alloc > self.max_alloc:
            raise ValueError("min_alloc must be <= max_alloc.")
        for a_name, b_name in (("A_ub", "b_ub"), ("A_eq", "b_eq")):
            A, b = getattr(self, a_name), getattr(self, b_name)
            if (A is None) != (b is None):
                raise ValueError(f"{a_name} and {b_name} must be provided together.")
            if A is not None and _as_matrix(A, a_name).shape[0] != len(b):
                raise ValueError(f"{a_name} rows must match len({b_name}).")
        return self


@dataclass(frozen=True, slots=True)
class _LPState:
    allocation: FloatArray
    objective: float
    expected_returns: FloatArray
    ineq_marginals: FloatArray | None
    eq_marginals: FloatArray | None


class SimplexOptimizationModel(BaseMathModel):
    r"""Allocation LP:

    .. math:: \max_x\ c^\top x\quad\text{s.t.}\quad A_{ub}x \le b_{ub},\quad A_{eq}x = b_{eq},\quad
              \ell \le x_i \le u\quad\bigl(\text{optionally } \mathbf 1^\top x = B\bigr)

    It is solved as :math:`\min -c^\top x` with ``linprog(method="highs")``. Failure to reach
    optimality raises ``NumericalStabilityError``. ``fit(X)`` takes :math:`c`; ``predict(X)``
    returns scenario returns :math:`X x^\star` for an ``(m, n)`` matrix.
    """

    config: SimplexOptimizationConfig

    def __init__(self, config: SimplexOptimizationConfig | None = None) -> None:
        super().__init__(resolve_config(config, SimplexOptimizationConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        c = as_vector(X, name="X")
        n = c.size
        A_ub = b_ub = A_eq = b_eq = None
        if cfg.A_ub is not None and cfg.b_ub is not None:
            A_ub, b_ub = _as_matrix(cfg.A_ub, "A_ub"), np.asarray(cfg.b_ub, dtype=np.float64)
            if A_ub.shape[1] != n:
                raise ValueError(f"A_ub must have {n} columns.")
        eq_rows: list[FloatArray] = []
        eq_rhs: list[float] = []
        if cfg.A_eq is not None and cfg.b_eq is not None:
            A_cfg = _as_matrix(cfg.A_eq, "A_eq")
            if A_cfg.shape[1] != n:
                raise ValueError(f"A_eq must have {n} columns.")
            eq_rows.extend(A_cfg)
            eq_rhs.extend(cfg.b_eq)
        if cfg.enforce_budget:
            eq_rows.append(np.ones(n))
            eq_rhs.append(cfg.budget)
        if eq_rows:
            A_eq, b_eq = np.vstack(eq_rows).reshape(len(eq_rows), n), np.asarray(eq_rhs, dtype=np.float64)
        result = linprog(
            -c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
            bounds=[(cfg.min_alloc, cfg.max_alloc)] * n, method=cfg.method,
        )
        if not result.success or result.x is None:
            raise NumericalStabilityError(f"LP did not reach optimality: {result.message}")
        x = check_finite_array(result.x, name="allocation")
        ineq = getattr(getattr(result, "ineqlin", None), "marginals", None)
        eq = getattr(getattr(result, "eqlin", None), "marginals", None)
        self._publish_state(
            _LPState(
                allocation=freeze(x),
                objective=check_finite_scalar(float(c @ x), name="objective"),
                expected_returns=freeze(c),
                ineq_marginals=None if ineq is None or len(ineq) == 0 else freeze(np.asarray(ineq, dtype=np.float64)),
                eq_marginals=None if eq is None or len(eq) == 0 else freeze(np.asarray(eq, dtype=np.float64)),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _LPState = self._current_state()
        arr = as_float_array(X, name="X", ndim=(1, 2))
        scenarios = arr.reshape(1, -1) if arr.ndim == 1 else arr
        if scenarios.shape[1] != state.allocation.size:
            raise ValueError(f"X must have {state.allocation.size} columns.")
        return sanitize(scenarios @ state.allocation)

    @property
    def allocation(self) -> FloatArray:
        return self._current_state().allocation

    @property
    def objective(self) -> float:
        return self._current_state().objective
