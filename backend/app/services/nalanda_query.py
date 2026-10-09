"""Nalanda's read side (CQRS): forensic queries that can never choke the firehose.

Writes (the firehose, the mirror, the maintenance tasks) use the application's engine. Reads use
``read_sessions``: their own small pool (``NALANDA_READ_POOL_SIZE``, no overflow), optionally on a
replica (``NALANDA_READ_DATABASE_URL``), so a burst of reads queues for its own connections instead of
taking the writers'. Every read transaction is then ``governed``:

* ``SET TRANSACTION READ ONLY``: a read can never write, whatever its SQL;
* ``work_mem`` (``NALANDA_READ_WORK_MEM``): each sort or hash of a three-year scan spills to disk past
  this instead of growing the backend's memory;
* ``statement_timeout`` (``NALANDA_READ_STATEMENT_TIMEOUT_MS``) and no parallel workers (each would
  take its own ``work_mem``).

All ``SET LOCAL``: they end with the transaction and never leak into the pool. Range queries filter on
the partition key, so PostgreSQL prunes to the weeks asked for and reads them through BRIN.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models.nalanda_lake import ChainState, ColdExport, MaintenanceLog, MirrorCursor, NalandaCandle, NalandaTick, RollupLog, SettlementArchive
from app.services.nalanda_chain import SETTLEMENT_CHAIN, anchor_path, read_anchors
from app.services.nalanda_partitions import partition_stats
from app.services.nalanda_tiering import disk_usage

_READ: dict[str, async_sessionmaker[AsyncSession]] = {}


def read_sessions(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """The read pool (created once per process)."""
    url = (settings.NALANDA_READ_DATABASE_URL or settings.DATABASE_URL).get_secret_value()
    factory = _READ.get(url)
    if factory is None:
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            kwargs.update(pool_size=settings.NALANDA_READ_POOL_SIZE, max_overflow=0, pool_timeout=10)
        factory = _READ[url] = async_sessionmaker(create_async_engine(url, **kwargs), expire_on_commit=False)
    return factory


async def govern(session: AsyncSession, settings: Settings) -> None:
    """Read-only, memory- and time-capped: the first statements of the transaction."""
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(
        text("SELECT set_config('work_mem', :work_mem, true), set_config('statement_timeout', :timeout, true), set_config('max_parallel_workers_per_gather', '0', true)"),
        {"work_mem": settings.NALANDA_READ_WORK_MEM, "timeout": str(settings.NALANDA_READ_STATEMENT_TIMEOUT_MS)},
    )


@asynccontextmanager
async def governed_session(factory: async_sessionmaker[AsyncSession], settings: Settings) -> AsyncIterator[AsyncSession]:
    async with factory() as session:  # closing hands the connection back, which ends (rolls back) the read-only transaction
        await govern(session, settings)
        yield session


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    m = _aware(moment)
    return None if m is None else m.isoformat()


# ---------------------------------------------------------------- line movements
async def ticks(
    session: AsyncSession, *, fixture_id: str | None = None, market: str | None = None, selection: str | None = None, bookmaker_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None, include_anomalies: bool = True, limit: int = 500,
) -> list[dict[str, Any]]:
    """Newest first. Bounded by time (the partition key) whenever a window is given."""
    query = select(NalandaTick)
    for column, value in ((NalandaTick.fixture_id, fixture_id), (NalandaTick.market, market), (NalandaTick.selection, selection), (NalandaTick.bookmaker_id, bookmaker_id)):
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(NalandaTick.created_at >= since)
    if until:
        query = query.where(NalandaTick.created_at < until)
    if not include_anomalies:
        query = query.where(NalandaTick.is_anomaly.is_(False))
    rows = (await session.execute(query.order_by(NalandaTick.created_at.desc(), NalandaTick.stream_id.desc()).limit(limit))).scalars().all()
    return [
        {"created_at": _iso(t.created_at), "observed_at": _iso(t.observed_at), "fixture_id": t.fixture_id, "market": t.market, "selection": t.selection, "source": t.source,
         "bookmaker_id": t.bookmaker_id, "odds": str(t.odds), "is_suspended": t.is_suspended, "is_anomaly": t.is_anomaly, "anomaly_z": t.anomaly_z, "stream_id": t.stream_id}
        for t in rows
    ]


async def candles(
    session: AsyncSession, *, fixture_id: str, market: str | None = None, selection: str | None = None, bookmaker_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None, limit: int = 2_000,
) -> list[dict[str, Any]]:
    query = select(NalandaCandle).where(NalandaCandle.fixture_id == fixture_id)
    for column, value in ((NalandaCandle.market, market), (NalandaCandle.selection, selection), (NalandaCandle.bookmaker_id, bookmaker_id)):
        if value:
            query = query.where(column == value)
    if since:
        query = query.where(NalandaCandle.bucket_start >= since)
    if until:
        query = query.where(NalandaCandle.bucket_start < until)
    rows = (await session.execute(query.order_by(NalandaCandle.bucket_start).limit(limit))).scalars().all()
    return [
        {"bucket_start": _iso(c.bucket_start), "market": c.market, "selection": c.selection, "source": c.source, "bookmaker_id": c.bookmaker_id, "open": str(c.open),
         "high": str(c.high), "low": str(c.low), "close": str(c.close), "ticks": c.ticks, "anomalies": c.anomalies}
        for c in rows
    ]


# ---------------------------------------------------------------- the warehouse
def record_view(row: SettlementArchive, *, payload: bool = True) -> dict[str, Any]:
    out = {
        "seq": row.seq, "created_at": _iso(row.created_at), "record_kind": row.record_kind, "source": row.source, "source_id": row.source_id,
        "user_id": None if row.user_id is None else str(row.user_id), "bot_id": None if row.bot_id is None else str(row.bot_id),
        "ledger_id": None if row.ledger_id is None else str(row.ledger_id), "fixture_id": row.fixture_id,
        "amount_inr": None if row.amount_inr is None else str(row.amount_inr), "occurred_at": _iso(row.occurred_at), "prev_hash": row.prev_hash, "row_hash": row.row_hash,
    }
    if payload:
        out["payload"] = row.payload
    return out


async def settlements(
    session: AsyncSession, *, viewer: uuid.UUID | None, kind: str | None = None, user_id: uuid.UUID | None = None, ledger_id: uuid.UUID | None = None,
    fixture_id: str | None = None, since: datetime | None = None, until: datetime | None = None, before_seq: int | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Newest first. ``viewer`` set: that user's records only (an administrator passes None to see all)."""
    query = select(SettlementArchive)
    owner = viewer if viewer is not None else user_id
    if owner is not None:
        query = query.where(SettlementArchive.user_id == owner)
    if kind:
        query = query.where(SettlementArchive.record_kind == kind)
    if ledger_id:
        query = query.where(SettlementArchive.ledger_id == ledger_id)
    if fixture_id:
        query = query.where(SettlementArchive.fixture_id == fixture_id)
    if since:
        query = query.where(SettlementArchive.created_at >= since)
    if until:
        query = query.where(SettlementArchive.created_at < until)
    if before_seq:
        query = query.where(SettlementArchive.seq < before_seq)
    rows = (await session.execute(query.order_by(SettlementArchive.seq.desc()).limit(limit))).scalars().all()
    return [record_view(r) for r in rows]


