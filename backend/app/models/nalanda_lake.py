"""Nalanda (Group 67): the tick data lake and the settlement warehouse.

Three time-series tables, each a PostgreSQL declarative partition tree ``RANGE`` over its time column,
one partition per ISO week (``<table>_p<iso-year>w<week>``, created ahead of time by the partition
pre-allocator, with a ``<table>_pdefault`` partition as the last line of defence), and each indexed on
that column with BRIN, never B-tree: a block-range index is a few kilobytes per gigabyte of an
append-ordered table, so the lake scales to billions of rows without index bloat.

* ``nalanda_ticks``: every price every book quoted, as the firehose received it, with the ghost-spike
  cleanser's verdict (``is_anomaly``, ``anomaly_z``). Rolled up into candles after 30 days, exported
  to Parquet and dropped from PostgreSQL after 90.
* ``nalanda_ohlcv_1m``: one-minute OHLC candles per book and selection, anomalies excluded.
* ``nalanda_settlement_archive``: the warehouse. Every ledger posting, settlement receipt, audit event
  (bookmaker request and response bodies included) and market result, as JSONB, in one SHA-256 hash
  chain: each row's ``row_hash`` covers its own content and the ``prev_hash`` of the row before it,
  ``seq`` runs 1, 2, 3... without gaps. The table is append-only (a trigger refuses UPDATE, DELETE and
  TRUNCATE); the chain proves what the trigger cannot (``app.services.nalanda_chain.verify_chain``).
  Its LEDGER_POSTING rows alone rebuild every bankroll account.

Bookkeeping: the chain head, the dedupe index of what has been archived, the mirror cursors, the
cold-export manifest, the rollup log and the maintenance log. These are small, ordinary tables.

On SQLite (tests) the partitioning and BRIN options are ignored and the tables are plain tables.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DDL, BigInteger, Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, Numeric, String, Text, Uuid, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.models import Base, utc_now

ODDS = Numeric(10, 4)
MONEY = Numeric(18, 2)
HASH = String(64)
JsonColumn = JSON().with_variant(JSONB(), "postgresql")
BRIN = {"postgresql_using": "brin", "postgresql_with": {"pages_per_range": 32}}


class NalandaTick(Base):
    __tablename__ = "nalanda_ticks"
    __table_args__ = (
        CheckConstraint("odds > 1", name="odds_above_one"),
        Index("ix_nalanda_ticks_created_at_brin", "created_at", **BRIN),
        Index("ix_nalanda_ticks_market", "fixture_id", "market"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )
    __mapper_args__ = {"primary_key": ["created_at", "stream_id", "bookmaker_id", "selection"]}

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the firehose stored it (the partition key)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the source fetched it
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    odds: Mapped[Decimal] = mapped_column(ODDS)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    stream_id: Mapped[str] = mapped_column(String(32))  # the firehose entry it came in


class NalandaCandle(Base):
    __tablename__ = "nalanda_ohlcv_1m"
    __table_args__ = (
        CheckConstraint("low > 1 AND low <= open AND low <= close AND high >= open AND high >= close", name="ohlc_consistent"),
        CheckConstraint("ticks > 0", name="ticks_positive"),
        Index("ix_nalanda_ohlcv_1m_bucket_start_brin", "bucket_start", **BRIN),
        Index("ix_nalanda_ohlcv_1m_market", "fixture_id", "market"),
        {"postgresql_partition_by": "RANGE (bucket_start)"},
    )
    __mapper_args__ = {"primary_key": ["bucket_start", "fixture_id", "market", "selection", "source", "bookmaker_id"]}

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    open: Mapped[Decimal] = mapped_column(ODDS)
    high: Mapped[Decimal] = mapped_column(ODDS)
    low: Mapped[Decimal] = mapped_column(ODDS)
    close: Mapped[Decimal] = mapped_column(ODDS)
    ticks: Mapped[int] = mapped_column(Integer)
    anomalies: Mapped[int] = mapped_column(Integer, default=0)  # ghost spikes left out of this candle
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SettlementArchive(Base):
    __tablename__ = "nalanda_settlement_archive"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("length(row_hash) = 64 AND length(prev_hash) = 64", name="hash_lengths"),
        Index("ix_nalanda_settlement_archive_created_at_brin", "created_at", **BRIN),
        Index("ix_nalanda_settlement_archive_kind_user", "record_kind", "user_id"),
        Index("ix_nalanda_settlement_archive_ledger", "ledger_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # chain position, gapless from 1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)  # the partition key is in every unique key
    record_kind: Mapped[str] = mapped_column(String(32))  # LEDGER_POSTING | SETTLEMENT_RECEIPT | AUDIT_EVENT | MARKET_RESULT | BOOKMAKER_RESPONSE
    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fixture_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # when it happened at its source
    payload: Mapped[dict[str, Any]] = mapped_column(JsonColumn)
    prev_hash: Mapped[str] = mapped_column(HASH)
    row_hash: Mapped[str] = mapped_column(HASH)


class ChainState(Base):
    """The head of each hash chain: the last row's position, hash and time (locked FOR UPDATE per append)."""

    __tablename__ = "nalanda_chain_state"

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    last_hash: Mapped[str] = mapped_column(HASH)
    last_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MirrorIndex(Base):
    """What is already in the archive, by its source identity: an archived fact is never archived twice."""

    __tablename__ = "nalanda_mirror_index"

    record_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger)


