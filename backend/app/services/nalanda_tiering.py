"""Nalanda's tiers: hot ticks, warm candles, cold Parquet.

* Rollup (``rollup_day``): a day of ticks older than ``NALANDA_ROLLUP_AFTER_DAYS`` becomes one-minute
  OHLC candles per (fixture, market, selection, source, book): open and close by the ticks' own
  observed time, ghost spikes and suspended quotes left out (counted in ``anomalies``). Idempotent:
  a day is recomputed whole and logged in ``nalanda_rollup_log``.
* Cold export (``export_cold``): a weekly tick partition entirely older than
  ``NALANDA_COLD_AFTER_DAYS`` is written to ``<NALANDA_ARCHIVE_DIR>/<table>/<partition>.parquet``
  (zstd), read back and checked (row count and a content digest computed on both sides), its file
  SHA-256 recorded in ``nalanda_cold_exports``, mirrored when a bucket is configured, and only then
  detached and dropped from PostgreSQL (``DROP`` frees a partition at once: no DELETE, no dead
  tuples, no vacuum). Candles follow after ``NALANDA_CANDLE_RETENTION_DAYS``.
* Archive backup (``backup_archive``): each closed week of the settlement warehouse is copied to
  Parquet, hashes and all, and stays in PostgreSQL: the financial record is never tiered away.

The file is written to ``<name>.parquet.tmp`` and renamed into place only after it verifies, so a
half-written file never sits under a final name. Off PostgreSQL (tests) the same export runs by time
range and the rows are deleted instead of dropped.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.nalanda_lake import ColdExport, NalandaCandle, NalandaTick, RollupLog, SettlementArchive
from app.services.nalanda_chain import canonical_json
from app.services.nalanda_partitions import drop_partition, parse_partition_week, partition_name, partitions_of, week_start

logger = logging.getLogger("betdoc.nalanda")
_ROW_GROUP = 50_000


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------- rollup
async def rollup_day(session: AsyncSession, day: date) -> dict[str, int]:
    """One day's ticks as one-minute candles (the caller commits)."""
    start, end = _day_bounds(day)
    await session.execute(delete(NalandaCandle).where(NalandaCandle.bucket_start >= start, NalandaCandle.bucket_start < end).execution_options(synchronize_session=False))
    query = (
        select(NalandaTick.created_at, NalandaTick.observed_at, NalandaTick.fixture_id, NalandaTick.market, NalandaTick.selection, NalandaTick.source,
               NalandaTick.bookmaker_id, NalandaTick.odds, NalandaTick.is_anomaly, NalandaTick.is_suspended)
        .where(NalandaTick.created_at >= start, NalandaTick.created_at < end)
        .order_by(NalandaTick.fixture_id, NalandaTick.market, NalandaTick.selection, NalandaTick.source, NalandaTick.bookmaker_id, NalandaTick.observed_at, NalandaTick.created_at)
    )
    candles: list[dict[str, Any]] = []
    ticks = anomalies = 0
    current: dict[str, Any] | None = None
    result = await session.stream(query.execution_options(yield_per=10_000))
    async for created, observed, fixture, market, selection, source, book, odds, anomaly, suspended in result:
        ticks += 1
        bucket = _aware(created).replace(second=0, microsecond=0)  # the partition clock: a day's rollup owns exactly that day's buckets
        key = (bucket, fixture, market, selection, source, book)
        if current is None or current["_key"] != key:
            if current is not None and current["ticks"]:  # a minute of nothing but ghost spikes has no price to show
                candles.append(current)
            current = {"_key": key, "bucket_start": bucket, "fixture_id": fixture, "market": market, "selection": selection, "source": source, "bookmaker_id": book,
                       "open": None, "high": None, "low": None, "close": None, "ticks": 0, "anomalies": 0, "created_at": datetime.now(UTC)}
        if anomaly:
            current["anomalies"] += 1
            anomalies += 1
            continue
        if suspended:
            continue
        price = Decimal(odds)
        if current["open"] is None:
            current["open"] = current["high"] = current["low"] = price
        current["high"], current["low"], current["close"] = max(current["high"], price), min(current["low"], price), price
        current["ticks"] += 1
    if current is not None and current["ticks"]:
        candles.append(current)
    rows = [{k: v for k, v in c.items() if k != "_key"} for c in candles]
    for i in range(0, len(rows), 5_000):
        await session.execute(insert(NalandaCandle), rows[i : i + 5_000])
    log = await session.get(RollupLog, day)
    if log is None:
        session.add(RollupLog(day=day, ticks=ticks, candles=len(rows), anomalies=anomalies, rolled_at=datetime.now(UTC)))
    else:
        log.ticks, log.candles, log.anomalies, log.rolled_at = ticks, len(rows), anomalies, datetime.now(UTC)
    return {"ticks": ticks, "candles": len(rows), "anomalies": anomalies}


