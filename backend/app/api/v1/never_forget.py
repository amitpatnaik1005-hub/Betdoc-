"""The Never-Forget shield under ``/api/v1/twin/never-forget`` (Group 75).

    GET  /twin/never-forget/stats                    lessons by status, and the shield's measured record (fleet, and yours)
    GET  /twin/never-forget/memories                 memorised lost legs with their lesson, rule and record, newest first
    GET  /twin/never-forget/rules                    the rules (?status=ACTIVE|EXPERIMENTAL|ARCHIVED)
    GET  /twin/never-forget/preventions              legs pillar 15 turned away and how they finished (yours; ?all=true for administrators)
    POST /twin/never-forget/rules/{rule_id}/archive  retire a lesson (admin, with a reason)
    POST /twin/never-forget/rules/{rule_id}/activate make a lesson veto (admin, with a reason)

Lessons are fleet-wide: a leg one user lost guards every user's slips. They show the fixture, market, odds and
situation, never whose bet it was.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ADMIN_ROLE, CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import never_forget as nf
from app.models.never_forget import AshokaMistakeMemory, NeverForgetPreventionAudit, NeverForgetRule, RuleStatus
from app.schemas.never_forget import RuleStatusRequest
from app.services.twin import never_forget
from app.services.twin.vetting import developer_credit

router = APIRouter(prefix="/twin/never-forget", tags=["Never-Forget Shield"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


@router.get("/stats")
async def stats(user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    async with sessions() as session:
        by_status = dict((await session.execute(select(NeverForgetRule.status, func.count(NeverForgetRule.id)).group_by(NeverForgetRule.status))).all())
        memories = int(await session.scalar(select(func.count(AshokaMistakeMemory.id))) or 0)
        rows = list((await session.execute(select(NeverForgetPreventionAudit))).scalars())
        credit = await developer_credit(session)
    return {
        "enabled": settings.NEVER_FORGET_ENABLED, "total_mistakes_memorized": memories,
        "rules": {s.value: int(by_status.get(s.value, 0)) for s in RuleStatus},
        "fleet": never_forget.record_of(rows), "mine": never_forget.record_of([r for r in rows if r.user_id == user.id]),
        "policy": nf.NeverForgetPolicy.from_settings(settings).as_dict(), "developer_credit": credit,
    }


@router.get("/memories")
async def memories(user: CurrentUser, sessions: Sessions, limit: int = Query(30, ge=1, le=200)) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        rows = list((await session.execute(select(AshokaMistakeMemory).order_by(AshokaMistakeMemory.created_at.desc(), AshokaMistakeMemory.id).limit(limit))).scalars())
        rules = {r.mistake_id: r for r in (await session.execute(select(NeverForgetRule).where(NeverForgetRule.mistake_id.in_([m.id for m in rows])))).scalars()} if rows else {}
        records = await never_forget.records_by_mistake(session, [m.id for m in rows])
    return [never_forget.memory_view(m, rules.get(m.id), records.get(m.id)) for m in rows]


@router.get("/rules")
async def rules(user: CurrentUser, sessions: Sessions, rule_status: RuleStatus | None = Query(None, alias="status")) -> list[dict[str, Any]]:  # noqa: ARG001
    query = select(NeverForgetRule).order_by(NeverForgetRule.created_at.desc(), NeverForgetRule.id)
    if rule_status is not None:
        query = query.where(NeverForgetRule.status == rule_status.value)
    async with sessions() as session:
        return [never_forget.rule_view(r) for r in (await session.execute(query)).scalars()]


@router.get("/preventions")
async def preventions(user: CurrentUser, sessions: Sessions, limit: int = Query(30, ge=1, le=200), all_users: bool = Query(False, alias="all")) -> list[dict[str, Any]]:
    if all_users and user.role != ADMIN_ROLE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin privileges required to read every user's vetoes")
    query = (select(NeverForgetPreventionAudit, NeverForgetRule.rule_code).join(NeverForgetRule, NeverForgetRule.id == NeverForgetPreventionAudit.rule_id)
             .order_by(NeverForgetPreventionAudit.created_at.desc(), NeverForgetPreventionAudit.id).limit(limit))
    if not all_users:
        query = query.where(NeverForgetPreventionAudit.user_id == user.id)
    async with sessions() as session:
        return [never_forget.prevention_view(row, code) for row, code in (await session.execute(query)).all()]


async def _change(sessions: async_sessionmaker[AsyncSession], rule_id: uuid.UUID, target: RuleStatus, body: RuleStatusRequest, admin_id: uuid.UUID) -> dict[str, Any]:
    async with sessions() as session:
        rule = await session.get(NeverForgetRule, rule_id, with_for_update=True)
        if rule is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "RULE_NOT_FOUND", "message": "No such Never-Forget rule"})
        if rule.status == target.value:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "ALREADY_IN_STATE", "message": f"{rule.rule_code} is already {target.value}"})
        await never_forget.set_status(session, rule, target, body.reason.strip(), admin_id, datetime.now(UTC))
        await session.commit()
        return never_forget.rule_view(rule)


@router.post("/rules/{rule_id}/archive")
async def archive(rule_id: uuid.UUID, body: RuleStatusRequest, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:
    return await _change(sessions, rule_id, RuleStatus.ARCHIVED, body, admin.id)


@router.post("/rules/{rule_id}/activate")
async def activate(rule_id: uuid.UUID, body: RuleStatusRequest, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:
    return await _change(sessions, rule_id, RuleStatus.ACTIVE, body, admin.id)
