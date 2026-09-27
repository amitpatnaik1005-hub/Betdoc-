import logging
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status

from app.api.deps import CurrentUser
from app.domain.bet_types import (
    AnyBetStructure,
    CalculatorResult,
    ExposureResult,
    SettleRequest,
    SettlementError,
    SettlementResult,
    calculate_bet_exposure,
    calculate_potential_return,
    settle_bet,
)

logger = logging.getLogger("betdoc.calculator")

router = APIRouter(tags=["calculator"])


@router.post("/calculate", response_model=CalculatorResult)
async def calculate(structure: Annotated[AnyBetStructure, Body()]) -> CalculatorResult:
    return calculate_potential_return(structure)


@router.post("/settle", response_model=SettlementResult)
async def settle(request: SettleRequest, current_user: CurrentUser) -> SettlementResult:
    try:
        result = settle_bet(request.structure, request.leg_contexts)
    except SettlementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "Settlement preview by user=%s: %s -> %s payout=%.4f",
        current_user.id, request.structure.structure_type, result.status, result.payout,
    )
    return result


@router.post("/exposure", response_model=ExposureResult)
async def exposure(structure: Annotated[AnyBetStructure, Body()]) -> ExposureResult:
    return calculate_bet_exposure(structure)