# ---------------------------------------------------------------- telemetry
async def _storage(engine: AsyncEngine) -> dict[str, Any]:
    if engine.dialect.name != "postgresql":
        return {"database_bytes": None, "tables": {}, "brin": {}}
    async with engine.connect() as conn:
        database = await conn.scalar(text("SELECT pg_database_size(current_database())"))
        tables = (
            await conn.execute(
                text(
                    "SELECT p.relname, count(*), coalesce(sum(pg_total_relation_size(c.oid)), 0), coalesce(sum(greatest(c.reltuples, 0)), 0)::bigint "
                    "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname LIKE 'nalanda\\_%' GROUP BY p.relname"
                )
            )
        ).all()
        brin = (
            await conn.execute(
                text(
                    "SELECT p.relname, coalesce(sum(pg_relation_size(ix.indexrelid)), 0), coalesce(sum(pg_relation_size(c.oid)), 0) "
                    "FROM pg_index ix JOIN pg_class ic ON ic.oid = ix.indexrelid JOIN pg_am am ON am.oid = ic.relam JOIN pg_class c ON c.oid = ix.indrelid "
                    "JOIN pg_inherits i ON i.inhrelid = c.oid JOIN pg_class p ON p.oid = i.inhparent WHERE am.amname = 'brin' AND p.relname LIKE 'nalanda\\_%' GROUP BY p.relname"
                )
            )
        ).all()
    return {
        "database_bytes": int(database or 0),
        "tables": {name: {"partitions": int(n), "bytes": int(size), "rows_estimate": int(rows)} for name, n, size, rows in tables},
        "brin": {
            name: {"index_bytes": int(ix), "heap_bytes": int(heap), "heap_per_index_byte": round(int(heap) / int(ix), 1) if ix else None,
                   "index_pct_of_heap": round(int(ix) / int(heap) * 100, 4) if heap else None}
            for name, ix, heap in brin
        },
    }


