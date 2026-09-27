import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.domain.math.ensemble import EnsembleModel
from app.domain.math.value_detector import ValueDetector
from app.schemas.math import EnginePredictionResponse, MatchContext, ValueBetFlag

logger = logging.getLogger("betdoc.panini")

router = APIRouter(tags=["engine"])

_ensemble = EnsembleModel()
_value_detector = ValueDetector()


@router.post("/predict", response_model=EnginePredictionResponse)
async def predict_match(
    context: MatchContext,
    current_user=Depends(get_current_user),
) -> EnginePredictionResponse:
    logger.info(
        "PANINI predict requested by user=%s: %s vs %s",
        getattr(current_user, "id", "unknown"), context.home_team, context.away_team,
    )

    prediction = await _ensemble.predict(context)
    if prediction is None:
        logger.warning("PANINI rejected %s vs %s: no model had sufficient data",
                       context.home_team, context.away_team)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Insufficient data to run predictions",
        )

    value_bets: list[ValueBetFlag] = []
    if context.bookmaker_odds:
        value_bets = _value_detector.detect(prediction, context.bookmaker_odds, match_id=context.match_id or "")

    return EnginePredictionResponse(prediction=prediction, value_bets=value_bets)
