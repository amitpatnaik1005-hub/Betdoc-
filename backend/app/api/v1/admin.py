import logging
from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentAdmin, DbSession
from app.schemas.admin import ResolveBetRequest, ResolveBetResponse, SweepResponse
from app.services.reconciliation_service import resolve_unknown_bet, sweep_stale_bets

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.post("/sweep-stale-bets", response_model=SweepResponse)
async def sweep_stale_bets_endpoint(admin: CurrentAdmin, db: DbSession) -> SweepResponse:
    count = await sweep_stale_bets(db)
    logger.info("ADMIN_AUDIT sweep_triggered admin_id=%s swept_count=%d", admin.id, count)
    return SweepResponse(swept_count=count)


@router.post("/bets/{bet_id}/resolve", response_model=ResolveBetResponse)
async def resolve_bet_endpoint(
    bet_id: UUID,
    req: ResolveBetRequest,
    admin: CurrentAdmin,
    db: DbSession,
) -> ResolveBetResponse:
    bet = await resolve_unknown_bet(db, bet_id, req, admin.id)
    return ResolveBetResponse(bet_id=bet.id, status=bet.status)
