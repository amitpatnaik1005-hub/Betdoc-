"""KUMBHA's capital growth under ``/api/v1/the-vault/cfo/growth`` (Group 76).

    GET   /the-vault/cfo/growth/policy                 the sizing policy, your bankroll, drawdown, regime, halt latch and skill
    GET   /the-vault/cfo/growth/strategies             every sizing strategy forecast over your own settled bets (same seed)
    POST  /the-vault/cfo/growth/simulate               a Monte Carlo forecast of one strategy (recorded)
    GET   /the-vault/cfo/growth/simulations            your recorded forecasts, newest first
    GET   /the-vault/cfo/growth/advisories             your advisories, newest first
    POST  /the-vault/cfo/growth/advisories/scan        observe your regime now
    POST  /the-vault/cfo/growth/advisories/{id}/ack    acknowledge; a halt needs an administrator's sign-off with a note
    GET   /the-vault/cfo/growth/rebalance              the venues' balances, EV flow, targets and transfers now; recorded transfers (admin)
    POST  /the-vault/cfo/growth/rebalance              record the current plan, superseding the pending one (admin)
    PATCH /the-vault/cfo/growth/rebalance/{id}         approve, mark executed or dismiss a transfer (admin)

Forecasts need ``CFO_MIN_HISTORY_BETS`` settled bets with a model probability (409 INSUFFICIENT_HISTORY otherwise):
nothing here runs on assumed win rates or odds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ADMIN_ROLE, CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.cfo import growth_math as gm
from app.models.cfo_growth import CFOAdvisoryLog, CFOGrowthSimulation, CFORebalanceRecommendation, InsightCode, RebalanceStatus
from app.schemas.cfo_growth import AcknowledgeRequest, RunSimulationRequest, TransferStatusRequest
from app.services.cfo import growth_optimizer as growth
from app.services.twin.intel import model_weights
from app.services.twin.vetting import developer_credit

router = APIRouter(prefix="/the-vault/cfo/growth", tags=["KUMBHA: Capital Growth"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, growth.NotEnoughHistory):
        return HTTPException(status.HTTP_409_CONFLICT, {"reason": "INSUFFICIENT_HISTORY", "message": str(exc), "found": exc.found, "needed": exc.needed})
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "INVALID", "message": str(exc)})


@router.get("/policy")
async def policy(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    rules = gm.SizingPolicy.from_settings(settings)
    weights = await model_weights(_redis(request), settings) if _redis(request) is not None else {}
    now = datetime.now(UTC)
    async with sessions() as session:
        wallet, drawdown = await growth.drawdown_state(session, user.id, settings, now)
        latched = await growth.halt_latched(session, user.id)
        skill = await growth.fleet_skill(session, weights)
        credit = await developer_credit(session)
    regime = gm.damper(drawdown, rules)
    if latched and not regime.halted:
        regime = gm.damper(drawdown, rules, latched=True)
    ceiling = rules.max_fraction * regime.multiplier
    return {
        "bankroll_inr": None if wallet is None else str(wallet), "drawdown": round(drawdown, 6), "window_days": settings.CFO_DRAWDOWN_WINDOW_DAYS,
        "regime": regime.name, "damper": regime.multiplier, "halt_latched": latched, "effective_ceiling": round(ceiling, 6),
        "stake_ceiling_inr": None if wallet is None else str(gm.round_stake(wallet, ceiling, settings.TWIN_STAKE_STEP_INR)),
        "skill_bss": None if skill is None else round(skill, 6), "skill_multiplier": round(gm.skill_multiplier(skill, rules), 4),
        "ruin_bound_halving": round(gm.ruin_probability(rules.kelly_fraction, 0.5), 6), "policy": rules.as_dict(),
        "horizons": list(settings.CFO_SIMULATION_HORIZONS), "default_paths": settings.CFO_MONTE_CARLO_PATHS,
        "strategies": [{"code": s.code, "label": s.label, "kelly": s.kelly, "fixed": s.fixed} for s in gm.strategies(rules)],
        "active_strategy": gm.active_code(rules, gm.strategies(rules)), "developer_credit": credit,
    }


@router.get("/strategies")
async def strategies(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        return await growth.compare(sessions, _redis(request), settings, user.id, datetime.now(UTC))
    except (growth.NotEnoughHistory, ValueError) as exc:
        raise _refused(exc) from exc


@router.post("/simulate", status_code=status.HTTP_201_CREATED)
async def simulate(body: RunSimulationRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        row = await growth.forecast(sessions, _redis(request), settings, user.id, datetime.now(UTC), strategy=body.strategy, horizon_days=body.horizon_days,
                                    paths=body.paths or settings.CFO_MONTE_CARLO_PATHS)
    except (growth.NotEnoughHistory, ValueError) as exc:
        raise _refused(exc) from exc
    return growth.simulation_view(row)


@router.get("/simulations")
async def simulations(user: CurrentUser, sessions: Sessions, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, Any]]:
    async with sessions() as session:
        rows = (await session.execute(
            select(CFOGrowthSimulation).where(CFOGrowthSimulation.user_id == user.id).order_by(CFOGrowthSimulation.created_at.desc(), CFOGrowthSimulation.id).limit(limit)
        )).scalars()
        return [growth.simulation_view(r) for r in rows]


@router.get("/advisories")
async def advisories(user: CurrentUser, sessions: Sessions, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    async with sessions() as session:
        rows = (await session.execute(
            select(CFOAdvisoryLog).where(CFOAdvisoryLog.user_id == user.id).order_by(CFOAdvisoryLog.created_at.desc(), CFOAdvisoryLog.id).limit(limit)
        )).scalars()
        return [growth.advisory_view(r) for r in rows]


@router.post("/advisories/scan")
async def scan_mine(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        wallet, drawdown = await growth.drawdown_state(session, user.id, settings, now)
    state = await growth.observe_regime(sessions, _redis(request), settings, user.id, drawdown, wallet, now)
    return {"regime": state.regime.name, "damper": state.regime.multiplier, "drawdown": round(drawdown, 6), "halt_latched": state.latched,
            "advisory": None if state.advisory is None else growth.advisory_view(state.advisory)}


@router.post("/advisories/{advisory_id}/ack")
async def acknowledge(advisory_id: uuid.UUID, body: AcknowledgeRequest, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        row = await session.get(CFOAdvisoryLog, advisory_id, with_for_update=True)
        if row is None or (row.user_id != user.id and user.role != ADMIN_ROLE):
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "ADVISORY_NOT_FOUND", "message": "No such advisory"})
        if row.is_acknowledged:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "ALREADY_ACKNOWLEDGED", "message": "Already acknowledged"})
        halts = [row]
        if row.insight_code == InsightCode.CAPITAL_PRESERVATION_HALT.value:
            if user.role != ADMIN_ROLE:
                raise HTTPException(status.HTTP_403_FORBIDDEN, {"reason": "SIGN_OFF_REQUIRED", "message": "A capital-preservation halt is signed off by an administrator"})
            if not body.note or len(body.note.strip()) < 5:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "SIGN_OFF_NOTE", "message": "A halt's sign-off needs a reason (5 characters or more)"})
            halts = list((await session.execute(
                select(CFOAdvisoryLog).where(CFOAdvisoryLog.user_id == row.user_id, CFOAdvisoryLog.insight_code == InsightCode.CAPITAL_PRESERVATION_HALT.value,
                                             CFOAdvisoryLog.is_acknowledged.is_(False)).with_for_update()
            )).scalars())
        for item in halts:
            await growth.acknowledge(session, item, user.id, None if body.note is None else body.note.strip(), now)
        await session.commit()
        return {**growth.advisory_view(row), "signed_off": len(halts) if row.insight_code == InsightCode.CAPITAL_PRESERVATION_HALT.value else 0}


@router.get("/rebalance")
async def rebalance(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        plan = await growth.rebalance_plan(session, _redis(request), settings, datetime.now(UTC))
        recorded = await growth.recent_transfers(session, limit)
        credit = await developer_credit(session)
    return {**plan, "recorded": [growth.transfer_view(r) for r in recorded], "developer_credit": credit}


@router.post("/rebalance", status_code=status.HTTP_201_CREATED)
async def record_rebalance(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    plan, rows = await growth.record_plan(sessions, _redis(request), settings, admin.id, datetime.now(UTC))
    return {**plan, "recorded": [growth.transfer_view(r) for r in rows]}


@router.patch("/rebalance/{transfer_id}")
async def transfer_status(transfer_id: uuid.UUID, body: TransferStatusRequest, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:
    async with sessions() as session:
        row = await session.get(CFORebalanceRecommendation, transfer_id, with_for_update=True)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "TRANSFER_NOT_FOUND", "message": "No such transfer"})
        try:
            await growth.set_transfer_status(session, row, RebalanceStatus(body.status), admin.id, body.note, datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "INVALID_TRANSITION", "message": str(exc)}) from exc
        await session.commit()
        return growth.transfer_view(row)
