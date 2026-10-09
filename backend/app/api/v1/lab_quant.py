"""The Lab's quantitative backtester under ``/api/v1/lab/quant`` (Group 66).

    GET    /lab/quant/dataset             what history is loaded (ticks, fixtures, span, books, FX)
    POST   /lab/quant/dataset/seed        generate and load the synthetic dataset (admin)
    GET    /lab/quant/bots                your Hive bots, each with whether it can be backtested
    POST   /lab/quant/backtests           queue a backtest (202)
    GET    /lab/quant/backtests           your backtests, newest first (summaries)
    GET    /lab/quant/backtests/{id}      one backtest with its full result
    DELETE /lab/quant/backtests/{id}      forget a finished backtest

Refusals carry ``{"reason", "message"}`` in ``detail``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.db.seed_historical_ticks import DEFAULT_START, describe, generate_dataset, seed_dataset
from app.models.hive_bots import BotStatus, TradingBot
from app.models.lab_quant import BacktestStatus, LabBacktestRun, LabFixture, LabFixtureResult, LabFxRate, LabOddsTick, ResultStatus
from app.schemas.lab_quant import BacktestDetail, BacktestParams, BacktestRead, DatasetSeedRequest
from app.services.backtesting.runner import BacktestError, prepare_bots, resolve_window, summary_of
from app.services.hive_pipeline import validate_pipeline
from app.services.hive_registry import registry_components
from app.services.venue_costs import TermsTable
from app.workers.lab_worker import dispatch

router = APIRouter(prefix="/lab/quant", tags=["The Lab · backtesting"])

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]
_LIVE = (BacktestStatus.QUEUED, BacktestStatus.RUNNING)


def _refuse(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(code, {"reason": reason, "message": message})


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _read(run: LabBacktestRun) -> BacktestRead:
    return BacktestRead(
        id=run.id, name=run.name, status=run.status, progress=run.progress, stage=run.stage, params=run.params, error=run.error,
        created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at, summary=summary_of(run.result if run.status == BacktestStatus.COMPLETED else None),
    )


async def _expire_stale(session: AsyncSession, settings: Settings, user_id: uuid.UUID) -> None:
    """A RUNNING run whose heartbeat stopped (its process died) is failed, not left spinning."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.LAB_RUN_STALE_SECONDS)
    rows = (await session.execute(select(LabBacktestRun).where(LabBacktestRun.user_id == user_id, LabBacktestRun.status.in_(_LIVE)))).scalars().all()
    changed = False
    for run in rows:
        beat = _aware(run.heartbeat_at) or _aware(run.created_at)
        if beat is not None and beat < cutoff:
            run.status, run.error, run.stage, run.finished_at = BacktestStatus.FAILED, "interrupted: the process running it stopped", "failed", datetime.now(UTC)
            changed = True
    if changed:
        await session.commit()


