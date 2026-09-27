import numpy as np

from app.domain.math.base import BaseMathModel
from app.domain.math.models.poisson import GRID, MAX_GOALS, format_scoreline
from app.schemas.math import MatchContext, PredictionResult


class MonteCarloModel(BaseMathModel):
    name = "monte_carlo"

    def __init__(self, iterations: int = 10_000, seed: int | None = None) -> None:
        self.iterations = iterations
        self.seed = seed

    def _calculate(self, context: MatchContext) -> PredictionResult | None:
        if context.home_xg is None or context.away_xg is None:
            return None

        rng = np.random.default_rng(self.seed)  # per-call generator: thread-safe
        home_goals = rng.poisson(float(context.home_xg), self.iterations)
        away_goals = rng.poisson(float(context.away_xg), self.iterations)

        home = float(np.mean(home_goals > away_goals))
        draw = float(np.mean(home_goals == away_goals))
        away = float(np.mean(home_goals < away_goals))

        h_idx = np.minimum(home_goals, MAX_GOALS)
        a_idx = np.minimum(away_goals, MAX_GOALS)
        counts = np.bincount(h_idx * GRID + a_idx, minlength=GRID * GRID).reshape(GRID, GRID)
        i, j = np.unravel_index(int(np.argmax(counts)), counts.shape)

        return self.build_result(home, draw, away, format_scoreline(int(i), int(j)))
