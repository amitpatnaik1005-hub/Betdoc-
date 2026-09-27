import numpy as np
from scipy.stats import poisson

from app.domain.math.base import BaseMathModel
from app.schemas.math import MatchContext, PredictionResult

MAX_GOALS = 5          # index 5 represents "5+"
GRID = MAX_GOALS + 1   # 6x6 matrix


def format_scoreline(home_goals: int, away_goals: int) -> str:
    def label(g: int) -> str:
        return f"{MAX_GOALS}+" if g >= MAX_GOALS else str(g)

    return f"{label(home_goals)}-{label(away_goals)}"


class PoissonModel(BaseMathModel):
    name = "poisson"

    def _calculate(self, context: MatchContext) -> PredictionResult | None:
        if context.home_xg is None or context.away_xg is None:
            return None
        matrix = self._build_matrix(float(context.home_xg), float(context.away_xg))
        return self._result_from_matrix(matrix)

    @staticmethod
    def _goal_distribution(xg: float) -> np.ndarray:
        dist = np.zeros(GRID, dtype=np.float64)
        if xg <= 0.0:
            dist[0] = 1.0
            return dist
        dist[:MAX_GOALS] = poisson.pmf(np.arange(MAX_GOALS), xg)       # P(0..4)
        dist[MAX_GOALS] = 1.0 - poisson.cdf(MAX_GOALS - 1, xg)         # P(>=5) tail
        dist = np.clip(dist, 0.0, 1.0)
        return dist / dist.sum()

    def _build_matrix(self, home_xg: float, away_xg: float) -> np.ndarray:
        # rows = home goals, cols = away goals
        return np.outer(self._goal_distribution(home_xg), self._goal_distribution(away_xg))

    def _result_from_matrix(self, matrix: np.ndarray) -> PredictionResult:
        home = float(np.tril(matrix, k=-1).sum())
        draw = float(np.trace(matrix))
        away = float(np.triu(matrix, k=1).sum())
        i, j = np.unravel_index(int(np.argmax(matrix)), matrix.shape)
        return self.build_result(home, draw, away, format_scoreline(int(i), int(j)))