async def rollup_due(session_factory: async_sessionmaker[AsyncSession], now: datetime, after_days: int, max_days: int = 14) -> dict[str, dict[str, int]]:
    """Roll up every day that is old enough and not rolled up yet, oldest first."""
    cutoff = (now - timedelta(days=after_days)).date()  # days strictly before this one are due
    async with session_factory() as session:
        last = await session.scalar(select(func.max(RollupLog.day)))
        first = last + timedelta(days=1) if last else None
        if first is None:
            oldest = await session.scalar(select(func.min(NalandaTick.created_at)))
            first = _aware(oldest).date() if oldest else None
    done: dict[str, dict[str, int]] = {}
    day = first
    while day is not None and day < cutoff and len(done) < max_days:
        async with session_factory() as session:
            done[day.isoformat()] = await rollup_day(session, day)
            await session.commit()
        day += timedelta(days=1)
    return done


# ---------------------------------------------------------------- Parquet
@dataclass(frozen=True, slots=True)
class TierSpec:
    model: Any
    column: str
    schema: pa.Schema
    order: tuple[str, ...]


def _ts() -> pa.DataType:
    return pa.timestamp("us", tz="UTC")


TICKS = TierSpec(
    NalandaTick, "created_at",
    pa.schema([("created_at", _ts()), ("observed_at", _ts()), ("fixture_id", pa.string()), ("market", pa.string()), ("selection", pa.string()), ("source", pa.string()),
               ("bookmaker_id", pa.string()), ("odds", pa.decimal128(10, 4)), ("is_suspended", pa.bool_()), ("is_anomaly", pa.bool_()), ("anomaly_z", pa.float64()), ("stream_id", pa.string())]),
    ("created_at", "stream_id", "bookmaker_id", "selection"),
)
CANDLES = TierSpec(
    NalandaCandle, "bucket_start",
    pa.schema([("bucket_start", _ts()), ("fixture_id", pa.string()), ("market", pa.string()), ("selection", pa.string()), ("source", pa.string()), ("bookmaker_id", pa.string()),
               ("open", pa.decimal128(10, 4)), ("high", pa.decimal128(10, 4)), ("low", pa.decimal128(10, 4)), ("close", pa.decimal128(10, 4)), ("ticks", pa.int64()),
               ("anomalies", pa.int64()), ("created_at", _ts())]),
    ("bucket_start", "fixture_id", "market", "selection", "source", "bookmaker_id"),
)
ARCHIVE = TierSpec(
    SettlementArchive, "created_at",
    pa.schema([("seq", pa.int64()), ("created_at", _ts()), ("record_kind", pa.string()), ("source", pa.string()), ("source_id", pa.string()), ("user_id", pa.string()),
               ("bot_id", pa.string()), ("ledger_id", pa.string()), ("fixture_id", pa.string()), ("amount_inr", pa.decimal128(18, 2)), ("occurred_at", _ts()),
               ("payload", pa.string()), ("prev_hash", pa.string()), ("row_hash", pa.string())]),
    ("seq",),
)


