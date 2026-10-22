"""The BetDoc Consultant under ``/api/v1/consultant`` (Group 105, ASHVATTHAMA, points b.6 and b.13b). Developed for Amit
Ashok Kumar Patnaik. Every route needs a signed-in user; nothing is placed from here.

    POST /consultant/audit                  the Doctor, the Lawyer, the Consultant and TipMaster on the slip; the verdict sealed and recorded
    POST /consultant/apply-prescription     the same on the slip with one leg swapped (the Doctor's prescription)
    GET  /consultant/verdicts               your signed verdicts, newest first, each seal re-checked
    GET  /consultant/verdicts/{report_id}   one signed verdict as recorded, its seal re-checked
    GET  /consultant/rules-library          the Lawyer's rule book, with what you recorded from each book's own rules
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, get_db
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.schemas.backtest_and_parlay import InspectRequest
from app.schemas.consultant import ApplyPrescriptionRequest
from app.services import consultant
from app.services import manual_parlay as workbench
from app.services.twin import vetting

router = APIRouter(prefix="/consultant", tags=["BetDoc Consultant (ASHVATTHAMA)"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Db = Annotated[AsyncSession, Depends(get_db)]


def _need_redis(request: Request) -> Redis:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The live market lives in Redis, which is unavailable"})
    return redis


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, workbench.WorkbenchRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message})
    if isinstance(exc, vetting.TwinRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, **exc.detail})
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "INVALID", "message": str(exc)})


def _legs_fit(leg_ids: list[str], settings: Settings) -> None:
    if len(leg_ids) > settings.MANUAL_PARLAY_MAX_LEGS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "TOO_MANY_LEGS", "message": f"at most {settings.MANUAL_PARLAY_MAX_LEGS} legs"})


@router.post("/audit")
async def audit(body: InspectRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    """The full consultation: pathologies and prescription, settlement rules, capital at KUMBHA's stake, the crowd
    against BetDoc, and the sealed verdict (recorded)."""
    _legs_fit(body.leg_ids, settings)
    try:
        return await consultant.consult(sessions, _need_redis(request), settings, user.id, leg_ids=body.leg_ids, kind=body.kind, skin=body.skin,
                                        bankroll=body.bankroll_inr, now=datetime.now(UTC))
    except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValueError) as exc:
        raise _refused(exc) from exc


@router.post("/apply-prescription")
async def apply_prescription(body: ApplyPrescriptionRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    """The consultation of the slip with ``replace_leg_id`` swapped for ``with_leg_id``: the cure is re-examined from the
    live market like any slip, never taken on trust."""
    _legs_fit(body.leg_ids, settings)
    try:
        leg_ids = consultant.swap(body.leg_ids, body.replace_leg_id, body.with_leg_id)
        return await consultant.consult(sessions, _need_redis(request), settings, user.id, leg_ids=leg_ids, kind=body.kind, skin=body.skin,
                                        bankroll=body.bankroll_inr, now=datetime.now(UTC))
    except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValueError) as exc:
        raise _refused(exc) from exc


@router.get("/verdicts")
async def verdicts(user: CurrentUser, db: Db, settings: AppSettings, limit: Annotated[int | None, Query(ge=1, le=200)] = None) -> dict[str, Any]:
    rows = await consultant.history(db, settings, user.id, limit or settings.CONSULTANT_HISTORY_LIMIT)
    return {"verdicts": rows, "developer_credit": await vetting.developer_credit(db)}


@router.get("/verdicts/{report_id}")
async def verdict(report_id: uuid.UUID, user: CurrentUser, db: Db, settings: AppSettings) -> dict[str, Any]:
    try:
        return await consultant.verdict(db, settings, user.id, report_id)
    except workbench.WorkbenchRefusal as exc:
        raise _refused(exc) from exc


@router.get("/rules-library")
async def rules_library(user: CurrentUser, db: Db, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    return {**consultant.rules(settings), "developer_credit": await vetting.developer_credit(db)}
