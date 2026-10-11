"""
Paper Trading Sandbox & Currency API Router (Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Mounted under ``/paper-trading``. Every route needs a signed-in user and reads or writes that user's own sandbox;
forcing a provider refresh of the exchange rates is administrator-only.

* ``GET  /account``; ``POST /reset``: the sandbox (₹1,00,000 by default) and its one-click fresh slate
* ``POST /place-bet`` (201; idempotent per ``client_ref``): a single or a parlay, executed by ASHVATTHAMA on the target
  book (Parimatch by default) at the live price it can get
* ``POST /place-oracle-slip``: an Ashoka slip in the Oracle's market grammar, idempotent per slip id
* ``GET  /bets``; ``POST /bets/{id}/settle`` (a hand settlement from the match's final result);
  ``POST /bets/{id}/live-check`` (FA-1: the live probability, the stop-loss shield, the cash-out alert);
  ``POST /bets/{id}/cashout``
* ``GET  /performance``: ROI, win rate on decided bets, drawdown, profit factor, Sharpe, the forward-test's significance
* ``GET  /currency/rate``, ``/currency/convert``, ``/currency/board``; ``POST /currency/refresh`` (admin): TODAR MAL's FX
* ``GET  /tax/estimate``, ``/tax/ledger``: Section 115BBJ (30% TDS on each bet's net winnings)
* ``POST /execution/live-ticket``: the real-money placement ticket (nothing is placed automatically)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, CurrentUser, get_db
from app.core.config import Settings, get_settings
from app.domain.sports.errors import SportNotFoundError
from app.models.paper_trading import PaperBet, PaperBetSource, PaperBetStatus
from app.schemas.paper_trading import (
    CashOutRequest,
    ConversionRead,
    CurrencyBoardRead,
    CurrencyRateRead,
    ExecutionReceiptRead,
    LiveCheckRequest,
    LiveCheckResponse,
    LiveTicketRequest,
    OracleSlipCreate,
    PaperAccountRead,
    PaperBetCreate,
    PaperBetRead,
    PaperPerformanceStats,
    PaperSettleRequest,
    ResetRequest,
    TaxEstimateResponse,
    TaxLedgerRead,
)
from app.services import ashvatthama, sports_twin
from app.services import paper_trading_service as pts
from app.services.currency_service import CurrencyService, CurrencyUnavailableError

router = APIRouter(prefix="/paper-trading", tags=["Paper Trading Sandbox"])

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
SIDES = frozenset({"home", "away", "draw"})

ERROR_STATUS: dict[str, int] = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "TRADING_HALTED": status.HTTP_423_LOCKED,
    "PRICE_MOVED": status.HTTP_409_CONFLICT,
    "MARKET_SUSPENDED": status.HTTP_409_CONFLICT,
    "ALREADY_SETTLED": status.HTTP_409_CONFLICT,
    "ACCOUNT_INACTIVE": status.HTTP_409_CONFLICT,
    "RESULT_MISSING": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "RESULT_INSUFFICIENT": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "UNKNOWN_SPORT": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "UNKNOWN_MARKET": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "UNKNOWN_VENUE": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _fail(exc: pts.PaperTradingError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.receipt is not None:
        detail["receipt"] = exc.receipt
    return HTTPException(ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST), detail)


def _fx_down(exc: CurrencyUnavailableError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _bet(bet: PaperBet, created: bool = True) -> PaperBetRead:
    out = PaperBetRead.model_validate(bet)
    out.created = created
    return out


# ================================================================ account
@router.get("/account", response_model=PaperAccountRead, summary="Get Paper Trading Account")
async def get_paper_account(db: Db, settings: AppSettings, current_user: CurrentUser) -> PaperAccountRead:
    """The user's virtual balance and counters (the sandbox opens with ₹1,00,000 on first read)."""
    account = await pts.get_or_create_account(db, settings, current_user.id)
    await db.commit()
    return PaperAccountRead.model_validate(account)


