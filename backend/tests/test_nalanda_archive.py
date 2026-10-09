"""Group 67: Nalanda, the tick lake and the hash-chained settlement warehouse.

The brief's three proofs first: the SHA-256 chain catches every kind of tampering (an edited field, a
deleted row, a backdated row, a truncated tail, and a consistent rewrite of the whole chain, which only
the external anchor can catch) while the table itself refuses edits; the partition pre-allocator names
future weeks correctly (ISO years included) and creates them; the cold tier writes Parquet files that
read back row for row, and never lets go of a row it could not verify. Then the ghost-spike cleanser,
the firehose end to end, the ledger mirror and the rebuild, the read governor, and the API.

SQLite always; PostgreSQL too when ``TEST_POSTGRES_URL`` is set (partitions, BRIN, the trigger, the
read-only governor). Redis is a real server on a dedicated, flushed index (skipped when unreachable).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import Settings, get_settings
from app.models import BetLedger, ExchangeAccount, RiskMandate, User
from app.models.cfo_vault import AuditLog, BankrollAccount, LedgerEntry, MarketResult, PhantomLedger
from app.models.control_panel import SystemSettingsModel
from app.models.hive_bots import TradingBot
from app.models.nalanda_lake import (
    ChainState,
    ColdExport,
    MaintenanceLog,
    MirrorCursor,
    MirrorIndex,
    NalandaCandle,
    NalandaTick,
    RollupLog,
    SettlementArchive,
)
from app.schemas.aryabhata import BookQuote, MarketQuote
from app.services import nalanda_tiering
from app.services.cfo_ledger import OrderTicket, lock_bankroll, reserve, settle
from app.services.nalanda_chain import GENESIS_HASH, ArchiveRecord, anchor_path, append_records, canonical, hash_of_row, read_anchors, row_hash, verify_chain, write_anchor
from app.services.nalanda_cleanser import GhostSpikeFilter, Tick
from app.services.nalanda_mirror import rebuild_financial_state, sweep
from app.services.nalanda_partitions import create_sql, partition_name, partitions_of, preallocate, upcoming, week_start
from app.services.nalanda_query import governed_session
from app.services.nalanda_tiering import S3Mirror, export_cold, rollup_day, write_parquet
from app.workers.nalanda_firehose import NalandaFirehose, NalandaKeys, emit, emit_quotes, emit_record_soon

D = Decimal
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_SENTINEL = "betdoc:test-sentinel"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
TABLES = [
    User.__table__, ExchangeAccount.__table__, RiskMandate.__table__, BetLedger.__table__, SystemSettingsModel.__table__, TradingBot.__table__, BankrollAccount.__table__, PhantomLedger.__table__, LedgerEntry.__table__, AuditLog.__table__, MarketResult.__table__,
    NalandaTick.__table__, NalandaCandle.__table__, SettlementArchive.__table__, ChainState.__table__, MirrorIndex.__table__, MirrorCursor.__table__,
    ColdExport.__table__, RollupLog.__table__, MaintenanceLog.__table__,
]


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        md = _sqlite_metadata()
        from app.models.nalanda_lake import APPEND_ONLY_TRIGGERS_SQLITE  # noqa: PLC0415

        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)
            for statement in APPEND_ONLY_TRIGGERS_SQLITE:  # the copied metadata does not carry the DDL events
                await conn.execute(text(statement))
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


def is_pg(sessions: async_sessionmaker[AsyncSession]) -> bool:
    return sessions.kw["bind"].dialect.name == "postgresql"


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_SENTINEL):
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_SENTINEL, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return get_settings().model_copy(
        update={
            "NALANDA_PREFIX": "test_nalanda", "NALANDA_ARCHIVE_DIR": str(tmp_path / "archive"), "NALANDA_ENABLED": True,
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak", "CFO_KILL_SWITCH_KEY": "test:kill_switch", "starting_bankroll": 10_000.0,
        }
    )


def record(i: int, kind: str = "SETTLEMENT_RECEIPT", user: uuid.UUID | None = None, **payload: Any) -> ArchiveRecord:
    return ArchiveRecord(kind, "test", f"src-{i}", {"n": i, "stake": D("12.50"), "at": NOW, **payload}, user_id=user, amount_inr=D("12.5"), fixture_id="fx-ars-lee", occurred_at=NOW)


async def chain_of(sessions: async_sessionmaker[AsyncSession], n: int, **kwargs: Any) -> None:
    async with sessions() as session:
        await append_records(session, [record(i, **kwargs) for i in range(1, n + 1)], now=NOW)
        await session.commit()


async def verify(sessions: async_sessionmaker[AsyncSession], anchors: list[dict[str, Any]] | None = None) -> Any:
    async with sessions() as session:
        return await verify_chain(session, anchors=anchors or [])


async def bypass(sessions: async_sessionmaker[AsyncSession], *statements: str, params: dict[str, Any] | None = None) -> None:
    """Edit the warehouse the way an intruder with database access would: around its trigger."""
    async with sessions() as session:
        if is_pg(sessions):
            await session.execute(text("SET LOCAL session_replication_role = replica"))  # triggers off for this transaction
        else:
            await session.execute(text("DROP TRIGGER IF EXISTS nalanda_settlement_archive_no_update"))
            await session.execute(text("DROP TRIGGER IF EXISTS nalanda_settlement_archive_no_delete"))
        for statement in statements:
            await session.execute(text(statement), params or {})
        await session.commit()


# ================================================================ proof 1: the hash chain
@pytest.mark.asyncio
async def test_the_chain_links_every_row_and_verifies(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 5)
    async with sessions() as session:
        again = await append_records(session, [record(3), record(6)], now=NOW + timedelta(seconds=1))  # src-3 again: already archived
        await session.commit()
        rows = (await session.execute(select(SettlementArchive).order_by(SettlementArchive.seq))).scalars().all()
        head = await session.get(ChainState, "settlement")
    assert (again.appended, again.duplicates) == (1, 1)
    assert [r.seq for r in rows] == [1, 2, 3, 4, 5, 6] and rows[0].prev_hash == GENESIS_HASH
    assert all(b.prev_hash == a.row_hash for a, b in zip(rows, rows[1:]))
    assert all(hash_of_row(r) == r.row_hash for r in rows)
    # The hash is SHA-256 over the previous hash and the canonical header and payload, exactly
    first = rows[0]
    header = {"seq": "1", "created_at": NOW.isoformat(timespec="microseconds"), "record_kind": "SETTLEMENT_RECEIPT", "source": "test", "source_id": "src-1",
              "user_id": None, "bot_id": None, "ledger_id": None, "fixture_id": "fx-ars-lee", "amount_inr": "12.50", "occurred_at": NOW.isoformat(timespec="microseconds")}
    material = GENESIS_HASH + "\n" + json.dumps({"header": header, "payload": first.payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert first.row_hash == hashlib.sha256(material.encode()).hexdigest() == row_hash(GENESIS_HASH, header, first.payload)
    assert first.payload == {"n": "1", "stake": "12.50", "at": "2026-10-09T12:00:00.000000+00:00"}  # numbers and times in one spelling
    assert head is not None and (head.last_seq, head.last_hash) == (6, rows[-1].row_hash)
    report = await verify(sessions)
    assert report.ok and report.rows == 6 and report.failures == []


@pytest.mark.asyncio
async def test_the_warehouse_refuses_updates_and_deletes(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 2)
    for statement in ("UPDATE nalanda_settlement_archive SET source = 'forged' WHERE seq = 1", "DELETE FROM nalanda_settlement_archive WHERE seq = 2"):
        async with sessions() as session:
            with pytest.raises((DBAPIError, IntegrityError)) as caught:
                await session.execute(text(statement))
            assert "append-only" in str(caught.value)
            await session.rollback()
    assert (await verify(sessions)).ok


@pytest.mark.asyncio
async def test_an_edited_field_is_caught_at_its_row(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 6)
    await bypass(sessions, "UPDATE nalanda_settlement_archive SET amount_inr = 99999.00 WHERE seq = 4")
    report = await verify(sessions)
    assert not report.ok and [(f["seq"], f["problem"]) for f in report.failures] == [(4, "altered")]


@pytest.mark.asyncio
async def test_an_edited_payload_is_caught(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 3)
    forged = json.dumps({"n": "2", "stake": "1250.00", "at": "2026-10-09T12:00:00.000000+00:00"})
    await bypass(sessions, "UPDATE nalanda_settlement_archive SET payload = " + ("CAST(:p AS JSONB)" if is_pg(sessions) else ":p") + " WHERE seq = 2", params={"p": forged})
    report = await verify(sessions)
    assert [(f["seq"], f["problem"]) for f in report.failures] == [(2, "altered")]


@pytest.mark.asyncio
async def test_a_deleted_row_breaks_the_sequence_and_the_link(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 6)
    await bypass(sessions, "DELETE FROM nalanda_settlement_archive WHERE seq = 3")
    problems = {(f["seq"], f["problem"]) for f in (await verify(sessions)).failures}
    assert problems == {(4, "gap"), (4, "broken_link")}


@pytest.mark.asyncio
async def test_a_backdated_row_is_caught(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 3)
    async with sessions() as session:
        await append_records(session, [record(4)], now=NOW + timedelta(hours=1))
        await session.commit()
    await bypass(sessions, "UPDATE nalanda_settlement_archive SET created_at = :t WHERE seq = 4", params={"t": NOW - timedelta(days=30)})
    problems = {f["problem"] for f in (await verify(sessions)).failures if f["seq"] == 4}
    assert problems == {"altered", "backdated"}


@pytest.mark.asyncio
async def test_a_truncated_tail_is_caught_by_the_head(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chain_of(sessions, 5)
    await bypass(sessions, "DELETE FROM nalanda_settlement_archive WHERE seq = 5")
    report = await verify(sessions)
    assert [(f["seq"], f["problem"]) for f in report.failures] == [(4, "head_mismatch")]


@pytest.mark.asyncio
async def test_a_consistent_rewrite_is_caught_only_by_the_external_anchor(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """An intruder rewrites row 3 and re-hashes everything after it, head included: the chain is
    internally perfect again. The anchor written outside the database before the rewrite is not."""
    await chain_of(sessions, 5)
    path = anchor_path(settings.NALANDA_ARCHIVE_DIR)
    async with sessions() as session:
        anchor = await write_anchor(session, path)
        assert await write_anchor(session, path) is None  # no new head: no new anchor
    assert anchor is not None and anchor["seq"] == 5 and read_anchors(path) == [anchor]
    async with sessions() as session:
        rows = (await session.execute(select(SettlementArchive).order_by(SettlementArchive.seq))).scalars().all()
    prev, statements, params = rows[1].row_hash, [], {}
    for row in rows[2:]:
        payload = canonical({**row.payload, "stake": "0.01"}) if row.seq == 3 else row.payload
        header = {"seq": str(row.seq), "created_at": row.created_at.replace(tzinfo=UTC).isoformat(timespec="microseconds"), "record_kind": row.record_kind, "source": row.source,
                  "source_id": row.source_id, "user_id": None, "bot_id": None, "ledger_id": None, "fixture_id": row.fixture_id, "amount_inr": f"{row.amount_inr:.2f}",
                  "occurred_at": row.occurred_at.replace(tzinfo=UTC).isoformat(timespec="microseconds")}
        digest = row_hash(prev, header, payload)
        cast = "CAST(:p{0} AS JSONB)" if is_pg(sessions) else ":p{0}"
        statements.append(f"UPDATE nalanda_settlement_archive SET payload = {cast.format(row.seq)}, prev_hash = :h{row.seq}, row_hash = :r{row.seq} WHERE seq = {row.seq}")
        params.update({f"p{row.seq}": json.dumps(payload), f"h{row.seq}": prev, f"r{row.seq}": digest})
        prev = digest
    statements.append("UPDATE nalanda_chain_state SET last_hash = :head WHERE chain = 'settlement'")
    params["head"] = prev
    await bypass(sessions, *statements, params=params)
    assert (await verify(sessions)).ok  # internally consistent: hashes alone cannot see this
    report = await verify(sessions, read_anchors(path))
    assert [(f["seq"], f["problem"]) for f in report.failures] == [(5, "anchor_mismatch")] and report.anchors_checked == 1


# ================================================================ proof 2: partitions
def test_partition_names_follow_iso_weeks_into_the_future() -> None:
    assert week_start(datetime(2026, 10, 9, 23, 59, tzinfo=UTC)) == datetime(2026, 10, 5, tzinfo=UTC)
    assert partition_name("nalanda_ticks", datetime(2026, 10, 9, tzinfo=UTC)) == "nalanda_ticks_p2026w41"
    assert partition_name("nalanda_ticks", date(2026, 12, 31)) == "nalanda_ticks_p2026w53"  # 2026 has 53 ISO weeks
    assert partition_name("nalanda_ticks", date(2027, 1, 3)) == "nalanda_ticks_p2026w53"  # a Sunday still in 2026-W53
    assert partition_name("nalanda_ticks", date(2027, 1, 4)) == "nalanda_ticks_p2027w01"
    assert partition_name("nalanda_settlement_archive", date(2028, 1, 1)) == "nalanda_settlement_archive_p2027w52"
    specs = upcoming("nalanda_ticks", datetime(2026, 12, 20, tzinfo=UTC), 8)
    assert [s.name for s in specs] == [f"nalanda_ticks_p2026w{w}" for w in (51, 52, 53)] + [f"nalanda_ticks_p2027w0{w}" for w in range(1, 7)]
    assert all(b.lower == a.upper for a, b in zip(specs, specs[1:])) and all((s.upper - s.lower) == timedelta(days=7) for s in specs)
    assert create_sql(specs[2]) == (
        "CREATE TABLE IF NOT EXISTS nalanda_ticks_p2026w53 PARTITION OF nalanda_ticks FOR VALUES FROM ('2026-12-28T00:00:00+00:00') TO ('2027-01-04T00:00:00+00:00')"
    )
    with pytest.raises(ValueError):
        partition_name("ticks; DROP TABLE users", date(2026, 1, 1))  # only Nalanda identifiers ever reach DDL


@pytest.mark.asyncio
async def test_the_preallocator_creates_weeks_and_rescues_stray_rows(sessions: async_sessionmaker[AsyncSession]) -> None:
    if not is_pg(sessions):
        pytest.skip("declarative partitioning is PostgreSQL's")
    engine = sessions.kw["bind"]
    far = datetime(2031, 3, 5, 10, tzinfo=UTC)  # a week nobody created: the row lands in the default partition
    async with sessions() as session:
        await session.execute(text("INSERT INTO nalanda_ticks (created_at, observed_at, fixture_id, market, selection, source, bookmaker_id, odds, is_suspended, is_anomaly, stream_id) "
                                   "VALUES (:t, :t, 'fx', 'Match Odds', 'HOME', 'test', 'b', 2.5, false, false, '1-0')"), {"t": far})
        await session.commit()
        assert await session.scalar(text("SELECT tableoid::regclass::text FROM nalanda_ticks")) == "nalanda_ticks_pdefault"
    created = await preallocate(engine, NOW, 8)
    assert [c.split(":")[0] for c in created["nalanda_ticks"]] == [s.name for s in upcoming("nalanda_ticks", NOW, 8)]
    assert await preallocate(engine, NOW, 8) == {t: [] for t in created}  # idempotent
    rescued = await preallocate(engine, far, 0, ["nalanda_ticks"])
    assert rescued["nalanda_ticks"] == ["nalanda_ticks_p2031w10: created (moved 1 row(s) out of the default partition)"]
    async with sessions() as session:
        assert await session.scalar(text("SELECT tableoid::regclass::text FROM nalanda_ticks")) == "nalanda_ticks_p2031w10"
        later = NOW + timedelta(days=15)
        await session.execute(text("INSERT INTO nalanda_ticks (created_at, observed_at, fixture_id, market, selection, source, bookmaker_id, odds, is_suspended, is_anomaly, stream_id) "
                                   "VALUES (:t, :t, 'fx', 'Match Odds', 'DRAW', 'test', 'b', 3.5, false, false, '2-0')"), {"t": later})
        await session.commit()
        assert await session.scalar(text("SELECT tableoid::regclass::text FROM nalanda_ticks WHERE selection = 'DRAW'")) == partition_name("nalanda_ticks", later)
        am = await session.scalar(text("SELECT am.amname FROM pg_index ix JOIN pg_class ic ON ic.oid = ix.indexrelid JOIN pg_am am ON am.oid = ic.relam "
                                        "WHERE ix.indrelid = to_regclass(:p) AND ic.relname LIKE '%created_at%'"), {"p": partition_name("nalanda_ticks", later)})
        assert am == "brin"  # every partition inherits the BRIN index, never a B-tree on the timestamp
    async with engine.connect() as conn:
        assert len(await partitions_of(conn, "nalanda_ticks")) == 1 + 9 + 1  # default + 9 weeks + the rescued one


# ================================================================ proof 3: the cold tier
async def _tick_rows(sessions: async_sessionmaker[AsyncSession], at: datetime, prices: list[tuple[int, str, bool]], *, selection: str = "HOME") -> None:
    async with sessions() as session:
        await session.execute(
            NalandaTick.__table__.insert(),
            [{"created_at": at + timedelta(seconds=s), "observed_at": at + timedelta(seconds=s), "fixture_id": "fx-old", "market": "Match Odds", "selection": selection, "source": "odds_api",
              "bookmaker_id": "pinnacle", "odds": D(o), "is_suspended": False, "is_anomaly": anomaly, "anomaly_z": None, "stream_id": f"{s}-0"} for s, o, anomaly in prices],
        )
        await session.commit()


@pytest.mark.asyncio
async def test_cold_ticks_go_to_parquet_that_reads_back_and_leave_the_database(sessions: async_sessionmaker[AsyncSession], settings: Settings, tmp_path: Path) -> None:
    engine = sessions.kw["bind"]
    old = NOW - timedelta(days=120)
    if is_pg(sessions):
        await preallocate(engine, old, 1, ["nalanda_ticks"])
        await preallocate(engine, NOW, 0, ["nalanda_ticks"])
    await _tick_rows(sessions, old, [(0, "2.10", False), (20, "2.12", False), (45, "500.00", True), (70, "2.08", False)])
    await _tick_rows(sessions, NOW - timedelta(days=2), [(0, "1.90", False)])
    root = tmp_path / "cold"
    done = await export_cold(sessions, engine, settings, NOW, root=root, mirror=S3Mirror(settings))
    exported = [d for d in done if d["rows"]]
    assert len(exported) == 1 and exported[0]["rows"] == 4 and exported[0]["dropped"]
    path = Path(exported[0]["file"])
    assert path.exists() and path.parent == root / "nalanda_ticks" and not list(root.rglob("*.tmp"))
    if is_pg(sessions):
        assert path.name == f"{partition_name('nalanda_ticks', old)}.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 4 and [str(o) for o in table.column("odds").to_pylist()] == ["2.1000", "2.1200", "500.0000", "2.0800"]
    assert table.column("is_anomaly").to_pylist() == [False, False, True, False] and table.schema.field("created_at").type.tz == "UTC"
    assert exported[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    async with sessions() as session:
        left = (await session.execute(select(NalandaTick.fixture_id, NalandaTick.odds))).all()
        manifest = (await session.execute(select(ColdExport))).scalars().all()
    assert [(f, str(o)) for f, o in left] == [("fx-old", "1.9000")]  # only the recent tick stays hot
    assert len(manifest) == 1 and manifest[0].status == "DROPPED" and manifest[0].rows == 4 and manifest[0].sha256 == exported[0]["sha256"]


@pytest.mark.asyncio
async def test_a_file_that_does_not_read_back_keeps_every_row(sessions: async_sessionmaker[AsyncSession], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = sessions.kw["bind"]
    old = NOW - timedelta(days=120)
    if is_pg(sessions):
        await preallocate(engine, old, 0, ["nalanda_ticks"])
    await _tick_rows(sessions, old, [(0, "2.10", False), (10, "2.20", False)])
    real = pq.read_table
    monkeypatch.setattr(nalanda_tiering.pq, "read_table", lambda p, *a, **k: real(p, *a, **k).slice(0, 1))  # the disk lost a row
    with pytest.raises(RuntimeError, match="read back"):
        await export_cold(sessions, engine, settings, NOW, root=tmp_path / "cold", mirror=S3Mirror(settings))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NalandaTick)) == 2  # nothing was dropped
        assert await session.scalar(select(func.count()).select_from(ColdExport)) == 0
    assert not list((tmp_path / "cold").rglob("*.parquet")) and not list((tmp_path / "cold").rglob("*.tmp"))


@pytest.mark.asyncio
async def test_the_mirror_uploads_with_the_checksum_and_can_gate_the_drop(sessions: async_sessionmaker[AsyncSession], settings: Settings, tmp_path: Path) -> None:
    engine = sessions.kw["bind"]
    old = NOW - timedelta(days=120)
    if is_pg(sessions):
        await preallocate(engine, old, 0, ["nalanda_ticks"])
    await _tick_rows(sessions, old, [(0, "2.10", False)])
    gated = settings.model_copy(update={"NALANDA_REQUIRE_MIRROR_BEFORE_DROP": True})
    kept = await export_cold(sessions, engine, gated, NOW, root=tmp_path / "a", mirror=S3Mirror(gated))
    assert kept[0]["rows"] == 1 and "kept" in kept[0]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NalandaTick)) == 1  # no mirror: it stays in PostgreSQL

    uploads: list[tuple[str, str, str, dict[str, Any]]] = []

    class FakeS3:
        def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: dict[str, Any]) -> None:  # noqa: N803 - boto3's spelling
            uploads.append((path, bucket, key, ExtraArgs))

    mirror = S3Mirror(gated.model_copy(update={"NALANDA_S3_BUCKET": "betdoc-cold"}))
    mirror._client, mirror.bucket = FakeS3(), "betdoc-cold"  # noqa: SLF001
    done = await export_cold(sessions, engine, gated, NOW, root=tmp_path / "b", mirror=mirror)
    assert done[0]["dropped"] and done[0]["mirrored"].startswith("s3://betdoc-cold/nalanda/nalanda_ticks/")
    (path, bucket, key, extra) = uploads[0]
    assert bucket == "betdoc-cold" and extra["Metadata"]["sha256"] == hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_rollup_makes_one_minute_candles_without_the_ghosts(sessions: async_sessionmaker[AsyncSession]) -> None:
    day = date(2026, 8, 1)
    start = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    if is_pg(sessions):
        await preallocate(sessions.kw["bind"], start, 0, ["nalanda_ticks", "nalanda_ohlcv_1m"])
    await _tick_rows(sessions, start, [(5, "2.10", False), (15, "2.30", False), (25, "500.00", True), (40, "1.95", False), (55, "2.05", False), (65, "2.40", False)])
    async with sessions() as session:
        summary = await rollup_day(session, day)
        await session.commit()
        rows = (await session.execute(select(NalandaCandle).order_by(NalandaCandle.bucket_start))).scalars().all()
        again = await rollup_day(session, day)  # idempotent: the day is recomputed, not doubled
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(NalandaCandle)) == 2
    assert summary == {"ticks": 6, "candles": 2, "anomalies": 1} == again
    first, second = rows
    assert (first.open, first.high, first.low, first.close, first.ticks, first.anomalies) == (D("2.10"), D("2.30"), D("1.95"), D("2.05"), 4, 1)  # 500.00 left out
    assert (second.open, second.close, second.ticks) == (D("2.40"), D("2.40"), 1)


# ================================================================ the ghost-spike cleanser
def _t(seconds: float, odds: str, *, suspended: bool = False) -> Tick:
    return Tick("fx", "Match Odds", "HOME", "odds_api", "pinnacle", D(odds), NOW + timedelta(seconds=seconds), suspended, f"{seconds}-0")


def _warm(f: GhostSpikeFilter) -> None:
    for i in range(10):
        f.push(_t(i, "2.00" if i % 2 else "2.02"))


def test_a_spike_that_reverts_within_3_seconds_is_a_ghost() -> None:
    f = GhostSpikeFilter()
    _warm(f)
    assert f.push(_t(10.0, "500.00")) == []  # a suspect waits
    out = f.push(_t(12.5, "2.01"))
    assert [(str(t.odds), t.is_anomaly) for t in out] == [("500.00", True), ("2.01", False)] and out[0].anomaly_z > 4
    assert f.flagged == 1 and f.holding == 0


def test_a_glitch_repeated_for_two_quotes_is_still_a_ghost() -> None:
    f = GhostSpikeFilter()
    _warm(f)
    f.push(_t(10.0, "500.00"))
    f.push(_t(11.0, "480.00"))
    assert [t.is_anomaly for t in f.push(_t(12.0, "2.00"))] == [True, True, False]


def test_a_real_move_is_released_after_the_hold_not_flagged() -> None:
    f = GhostSpikeFilter()
    _warm(f)
    assert f.push(_t(10.0, "3.00")) == []
    out = f.push(_t(13.5, "3.02"))  # 3.5s later the market is still there: it moved
    assert [(str(t.odds), t.is_anomaly) for t in out] == [("3.00", False), ("3.02", False)]
    held = GhostSpikeFilter()
    _warm(held)
    held.push(_t(10.0, "3.00"))
    assert held.flush(NOW + timedelta(seconds=12)) == [] and [t.is_anomaly for t in held.flush(NOW + timedelta(seconds=14))] == [False]


def test_suspended_quotes_and_cold_cells_pass_straight_through() -> None:
    f = GhostSpikeFilter()
    assert [str(t.odds) for t in f.push(_t(0, "900.00"))] == ["900.00"]  # no history yet: nothing to judge against
    _warm(f)
    assert f.push(_t(20, "1.01", suspended=True))[0].is_suspended


# ================================================================ the firehose
def quote(fixture: str, at: datetime, prices: dict[str, str], book: str = "pinnacle") -> MarketQuote:
    return MarketQuote(match_id=fixture, market_type="Match Odds", home_team="Arsenal", away_team="Leeds", source="odds_api", fetched_at=at,
                       books=(BookQuote(bookmaker_id=book, prices={k: D(v) for k, v in prices.items()}, observed_at=at),))


@pytest.mark.asyncio
async def test_the_firehose_stores_ticks_flags_ghosts_and_archives_records(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    if is_pg(sessions):
        await preallocate(sessions.kw["bind"], datetime.now(UTC), 1)
    t0 = datetime.now(UTC) - timedelta(seconds=30)
    frames = [quote("fx-1", t0 + timedelta(seconds=i), {"HOME": "2.00" if i % 2 else "2.02", "DRAW": "3.40", "AWAY": "3.80"}) for i in range(8)]
    frames += [quote("fx-1", t0 + timedelta(seconds=8.5), {"HOME": "500.00", "DRAW": "3.40", "AWAY": "3.80"}), quote("fx-1", t0 + timedelta(seconds=10), {"HOME": "2.01", "DRAW": "3.40", "AWAY": "3.80"})]
    assert await emit_quotes(redis, settings, frames)
    emit_record_soon(redis, settings, record(1, kind="BOOKMAKER_RESPONSE", response_payload={"bet_id": "BK-1", "status": "ACCEPTED"}))
    await asyncio.sleep(0.05)  # the background task appends it
    keys = NalandaKeys(settings)
    assert await redis.xlen(keys.stream) == 11
    hose = NalandaFirehose(redis, sessions, settings)
    await hose.ensure_group()
    assert await hose.step() == 11
    async with sessions() as session:
        stored = (await session.execute(select(NalandaTick).where(NalandaTick.selection == "HOME").order_by(NalandaTick.observed_at))).scalars().all()
        archived = (await session.execute(select(SettlementArchive))).scalars().all()
    assert len(stored) == 10 and [str(t.odds) for t in stored if t.is_anomaly] == ["500.0000"]
    assert [(r.record_kind, r.payload["response_payload"]["bet_id"]) for r in archived] == [("BOOKMAKER_RESPONSE", "BK-1")]
    info = await redis.xinfo_groups(keys.stream)
    assert info[0]["pending"] == 0  # every entry stored, every entry acknowledged
    stats = await redis.hgetall(keys.stats)
    assert int(stats["ticks"]) == 30 and int(stats["anomalies"]) == 1 and int(stats["records"]) == 1


@pytest.mark.asyncio
async def test_an_entry_is_acknowledged_only_when_its_held_tick_is_decided(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    if is_pg(sessions):
        await preallocate(sessions.kw["bind"], datetime.now(UTC), 1)
    t0 = datetime.now(UTC)
    hose = NalandaFirehose(redis, sessions, settings, clock=lambda: t0 + timedelta(seconds=9))
    await hose.ensure_group()
    await emit_quotes(redis, settings, [quote("fx-2", t0 + timedelta(seconds=i), {"HOME": "2.00" if i % 2 else "2.02", "AWAY": "3.8"}) for i in range(8)])
    await hose.step()
    await emit_quotes(redis, settings, [quote("fx-2", t0 + timedelta(seconds=8.5), {"HOME": "40.00", "AWAY": "3.8"})])
    await hose.step()
    keys = NalandaKeys(settings)
    assert (await redis.xinfo_groups(keys.stream))[0]["pending"] == 1  # its HOME price is still on hold
    hose.clock = lambda: t0 + timedelta(seconds=20)
    await hose.step()  # the hold ran out: a real move after all, stored and acknowledged
    assert (await redis.xinfo_groups(keys.stream))[0]["pending"] == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NalandaTick).where(NalandaTick.odds == D("40.00"), NalandaTick.is_anomaly.is_(False))) == 1


@pytest.mark.asyncio
async def test_producers_never_raise_when_redis_is_down(settings: Settings) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.2)
    assert await emit(dead, settings, [{"k": "q", "d": "{}"}]) is False
    emit_record_soon(dead, settings, record(1))  # scheduled, fails quietly in the background
    emit_record_soon(None, settings, record(1))
    await asyncio.sleep(0.3)
    await dead.aclose()


# ================================================================ the ledger mirror and the rebuild
@pytest.mark.asyncio
async def test_the_mirror_archives_the_ledger_and_the_archive_rebuilds_it(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with sessions() as session:
        user = User(username=f"nalanda_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        account = await lock_bankroll(session, user.id, settings)  # opens at ₹10,000 (an OPEN posting)
        won = await reserve(session, account, OrderTicket(user.id, uuid.uuid4(), "fx-ars-lee", "Match Odds", "HOME", "pinnacle", D("500.00"), D("2.10")))
        await reserve(session, account, OrderTicket(user.id, uuid.uuid4(), "fx-che-liv", "Match Odds", "AWAY", "pinnacle", D("200.00"), D("3.00")))
        settle(session, account, won, True)
        await session.commit()
        session.add(MarketResult(fixture_id="fx-ars-lee", market="Match Odds", winning_selection="HOME", source="test"))
        session.add(AuditLog(user_id=user.id, event="EXECUTED", reason="BOOKMAKER_ACCEPTED", fixture_id="fx-ars-lee", detail={"request_payload": {"stake": "500.00"}, "response_payload": {"bet_id": "BK-9"}}))
        await session.commit()
    first = await sweep(sessions, now=datetime.now(UTC), overlap_seconds=300, page=2)  # page 2: the keyset pagination is exercised
    async with sessions() as session:
        legs = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert legs == 2 + 2 * 2 + 3  # the opening (2 legs), two reservations (2 each), one win (3)
    assert first == {"LEDGER_POSTING": legs, "SETTLEMENT_RECEIPT": 1, "AUDIT_EVENT": 1, "MARKET_RESULT": 1}
    assert await sweep(sessions, now=datetime.now(UTC), overlap_seconds=300, page=2) == {k: 0 for k in first}  # the overlap re-reads, nothing doubles
    async with sessions() as session:
        report = await rebuild_financial_state(session)
        audit = (await session.execute(select(SettlementArchive).where(SettlementArchive.record_kind == "AUDIT_EVENT"))).scalar_one()
        assert audit.payload["detail"]["response_payload"]["bet_id"] == "BK-9"  # the bookmaker's answer, verbatim
        assert (await verify_chain(session)).ok
    (acct,) = report["accounts"]
    assert report["ok"] and acct["status"] == "MATCH" and report["unbalanced_journals"] == []
    assert D(acct["derived_available"]) == D("10000") - 500 - 200 + 500 + D("550.00") and D(acct["derived_exposure"]) == D("200.00")
    async with sessions() as session:  # the live ledger is corrupted; the archive still knows the truth
        await session.execute(update(BankrollAccount).values(available_balance=D("1.00")))
        await session.commit()
        drift = await rebuild_financial_state(session)
    assert not drift["ok"] and drift["accounts"][0]["status"] == "DRIFT" and drift["accounts"][0]["derived_available"] == acct["derived_available"]


# ================================================================ the read governor
@pytest.mark.asyncio
async def test_reads_are_read_only_and_memory_capped(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    if not is_pg(sessions):
        pytest.skip("work_mem and read-only transactions are PostgreSQL's")
    capped = settings.model_copy(update={"NALANDA_READ_WORK_MEM": "8MB", "NALANDA_READ_STATEMENT_TIMEOUT_MS": 4_000})
    async with governed_session(sessions, capped) as session:
        assert await session.scalar(text("SHOW work_mem")) == "8MB"
        assert await session.scalar(text("SHOW statement_timeout")) == "4s"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW max_parallel_workers_per_gather")) == "0"
        with pytest.raises(DBAPIError, match="read-only"):
            await session.execute(text("INSERT INTO nalanda_rollup_log (day, ticks, candles, anomalies, rolled_at) VALUES ('2026-01-01', 0, 0, 0, now())"))
    async with sessions() as session:
        assert await session.scalar(text("SHOW work_mem")) != "8MB"  # SET LOCAL: nothing leaks into the pool


# ================================================================ the API
@pytest.mark.asyncio
async def test_the_nalanda_api(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    from fastapi import FastAPI

    from app.api.deps import get_current_admin, get_current_user
    from app.api.v1 import cfo_execution, nalanda

    async with sessions() as session:
        alice, admin = User(username="alice_n", hashed_password="x"), User(username="root_n", hashed_password="x", role="ADMIN")
        session.add_all([alice, admin])
        await session.commit()
    if is_pg(sessions):
        await preallocate(sessions.kw["bind"], datetime.now(UTC), 1)
    async with sessions() as session:
        await append_records(session, [record(1, user=alice.id), record(2, user=admin.id), record(3, user=alice.id)])
        await session.commit()
    await _tick_rows(sessions, datetime.now(UTC) - timedelta(minutes=5), [(0, "2.10", False), (30, "2.20", False)])
    app = FastAPI()
    app.include_router(nalanda.router, prefix="/api/v1")
    app.state.redis = redis
    who = {"user": alice}
    app.dependency_overrides[get_current_user] = lambda: who["user"]
    app.dependency_overrides[cfo_execution.get_session_factory] = lambda: sessions
    app.dependency_overrides[nalanda.get_nalanda_reader] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings

    def as_admin() -> User:
        if who["user"].role != "ADMIN":
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(403, "admins only")
        return who["user"]

    app.dependency_overrides[get_current_admin] = as_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        mine = (await client.get("/api/v1/nalanda/settlements")).json()
        assert mine["scope"] == "own" and [r["source_id"] for r in mine["records"]] == ["src-3", "src-1"]
        assert (await client.get("/api/v1/nalanda/settlements/2")).status_code == 404  # someone else's record
        ticks = (await client.get("/api/v1/nalanda/ticks", params={"fixture_id": "fx-old"})).json()
        assert ticks["count"] == 2 and ticks["ticks"][0]["odds"] == "2.2000"
        assert (await client.get("/api/v1/nalanda/ticks", params={"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"})).status_code == 422
        proof = (await client.get("/api/v1/nalanda/verify-ledger")).json()
        assert proof["ok"] and proof["rows"] == 3 and proof["cached"] is False
        assert (await client.get("/api/v1/nalanda/verify-ledger")).json()["cached"] is True
        tele = (await client.get("/api/v1/nalanda/telemetry")).json()
        assert tele["chain"]["head_seq"] == 3 and tele["firehose"]["available"] and "parquet" in tele["cold"]
        assert (await client.post("/api/v1/nalanda/maintenance/anchor")).status_code == 403
        who["user"] = admin
        everything = (await client.get("/api/v1/nalanda/settlements")).json()
        assert everything["scope"] == "all" and len(everything["records"]) == 3
        anchored = (await client.post("/api/v1/nalanda/maintenance/anchor")).json()
        assert anchored["status"] == "OK" and anchored["anchor"]["seq"] == 3
        assert (await client.post("/api/v1/nalanda/maintenance/nonsense")).status_code == 404
        fresh = (await client.get("/api/v1/nalanda/verify-ledger", params={"fresh": True})).json()
        assert fresh["cached"] is False and fresh["anchors_checked"] == 1
        rebuilt = (await client.get("/api/v1/nalanda/rebuild")).json()
        assert "accounts" in rebuilt and rebuilt["mirrored_first"] is not None
        log = (await client.get("/api/v1/nalanda/maintenance")).json()
        assert log[0]["task"] == "anchor"
