"""
FastAPI Router for the Real-Money Betting Gateway (Group 95).
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Mounted under ``/real-money``. Every route needs a signed-in user and touches only that user's own vault and
tickets; every response carries ``X-Engineered-For: Amit Ashok Kumar Patnaik``. Nothing here places a bet: the
trader places ASHVATTHAMA's ticket on Parimatch (or 1xBet) by hand and confirms it.

* ``GET  /bankroll``; ``POST /deposit``, ``/withdraw``: TODAR MAL's real cash vault (INR)
* ``POST /create-ticket``: the Parimatch ticket (deep link, clipboard slip, odds floor, Kelly stake)
* ``POST /confirm-placement``: lock the stake (400 under the odds floor without an explicit override)
* ``POST /settle-ticket``: settle from the outcome or the scorecard, with Section 115BBJ's 30% TDS
* ``GET  /active-tickets``, ``/history``, ``/transactions``: the open tickets, every ticket, the cash ledger
* ``GET  /tickets/{reference}/odds-check``: the book's live price against the floor (line drift)
* ``GET  /tax-certificate``: the financial year's TDS certificate for the live desk
* ``POST /confirm-handcrafted-placement``: a Bet-Slip placed by hand: the seal checked, the stake locked, the Oracle
  bet that settles it and the twin's in-play watch (Group 114)
* ``GET  /tickets/{ticket}/cashout-evaluation?offer_inr=``: the book's live cashout offer: HOLD, CASHOUT_ADVISED or
  STOP_LOSS_BREACH
* ``POST /cashout``: the cashout taken at the book, with Section 115BBJ's 30% TDS on the net winnings
* ``POST /reconcile``: locked exposure brought to the open tickets; liquid cash checked against its ledger

Refusals answer ``{"detail": {"code", "message", ...}}``: 400 (insufficient funds, odds decayed), 404, 409 (state),
422 (validation, no edge, stake over the 5% ceiling), 423 (SHIVAJI's stop-loss or cool-down, the kill switch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db
from app.core.config import Settings, get_settings
from app.domain.wagering.tax import financial_year
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.real_money import RealBetTicket
from app.schemas.real_money import (
    DEVELOPER_CREDIT,
    CashoutEvaluationResponse,
    ConfirmHandcraftedPlacementRequest,
    ConfirmPlacementRequest,
    CreateRealTicketRequest,
    DepositRequest,
    ExecuteCashoutRequest,
    ExecuteCashoutResponse,
    HandcraftedPlacementResponse,
    OddsCheckResponse,
    RealBankrollResponse,
    RealBetTicketResponse,
    RealCashTransactionResponse,
    SettleRealTicketRequest,
    TaxCertificateResponse,
    VaultReconciliationResponse,
    WithdrawRequest,
)
from app.services import handcrafted_execution_service as hes
from app.services import live_cashout_service as lcs
from app.services import real_money_service as rms
from app.services import vault_reconciliation_service as vrs

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Limit = Annotated[int, Query(ge=1, le=500)]


async def _engineered_for(response: Response) -> None:
    response.headers["X-Engineered-For"] = DEVELOPER_CREDIT


router = APIRouter(prefix="/real-money", tags=["Real-Money Execution Desk"], dependencies=[Depends(_engineered_for)])


def _redis(request: Request) -> Any:
    return getattr(request.app.state, "redis", None)


def _fail(exc: rms.RealMoneyError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message, **exc.detail}
    return HTTPException(exc.status, detail, headers={"X-Engineered-For": DEVELOPER_CREDIT})


async def _view(db: AsyncSession, settings: Settings, user_id: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    bankroll = await rms.get_or_create_bankroll(db, user_id, now)
    await db.commit()
    return rms.bankroll_view(bankroll, settings, now)


@router.get("/bankroll", response_model=RealBankrollResponse, summary="Real Cash Vault")
async def get_real_bankroll(db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    return await _view(db, settings, current_user.id)


@router.post("/deposit", response_model=RealBankrollResponse, summary="Record a Deposit")
async def deposit_real_cash(payload: DepositRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    try:
        await rms.deposit(db, current_user.id, Decimal(str(payload.amount)), reference=payload.reference, notes=payload.notes)
        await db.commit()
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    return await _view(db, settings, current_user.id)


@router.post("/withdraw", response_model=RealBankrollResponse, summary="Record a Withdrawal")
async def withdraw_real_cash(payload: WithdrawRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    try:
        await rms.withdraw(db, current_user.id, Decimal(str(payload.amount)), notes=payload.notes)
        await db.commit()
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    return await _view(db, settings, current_user.id)


@router.post("/create-ticket", response_model=RealBetTicketResponse, status_code=status.HTTP_201_CREATED, summary="Build ASHVATTHAMA's Execution Ticket")
async def create_real_ticket(
    payload: CreateRealTicketRequest,
    request: Request,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    ticket_request = rms.TicketRequest(
        sport=payload.sport,
        league=payload.league,
        match_id=payload.match_id,
        event_name=payload.event_name,
        market_type=payload.market_type,
        selection=payload.selection,
        target_odds=Decimal(str(payload.target_odds)),
        true_probability=payload.true_probability,
        model_edge_pct=payload.model_edge_pct,
        vetting_pillar_score=payload.vetting_pillar_score,
        bookmaker=payload.bookmaker,
        line=payload.line,
        quote_market=payload.quote_market,
        quote_selection=payload.quote_selection,
        notes=payload.notes,
    )
    now = datetime.now(UTC)
    try:
        ticket = await rms.create_execution_ticket(db, _redis(request), settings, current_user.id, ticket_request, now)
        await db.commit()
        return rms.ticket_view(ticket)
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc


@router.post("/confirm-placement", response_model=RealBetTicketResponse, summary="Confirm Placement and Lock Stake")
async def confirm_ticket_placement(
    payload: ConfirmPlacementRequest,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    try:
        ticket = await rms.confirm_placement(
            db=db,
            settings=settings,
            user_id=current_user.id,
            ticket_ref=payload.ticket_reference,
            placed_odds=Decimal(str(payload.placed_odds)),
            placed_stake=Decimal(str(payload.placed_stake)),
            slip_id=payload.bookmaker_slip_id,
            override_odds_decay=payload.override_odds_decay,
        )
        await db.commit()
        return rms.ticket_view(ticket)
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc


@router.post("/settle-ticket", response_model=RealBetTicketResponse, summary="Settle a Real Money Bet")
async def settle_real_ticket(
    payload: SettleRealTicketRequest,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    try:
        ticket = await rms.settle_ticket(
            db=db,
            settings=settings,
            user_id=current_user.id,
            ticket_ref=payload.ticket_reference,
            outcome=payload.outcome,
            scorecard=payload.scorecard,
            notes=payload.notes,
        )
        await db.commit()
        return rms.ticket_view(ticket)
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc


@router.get("/active-tickets", response_model=list[RealBetTicketResponse], summary="Active Real Tickets")
async def get_active_tickets(db: Db, current_user: CurrentUser) -> list[dict[str, Any]]:
    tickets = await rms.get_active_tickets(db, current_user.id)
    return [rms.ticket_view(t) for t in tickets]


@router.get("/history", response_model=list[RealBetTicketResponse], summary="Ticket History")
async def get_ticket_history(db: Db, current_user: CurrentUser, limit: Limit = 50) -> list[dict[str, Any]]:
    tickets = await rms.get_ticket_history(db, current_user.id, limit=limit)
    return [rms.ticket_view(t) for t in tickets]


@router.get("/transactions", response_model=list[RealCashTransactionResponse], summary="Cash Ledger Entries")
async def get_cash_transactions(db: Db, current_user: CurrentUser, limit: Limit = 50) -> list[dict[str, Any]]:
    txs = await rms.get_cash_transactions(db, current_user.id, limit=limit)
    return [rms.transaction_view(t) for t in txs]


@router.get("/tickets/{ticket_reference}/odds-check", response_model=OddsCheckResponse, summary="Live Odds Floor Check")
async def check_ticket_live_odds(
    ticket_reference: str,
    request: Request,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    stmt = select(RealBetTicket).where(RealBetTicket.ticket_reference == ticket_reference, RealBetTicket.user_id == current_user.id)
    res = await db.execute(stmt)
    ticket = res.scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return await rms.check_ticket_odds(_redis(request), settings, ticket)


@router.get("/tax-certificate", response_model=TaxCertificateResponse, summary="Section 115BBJ Tax Certificate")
async def get_tax_certificate(
    db: Db,
    current_user: CurrentUser,
    fy: str | None = None,
) -> dict[str, Any]:
    target_fy = fy or financial_year(datetime.now(UTC))
    stmt = select(TaxLedgerEntry).where(
        TaxLedgerEntry.user_id == current_user.id,
        TaxLedgerEntry.mode == "LIVE",
        TaxLedgerEntry.financial_year == target_fy,
    )
    res = await db.execute(stmt)
    entries = list(res.scalars().all())

    gross = sum((float(e.net_winnings) for e in entries), 0.0)
    tds = sum((float(e.tds_amount) for e in entries), 0.0)
    cess = round(tds * 0.04, 2)
    effective_rate = round(30.0 * 1.04, 2) if entries else 31.2

    return {
        "user_id": str(current_user.id),
        "financial_year": target_fy,
        "gross_winnings": round(gross, 2),
        "total_tds_withheld": round(tds, 2),
        "cess_amount": cess,
        "effective_rate_pct": effective_rate,
        "total_winning_tickets": len(entries),
        "jurisdiction": "IN",
        "section": "115BBJ",
        "issued_at": datetime.now(UTC).isoformat(),
        "developer_name": DEVELOPER_CREDIT,
    }

# ================================================================ Group 114: the handcrafted desk
@router.post("/confirm-handcrafted-placement", response_model=HandcraftedPlacementResponse, summary="Confirm a Bet-Slip Placed by Hand")
async def confirm_handcrafted_placement(
    payload: ConfirmHandcraftedPlacementRequest,
    request: Request,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    redis = _redis(request)
    try:
        placement = await hes.confirm_slip_placement(
            db, redis, settings, current_user.id, payload.report_id, payload.booking_code, datetime.now(UTC),
            placed_odds=None if payload.placed_odds is None else Decimal(str(payload.placed_odds)),
            placed_stake=None if payload.placed_stake_inr is None else Decimal(str(payload.placed_stake_inr)),
            override_odds_decay=payload.override_odds_decay,
        )
        await db.commit()
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    if not placement.replayed:
        ticket = placement.ticket
        await rms.announce(redis, "placement", current_user.id, ticket_reference=ticket.ticket_reference, stake=str(ticket.placed_stake),
                           odds=str(ticket.placed_odds), bookmaker=ticket.bookmaker, booking_code=ticket.bookmaker_slip_id)
    return hes.placement_dict(placement)


@router.get("/tickets/{ticket}/cashout-evaluation", response_model=CashoutEvaluationResponse, summary="Evaluate the Book's Cashout Offer")
async def evaluate_cashout_offer(
    ticket: str,
    request: Request,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
    offer_inr: Annotated[float, Query(gt=0, le=1e9, description="The bookmaker's live cashout offer, in rupees")],
) -> dict[str, Any]:
    try:
        evaluation = await lcs.evaluate(db, _redis(request), settings, current_user.id, ticket, Decimal(str(offer_inr)), datetime.now(UTC))
    except rms.RealMoneyError as exc:
        raise _fail(exc) from exc
    return evaluation.as_dict()


@router.post("/cashout", response_model=ExecuteCashoutResponse, summary="Record the Cashout Taken at the Book")
async def record_cashout(
    payload: ExecuteCashoutRequest,
    request: Request,
    db: Db,
    settings: AppSettings,
    current_user: CurrentUser,
) -> dict[str, Any]:
    try:
        cashout = await lcs.execute_cashout(db, settings, current_user.id, payload.ticket_reference, Decimal(str(payload.cashout_amount_inr)),
                                            payload.reason, datetime.now(UTC))
        await db.commit()
    except rms.RealMoneyError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    t = cashout.ticket
    await rms.announce(_redis(request), "cashout", current_user.id, ticket_reference=t.ticket_reference, gross=str(t.gross_return),
                       tds=str(t.tds_deducted), payout=str(t.settlement_payout), net_profit=str(t.net_profit))
    return cashout.as_dict()


@router.post("/reconcile", response_model=VaultReconciliationResponse, summary="Reconcile the Vault")
async def reconcile_vault(request: Request, db: Db, current_user: CurrentUser) -> dict[str, Any]:
    report = await vrs.reconcile(db, current_user.id, datetime.now(UTC))
    await db.commit()
    if report["adjusted"]:
        await rms.announce(_redis(request), "reconcile", current_user.id, exposure_drift=report["exposure_drift"])
    return report
