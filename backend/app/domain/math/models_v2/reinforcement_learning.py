"""Q-Learning value iteration for discrete decision processes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    freeze,
    require_integral,
    resolve_config,
    sanitize,
)

__all__ = ["QLearningConfig", "QLearningModel"]


class QLearningConfig(MathModelConfig):
    n_states: int = Field(default=10, gt=0)
    n_actions: int = Field(default=2, gt=0)
    learning_rate: float = Field(default=0.1, gt=0.0, le=1.0)
    discount_factor: float = Field(default=0.99, gt=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class _QState:
    q_table: FloatArray


class QLearningModel(BaseMathModel):
    """Tabular Q-Learning updated offline from historical transitions.

    Inputs must be sequences of ``[state, action, reward, next_state]``.
    """

    config: QLearningConfig

    def __init__(self, config: QLearningConfig | None = None) -> None:
        super().__init__(resolve_config(config, QLearningConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        """Process a batch of transitions to update the Q-table."""
        matrix = as_feature_matrix(X, name="X", n_features=4)
        states = require_integral(matrix[:, 0:1], name="states")[:, 0]
        actions = require_integral(matrix[:, 1:2], name="actions")[:, 0]
        rewards = matrix[:, 2]
        next_states = require_integral(matrix[:, 3:4], name="next_states")[:, 0]

        q_table = np.zeros((self.config.n_states, self.config.n_actions), dtype=np.float64)

        alpha = self.config.learning_rate
        gamma = self.config.discount_factor

        for s, a, r, s_next in zip(states, actions, rewards, next_states):
            if not (0 <= s < self.config.n_states):
                raise ValueError(f"State {s} out of bounds.")
            if not (0 <= a < self.config.n_actions):
                raise ValueError(f"Action {a} out of bounds.")
            if not (0 <= s_next < self.config.n_states):
                raise ValueError(f"Next state {s_next} out of bounds.")

            best_next = q_table[s_next].max()
            q_table[s, a] = q_table[s, a] + alpha * (r + gamma * best_next - q_table[s, a])

        self._publish_state(_QState(freeze(q_table)))

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        """Returns the optimal action (greedy) for each given state."""
        state: _QState = self._current_state()
        states = require_integral(as_feature_matrix(X, name="X", n_features=1), name="X")[:, 0]
        if np.any((states < 0) | (states >= self.config.n_states)):
            raise ValueError("Query states out of bounds.")

        q_values = state.q_table[states]
        best_actions = np.argmax(q_values, axis=1)
        return sanitize(best_actions.astype(np.float64))
