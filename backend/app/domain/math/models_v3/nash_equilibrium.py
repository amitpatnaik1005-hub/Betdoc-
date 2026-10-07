"""Two-player zero-sum Nash equilibrium via linear programming (primal and dual)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field
from scipy.optimize import linprog

from app.domain.math.models_v2.base import (
    FLOAT_TINY,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_feature_matrix,
    check_finite_scalar,
    freeze,
    resolve_config,
)

__all__ = ["NashEquilibriumConfig", "NashEquilibriumModel", "NashSolution"]


class NashEquilibriumConfig(MathModelConfig):
    method: Literal["highs", "highs-ds", "highs-ipm"] = "highs"
    probability_tolerance: float = Field(default=1e-12, ge=0.0)
    max_actions: int = Field(default=10_000, ge=1)


@dataclass(frozen=True, slots=True)
class NashSolution:
    player1_strategy: FloatArray
    player2_strategy: FloatArray
    game_value: float


@dataclass(frozen=True, slots=True)
class _NashState:
    solution: NashSolution | None


class NashEquilibriumModel(BaseMathModel):
    r"""Minimax solution of a zero-sum game with payoff matrix :math:`A \in \mathbb{R}^{m\times n}`
    (Player 1 = row maximiser, e.g. bettor; Player 2 = column minimiser, e.g. bookmaker).

    Player 1 (primal):

    .. math:: \max_{x, v}\ v \quad\text{s.t.}\quad A^\top x \ge v\mathbf 1,\quad \mathbf 1^\top x = 1,\quad 0 \le x_i \le 1

    Player 2 (dual):

    .. math:: \min_{y, w}\ w \quad\text{s.t.}\quad A y \le w\mathbf 1,\quad \mathbf 1^\top y = 1,\quad 0 \le y_j \le 1

    By von Neumann's minimax theorem :math:`v^\star = w^\star`. Probability variables use
    ``bounds=(0, 1)``; the value variable is free. ``predict`` solves ``X`` and returns :math:`x^\star`.
    """

    config: NashEquilibriumConfig

    def __init__(self, config: NashEquilibriumConfig | None = None) -> None:
        super().__init__(resolve_config(config, NashEquilibriumConfig))
        self._publish_state(_NashState(solution=None))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        self._publish_state(_NashState(solution=self.solve(X)))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        self._current_state()
        return np.array(self.solve(X).player1_strategy, dtype=np.float64)

    def solve(self, X: FloatArray) -> NashSolution:
        payoff = as_feature_matrix(X, name="X")
        m, n = payoff.shape
        if max(m, n) > self.config.max_actions:
            raise ValueError("Payoff matrix exceeds max_actions.")
        x, v = self._solve_side(payoff, maximise=True)
        y, w = self._solve_side(payoff, maximise=False)
        value = check_finite_scalar(0.5 * (v + w), name="game_value")
        return NashSolution(freeze(x), freeze(y), value)

    @property
    def last_solution(self) -> NashSolution | None:
        return self._current_state().solution

    def _solve_side(self, payoff: FloatArray, *, maximise: bool) -> tuple[FloatArray, float]:
        matrix = payoff.T if maximise else payoff
        k, constraints = matrix.shape[1], matrix.shape[0]
        c = np.zeros(k + 1, dtype=np.float64)
        c[-1] = -1.0 if maximise else 1.0
        sign = -1.0 if maximise else 1.0
        a_ub = np.hstack([sign * matrix, -sign * np.ones((constraints, 1))])
        b_ub = np.zeros(constraints, dtype=np.float64)
        a_eq = np.hstack([np.ones((1, k)), np.zeros((1, 1))])
        bounds = [(0.0, 1.0)] * k + [(None, None)]
        result = linprog(c, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=np.array([1.0]), bounds=bounds, method=self.config.method)
        if not result.success or result.x is None:
            raise NumericalStabilityError(f"Nash LP failed: {result.message}")
        strategy = np.clip(np.asarray(result.x[:k], dtype=np.float64), 0.0, 1.0)
        strategy[strategy < self.config.probability_tolerance] = 0.0
        total = float(strategy.sum())
        if total <= FLOAT_TINY:
            raise NumericalStabilityError("Nash LP returned a degenerate strategy.")
        return strategy / total, float(result.x[-1])
