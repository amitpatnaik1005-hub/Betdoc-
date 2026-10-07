"""Scout Oracle (ASHOKA) router. Mount with prefix="/oracle-scout"."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import _decode_user_id, get_db
from app.domain.dashboard import build_dashboard_summary
from app.domain.oracle_scout.errors import ScoutDomainError
from app.domain.oracle_scout.manager import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT, OracleScoutManager, ScoutFacts
from app.schemas.oracle_scout import ScoutChatRequest, ScoutChatResponse, ScoutHistoryRead

logger = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["ASHOKA Scout Oracle"])

_manager = OracleScoutManager()


def get_oracle_scout_manager() -> OracleScoutManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[OracleScoutManager, Depends(get_oracle_scout_manager)]


def _bearer_user_id(request: Request) -> UUID | None:
    """The caller's own id from its bearer token (the router is mounted behind auth); None without one."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return _decode_user_id(header[7:].strip())
    except HTTPException:
        return None


async def _facts_for(user_id: UUID | None, db: AsyncSession) -> ScoutFacts | None:
    if user_id is None:
        return None
    try:
        summary = await build_dashboard_summary(user_id, db, 0.0)
    except Exception:  # noqa: BLE001 - a briefing without numbers beats no briefing
        logger.warning("ASHOKA: live book unavailable for %s", user_id, exc_info=True)
        await db.rollback()
        return None
    return ScoutFacts(
        bankroll=float(summary.total_bankroll),
        exposure=float(summary.current_exposure),
        daily_pnl=float(summary.daily_pnl),
        open_positions=int(summary.active_bets_count),
        win_rate_pct=float(summary.win_rate_pct),
        stop_loss_status=str(summary.stop_loss_status),
    )


@router.post("/chat", response_model=ScoutChatResponse)
async def scout_chat(payload: ScoutChatRequest, request: Request, db: DbSession, manager: Manager) -> ScoutChatResponse:
    # Authenticated callers always chat as themselves; a payload user_id can't impersonate anyone.
    caller = _bearer_user_id(request)
    try:
        entry = await manager.chat(
            db,
            user_message=payload.user_message,
            page_context=payload.page_context,
            user_id=caller or payload.user_id,
            facts=await _facts_for(caller, db),
        )
    except ScoutDomainError as exc:
        logger.warning("ASHOKA: chat rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return ScoutChatResponse(response_text=entry.oracle_response, history_id=entry.id)


@router.get("/history", response_model=list[ScoutHistoryRead])
async def scout_history(
    db: DbSession,
    manager: Manager,
    user_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
) -> list[ScoutHistoryRead]:
    try:
        history = await manager.get_history(db, user_id=user_id, limit=limit)
    except ScoutDomainError as exc:
        logger.warning("ASHOKA: history rejected: %s", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return [ScoutHistoryRead.model_validate(entry) for entry in history]
