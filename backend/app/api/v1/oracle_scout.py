"""Scout Oracle (ASHOKA) router. Mount with prefix="/oracle-scout"."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.oracle_scout.errors import ScoutDomainError
from app.domain.oracle_scout.manager import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT, OracleScoutManager
from app.schemas.oracle_scout import ScoutChatRequest, ScoutChatResponse, ScoutHistoryRead

logger = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["ASHOKA Scout Oracle"])

_manager = OracleScoutManager()


def get_oracle_scout_manager() -> OracleScoutManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[OracleScoutManager, Depends(get_oracle_scout_manager)]


@router.post("/chat", response_model=ScoutChatResponse)
async def scout_chat(payload: ScoutChatRequest, db: DbSession, manager: Manager) -> ScoutChatResponse:
    try:
        entry = await manager.chat(
            db,
            user_message=payload.user_message,
            page_context=payload.page_context,
            user_id=payload.user_id,
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