async def _firehose(redis: Redis | None, settings: Settings) -> dict[str, Any]:
    from app.workers.nalanda_firehose import NalandaKeys  # noqa: PLC0415 - the worker module imports this one's siblings

    keys = NalandaKeys(settings)
    out: dict[str, Any] = {"available": False}
    if redis is None:
        return out
    try:
        length = await redis.xlen(keys.stream)
        groups = await redis.xinfo_groups(keys.stream) if length or await redis.exists(keys.stream) else []
        stats = await redis.hgetall(keys.stats)
        velocity = await redis.hgetall(keys.velocity)
        verify = await redis.get(keys.verify)
        leader = await redis.get(keys.leader)
    except (RedisError, OSError):
        return out
    now = int(datetime.now(UTC).timestamp())
    per_second = {int(k): int(v) for k, v in velocity.items() if k.isdigit()}
    series = [{"t": datetime.fromtimestamp(start, UTC).isoformat(), "rows": sum(per_second.get(s, 0) for s in range(start, start + 10))} for start in range(now - 600, now, 10)]
    group = next((g for g in groups if g.get("name") == keys.group), None)
    return {
        "available": True, "stream_length": int(length), "pending": None if group is None else int(group.get("pending", 0)),
        "lag": None if group is None or group.get("lag") is None else int(group["lag"]), "consumers": None if group is None else int(group.get("consumers", 0)),
        "leader": bool(leader), "totals": {k: (v if k == "last_batch_at" else int(v)) for k, v in stats.items()},
        "rows_per_second_1m": round(sum(v for s, v in per_second.items() if s >= now - 60) / 60, 2), "series": series,
        "last_verification": json.loads(verify) if verify else None,
    }


async def telemetry(session: AsyncSession, engine: AsyncEngine, redis: Redis | None, settings: Settings, root: Path) -> dict[str, Any]:
    head = (await session.execute(select(ChainState).where(ChainState.chain == SETTLEMENT_CHAIN))).scalar_one_or_none()
    kinds = dict((await session.execute(select(SettlementArchive.record_kind, func.count()).group_by(SettlementArchive.record_kind))).all())
    cursors = (await session.execute(select(MirrorCursor))).scalars().all()
    exports = (await session.execute(select(ColdExport.status, func.count(), func.coalesce(func.sum(ColdExport.rows), 0), func.coalesce(func.sum(ColdExport.bytes), 0)).group_by(ColdExport.status))).all()
    rollups = (await session.execute(select(func.count(), func.max(RollupLog.day), func.coalesce(func.sum(RollupLog.candles), 0)).select_from(RollupLog))).one()
    latest: dict[str, dict[str, Any]] = {}
    for log in (await session.execute(select(MaintenanceLog).order_by(MaintenanceLog.started_at.desc()).limit(200))).scalars():
        latest.setdefault(log.task, {"status": log.status, "started_at": _iso(log.started_at), "finished_at": _iso(log.finished_at), "detail": log.detail})
    partitions = await partition_stats(engine)
    since = datetime.now(UTC) - timedelta(days=1)
    recent = await session.scalar(select(func.count()).select_from(NalandaTick).where(NalandaTick.created_at >= since))
    anomalies = await session.scalar(select(func.count()).select_from(NalandaTick).where(NalandaTick.created_at >= since, NalandaTick.is_anomaly.is_(True)))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "storage": await _storage(engine),
        "partitions": partitions,
        "dead_tuples": sum(int(p["dead_tuples"]) for p in partitions),
        "default_partition_rows": sum(int(p["rows_estimate"]) for p in partitions if p["is_default"]),
        "ticks_24h": int(recent or 0), "anomalies_24h": int(anomalies or 0),
        "firehose": await _firehose(redis, settings),
        "chain": {
            "head_seq": head.last_seq if head else 0, "head_hash": head.last_hash if head else None, "head_at": _iso(head.last_created_at) if head else None,
            "records": {k: int(v) for k, v in kinds.items()}, "anchors": len(read_anchors(anchor_path(root))),
        },
        "mirror": [{"kind": c.record_kind, "watermark": _iso(c.watermark), "mirrored": c.mirrored, "updated_at": _iso(c.updated_at)} for c in cursors],
        "cold": {"parquet": disk_usage(root), "exports": {status: {"files": int(n), "rows": int(r), "bytes": int(b)} for status, n, r, b in exports}},
        "rollups": {"days": int(rollups[0] or 0), "last_day": rollups[1].isoformat() if rollups[1] else None, "candles": int(rollups[2] or 0)},
        "maintenance": latest,
        "settings": {
            "weeks_ahead": settings.NALANDA_WEEKS_AHEAD, "rollup_after_days": settings.NALANDA_ROLLUP_AFTER_DAYS, "cold_after_days": settings.NALANDA_COLD_AFTER_DAYS,
            "candle_retention_days": settings.NALANDA_CANDLE_RETENTION_DAYS, "read_work_mem": settings.NALANDA_READ_WORK_MEM, "s3_mirror": bool(settings.NALANDA_S3_BUCKET),
        },
    }
