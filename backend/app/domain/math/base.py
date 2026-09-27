import asyncio
import math
from abc import ABC, abstractmethod

from app.schemas.math import MatchContext, PredictionResult

NO_SCORELINE = "N/A"


class BaseMathModel(ABC):
    name: str = "base"

    async def predict(self, context: MatchContext) -> PredictionResult | None:
        # Offload CPU-bound matrix math so the event loop never blocks.
        return await asyncio.to_thread(self._calculate, context)

    @abstractmethod
    def _calculate(self, context: MatchContext) -> PredictionResult | None:
        """Return None when the model's required inputs are missing."""

    @staticmethod
    def confidence_from_probs(home: float, draw: float, away: float) -> float:
        """1 - normalized Shannon entropy of the 1X2 distribution (0 = coin flip, 1 = certain)."""
        probs = [p for p in (home, draw, away) if p > 0.0]
        entropy = -sum(p * math.log(p) for p in probs)
        return float(max(0.0, min(1.0, 1.0 - entropy / math.log(3))))

    @classmethod
    def build_result(
        cls,
        home: float,
        draw: float,
        away: float,
        scoreline: str,
        confidence: float | None = None,
    ) -> PredictionResult:
        total = float(home) + float(draw) + float(away)
        if not math.isfinite(total) or total <= 0.0:
            raise ValueError("Probability mass must be positive and finite")
        h, d, a = float(home) / total, float(draw) / total, float(away) / total
        conf = cls.confidence_from_probs(h, d, a) if confidence is None else float(confidence)
        return PredictionResult(
            home_win_prob=float(h),
            draw_prob=float(d),
            away_win_prob=float(a),
            most_likely_scoreline=str(scoreline),
            confidence_score=float(max(0.0, min(1.0, conf))),
        )
