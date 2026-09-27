import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.domain.arena import ArenaEngine, ArenaError
from app.schemas.arena import (
    ActiveBetOverview,
    ArenaFilterParams,
    CashOutQuote,
    SettlementRequest,
    StrategyAnalyticsNode,
)

arena_log = logging.getLogger("betdoc.arena")

router = APIRouter(tags=["arena"])

_arena = ArenaEngine()


def _http(exc: ArenaError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/active", response_model=list[ActiveBetOverview])
async def get_active_bets(db: DbSession, current_user: CurrentUser) -> list[ActiveBetOverview]:
    return await _arena.get_active_bets(db, current_user.id)


@router.get("/analytics/strategies", response_model=list[StrategyAnalyticsNode])
async def get_strategy_analytics(
    db: DbSession,
    current_user: CurrentUser,
    filters: Annotated[ArenaFilterParams, Depends()],
) -> list[StrategyAnalyticsNode]:
    try:
        return await _arena.get_strategy_analytics(db, current_user.id, filters)
    except ArenaError as exc:
        raise _http(exc) from exc


@router.get("/{bet_id}/cash-out", response_model=CashOutQuote)
async def get_cash_out_quote(
    bet_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
    current_true_prob: float = Query(..., ge=0.0, le=1.0),
    margin_pct: float = Query(0.05, ge=0.0, le=0.5),
) -> CashOutQuote:
    try:
        return await _arena.get_cash_out_quote(db, current_user.id, bet_id, current_true_prob, margin_pct)
    except ArenaError as exc:
        raise _http(exc) from exc


@router.post("/{bet_id}/settle", status_code=status.HTTP_204_NO_CONTENT)
async def settle_bet(
    bet_id: uuid.UUID,
    request: SettlementRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> None:
    try:
        await _arena.settle_bet(db, current_user.id, bet_id, request)
    except ArenaError as exc:
        raise _http(exc) from exc
