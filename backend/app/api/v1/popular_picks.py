"""Oracle Popular Picks router. Mount with prefix="/popular-picks"."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.popular_picks.errors import PopularPickNotFoundError, PopularPicksDomainError
from app.domain.popular_picks.manager import PopularPicksManager
from app.schemas.popular_picks import PopularParlayRead, ReviewDecisionRequest, ReviewDecisionResponse

logger = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["Oracle Popular Picks"])

_manager = PopularPicksManager()


def get_popular_picks_manager() -> PopularPicksManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[PopularPicksManager, Depends(get_popular_picks_manager)]


@router.get("", response_model=list[PopularParlayRead])
async def list_popular_picks(db: DbSession, manager: Manager) -> list[PopularParlayRead]:
    picks = await manager.get_active_picks(db)
    if not picks:
        # Temporary: seed mock parlays until the real trending feed is wired in.
        logger.info("ASHOKA found no active popular parlays; seeding mock picks.")
        await manager.generate_mock_picks(db)
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
