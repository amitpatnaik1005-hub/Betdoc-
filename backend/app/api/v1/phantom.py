"""PHANTOM router. Mount with prefix="/phantom"."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.domain.phantom.errors import PhantomDomainError
from app.domain.phantom.manager import PhantomManager
from app.models.phantom import ArbitrageOpportunityModel, PhantomCalculationLogModel
from app.schemas.phantom import (
    ArbitrageOpportunityRead,
    ArbitrageRequest,
    ArbitrageResponse,
    CointegrationRequest,
    CointegrationResponse,
    DutchingRequest,
    DutchingResponse,
    MarketMakerRequest,
    MarketMakerResponse,
    MatchedBettingRequest,
    MatchedBettingResponse,
    PhantomCalculationLogRead,
)

logger = logging.getLogger("betdoc.phantom.garuda")

router = APIRouter(tags=["PHANTOM: Arbitrage & Execution"])

_manager = PhantomManager()


def get_phantom_manager() -> PhantomManager:
    return _manager


DbSession = Annotated[AsyncSession, Depends(get_db)]
Manager = Annotated[PhantomManager, Depends(get_phantom_manager)]


def _bad_request(exc: PhantomDomainError) -> HTTPException:
    logger.warning("[GARUDA]: request rejected: %s", exc.message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)


@router.post("/arbitrage", response_model=ArbitrageResponse)
async def detect_arbitrage(payload: ArbitrageRequest, db: DbSession, manager: Manager) -> ArbitrageResponse:
    try:
        outcome = await manager.detect_arbitrage(
            db,
            event_name=payload.event_name,
            market_type=payload.market_type,
            odds=payload.odds,
            commissions_pct=payload.commissions_pct,
            target_total_stake=payload.target_total_stake,
            minimum_profit_margin_pct=payload.minimum_profit_margin_pct,
        )
    except PhantomDomainError as exc:
        raise _bad_request(exc) from exc
    opportunity = outcome["opportunity"]
    return ArbitrageResponse(
        **outcome["result"],
        opportunity=ArbitrageOpportunityRead.model_validate(opportunity) if opportunity is not None else None,
    )


@router.post("/dutching", response_model=DutchingResponse)
async def calculate_dutching(payload: DutchingRequest, db: DbSession, manager: Manager) -> DutchingResponse:
    try:
        outcome = await manager.calculate_dutching(db, target_total_stake=payload.target_total_stake, odds=payload.odds)
    except PhantomDomainError as exc:
        raise _bad_request(exc) from exc
    return DutchingResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/matched-betting", response_model=MatchedBettingResponse)
async def calculate_matched_bet(
    payload: MatchedBettingRequest, db: DbSession, manager: Manager
) -> MatchedBettingResponse:
    try:
        outcome = await manager.calculate_matched_bet(
            db,
            back_stake=payload.back_stake,
            back_odds=payload.back_odds,
            lay_odds=payload.lay_odds,
            lay_commission_pct=payload.lay_commission_pct,
            mode=payload.mode,
        )
    except PhantomDomainError as exc:
        raise _bad_request(exc) from exc
    return MatchedBettingResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/market-maker", response_model=MarketMakerResponse)
async def calculate_market_maker(payload: MarketMakerRequest, db: DbSession, manager: Manager) -> MarketMakerResponse:
    try:
        outcome = await manager.calculate_market_maker_quotes(
            db,
            mid_price=payload.mid_price,
            inventory=payload.inventory,
            gamma=payload.gamma,
            volatility_sigma=payload.volatility_sigma,
            time_horizon_t=payload.time_horizon_t,
            current_time_t=payload.current_time_t,
            liquidity_k=payload.liquidity_k,
        )
    except PhantomDomainError as exc:
        raise _bad_request(exc) from exc
    return MarketMakerResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/cointegration", response_model=CointegrationResponse)
async def evaluate_cointegration(
    payload: CointegrationRequest, db: DbSession, manager: Manager
) -> CointegrationResponse:
    try:
        outcome = await manager.evaluate_cointegration(
            db,
            current_z_score=payload.current_z_score,
            entry_threshold=payload.entry_threshold,
            exit_threshold=payload.exit_threshold,
            stop_loss_threshold=payload.stop_loss_threshold,
        )
    except PhantomDomainError as exc:
        raise _bad_request(exc) from exc
    return CointegrationResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.get("/opportunities", response_model=list[ArbitrageOpportunityRead])
async def list_opportunities(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    active_only: bool = False,
) -> list[ArbitrageOpportunityRead]:
    """Persisted arbitrage hits, newest first (the shadow execution ledger)."""
    stmt = select(ArbitrageOpportunityModel).order_by(ArbitrageOpportunityModel.created_at.desc()).limit(limit)
    if active_only:
        stmt = stmt.where(ArbitrageOpportunityModel.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [ArbitrageOpportunityRead.model_validate(row) for row in rows]


@router.get("/calculations", response_model=list[PhantomCalculationLogRead])
async def list_calculations(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[PhantomCalculationLogRead]:
    """Audit log of every Phantom calculation (dutching, matched betting, market making, cointegration)."""
    stmt = select(PhantomCalculationLogModel).order_by(PhantomCalculationLogModel.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [PhantomCalculationLogRead.model_validate(row) for row in rows]
