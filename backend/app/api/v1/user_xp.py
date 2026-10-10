"""Experience (FA-2) under ``/api/v1/user/xp`` (Group 75).

    GET /user/xp/profile    your XP, rank, progress to the next rank, what each action earns, the fleet's total
    GET /user/xp/history    your awards, newest first
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.never_forget import UserXPProfile, XPAuditLog
from app.services.twin import xp_engine
from app.services.twin.vetting import developer_credit

router = APIRouter(prefix="/user/xp", tags=["Experience (FA-2)"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


@router.get("/profile")
async def profile(user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    async with sessions() as session:
        row = (await session.execute(select(UserXPProfile).where(UserXPProfile.user_id == user.id))).scalar_one_or_none()
        fleet = await xp_engine.fleet_xp(session)
        credit = await developer_credit(session)
    view = xp_engine.profile_view(row, user.id, settings, fleet, credit)
    view["as_of"] = datetime.now(UTC).isoformat()
    return view


@router.get("/history")
async def history(user: CurrentUser, sessions: Sessions, limit: int = Query(30, ge=1, le=200)) -> list[dict[str, Any]]:
    async with sessions() as session:
        rows = (await session.execute(
            select(XPAuditLog).join(UserXPProfile, UserXPProfile.id == XPAuditLog.profile_id).where(UserXPProfile.user_id == user.id)
            .order_by(XPAuditLog.created_at.desc(), XPAuditLog.id).limit(limit)
        )).scalars()
        return [xp_engine.log_view(r) for r in rows]
