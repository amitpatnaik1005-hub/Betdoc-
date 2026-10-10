"""The manual parlay workbench and the in-play stop-loss shield under ``/api/v1/manual-parlay`` (Group 77).

    GET  /manual-parlay/board                           match KPI cards (?sport=Football ...), counts per sport
    GET  /manual-parlay/accounts                        the account books' balances as last recorded in the Vault (admin)
    POST /manual-parlay/inspect                         the parlay through the 15-pillar fortress at the skin's book: score, tier, advice, margins, stop-loss
    POST /manual-parlay/submit                          the inspected slip into Ashoka's ledger, stop-loss shield armed
    GET  /manual-parlay/live-shields                    your shields (?active_only=false for the fired ones too)
    POST /manual-parlay/emergency-cashout/{shield_id}   issue the cashout ticket now, or record the cashout you took

Live frames of every shield: the ``/api/v1/ws/inplay-shield`` socket (``app/api/v1/inplay_ws.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ADMIN_ROLE, CurrentUser, get_db
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.schemas.backtest_and_parlay import EmergencyCashoutRequest, InspectRequest, SubmitRequest
from app.services import manual_parlay as workbench
from app.services.twin import vetting

router = APIRouter(prefix="/manual-parlay", tags=["Manual Parlay Workbench"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Db = Annotated[AsyncSession, Depends(get_db)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _need_redis(request: Request) -> Redis:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The live market lives in Redis, which is unavailable"})
    return redis


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, workbench.WorkbenchRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message})
    if isinstance(exc, vetting.TwinRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, **exc.detail})
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "INVALID", "message": str(exc)})


@router.get("/board")
async def board(request: Request, user: CurrentUser, settings: AppSettings, sport: str | None = Query(None, max_length=40)) -> dict[str, Any]:  # noqa: ARG001
    if sport is not None and sport not in settings.MANUAL_PARLAY_SPORTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "UNKNOWN_SPORT", "message": f"sport is one of {', '.join(settings.MANUAL_PARLAY_SPORTS)}"})
    return await workbench.board(_need_redis(request), settings, datetime.now(UTC), sport=sport)


@router.get("/accounts")
async def accounts(user: CurrentUser, db: Db) -> dict[str, Any]:
    if user.role != ADMIN_ROLE:
        return {"accounts": {}, "visible": False, "message": "The Vault's balances are shown to administrators", "developer_credit": await vetting.developer_credit(db)}
    return {"accounts": await workbench.accounts(db), "visible": True, "developer_credit": await vetting.developer_credit(db)}


@router.post("/inspect")
async def inspect(body: InspectRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    if len(body.leg_ids) > settings.MANUAL_PARLAY_MAX_LEGS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "TOO_MANY_LEGS", "message": f"at most {settings.MANUAL_PARLAY_MAX_LEGS} legs"})
    try:
        result = await workbench.inspect(sessions, _need_redis(request), settings, user.id, leg_ids=body.leg_ids, kind=body.kind, skin=body.skin,
                                         bankroll=body.bankroll_inr, now=datetime.now(UTC))
    except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValueError) as exc:
        raise _refused(exc) from exc
    return result.payload


@router.post("/submit", status_code=status.HTTP_201_CREATED)
async def submit(body: SubmitRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        return await workbench.submit(sessions, _redis(request), settings, user.id, body.audit_id, skin=body.skin, stake_inr=body.stake_inr, placed_odds=body.placed_odds,
                                      booking_code=body.booking_code, stop_loss_pct=body.stop_loss_pct, now=datetime.now(UTC), placed_at=body.placed_at)
    except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValidationError, ValueError) as exc:
        raise _refused(exc) from exc


@router.get("/live-shields")
async def live_shields(user: CurrentUser, db: Db, active_only: bool = True) -> dict[str, Any]:
    return {"shields": await workbench.shields(db, user.id, active_only=active_only), "developer_credit": await vetting.developer_credit(db)}


@router.post("/emergency-cashout/{shield_id}")
async def emergency_cashout(shield_id: uuid.UUID, body: EmergencyCashoutRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        return await workbench.emergency_cashout(sessions, _redis(request), settings, user.id, shield_id, amount_inr=body.amount_inr, now=datetime.now(UTC))
    except (workbench.WorkbenchRefusal, ValueError) as exc:
        raise _refused(exc) from exc
