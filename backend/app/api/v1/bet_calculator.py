import logging
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status

from app.api.deps import CurrentUser
from app.domain.bet_types import (
    BetStructureUnion,
    CalculatorResult,
    ExposureResult,
    SettleRequest,
    SettlementError,
    SettlementResult,
    build_exposure_result,
    settle_bet,
    summarize_structure,
)

logger = logging.getLogger("betdoc.calculator")

router = APIRouter(tags=["calculator"])

# FastAPI rejects Field(discriminator=...) + Body() together; discriminate via Body instead.
StructureBody = Annotated[BetStructureUnion, Body(discriminator="structure_type")]


@router.post("/calculate", response_model=CalculatorResult)
async def calculate(structure: StructureBody) -> CalculatorResult:
    return summarize_structure(structure)


@router.post("/settle", response_model=SettlementResult)
async def settle(request: SettleRequest, current_user: CurrentUser) -> SettlementResult:
    try:
        result = settle_bet(request)
    except SettlementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "Settlement preview by user=%s: %s -> %s payout=%.4f commission=%.4f",
        current_user.id,
        request.structure.structure_type,
        result.status,
        result.payout,
        result.commission,
    )
    return result


@router.post("/exposure", response_model=ExposureResult)
async def exposure(structure: StructureBody) -> ExposureResult:
    return build_exposure_result(structure)