class MirrorCursor(Base):
    """How far the mirror sweeper has read each source table (it re-reads an overlap behind it)."""

    __tablename__ = "nalanda_mirror_cursors"

    record_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mirrored: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ColdExport(Base):
    """One Parquet file of the cold tier: what range of which table, its checksum, where it went."""

    __tablename__ = "nalanda_cold_exports"
    __table_args__ = (
        CheckConstraint("status IN ('EXPORTED', 'DROPPED', 'BACKUP', 'FAILED')", name="status_known"),
        CheckConstraint("rows >= 0 AND bytes >= 0", name="sizes_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    table_name: Mapped[str] = mapped_column(String(64))
    partition_name: Mapped[str | None] = mapped_column(String(96), nullable=True)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    file_path: Mapped[str] = mapped_column(Text)
    rows: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(HASH)
    status: Mapped[str] = mapped_column(String(16), default="EXPORTED")
    mirror_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    mirrored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dropped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


class RollupLog(Base):
    __tablename__ = "nalanda_rollup_log"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    ticks: Mapped[int] = mapped_column(BigInteger, default=0)
    candles: Mapped[int] = mapped_column(BigInteger, default=0)
    anomalies: Mapped[int] = mapped_column(BigInteger, default=0)
    rolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MaintenanceLog(Base):
    __tablename__ = "nalanda_maintenance_log"
    __table_args__ = (Index("ix_nalanda_maintenance_log_task", "task", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16))  # OK | FAILED
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- the archive is append-only
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION nalanda_append_only() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
    RAISE EXCEPTION 'nalanda: % is append-only; % refused', TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
END
$body$
"""
APPEND_ONLY_TRIGGERS_PG = (
    "CREATE TRIGGER nalanda_settlement_archive_append_only BEFORE UPDATE OR DELETE ON nalanda_settlement_archive "
    "FOR EACH ROW EXECUTE FUNCTION nalanda_append_only()",
    "CREATE TRIGGER nalanda_settlement_archive_no_truncate BEFORE TRUNCATE ON nalanda_settlement_archive "
    "FOR EACH STATEMENT EXECUTE FUNCTION nalanda_append_only()",
)
APPEND_ONLY_TRIGGERS_SQLITE = (
    "CREATE TRIGGER nalanda_settlement_archive_no_update BEFORE UPDATE ON nalanda_settlement_archive "
    "BEGIN SELECT RAISE(ABORT, 'nalanda: nalanda_settlement_archive is append-only; UPDATE refused'); END",
    "CREATE TRIGGER nalanda_settlement_archive_no_delete BEFORE DELETE ON nalanda_settlement_archive "
    "BEGIN SELECT RAISE(ABORT, 'nalanda: nalanda_settlement_archive is append-only; DELETE refused'); END",
)

_archive = SettlementArchive.__table__
event.listen(_archive, "after_create", DDL(APPEND_ONLY_FUNCTION.replace("%", "%%")).execute_if(dialect="postgresql"))
for _statement in APPEND_ONLY_TRIGGERS_PG:
    event.listen(_archive, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in APPEND_ONLY_TRIGGERS_SQLITE:
    event.listen(_archive, "after_create", DDL(_statement).execute_if(dialect="sqlite"))

PARTITIONED_TABLES: dict[str, str] = {  # table -> its partition key
    NalandaTick.__tablename__: "created_at",
    NalandaCandle.__tablename__: "bucket_start",
    SettlementArchive.__tablename__: "created_at",
}

# A partition tree with no partition refuses every insert: each one is born with its DEFAULT partition,
# so a row whose week was never pre-allocated still lands (and the pre-allocator moves it out later)
for _model in (NalandaTick, NalandaCandle, SettlementArchive):
    _name = _model.__tablename__
    event.listen(_model.__table__, "after_create", DDL(f"CREATE TABLE IF NOT EXISTS {_name}_pdefault PARTITION OF {_name} DEFAULT").execute_if(dialect="postgresql"))
