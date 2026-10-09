"""Nalanda, the tick lake and settlement warehouse, under ``/api/v1/nalanda`` (Group 67).

    GET  /nalanda/telemetry          storage, partitions, BRIN, dead tuples, Parquet, firehose, chain
    GET  /nalanda/ticks              raw line movements (filters, a time window, newest first)
    GET  /nalanda/candles            one-minute OHLC candles of a fixture
    GET  /nalanda/line               a fixture's line over a window: candles and raw ticks together
    GET  /nalanda/settlements        the warehouse (your own records; an administrator sees all)
    GET  /nalanda/settlements/{seq}  one record, with its hashes
    GET  /nalanda/verify-ledger      recompute the SHA-256 chain (cached 60s; an administrator may force)
    GET  /nalanda/rebuild            every bankroll account rebuilt from the archive alone (admin)
    GET  /nalanda/exports            the cold tier's manifest (admin)
    POST /nalanda/maintenance/{task} preallocate | vacuum | compress | mirror | anchor | verify (admin)

Every read runs on Nalanda's own read pool, read-only, with a capped ``work_mem`` and a statement
timeout (``app.services.nalanda_query``): forensic queries never take the firehose's connections.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.nalanda_lake import ColdExport, MaintenanceLog, SettlementArchive
from app.services import nalanda_query as q
from app.services.nalanda_chain import RECORD_KINDS, anchor_path, read_anchors, verify_chain
from app.services.nalanda_mirror import rebuild_financial_state, sweep
from app.services.nalanda_tiering import archive_root
from app.services.sentinel_watch import watch_ledger
from app.workers.nalanda_firehose import NalandaKeys
from app.workers.nalanda_maintenance import TASKS, run_maintenance

router = APIRouter(prefix="/nalanda", tags=["Nalanda · archive"])

AppSettings = Annotated[Settings, Depends(get_settings)]
WriteSessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
_VERIFY_CACHE_SECONDS = 60


def get_nalanda_reader(settings: AppSettings) -> async_sessionmaker[AsyncSession]:
    return q.read_sessions(settings)


ReadSessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_nalanda_reader)]


def _engine(factory: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    bind = factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise HTTPException(503, {"reason": "NO_ENGINE", "message": "The archive's database is not configured"})
    return bind


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _window(since: datetime | None, until: datetime | None, default_hours: int) -> tuple[datetime, datetime]:
    """Every lake query is bounded by time (the partition key): PostgreSQL reads only those weeks."""
    end = until or datetime.now(UTC)
    start = since or end - timedelta(hours=default_hours)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    if start >= end:
        raise HTTPException(422, {"reason": "BAD_WINDOW", "message": "since must be before until"})
    if end - start > timedelta(days=3 * 366):
        raise HTTPException(422, {"reason": "WINDOW_TOO_WIDE", "message": "at most three years per query"})
    return start, end


# ---------------------------------------------------------------- telemetry
@router.get("/telemetry")
async def telemetry(request: Request, user: CurrentUser, reader: ReadSessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    async with q.governed_session(reader, settings) as session:
        return await q.telemetry(session, _engine(reader), _redis(request), settings, archive_root(settings))


# ---------------------------------------------------------------- line movements
@router.get("/ticks")
async def ticks(
    user: CurrentUser, reader: ReadSessions, settings: AppSettings,  # noqa: ARG001
    fixture_id: str | None = None, market: str | None = None, selection: str | None = None, bookmaker_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None, include_anomalies: bool = True, limit: Annotated[int, Query(ge=1, le=5_000)] = 500,
) -> dict[str, Any]:
    start, end = _window(since, until, 24)
    async with q.governed_session(reader, settings) as session:
        rows = await q.ticks(session, fixture_id=fixture_id, market=market, selection=selection, bookmaker_id=bookmaker_id, since=start, until=end, include_anomalies=include_anomalies, limit=limit)
    return {"since": start.isoformat(), "until": end.isoformat(), "count": len(rows), "ticks": rows}


@router.get("/candles")
async def candles(
    user: CurrentUser, reader: ReadSessions, settings: AppSettings, fixture_id: str,  # noqa: ARG001
    market: str | None = None, selection: str | None = None, bookmaker_id: str | None = None, since: datetime | None = None, until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=20_000)] = 2_000,
) -> dict[str, Any]:
    start, end = _window(since, until, 24 * 90)
    async with q.governed_session(reader, settings) as session:
        rows = await q.candles(session, fixture_id=fixture_id, market=market, selection=selection, bookmaker_id=bookmaker_id, since=start, until=end, limit=limit)
    return {"since": start.isoformat(), "until": end.isoformat(), "count": len(rows), "candles": rows}


@router.get("/line")
async def line(
    user: CurrentUser, reader: ReadSessions, settings: AppSettings, fixture_id: str, market: str, selection: str,  # noqa: ARG001
    bookmaker_id: str | None = None, since: datetime | None = None, until: datetime | None = None,
) -> dict[str, Any]:
    """A fixture's line for The Lab: candles where the ticks were rolled up, raw ticks (ghost spikes
    left out) where they are still hot."""
    start, end = _window(since, until, 24 * 14)
    async with q.governed_session(reader, settings) as session:
        rolled = await q.candles(session, fixture_id=fixture_id, market=market, selection=selection, bookmaker_id=bookmaker_id, since=start, until=end, limit=20_000)
        raw = await q.ticks(session, fixture_id=fixture_id, market=market, selection=selection, bookmaker_id=bookmaker_id, since=start, until=end, include_anomalies=False, limit=5_000)
    return {"fixture_id": fixture_id, "market": market, "selection": selection, "since": start.isoformat(), "until": end.isoformat(), "candles": rolled, "ticks": list(reversed(raw))}


# ---------------------------------------------------------------- the warehouse
@router.get("/settlements")
async def settlements(
    user: CurrentUser, reader: ReadSessions, settings: AppSettings,
    kind: str | None = None, user_id: uuid.UUID | None = None, ledger_id: uuid.UUID | None = None, fixture_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None, before_seq: Annotated[int | None, Query(ge=2)] = None, limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    if kind is not None and kind not in RECORD_KINDS:
        raise HTTPException(422, {"reason": "UNKNOWN_KIND", "message": f"kind is one of {', '.join(RECORD_KINDS)}"})
    admin = user.role == "ADMIN"
    async with q.governed_session(reader, settings) as session:
        rows = await q.settlements(
            session, viewer=None if admin else user.id, kind=kind, user_id=user_id if admin else None, ledger_id=ledger_id, fixture_id=fixture_id,
            since=since, until=until, before_seq=before_seq, limit=limit,
        )
    return {"count": len(rows), "records": rows, "scope": "all" if admin else "own"}


@router.get("/settlements/{seq}")
async def settlement(seq: int, user: CurrentUser, reader: ReadSessions, settings: AppSettings) -> dict[str, Any]:
    async with q.governed_session(reader, settings) as session:
        row = (await session.execute(select(SettlementArchive).where(SettlementArchive.seq == seq))).scalars().first()
        if row is None or (user.role != "ADMIN" and row.user_id != user.id):
            raise HTTPException(404, {"reason": "NOT_FOUND", "message": "No such archive record"})
        return q.record_view(row)


@router.get("/verify-ledger")
async def verify_ledger(request: Request, user: CurrentUser, reader: ReadSessions, settings: AppSettings, fresh: bool = False) -> dict[str, Any]:
    """Recompute the whole chain. Any user may ask; a result younger than a minute is shared, and only
    an administrator can force a fresh walk inside that minute."""
    redis = _redis(request)
    key = NalandaKeys(settings).verify
    if redis is not None and not (fresh and user.role == "ADMIN"):
        try:
            cached = await redis.get(key)
        except (RedisError, OSError):
            cached = None
        if cached:
            report = json.loads(cached)
            age = (datetime.now(UTC) - datetime.fromisoformat(report["verified_at"])).total_seconds()
            if age < _VERIFY_CACHE_SECONDS:
                return {**report, "cached": True, "age_seconds": round(age, 1)}
    async with q.governed_session(reader, settings) as session:
        report = (await verify_chain(session, anchors=read_anchors(anchor_path(archive_root(settings))))).as_dict()
    if redis is not None:
        try:
            await redis.set(key, json.dumps(report))
        except (RedisError, OSError):
            pass
    await watch_ledger(redis, settings, report)  # a broken chain is the Sentinel's FATAL
    return {**report, "cached": False}


@router.get("/rebuild")
async def rebuild(admin: CurrentAdmin, writer: WriteSessions, reader: ReadSessions, settings: AppSettings, mirror_first: bool = True) -> dict[str, Any]:  # noqa: ARG001
    """Every account's balances from the archive's ledger postings alone, beside the live ledger.
    Mirrors first (by default), so what was committed a moment ago is in the comparison."""
    mirrored = await sweep(writer, now=datetime.now(UTC), overlap_seconds=settings.NALANDA_MIRROR_OVERLAP_SECONDS, page=settings.NALANDA_MIRROR_BATCH) if mirror_first else {}
    async with q.governed_session(reader, settings) as session:
        report = await rebuild_financial_state(session)
    return {**report, "mirrored_first": mirrored}


@router.get("/exports")
async def exports(admin: CurrentAdmin, reader: ReadSessions, settings: AppSettings, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:  # noqa: ARG001
    async with q.governed_session(reader, settings) as session:
        rows = (await session.execute(select(ColdExport).order_by(ColdExport.created_at.desc()).limit(limit))).scalars().all()
    return [
        {"id": str(r.id), "table": r.table_name, "partition": r.partition_name, "from": r.range_start.isoformat(), "to": r.range_end.isoformat(), "file": r.file_path,
         "rows": r.rows, "bytes": r.bytes, "sha256": r.sha256, "status": r.status, "mirror_target": r.mirror_target,
         "mirrored_at": r.mirrored_at.isoformat() if r.mirrored_at else None, "dropped_at": r.dropped_at.isoformat() if r.dropped_at else None, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/maintenance")
async def maintenance_log(admin: CurrentAdmin, reader: ReadSessions, settings: AppSettings, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:  # noqa: ARG001
    async with q.governed_session(reader, settings) as session:
        rows = (await session.execute(select(MaintenanceLog).order_by(MaintenanceLog.started_at.desc()).limit(limit))).scalars().all()
    return [{"task": r.task, "status": r.status, "detail": r.detail, "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]


@router.post("/maintenance/{task}")
async def maintenance(task: str, request: Request, admin: CurrentAdmin, writer: WriteSessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    if task not in TASKS:
        raise HTTPException(404, {"reason": "UNKNOWN_TASK", "message": f"task is one of {', '.join(TASKS)}"})
    return await run_maintenance(task, _engine(writer), writer, _redis(request), settings)
