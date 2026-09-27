from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.execution import PlaceBetRequest, PlaceBetResponse
from app.services.execution_service import execute_bet

router = APIRouter(tags=["execution"])


@router.post("/place-bet", response_model=PlaceBetResponse)
async def place_bet(
    req: PlaceBetRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> PlaceBetResponse:
    bet = await execute_bet(db, current_user.id, req)
    return PlaceBetResponse(bet_id=bet.id, status=bet.status)