@router.post("/reset", response_model=PaperAccountRead, summary="Reset Virtual Bankroll")
async def reset_virtual_bankroll(db: Db, settings: AppSettings, current_user: CurrentUser,
                                 payload: ResetRequest | None = None) -> PaperAccountRead:
    """A fresh epoch at ₹1,00,000 (or ``initial_balance``); the old epoch's open bets are voided unpaid."""
    initial = None if payload is None or payload.initial_balance is None else Decimal(str(payload.initial_balance))
    try:
        account = await pts.reset_account(db, settings, current_user.id, initial)
    except pts.PaperTradingError as exc:
        raise _fail(exc) from exc
    return PaperAccountRead.model_validate(account)


# ================================================================ placing
@router.post("/place-bet", response_model=PaperBetRead, status_code=status.HTTP_201_CREATED, summary="Place Mock Bet")
async def place_mock_bet(payload: PaperBetCreate, request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> PaperBetRead:
    """Execute a paper bet with virtual INR at the target book's live odds (ASHVATTHAMA)."""
    legs = [pts.LegRequest(leg.sport_key, leg.match_id, leg.market_type, leg.selection, Decimal(str(leg.odds)), leg.line, dict(leg.params),
                           leg.event_name, leg.quote_market, leg.quote_selection) for leg in payload.leg_list()]
    place = pts.PlaceRequest(legs, Decimal(str(payload.stake_amount)), payload.target_bookmaker, payload.source, payload.strategy,
                             payload.fair_probability, payload.confidence_score, payload.client_ref,
                             payload.event_name if payload.legs is not None else None)
    try:
        bet, created = await pts.place_paper_bet(db, _redis(request), settings, current_user.id, place)
    except pts.PaperTradingError as exc:
        raise _fail(exc) from exc
    return _bet(bet, created)


@router.post("/place-oracle-slip", response_model=PaperBetRead, status_code=status.HTTP_201_CREATED, summary="Forward-test an Ashoka slip")
async def place_oracle_slip(payload: OracleSlipCreate, request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> PaperBetRead:
    """Ashoka's slip (Oracle market grammar) placed on the sandbox at the target book's live price."""
    try:
        legs = pts.oracle_legs([leg.model_dump() for leg in payload.legs])
        place = pts.PlaceRequest(legs, Decimal(str(payload.stake_amount)), payload.target_bookmaker, PaperBetSource.ASHOKA.value, payload.strategy,
                                 payload.fair_probability, payload.confidence_score, f"ashoka:{payload.slip_id}" if payload.slip_id else None,
                                 payload.event_name)
        bet, created = await pts.place_paper_bet(db, _redis(request), settings, current_user.id, place)
    except pts.PaperTradingError as exc:
        raise _fail(exc) from exc
    return _bet(bet, created)


# ================================================================ bets
@router.get("/bets", response_model=list[PaperBetRead], summary="List Paper Bets")
async def list_paper_bets(db: Db, settings: AppSettings, current_user: CurrentUser,
                          status_filter: Annotated[PaperBetStatus | None, Query(alias="status")] = None,
                          limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[PaperBetRead]:
    account = await pts.get_or_create_account(db, settings, current_user.id)
    await db.commit()
    rows = await pts.list_bets(db, account, status=None if status_filter is None else status_filter.value, limit=limit)
    return [_bet(b) for b in rows]


@router.post("/bets/{bet_id}/settle", response_model=PaperBetRead, summary="Settle a Paper Bet by Hand")
async def settle_bet(bet_id: uuid.UUID, payload: PaperSettleRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> PaperBetRead:
    """Settle from the match's final result through the sport adapter (markets the beat cannot settle from a score)."""
    try:
        if payload.match_result is not None:
            bet = await pts.settle_paper_bet(db, settings, bet_id, payload.match_result, user_id=current_user.id, notes=payload.settlement_notes)
        else:
            bet = await pts.settle_with_results(db, settings, bet_id, payload.results_by_match or {}, user_id=current_user.id,
                                                notes=payload.settlement_notes)
    except pts.PaperTradingError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    return _bet(bet)


def _side_of(bet: PaperBet, requested: str | None) -> str:
    if requested is not None:
        return requested
    if bet.structure == "SINGLE" and bet.market_type in {"moneyline", "match_winner", "1x2", "1x2_match_odds", "h2h"}:
        pick = bet.selection.strip().lower()
        if pick in SIDES:
            return pick
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name the backed side (home, away or draw) for this bet's live check")


@router.post("/bets/{bet_id}/live-check", response_model=LiveCheckResponse, summary="FA-1 Live Check")
async def live_check(bet_id: uuid.UUID, payload: LiveCheckRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> LiveCheckResponse:
    """The backed side's live probability from the sport adapter, the stop-loss shield's call and the cash-out alert
    (below ``PAPER_CASHOUT_ALERT_PROBABILITY``, 15%)."""
    try:
        bet = await pts.get_bet(db, bet_id, current_user.id)
    except pts.PaperTradingError as exc:
        raise _fail(exc) from exc
    if bet.status != PaperBetStatus.PENDING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "the paper bet is already settled")
    side = _side_of(bet, payload.side)
    try:
        adapter = pts.adapter_for(bet.sport_key)
        live = adapter.in_play_win_probability(payload.home_stats, payload.away_stats, payload.live_state, payload.context or {})
    except SportNotFoundError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message) from exc
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if side == "draw" and not adapter.has_draw:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{adapter.display_name} has no draw to back")
    entry = bet.fair_probability if bet.fair_probability is not None else min(0.99, 1.0 / float(bet.executed_odds))
    shield = sports_twin.shield_call(settings, live, selection=side, entry_probability=entry, stake_inr=float(bet.stake_amount),
                                     entry_odds=float(bet.executed_odds), stop_loss_pct=payload.stop_loss_pct, cashout_offer_inr=payload.cashout_offer_inr)
    live_p = live.probability_of(side)
    threshold = settings.PAPER_CASHOUT_ALERT_PROBABILITY
    alert = live_p < threshold or shield["advice"] == "CASH_OUT"
    message = (f"Cash out: {side} is down to {live_p:.1%} (alert below {threshold:.0%})" if live_p < threshold
               else f"Cash out: {shield['reason']}" if shield["advice"] == "CASH_OUT" else f"Hold: {side} at {live_p:.1%}")
    return LiveCheckResponse(bet_id=bet.id, sport_key=adapter.sport_key, side=side, home_win_prob=round(live.home_win_prob, 5),
                             away_win_prob=round(live.away_win_prob, 5), draw_prob=round(live.draw_prob, 5), live_probability=round(live_p, 5),
                             alert=alert, alert_threshold=threshold, shield=shield, message=message)


@router.post("/bets/{bet_id}/cashout", response_model=PaperBetRead, summary="Virtual Cash-Out")
async def cash_out(bet_id: uuid.UUID, payload: CashOutRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> PaperBetRead:
    try:
        bet = await pts.cash_out(db, settings, current_user.id, bet_id, Decimal(str(payload.amount)))
    except pts.PaperTradingError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    return _bet(bet)


@router.get("/performance", response_model=PaperPerformanceStats, summary="Paper Trading Performance")
async def get_paper_performance(db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    """Win rate (decided bets), ROI, P&L before and after TDS, drawdown, profit factor, Sharpe and significance."""
    return await pts.performance(db, settings, current_user.id)


# ================================================================ currency (TODAR MAL)
@router.get("/currency/rate", response_model=CurrencyRateRead, summary="Get Live FX Rate")
async def get_fx_rate(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser,
                      base: Annotated[str, Query(min_length=3, max_length=8)] = "USD",
                      quote: Annotated[str, Query(min_length=3, max_length=8)] = "INR") -> CurrencyRateRead:
    """Quote units per one base unit, from a fresh rate (never a guessed constant: 503 when none is fresh)."""
    service = CurrencyService(settings, _redis(request))
    try:
        rate = await service.get_exchange_rate(db, base, quote, datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CurrencyUnavailableError as exc:
        raise _fx_down(exc) from exc
    return CurrencyRateRead(base_currency=rate.base, quote_currency=rate.quote, rate=float(rate.rate), last_updated_at=rate.as_of, provider=rate.provider)


@router.get("/currency/convert", response_model=ConversionRead, summary="Convert an Amount")
async def convert(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser,
                  amount: Annotated[float, Query(ge=0, le=1e12)], from_currency: Annotated[str, Query(alias="from", min_length=3, max_length=8)] = "INR",
                  to_currency: Annotated[str, Query(alias="to", min_length=3, max_length=8)] = "USD") -> ConversionRead:
    service = CurrencyService(settings, _redis(request))
    try:
        converted, rate = await service.convert(db, Decimal(str(amount)), from_currency, to_currency, datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CurrencyUnavailableError as exc:
        raise _fx_down(exc) from exc
    return ConversionRead(amount=amount, from_currency=rate.base, to_currency=rate.quote, converted=float(converted), rate=float(rate.rate),
                          last_updated_at=rate.as_of, provider=rate.provider)


@router.get("/currency/board", response_model=CurrencyBoardRead, summary="INR per Unit of Every Supported Currency")
async def board(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    return await CurrencyService(settings, _redis(request)).board(db, datetime.now(UTC))


@router.post("/currency/refresh", response_model=CurrencyBoardRead, summary="Refresh the Rates from the Providers (admin)")
async def refresh(request: Request, db: Db, settings: AppSettings, admin: CurrentAdmin) -> dict[str, Any]:
    service = CurrencyService(settings, _redis(request))
    now = datetime.now(UTC)
    await service.refresh(db, now)
    return await service.board(db, now)


# ================================================================ tax (Section 115BBJ)
@router.get("/tax/estimate", response_model=TaxEstimateResponse, summary="Section 115BBJ 30% TDS Estimator")
async def estimate_indian_tds(settings: AppSettings, current_user: CurrentUser, gross_win: Annotated[float, Query(ge=0, le=1e12)],
                              stake: Annotated[float, Query(ge=0, le=1e12)]) -> TaxEstimateResponse:
    """30% TDS on the net winnings (``max(0, payout - stake)``) of one bet."""
    calc = CurrencyService(settings).calculate_indian_tds(gross_win, stake)
    return TaxEstimateResponse(gross_win=calc["gross_win"], stake=calc["stake"], gross_profit=calc["net_profit"], tds_rate_pct=calc["tds_rate_pct"],
                               tds_deducted=calc["tds_deducted"], net_take_home=calc["net_take_home"], cess=calc["cess"],
                               effective_rate_pct=calc["effective_rate_pct"])


@router.get("/tax/ledger", response_model=TaxLedgerRead, summary="The User's Section 115BBJ Ledger")
async def tax_ledger(db: Db, current_user: CurrentUser,
                     financial_year: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None) -> dict[str, Any]:
    return await pts.tax_ledger(db, current_user.id, financial_year)


# ================================================================ ASHVATTHAMA
@router.post("/execution/live-ticket", response_model=ExecutionReceiptRead, summary="Real-Money Placement Ticket (ASHVATTHAMA)")
async def live_ticket(payload: LiveTicketRequest, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    """The ticket for placing a real-money slip by hand on the venue: no bookmaker is automated."""
    try:
        receipt = ashvatthama.live_ticket(settings, target=payload.target_bookmaker, odds=Decimal(str(payload.odds)),
                                          stake_inr=Decimal(str(payload.stake_amount)), description=payload.description)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return receipt.as_dict()
