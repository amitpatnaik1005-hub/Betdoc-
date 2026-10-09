"""Oracle Popular Picks router. Mount with prefix="/popular-picks"."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_db
from app.core.config import get_settings
from app.domain.popular_picks.errors import PopularPickNotFoundError, PopularPicksDomainError
from app.domain.popular_picks.manager import PopularPicksManager
from app.schemas.popular_picks import ExternalParlaySubmit, PopularParlayRead, ReviewDecisionRequest, ReviewDecisionResponse

logger = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["Oracle Popular Picks"])

_manager = PopularPicksManager()


def get_popular_picks_manager() -> PopularPicksManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[PopularPicksManager, Depends(get_popular_picks_manager)]


@router.get("", response_model=list[PopularParlayRead])
async def list_popular_picks(request: Request, db: DbSession, manager: Manager) -> list[PopularParlayRead]:
    picks = await manager.get_active_picks(db)
    redis = getattr(request.app.state, "redis", None)
    if not picks and redis is not None:
        # Nothing live: run Ashoka's trend scan on the current market (never demo data)
        try:
            await manager.scan_trending(db, redis, get_settings())
        except (RedisError, OSError, TimeoutError):
            logger.warning("ASHOKA trend scan skipped: the live market (Redis) is unreachable")
        picks = await manager.get_active_picks(db)
    response = [PopularParlayRead.model_validate(pick) for pick in picks]
    logger.info("ASHOKA served %d popular parlays.", len(response))
    return response


@router.post("/{parlay_id}/review", response_model=ReviewDecisionResponse)
async def review_popular_pick(
    parlay_id: UUID,
    payload: ReviewDecisionRequest,
    db: DbSession,
    manager: Manager,
) -> ReviewDecisionResponse:
    try:
        gate = await manager.record_review_decision(
            db,
            parlay_id=parlay_id,
            user_id=None,  # anonymous until an auth dependency supplies the user
            decision=payload.decision,
        )
    except PopularPickNotFoundError as exc:
        logger.warning("ASHOKA review gate rejected: parlay %s not found.", parlay_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Popular parlay {parlay_id} not found."
        ) from exc
    except PopularPicksDomainError as exc:
        logger.warning("ASHOKA review gate error for parlay %s: %s", parlay_id, exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc

    response = ReviewDecisionResponse.model_validate(gate)
    logger.info("ASHOKA review gate %s stored with decision %s.", response.id, response.decision)
    return response


@router.post("/scan", response_model=list[PopularParlayRead])
async def scan_trending(request: Request, admin: CurrentAdmin, db: DbSession, manager: Manager) -> list[PopularParlayRead]:  # noqa: ARG001
    """Re-run Ashoka's trend scan now (it also runs whenever the list is empty)."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The live market (Redis) is unavailable")
    rows = await manager.scan_trending(db, redis, get_settings())
    return [PopularParlayRead.model_validate(row) for row in rows]


@router.post("/external", response_model=PopularParlayRead, status_code=status.HTTP_201_CREATED)
async def submit_external(payload: ExternalParlaySubmit, request: Request, admin: CurrentAdmin, db: DbSession, manager: Manager) -> PopularParlayRead:  # noqa: ARG001
    """A popular parlay copied from a bookmaker's own page, priced by Ashoka (a negative EV is a trap)."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The live market (Redis) is unavailable")
    try:
        row = await manager.submit_external(db, redis, get_settings(), payload)
    except PopularPicksDomainError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message) from exc
    return PopularParlayRead.model_validate(row)
