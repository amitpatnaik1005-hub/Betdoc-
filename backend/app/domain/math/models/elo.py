import math

from app.domain.math.base import NO_SCORELINE, BaseMathModel
from app.schemas.math import MatchContext, PredictionResult

HFA = 100.0
SIGMA = 150.0
DRAW_PEAK = 0.28


class EloProbabilityModel(BaseMathModel):
    name = "elo"

    def _calculate(self, context: MatchContext) -> PredictionResult | None:
        if context.home_elo is None or context.away_elo is None:
            return None

        diff = float(context.home_elo) - float(context.away_elo) + HFA
        exponent = max(-50.0, min(50.0, -diff / 400.0))
        expected_home = 1.0 / (1.0 + math.pow(10.0, exponent))

        draw = DRAW_PEAK * math.exp(-(diff ** 2) / (2.0 * SIGMA ** 2))
        home = max(0.0, expected_home - draw / 2.0)
        away = max(0.0, (1.0 - expected_home) - draw / 2.0)

        return self.build_result(home, draw, away, NO_SCORELINE)
