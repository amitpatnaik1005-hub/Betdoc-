import asyncio
import logging

from app.domain.math.base import NO_SCORELINE, BaseMathModel
from app.domain.math.models import DixonColesModel, EloProbabilityModel, PoissonModel
from app.schemas.math import MatchContext, PredictionResult

logger = logging.getLogger("betdoc.panini")


class EnsembleModel:
    def __init__(self, members: list[tuple[BaseMathModel, float]] | None = None) -> None:
        self.members = members or [
            (PoissonModel(), 0.30),
            (DixonColesModel(), 0.40),
            (EloProbabilityModel(), 0.30),
        ]
        if any(w <= 0 for _, w in self.members):
            raise ValueError("Ensemble weights must be positive")

    async def predict(self, context: MatchContext) -> PredictionResult | None:
        outcomes = await asyncio.gather(
            *(model.predict(context) for model, _ in self.members),
            return_exceptions=True,
        )

        successes: list[tuple[float, PredictionResult]] = []
        for (model, weight), outcome in zip(self.members, outcomes):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.error("Model %s failed", model.name, exc_info=outcome)
                continue
            if outcome is None:
                logger.info("Model %s skipped: insufficient data", model.name)
                continue
            successes.append((weight, outcome))

        if not successes:
            return None

        total_weight = sum(w for w, _ in successes)
        weighted = [(w / total_weight, r) for w, r in successes]  # sums to 1.0

        home = sum(w * r.home_win_prob for w, r in weighted)
        draw = sum(w * r.draw_prob for w, r in weighted)
        away = sum(w * r.away_win_prob for w, r in weighted)
        confidence = sum(w * r.confidence_score for w, r in weighted)

        scored = [(w, r.most_likely_scoreline) for w, r in weighted
                  if r.most_likely_scoreline != NO_SCORELINE]
        scoreline = max(scored, key=lambda x: x[0])[1] if scored else NO_SCORELINE

        return BaseMathModel.build_result(
            float(home), float(draw), float(away), scoreline, float(confidence)
        )