# ---------------------------------------------------------------- the dataset
@router.get("/dataset")
async def dataset(user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        ticks, first, last = (await session.execute(select(func.count(LabOddsTick.id), func.min(LabOddsTick.created_at), func.max(LabOddsTick.created_at)))).one()
        fixtures = await session.scalar(select(func.count()).select_from(LabFixture)) or 0
        results = dict((await session.execute(select(LabFixtureResult.status, func.count()).group_by(LabFixtureResult.status))).all())
        books = (await session.execute(select(LabOddsTick.bookmaker_id, LabOddsTick.currency, func.count()).group_by(LabOddsTick.bookmaker_id, LabOddsTick.currency).order_by(LabOddsTick.bookmaker_id))).all()
        fx = (await session.execute(select(LabFxRate.currency, func.count(), func.min(LabFxRate.created_at), func.max(LabFxRate.created_at)).group_by(LabFxRate.currency).order_by(LabFxRate.currency))).all()
        datasets = list((await session.execute(select(LabFixture.dataset).distinct())).scalars())
        sources = list((await session.execute(select(LabOddsTick.source).distinct())).scalars())
    terms = TermsTable(settings)
    return {
        "ticks": ticks, "fixtures": fixtures, "results": {str(k): v for k, v in results.items()}, "postponed": results.get(ResultStatus.POSTPONED, 0),
        "first_tick": _aware(first).isoformat() if first else None, "last_tick": _aware(last).isoformat() if last else None,
        "books": [{"bookmaker_id": b, "currency": c, "ticks": n, "commission": str(terms(b).commission)} for b, c, n in books],
        "fx": [{"currency": c, "fixings": n, "first": _aware(a).isoformat(), "last": _aware(z).isoformat()} for c, n, a, z in fx],
        "static_fx_rates": settings.LAB_STATIC_FX_RATES, "fx_max_age_hours": settings.LAB_FX_MAX_AGE_HOURS,
        "datasets": sorted(datasets), "sources": sorted(sources), "synthetic": "synthetic" in sources,
    }


@router.post("/dataset/seed", status_code=status.HTTP_201_CREATED)
async def seed(body: DatasetSeedRequest, admin: CurrentAdmin, sessions: SessionFactory) -> dict[str, Any]:  # noqa: ARG001
    start = (_aware(body.start) or datetime.combine(DEFAULT_START, datetime.min.time(), tzinfo=UTC)).date()
    data = generate_dataset(body.rows, seed=body.seed, start=start, days=body.days)
    async with sessions() as session:
        running = await session.scalar(select(func.count()).select_from(LabBacktestRun).where(LabBacktestRun.status.in_(_LIVE)))
        if running and body.replace:
            raise _refuse(409, "BACKTESTS_RUNNING", "A backtest is reading the history; replace it when none is running")
        try:
            counts = await seed_dataset(session, data, replace=body.replace)
        except RuntimeError as exc:
            raise _refuse(409, "DATASET_EXISTS", str(exc)) from exc
        await session.commit()
    return {**counts, "description": describe(data)}


@router.get("/bots")
async def bots(user: CurrentUser, sessions: SessionFactory) -> list[dict[str, Any]]:
    async with sessions() as session:
        registry = await registry_components(session)
        rows = (await session.execute(select(TradingBot).where(TradingBot.user_id == user.id, TradingBot.status != BotStatus.ARCHIVED).order_by(TradingBot.created_at))).scalars().all()
    return [
        {
            "id": str(b.id), "name": b.name, "status": str(b.status), "execution_mode": str(b.execution_mode), "kelly_multiplier": str(b.kelly_multiplier),
            "allocated_capital": str(b.allocated_capital), "math_models": b.math_models, "risk_models": b.risk_models, "target_bet_types": b.target_bet_types,
            "enable_order_slicing": b.enable_order_slicing, "problems": validate_pipeline(b.math_models or [], b.risk_models or [], b.target_bet_types or [], registry),
        }
        for b in rows
    ]


# ---------------------------------------------------------------- backtests
@router.post("/backtests", status_code=status.HTTP_202_ACCEPTED, response_model=BacktestRead)
async def create_backtest(body: BacktestParams, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> BacktestRead:
    async with sessions() as session:
        await _expire_stale(session, settings, user.id)
        live = await session.scalar(select(func.count()).select_from(LabBacktestRun).where(LabBacktestRun.user_id == user.id, LabBacktestRun.status.in_(_LIVE)))
        if (live or 0) >= settings.LAB_MAX_CONCURRENT_RUNS:
            raise _refuse(429, "TOO_MANY_BACKTESTS", f"{live} backtest(s) still running; at most {settings.LAB_MAX_CONCURRENT_RUNS} at a time")
        try:  # fail fast on what can be checked now: the bots, their pipelines, the window
            await prepare_bots(session, user.id, body)
            await resolve_window(session, body)
        except BacktestError as exc:
            raise _refuse(422, "BACKTEST_INVALID", str(exc)) from exc
        run = LabBacktestRun(user_id=user.id, name=body.name, status=BacktestStatus.QUEUED, params=body.model_dump(mode="json"), stage="queued", progress=0.0)
        session.add(run)
        await session.commit()
        await session.refresh(run)
    dispatch(run.id, settings, sessions)
    return _read(run)


@router.get("/backtests", response_model=list[BacktestRead])
async def list_backtests(user: CurrentUser, sessions: SessionFactory, settings: AppSettings, limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[BacktestRead]:
    async with sessions() as session:
        await _expire_stale(session, settings, user.id)
        rows = (await session.execute(select(LabBacktestRun).where(LabBacktestRun.user_id == user.id).order_by(LabBacktestRun.created_at.desc()).limit(limit))).scalars().all()
    return [_read(r) for r in rows]


@router.get("/backtests/{run_id}", response_model=BacktestDetail)
async def get_backtest(run_id: uuid.UUID, user: CurrentUser, sessions: SessionFactory) -> BacktestDetail:
    async with sessions() as session:
        run = await session.get(LabBacktestRun, run_id)
    if run is None or run.user_id != user.id:
        raise _refuse(404, "NOT_FOUND", "No such backtest")
    return BacktestDetail(**_read(run).model_dump(), result=run.result if run.status == BacktestStatus.COMPLETED else None)


@router.delete("/backtests/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backtest(run_id: uuid.UUID, user: CurrentUser, sessions: SessionFactory) -> Response:
    async with sessions() as session:
        run = await session.get(LabBacktestRun, run_id)
        if run is None or run.user_id != user.id:
            raise _refuse(404, "NOT_FOUND", "No such backtest")
        if run.status in _LIVE:
            raise _refuse(409, "BACKTEST_RUNNING", "A running backtest cannot be deleted")
        await session.execute(delete(LabBacktestRun).where(LabBacktestRun.id == run_id))
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
