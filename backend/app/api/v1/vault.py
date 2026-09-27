import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.domain.capital import VaultEngine, VaultQueryError
from app.schemas.vault import (
    BookmakerPnL,
    CapitalOverview,
    GrowthNode,
    MarketPnL,
    PnLTimeframe,
    TimeframePnLNode,
    VaultFilterParams,
    WaterfallNode,
)

porus = logging.getLogger("betdoc.porus")

router = APIRouter(tags=["vault"])

_vault = VaultEngine()

Filters = Annotated[VaultFilterParams, Depends()]


def _bad_request(exc: VaultQueryError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/overview", response_model=CapitalOverview)
async def get_overview(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
    bankroll: float = Query(..., gt=0),
) -> CapitalOverview:
    try:
        return await _vault.get_overview(db, current_user.id, filters, bankroll)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc


@router.get("/pnl/timeframe", response_model=list[TimeframePnLNode])
async def get_pnl_timeframe(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
    timeframe: PnLTimeframe = Query(default=PnLTimeframe.DAILY),
) -> list[TimeframePnLNode]:
    try:
        return await _vault.get_pnl_by_timeframe(db, current_user.id, filters, timeframe)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc


@router.get("/pnl/waterfall", response_model=list[WaterfallNode])
async def get_pnl_waterfall(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
    bankroll: float = Query(..., gt=0),
) -> list[WaterfallNode]:
    try:
        return await _vault.get_waterfall(db, current_user.id, filters, bankroll)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc


@router.get("/pnl/market", response_model=list[MarketPnL])
async def get_pnl_market(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
) -> list[MarketPnL]:
    try:
        return await _vault.get_pnl_by_market(db, current_user.id, filters)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc


@router.get("/pnl/bookmaker", response_model=list[BookmakerPnL])
async def get_pnl_bookmaker(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
) -> list[BookmakerPnL]:
    try:
        return await _vault.get_pnl_by_bookmaker(db, current_user.id, filters)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc


@router.get("/growth", response_model=list[GrowthNode])
async def get_growth(
    db: DbSession,
    current_user: CurrentUser,
    filters: Filters,
    bankroll: float = Query(..., gt=0),
) -> list[GrowthNode]:
    try:
        return await _vault.get_growth_trajectory(db, current_user.id, filters, bankroll)
    except VaultQueryError as exc:
        raise _bad_request(exc) from exc
