"""The model recalibration engine under ``/api/v1/twin/calibration`` (Group 74).

    POST /twin/calibration/recalibrate      run the engine now (admin): score, classify, publish pillar 1's weights
    GET  /twin/calibration/weights          the weights in force, the pins, and each model's state from its latest run
    GET  /twin/calibration/history          per-model audits, newest first (?model_name=)
    GET  /twin/calibration/runs             the runs, newest first
    GET  /twin/calibration/runs/{run_id}    one run with every model's audit and the thresholds it used
    POST /twin/calibration/override         set one model's weight by hand, optionally pinned (admin)
    POST /twin/calibration/reset            clear every weight and pin: pillar 1 back to equal weights (admin)

The engine is the only writer of ``<TWIN_PREFIX>:model_weights``; every write here is recorded as a run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.feedback import REFERENCE_PREDICTORS
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit, RecalibrationTrigger
from app.schemas.calibration import ManualWeightOverrideRequest, WeightResetRequest
from app.services.twin import model_calibrator
from app.services.twin.intel import model_weights
from app.services.twin.vetting import developer_credit

router = APIRouter(prefix="/twin/calibration", tags=["Model Recalibration Engine"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _need_redis(request: Request) -> Redis:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "Pillar 1's weights live in Redis, which is unavailable"})
    return redis


def _busy(exc: model_calibrator.RecalibrationBusy) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {"reason": "RECALIBRATION_RUNNING", "message": str(exc)})


@router.post("/recalibrate")
async def recalibrate(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        run = await model_calibrator.recalibrate(sessions, _need_redis(request), settings, datetime.now(UTC), RecalibrationTrigger.ON_DEMAND_ADMIN, triggered_by=admin.id)
    except model_calibrator.RecalibrationBusy as exc:
        raise _busy(exc) from exc
    async with sessions() as session:
        audits = list((await session.execute(select(ModelWeightAudit).where(ModelWeightAudit.run_id == run.id).order_by(ModelWeightAudit.model_name))).scalars())
    return model_calibrator.run_view(run, audits)


@router.get("/weights")
async def weights(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    in_force = await model_weights(redis, settings) if redis is not None else {}
    pins = await model_calibrator.read_pins(redis, settings)
    meta = await model_calibrator.read_meta(redis, settings)
    async with sessions() as session:
        latest = await model_calibrator.latest_audits(session)
        last_run = (await session.execute(select(ModelRecalibrationRun).order_by(ModelRecalibrationRun.created_at.desc()).limit(1))).scalars().first()
        credit = await developer_credit(session)
    names = sorted({*in_force, *latest, *pins})
    return {
        "weights": in_force, "pins": pins, "equal_weights": not in_force,
        "models": [
            {"model_name": name, "weight_in_force": in_force.get(name), "pinned": name in pins,
             **({"status": latest[name].status, "status_reason": latest[name].status_reason, "sample_count": latest[name].sample_count,
                 "brier_skill_score": latest[name].brier_skill_score, "brier_score_90d": latest[name].brier_score_90d, "avg_clv_pct": latest[name].avg_clv_pct,
                 "audited_at": latest[name].created_at.isoformat()} if name in latest else {"status": None})}
            for name in names
        ],
        "published": meta, "last_run": None if last_run is None else model_calibrator.run_view(last_run),
        "benchmark": settings.TWIN_RECALIBRATION_BENCHMARK_MODEL, "bands": {k: list(v) for k, v in settings.TWIN_RECALIBRATION_WEIGHT_BANDS.items()},
        "developer_credit": credit,
    }


@router.get("/history")
async def history(user: CurrentUser, sessions: Sessions, model_name: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        query = select(ModelWeightAudit).order_by(ModelWeightAudit.created_at.desc(), ModelWeightAudit.model_name).limit(limit)
        if model_name:
            query = query.where(ModelWeightAudit.model_name == model_name)
        return [model_calibrator.audit_view(a) for a in (await session.execute(query)).scalars()]


@router.get("/runs")
async def runs(user: CurrentUser, sessions: Sessions, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        rows = (await session.execute(select(ModelRecalibrationRun).order_by(ModelRecalibrationRun.created_at.desc()).limit(limit))).scalars()
        return [model_calibrator.run_view(r) for r in rows]


@router.get("/runs/{run_id}")
async def run(run_id: uuid.UUID, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        row = await session.get(ModelRecalibrationRun, run_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such recalibration run")
        audits = list((await session.execute(select(ModelWeightAudit).where(ModelWeightAudit.run_id == run_id).order_by(ModelWeightAudit.model_name))).scalars())
    return model_calibrator.run_view(row, audits)


@router.post("/override")
async def override(body: ManualWeightOverrideRequest, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    if body.model_name in REFERENCE_PREDICTORS or body.model_name == settings.TWIN_RECALIBRATION_BENCHMARK_MODEL:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "REFERENCE_PREDICTOR", "message": f"{body.model_name} is a benchmark: it is measured, never weighted"})
    ceiling = max(hi for _, hi in settings.TWIN_RECALIBRATION_WEIGHT_BANDS.values())
    if body.weight > ceiling:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "WEIGHT_TOO_HIGH", "message": f"the highest weight any state may carry is {ceiling:g}"})
    try:
        row = await model_calibrator.override(sessions, _need_redis(request), settings, datetime.now(UTC), model=body.model_name, weight=body.weight,
                                              reason=body.reason, pin=body.pin, admin_id=admin.id)
    except model_calibrator.RecalibrationBusy as exc:
        raise _busy(exc) from exc
    return {**model_calibrator.run_view(row), "model_name": body.model_name, "weight": body.weight, "pinned": body.pin}


@router.post("/reset")
async def reset(body: WeightResetRequest, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        row = await model_calibrator.reset(sessions, _need_redis(request), settings, datetime.now(UTC), reason=body.reason, admin_id=admin.id)
    except model_calibrator.RecalibrationBusy as exc:
        raise _busy(exc) from exc
    return {**model_calibrator.run_view(row), "message": "every weight and pin cleared: pillar 1 weighs every model equally until the next run"}