def _cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, dict | list):
        return canonical_json(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _digest_rows(digest: Any, rows: Sequence[dict[str, Any]], names: Sequence[str]) -> None:
    for row in rows:
        digest.update(repr(tuple(_normal(row[n]) for n in names)).encode())


def _normal(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat(timespec="microseconds")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 2.5000 from the database and 2.5 from Parquet are one number
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ExportedFile:
    path: Path
    rows: int
    bytes: int
    sha256: str


async def write_parquet(session: AsyncSession, spec: TierSpec, lower: datetime, upper: datetime, path: Path) -> ExportedFile | None:
    """Stream ``[lower, upper)`` of a table into a verified Parquet file; None when the range is empty."""
    names = spec.schema.names
    column = getattr(spec.model, spec.column)
    query = select(*(getattr(spec.model, n) for n in names)).where(column >= lower, column < upper).order_by(*(getattr(spec.model, n) for n in spec.order))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    written, writer, digest = 0, None, hashlib.sha256()
    try:
        result = await session.stream(query.execution_options(yield_per=_ROW_GROUP))
        batch: list[dict[str, Any]] = []
        async for row in result:
            batch.append({n: _cell(v) for n, v in zip(names, row, strict=True)})
            if len(batch) >= _ROW_GROUP:
                writer = writer or pq.ParquetWriter(tmp, spec.schema, compression="zstd")
                writer.write_table(pa.Table.from_pylist(batch, schema=spec.schema))
                _digest_rows(digest, batch, names)
                written += len(batch)
                batch = []
        if batch:
            writer = writer or pq.ParquetWriter(tmp, spec.schema, compression="zstd")
            writer.write_table(pa.Table.from_pylist(batch, schema=spec.schema))
            _digest_rows(digest, batch, names)
            written += len(batch)
    finally:
        if writer is not None:
            writer.close()
    if written == 0:
        tmp.unlink(missing_ok=True)
        return None
    # Read it back: every row, the same content, before the database lets go of anything
    check = hashlib.sha256()
    table = pq.read_table(tmp)
    if table.num_rows != written:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{path.name}: wrote {written} rows, read back {table.num_rows}")
    for i in range(0, table.num_rows, _ROW_GROUP):
        _digest_rows(check, table.slice(i, _ROW_GROUP).to_pylist(), names)
    if check.hexdigest() != digest.hexdigest():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{path.name}: the file read back differs from what was written")
    os.replace(tmp, path)
    return ExportedFile(path, written, path.stat().st_size, _sha256(path))


# ---------------------------------------------------------------- S3 mirror (optional)
class S3Mirror:
    """Uploads to ``s3://<NALANDA_S3_BUCKET>/<NALANDA_S3_PREFIX>/...`` when a bucket is configured and
    boto3 is installed; otherwise ``available`` is False and nothing leaves this machine."""

    def __init__(self, settings: Settings) -> None:
        self.bucket, self.prefix = settings.NALANDA_S3_BUCKET, settings.NALANDA_S3_PREFIX.strip("/")
        try:
            import boto3  # noqa: PLC0415 - optional dependency

            self._client = boto3.client("s3") if self.bucket else None
        except ImportError:
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def key_for(self, path: Path, archive_dir: Path) -> str:
        return f"{self.prefix}/{path.relative_to(archive_dir).as_posix()}"

    def upload(self, path: Path, archive_dir: Path, sha256: str) -> str:
        if self._client is None:
            raise RuntimeError("no S3 mirror configured")
        key = self.key_for(path, archive_dir)
        self._client.upload_file(str(path), self.bucket, key, ExtraArgs={"Metadata": {"sha256": sha256}})
        return f"s3://{self.bucket}/{key}"


# ---------------------------------------------------------------- the cold tier
def archive_root(settings: Settings) -> Path:
    root = Path(settings.NALANDA_ARCHIVE_DIR)
    return root if root.is_absolute() else Path(__file__).resolve().parents[2] / root


async def _export_one(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, settings: Settings, spec: TierSpec, lower: datetime, upper: datetime,
    partition: str | None, root: Path, mirror: S3Mirror, *, drop: bool,
) -> dict[str, Any]:
    table = spec.model.__tablename__
    stem = partition or f"{table}_{lower:%Y%m%d}_{upper:%Y%m%d}"
    path = root / table / f"{stem}.parquet"
    async with session_factory() as session:
        exported = await write_parquet(session, spec, lower, upper, path)
    outcome: dict[str, Any] = {"table": table, "partition": partition, "from": lower.isoformat(), "to": upper.isoformat(), "rows": 0}
    if exported is not None:
        outcome.update(rows=exported.rows, bytes=exported.bytes, sha256=exported.sha256, file=str(exported.path))
        target = mirrored_at = None
        if mirror.available:
            target, mirrored_at = mirror.upload(exported.path, root, exported.sha256), datetime.now(UTC)
        async with session_factory() as session:
            manifest = ColdExport(
                table_name=table, partition_name=partition, range_start=lower, range_end=upper, file_path=str(exported.path), rows=exported.rows, bytes=exported.bytes,
                sha256=exported.sha256, status="EXPORTED" if drop else "BACKUP", mirror_target=target, mirrored_at=mirrored_at,
            )
            session.add(manifest)
            await session.commit()
            manifest_id = manifest.id
        outcome["mirrored"] = target
        if drop and settings.NALANDA_REQUIRE_MIRROR_BEFORE_DROP and target is None:
            outcome["kept"] = "NALANDA_REQUIRE_MIRROR_BEFORE_DROP is on and no mirror is configured"
            return outcome
    if not drop:
        return outcome
    if partition is not None and engine.dialect.name == "postgresql":
        async with engine.begin() as conn:
            await drop_partition(conn, table, partition)
    else:
        async with session_factory() as session:
            column = getattr(spec.model, spec.column)
            await session.execute(delete(spec.model).where(column >= lower, column < upper).execution_options(synchronize_session=False))
            await session.commit()
    if exported is not None:
        async with session_factory() as session:
            row = await session.get(ColdExport, manifest_id)
            if row is not None:
                row.status, row.dropped_at = "DROPPED", datetime.now(UTC)
                await session.commit()
    outcome["dropped"] = True
    return outcome


async def _cold_weeks(session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, spec: TierSpec, cutoff: datetime) -> list[tuple[datetime, datetime, str | None]]:
    """The weeks of a table entirely before ``cutoff``: its attached weekly partitions on PostgreSQL,
    otherwise the weeks its rows fall in."""
    table = spec.model.__tablename__
    if engine.dialect.name == "postgresql":
        async with engine.connect() as conn:
            parts = await partitions_of(conn, table)
        weeks = []
        for name, _ in parts:
            start = parse_partition_week(name)
            if start is not None and start + timedelta(days=7) <= cutoff:
                weeks.append((start, start + timedelta(days=7), name))
        return sorted(weeks)
    async with session_factory() as session:
        oldest = await session.scalar(select(func.min(getattr(spec.model, spec.column))))
    if oldest is None:
        return []
    weeks, start = [], week_start(_aware(oldest))
    while start + timedelta(days=7) <= cutoff:
        weeks.append((start, start + timedelta(days=7), None))
        start += timedelta(days=7)
    return weeks


async def export_cold(session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, settings: Settings, now: datetime, *, root: Path | None = None, mirror: S3Mirror | None = None) -> list[dict[str, Any]]:
    """Ticks past ``NALANDA_COLD_AFTER_DAYS`` and candles past ``NALANDA_CANDLE_RETENTION_DAYS``: to
    Parquet, verified, then out of PostgreSQL."""
    root = root or archive_root(settings)
    mirror = mirror or S3Mirror(settings)
    done = []
    for spec, days in ((TICKS, settings.NALANDA_COLD_AFTER_DAYS), (CANDLES, settings.NALANDA_CANDLE_RETENTION_DAYS)):
        for lower, upper, partition in await _cold_weeks(session_factory, engine, spec, now - timedelta(days=days)):
            done.append(await _export_one(session_factory, engine, settings, spec, lower, upper, partition, root, mirror, drop=True))
    return done


async def backup_archive(session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, settings: Settings, now: datetime, *, root: Path | None = None, weeks: int = 4) -> list[dict[str, Any]]:
    """Each of the last ``weeks`` closed weeks of the settlement warehouse to Parquet, once; nothing deleted."""
    root = root or archive_root(settings)
    mirror = S3Mirror(settings)
    done = []
    current = week_start(now)
    for i in range(weeks, 0, -1):
        lower = current - timedelta(days=7 * i)
        name = partition_name(ARCHIVE.model.__tablename__, lower)
        if (root / ARCHIVE.model.__tablename__ / f"{name}.parquet").exists():
            continue
        done.append(await _export_one(session_factory, engine, settings, ARCHIVE, lower, lower + timedelta(days=7), name, root, mirror, drop=False))
    return done


def disk_usage(root: Path) -> dict[str, Any]:
    files = [p for p in root.rglob("*.parquet")] if root.exists() else []
    by_table: dict[str, dict[str, int]] = {}
    for p in files:
        entry = by_table.setdefault(p.parent.name, {"files": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += p.stat().st_size
    return {"root": str(root), "files": len(files), "bytes": sum(e["bytes"] for e in by_table.values()), "by_table": by_table}

