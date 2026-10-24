"""The street-smart synthesizer, the system splitter and TODAR MAL's daily market health (Group 115, points b.5, b.10,
b.12 and b.16). Mounted under ``/synthesizer`` for a signed-in user; every answer carries ``developer_name``.
Developed for Amit Ashok Kumar Patnaik.

* ``GET  /market-health``: today's score, tier and advice, evaluated now from the live board and the Wire
* ``POST /market-health/dispatch``: run the day's intimation now (admin; ``dry_run`` shows what it would send)
* ``POST /synthesize-street-smart``: the anchors toward the stake -> return target, at Parimatch or 1xBet
* ``POST /split-system-bets``: the chosen legs priced again and split into the Fortress, Growth and Moonshot tickets
* ``POST /record-system-tickets``: the same split, recorded as three ready-to-place real-money tickets (nothing locked)
* ``GET  /tickets``; ``POST /tickets/{reference}/cancel``: the user's split tickets; a recorded one not placed, cancelled

A ticket is placed by hand at the book and confirmed with its booking code on ``POST /real-money/tickets/{reference}/
confirm-handcrafted``. Nothing here places a bet or moves money. Refusals answer ``{"detail": {"code", "message", ...}}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser, get_db
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.schemas.synthesizer import (
    DEVELOPER_CREDIT,
    MarketHealthResponse,
    RecordedSplitResponse,
    StreetSmartSynthesizeRequest,
    StreetSmartSynthesizeResponse,
    SystemSplitRequest,
    SystemSplitResponse,
)
from app.services import live_sports_ingestion_service as live_ingest
from app.services import market_health_service as health
from app.services import street_smart_synthesizer as synth

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


async def _engineered_for(response: Response) -> None:
    response.headers["X-Engineered-For"] = DEVELOPER_CREDIT


router = APIRouter(prefix="/synthesizer", tags=["Street-Smart Synthesizer (Group 115)"], dependencies=[Depends(_engineered_for)])


def _redis(request: Request) -> Any:
    return getattr(request.app.state, "redis", None)


def _fail(exc: synth.SynthesisError) -> HTTPException:
    return HTTPException(exc.status_code, {"code": exc.reason, "message": exc.message, **exc.detail}, headers={"X-Engineered-For": DEVELOPER_CREDIT})


def _unavailable(exc: health.MarketHealthUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": "BOARD_UNAVAILABLE", "message": str(exc)}, headers={"X-Engineered-For": DEVELOPER_CREDIT})


@router.get("/market-health", response_model=MarketHealthResponse, summary="TODAR MAL's Daily Market Health")
async def market_health(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    try:
        snapshot, _ = await health.evaluate(db, _redis(request), settings, datetime.now(UTC))
    except health.MarketHealthUnavailable as exc:
        raise _unavailable(exc) from exc
    await db.commit()
    return health.view(snapshot, await health.delivered(db, snapshot.alert_id))


@router.post("/market-health/dispatch", summary="Run the Day's Market-Health Intimation (admin)")
async def dispatch_market_health(request: Request, sessions: Sessions, settings: AppSettings, admin: CurrentAdmin,  # noqa: ARG001
                                 dry_run: Annotated[bool, Query()] = True) -> dict[str, Any]:
    try:
        result = await health.intimate(sessions, _redis(request), settings, datetime.now(UTC), dry_run=dry_run)
    except health.MarketHealthUnavailable as exc:
        raise _unavailable(exc) from exc
    return {**result, "developer_name": DEVELOPER_CREDIT}


@router.post("/synthesize-street-smart", response_model=StreetSmartSynthesizeResponse, summary="Synthesize a Street-Smart Multi-Sport Slip")
async def synthesize(payload: StreetSmartSynthesizeRequest, request: Request, background: BackgroundTasks, settings: AppSettings,
                     current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    try:
        result = await synth.synthesize_for(_redis(request), settings, datetime.now(UTC), stake=Decimal(str(payload.target_stake_inr)),
                                          target_return=Decimal(str(payload.target_return_inr)), book=payload.bookmaker, max_legs=payload.max_legs,
                                          preferred_sports=payload.preferred_sports)
    except synth.SynthesisError as exc:
        if exc.reason in ("INVALID_TARGET", "BOARD_UNAVAILABLE"):  # Group 127: an empty board starts the fleet's guarded refresh
            background.add_task(live_ingest.nudge, getattr(request.app.state, "fleet_deps", None), fixtures_on_board=0)
        raise _fail(exc) from exc
    background.add_task(live_ingest.nudge, getattr(request.app.state, "fleet_deps", None), fixtures_on_board=result["anchors_available"])
    return result


@router.post("/split-system-bets", response_model=SystemSplitResponse, summary="Split a Slip into Fortress, Growth and Moonshot")
async def split_system_bets(payload: SystemSplitRequest, request: Request, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    try:
        result = await synth.split_for(_redis(request), settings, datetime.now(UTC), leg_ids=payload.leg_ids,
                                       session_stake=Decimal(str(payload.total_session_stake_inr)), book=payload.bookmaker)
    except synth.SynthesisError as exc:
        raise _fail(exc) from exc
    return synth.split_payload(result, payload.bookmaker)


@router.post("/record-system-tickets", response_model=RecordedSplitResponse, status_code=status.HTTP_201_CREATED, summary="Record the Three Tickets")
async def record_system_tickets(payload: SystemSplitRequest, request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:
    now = datetime.now(UTC)
    try:
        result = await synth.split_for(_redis(request), settings, now, leg_ids=payload.leg_ids, session_stake=Decimal(str(payload.total_session_stake_inr)),
                                       book=payload.bookmaker)
        tickets = await synth.record_split(db, settings, current_user.id, result, payload.bookmaker, now)
        await db.commit()
    except synth.SynthesisError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    references = [t.ticket_reference for t in tickets]
    return {**synth.split_payload(result, payload.bookmaker, references), "ticket_references": references,
            "message": f"Three tickets recorded for {ASHOKA_BOOKMAKERS.get(payload.bookmaker, payload.bookmaker)}: place each by hand, then confirm it "
                       "with the book's booking code to lock its stake."}


@router.get("/tickets", summary="Your System Tickets")
async def system_tickets(db: Db, settings: AppSettings, current_user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 30) -> dict[str, Any]:
    return {"tickets": await synth.system_tickets(db, settings, current_user.id, limit), "developer_name": DEVELOPER_CREDIT}


@router.post("/tickets/{reference}/cancel", summary="Cancel a Recorded Ticket Not Yet Placed")
async def cancel_system_ticket(reference: str, db: Db, current_user: CurrentUser) -> dict[str, Any]:
    try:
        ticket = await synth.cancel_ticket(db, current_user.id, reference, datetime.now(UTC))
        await db.commit()
    except synth.SynthesisError as exc:
        await db.rollback()
        raise _fail(exc) from exc
    return {"ticket_reference": ticket.ticket_reference, "status": ticket.status, "developer_name": DEVELOPER_CREDIT}
