"""Human Touch Mode (FA-8) router."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.human_touch.errors import (
    HumanTouchDomainError,
    OverrideLogAlreadyResolvedError,
    OverrideLogNotFoundError,
)
from app.domain.human_touch.manager import ConfigValues, HumanTouchManager, NarrativeFactorInput, NarrativeMetrics
from app.schemas.human_touch import (
    BlendExecuteRequest,
    BlendExecuteResponse,
    BlendRequest,
    BlendResponse,
    HumanTouchConfigRead,
    HumanTouchConfigUpdate,
    OverrideLogRead,
    ResolveOverrideRequest,
    ResolveOverrideResponse,
)

logger = logging.getLogger("betdoc.human_touch")

router = APIRouter(prefix="/human-touch", tags=["HUMAN TOUCH MODE (FA-8)"])

_manager = HumanTouchManager()


def get_human_touch_manager() -> HumanTouchManager:
    return _manager


def _metrics(payload: BlendRequest) -> NarrativeMetrics:
    return NarrativeMetrics(
        sentiment_score=payload.sentiment_score,
        factors=tuple(NarrativeFactorInput(name=f.name, value=f.value, impact=f.impact) for f in payload.factors),
    )


def _http_error(exc: HumanTouchDomainError) -> HTTPException:
    if isinstance(exc, OverrideLogNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, OverrideLogAlreadyResolvedError):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    logger.warning("HUMAN-TOUCH: request rejected (%d): %s", code, exc.message)
    return HTTPException(status_code=code, detail=exc.message)


@router.get("/config", response_model=HumanTouchConfigRead)
async def read_config(
    db: AsyncSession = Depends(get_db),
    manager: HumanTouchManager = Depends(get_human_touch_manager),
) -> HumanTouchConfigRead:
    try:
        config = await manager.get_or_seed_config(db)
    except HumanTouchDomainError as exc:
        raise _http_error(exc) from exc
    return HumanTouchConfigRead.model_validate(config)


@router.put("/config", response_model=HumanTouchConfigRead)
async def update_config(
    payload: HumanTouchConfigUpdate,
    db: AsyncSession = Depends(get_db),
    manager: HumanTouchManager = Depends(get_human_touch_manager),
) -> HumanTouchConfigRead:
    try:
        config = await manager.update_config(db, ConfigValues(**payload.model_dump()))
    except HumanTouchDomainError as exc:
        raise _http_error(exc) from exc
    return HumanTouchConfigRead.model_validate(config)


@router.post("/blend", response_model=BlendResponse)
async def blend(
    payload: BlendRequest,
    db: AsyncSession = Depends(get_db),
    manager: HumanTouchManager = Depends(get_human_touch_manager),
) -> BlendResponse:
    try:
        config = await manager.get_or_seed_config(db)
        result = manager.calculate_blended_probability(payload.pure_math_prob, _metrics(payload), config)
    except HumanTouchDomainError as exc:
        raise _http_error(exc) from exc
    return BlendResponse(**result.as_dict())


@router.post("/blend/execute", response_model=BlendExecuteResponse)
async def blend_and_log(
    payload: BlendExecuteRequest,
    db: AsyncSession = Depends(get_db),
    manager: HumanTouchManager = Depends(get_human_touch_manager),
) -> BlendExecuteResponse:
    try:
        result, log = await manager.execute_blend(db, payload.match_id, payload.pure_math_prob, _metrics(payload))
    except HumanTouchDomainError as exc:
        raise _http_error(exc) from exc
    return BlendExecuteResponse(**result.as_dict(), log=OverrideLogRead.model_validate(log))


@router.post("/override-logs/{log_id}/resolve", response_model=ResolveOverrideResponse)
async def resolve_override_log(
    log_id: UUID,
    payload: ResolveOverrideRequest,
    db: AsyncSession = Depends(get_db),
    manager: HumanTouchManager = Depends(get_human_touch_manager),
) -> ResolveOverrideResponse:
    try:
        log, improved, improvement = await manager.resolve_override_log(db, log_id, payload.actual_outcome)
    except HumanTouchDomainError as exc:
        raise _http_error(exc) from exc
    return ResolveOverrideResponse(
        log=OverrideLogRead.model_validate(log), human_touch_improved=improved, brier_improvement=improvement
    )
