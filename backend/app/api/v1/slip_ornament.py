"""The Bet-Slip ornament under ``/api/v1/slip-ornament`` (Group 106, ASHVATTHAMA, points b.7 and b.12). Developed for
Amit Ashok Kumar Patnaik. Every route needs a signed-in user; sending through a Sentinel channel is an administrator's
action (it reaches the administrators' own chats, phones and inboxes). Nothing is placed from here.

    GET  /slip-ornament/tickets                       your approved verdicts, each with its ticket number (a broken seal says so)
    GET  /slip-ornament/channels                      which Sentinel channels can carry a ticket now (Telegram, SMS, Email)
    GET  /slip-ornament/{report_id}                   the ticket: verdict, legs in the book's wording, money, seal; coupon text, deep links
    PUT  /slip-ornament/{report_id}/booking-code      the bookmaker's booking code once placed (null clears it)
    GET  /slip-ornament/{report_id}/export-docx       the editable Word ticket with the Consultant's diagnostic report
    POST /slip-ornament/{report_id}/send-telegram     the ticket to the Sentinel's Telegram chats (admin)
    POST /slip-ornament/{report_id}/send-sms          the ticket's SMS line to the Sentinel's Twilio numbers (admin)
    POST /slip-ornament/{report_id}/send-email        the HTML ticket to the Sentinel's email addresses (admin)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ADMIN_ROLE, CurrentAdmin, CurrentUser, get_db
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.domain.slip_ornament.docx import MEDIA_TYPE
from app.schemas.consultant import BookingCodeIn
from app.services import slip_ornament as ornament
from app.services.twin import vetting

router = APIRouter(prefix="/slip-ornament", tags=["Bet-Slip Ornament (ASHVATTHAMA)"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Db = Annotated[AsyncSession, Depends(get_db)]


def _vault(request: Request) -> VaultCrypto | None:
    return getattr(request.app.state, "vault", None)


def _refused(exc: ornament.OrnamentRefusal) -> HTTPException:
    return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message})


@router.get("/tickets")
async def tickets(user: CurrentUser, db: Db, settings: AppSettings, limit: Annotated[int | None, Query(ge=1, le=200)] = None) -> dict[str, Any]:
    rows = await ornament.tickets(db, settings, user.id, limit or settings.CONSULTANT_HISTORY_LIMIT, datetime.now(UTC))
    return {"tickets": rows, "developer_credit": await vetting.developer_credit(db)}


@router.get("/channels")
async def channels(request: Request, user: CurrentUser, db: Db) -> dict[str, Any]:
    if user.role != ADMIN_ROLE:
        return {"admin": False, "channels": {}, "message": "Sentinel channels reach the administrators' own chats, phones and inboxes: an administrator sends from here."}
    return {"admin": True, "channels": await ornament.channels(db, _vault(request))}


@router.get("/{report_id}")
async def ticket(report_id: uuid.UUID, user: CurrentUser, db: Db, settings: AppSettings) -> dict[str, Any]:
    try:
        return await ornament.view(db, settings, user.id, report_id, datetime.now(UTC))
    except ornament.OrnamentRefusal as exc:
        raise _refused(exc) from exc


@router.put("/{report_id}/booking-code")
async def booking_code(report_id: uuid.UUID, body: BookingCodeIn, user: CurrentUser, db: Db, settings: AppSettings) -> dict[str, Any]:
    try:
        return await ornament.record_booking(db, settings, user.id, report_id, body.booking_code, datetime.now(UTC))
    except ornament.OrnamentRefusal as exc:
        raise _refused(exc) from exc


@router.get("/{report_id}/export-docx", response_class=Response)
async def export_docx(report_id: uuid.UUID, user: CurrentUser, db: Db, settings: AppSettings) -> Response:
    try:
        filename, content = await ornament.export_docx(db, settings, user.id, report_id, datetime.now(UTC))
    except ornament.OrnamentRefusal as exc:
        raise _refused(exc) from exc
    return Response(content, media_type=MEDIA_TYPE, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


async def _send(via: str, report_id: uuid.UUID, request: Request, admin: Any, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, Any]:
    try:
        return await ornament.send(sessions, settings, _vault(request), admin.id, report_id, via, datetime.now(UTC))
    except ornament.OrnamentRefusal as exc:
        raise _refused(exc) from exc


@router.post("/{report_id}/send-telegram")
async def send_telegram(report_id: uuid.UUID, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    return await _send("telegram", report_id, request, admin, sessions, settings)


@router.post("/{report_id}/send-sms")
async def send_sms(report_id: uuid.UUID, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    return await _send("sms", report_id, request, admin, sessions, settings)


@router.post("/{report_id}/send-email")
async def send_email(report_id: uuid.UUID, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    return await _send("email", report_id, request, admin, sessions, settings)
