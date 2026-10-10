"""The settlement warehouse's SHA-256 hash chain.

Every row of ``nalanda_settlement_archive`` carries ``seq`` (1, 2, 3... with no gaps), ``prev_hash``
(the previous row's ``row_hash``; 64 zeros for the first) and

    row_hash = SHA-256( prev_hash || "\\n" || canonical_json({"header": {...}, "payload": {...}}) )

where the header is every column but the hashes (seq, created_at, kind, source, source id, user, bot,
bet, fixture, amount, occurred_at). Change any byte of any row and its hash no longer recomputes;
delete a row and the sequence has a gap and the next link breaks; insert or reorder and the links
break; backdate and ``created_at`` runs backwards. ``verify_chain`` walks the chain in ``seq`` order
and reports each of these at the row where it happens.

A rewrite of the whole chain from some row onwards (by someone holding the database) recomputes
consistently, so the chain's head is also anchored outside the database: ``write_anchor`` appends
``{seq, hash}`` to ``<NALANDA_ARCHIVE_DIR>/anchors/<chain>.jsonl``, and the verifier checks every
anchor still matches the chain at its position. The table's trigger refuses UPDATE, DELETE and
TRUNCATE outright; the chain is what proves it held.

Payloads are canonicalised before they are stored: every number becomes its decimal string, times
become UTC ISO-8601 with microseconds, UUIDs and enums their strings, keys sorted. JSONB round trips
such a document byte for byte, so a hash computed at append time recomputes at verification time.

Appends serialise on the chain head (``SELECT ... FOR UPDATE``): any number of writers, one chain.
An archived fact is identified by ``(record_kind, source_id)`` and is never archived twice.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nalanda_lake import ChainState, MirrorIndex, SettlementArchive

GENESIS_HASH = "0" * 64
SETTLEMENT_CHAIN = "settlement"
RECORD_KINDS = ("LEDGER_POSTING", "SETTLEMENT_RECEIPT", "AUDIT_EVENT", "MARKET_RESULT", "BOOKMAKER_RESPONSE", "ROUTED_ORDER")
_PAISA = Decimal("0.01")


# ---------------------------------------------------------------- canonical form
def _utc(moment: datetime) -> datetime:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def iso(moment: datetime) -> str:
    return _utc(moment).isoformat(timespec="microseconds")


def canonical(value: Any) -> Any:
    """A JSON-safe value with one spelling for every fact: numbers as decimal strings, times in UTC."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, enum.Enum):
        return canonical(value.value)
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(_PAISA)


def header_of(
    seq: int, created_at: datetime, kind: str, source: str, source_id: str, user_id: uuid.UUID | None, bot_id: uuid.UUID | None,
    ledger_id: uuid.UUID | None, fixture_id: str | None, amount_inr: Decimal | None, occurred_at: datetime | None,
) -> dict[str, Any]:
    return {
        "seq": str(seq), "created_at": iso(created_at), "record_kind": kind, "source": source, "source_id": source_id,
        "user_id": None if user_id is None else str(user_id), "bot_id": None if bot_id is None else str(bot_id),
        "ledger_id": None if ledger_id is None else str(ledger_id), "fixture_id": fixture_id,
        "amount_inr": None if amount_inr is None else f"{_money(amount_inr):.2f}", "occurred_at": None if occurred_at is None else iso(occurred_at),
    }


def row_hash(prev_hash: str, header: Mapping[str, Any], payload: Any) -> str:
    material = prev_hash + "\n" + canonical_json({"header": header, "payload": payload})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def hash_of_row(row: Any) -> str:
    """Recompute a stored row's hash from its own columns (and its stored ``prev_hash``)."""
    header = header_of(row.seq, row.created_at, row.record_kind, row.source, row.source_id, row.user_id, row.bot_id, row.ledger_id, row.fixture_id, row.amount_inr, row.occurred_at)
    return row_hash(row.prev_hash, header, row.payload)


# ---------------------------------------------------------------- appending
@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    kind: str
    source: str
    source_id: str
    payload: Mapping[str, Any]
    user_id: uuid.UUID | None = None
    bot_id: uuid.UUID | None = None
    ledger_id: uuid.UUID | None = None
    fixture_id: str | None = None
    amount_inr: Decimal | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in RECORD_KINDS:
            raise ValueError(f"unknown archive record kind {self.kind!r}")
        if not self.source_id or len(self.source_id) > 160:
            raise ValueError("a record needs a source id of at most 160 characters")


@dataclass(frozen=True, slots=True)
class AppendResult:
    appended: int
    duplicates: int
    head_seq: int
    head_hash: str


def _insert_ignore(session: AsyncSession, model: Any) -> Any:
    dialect = session.get_bind().dialect.name
    statement = (postgresql.insert(model) if dialect == "postgresql" else sqlite.insert(model)).on_conflict_do_nothing()
    return statement


async def _lock_head(session: AsyncSession, chain: str) -> ChainState:
    await session.execute(_insert_ignore(session, ChainState).values(chain=chain, last_seq=0, last_hash=GENESIS_HASH, last_created_at=None, updated_at=datetime.now(UTC)))
    head = (await session.execute(select(ChainState).where(ChainState.chain == chain).with_for_update().execution_options(populate_existing=True))).scalar_one()
    return head


async def _already(session: AsyncSession, keys: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for i in range(0, len(keys), 400):
        chunk = keys[i : i + 400]
        condition = or_(*(and_(MirrorIndex.record_kind == k, MirrorIndex.source_id == s) for k, s in chunk))
        found |= {(k, s) for k, s in (await session.execute(select(MirrorIndex.record_kind, MirrorIndex.source_id).where(condition))).all()}
    return found


async def append_records(session: AsyncSession, records: Sequence[ArchiveRecord], *, now: datetime | None = None, chain: str = SETTLEMENT_CHAIN) -> AppendResult:
    """Append to the chain under its head lock (the caller commits). Facts already archived are skipped."""
    head = await _lock_head(session, chain)
    unique: dict[tuple[str, str], ArchiveRecord] = {}
    for record in records:
        unique.setdefault((record.kind, record.source_id), record)
    seen = await _already(session, list(unique)) if unique else set()
    fresh = [r for key, r in unique.items() if key not in seen]
    if not fresh:
        return AppendResult(0, len(records), head.last_seq, head.last_hash)
    moment = _utc(now or datetime.now(UTC))
    if head.last_created_at is not None and moment < _utc(head.last_created_at):
        moment = _utc(head.last_created_at)  # the chain's clock never runs backwards
    seq, prev = head.last_seq, head.last_hash
    rows, index = [], []
    for record in fresh:
        seq += 1
        payload = canonical(dict(record.payload))
        amount = _money(record.amount_inr)
        occurred = None if record.occurred_at is None else _utc(record.occurred_at)
        header = header_of(seq, moment, record.kind, record.source, record.source_id, record.user_id, record.bot_id, record.ledger_id, record.fixture_id, amount, occurred)
        digest = row_hash(prev, header, payload)
        rows.append({
            "seq": seq, "created_at": moment, "record_kind": record.kind, "source": record.source, "source_id": record.source_id, "user_id": record.user_id,
            "bot_id": record.bot_id, "ledger_id": record.ledger_id, "fixture_id": record.fixture_id, "amount_inr": amount, "occurred_at": occurred,
            "payload": payload, "prev_hash": prev, "row_hash": digest,
        })
        index.append({"record_kind": record.kind, "source_id": record.source_id, "seq": seq})
        prev = digest
    await session.execute(insert(SettlementArchive), rows)
    await session.execute(insert(MirrorIndex), index)
    await session.execute(update(ChainState).where(ChainState.chain == chain).values(last_seq=seq, last_hash=prev, last_created_at=moment, updated_at=datetime.now(UTC)))
    return AppendResult(len(rows), len(records) - len(rows), seq, prev)


# ---------------------------------------------------------------- verifying
@dataclass(slots=True)
class VerifyReport:
    chain: str
    ok: bool = True
    rows: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    last_hash: str = GENESIS_HASH
    head_seq: int | None = None
    head_hash: str | None = None
    anchors_checked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    failures_total: int = 0
    elapsed_ms: float = 0.0
    verified_at: str = ""

    def fail(self, seq: int | None, problem: str, detail: str, limit: int) -> None:
        self.ok = False
        self.failures_total += 1
        if len(self.failures) < limit:
            self.failures.append({"seq": seq, "problem": problem, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain, "ok": self.ok, "rows": self.rows, "first_seq": self.first_seq, "last_seq": self.last_seq, "last_hash": self.last_hash,
            "head_seq": self.head_seq, "head_hash": self.head_hash, "anchors_checked": self.anchors_checked, "failures": self.failures,
            "failures_total": self.failures_total, "elapsed_ms": round(self.elapsed_ms, 1), "verified_at": self.verified_at,
        }


async def verify_chain(
    session: AsyncSession, *, chain: str = SETTLEMENT_CHAIN, anchors: Sequence[Mapping[str, Any]] = (), batch: int = 5_000, max_failures: int = 50
) -> VerifyReport:
    """Walk the chain from seq 1: gaps, broken links, altered rows, backdating, the head, the anchors."""
    started = time.perf_counter()
    report = VerifyReport(chain)
    wanted = {int(a["seq"]): str(a["hash"]) for a in anchors if a.get("chain", chain) == chain}
    seen_at: dict[int, str] = {}
    expected, prev, last_created, cursor = 1, GENESIS_HASH, None, 0
    while True:
        rows = (await session.execute(select(SettlementArchive).where(SettlementArchive.seq > cursor).order_by(SettlementArchive.seq).limit(batch))).scalars().all()
        if not rows:
            break
        for row in rows:
            report.rows += 1
            report.first_seq = row.seq if report.first_seq is None else report.first_seq
            if row.seq != expected:
                kind = "gap" if row.seq > expected else "duplicate"
                report.fail(row.seq, kind, f"expected seq {expected}, found {row.seq}" + (" (rows deleted)" if kind == "gap" else ""), max_failures)
            if row.prev_hash != prev:
                report.fail(row.seq, "broken_link", "prev_hash does not match the previous row's hash (a row was removed, inserted or reordered)", max_failures)
            if hash_of_row(row) != row.row_hash:
                report.fail(row.seq, "altered", "the row's content no longer hashes to its row_hash (a field was changed)", max_failures)
            created = _utc(row.created_at)
            if last_created is not None and created < last_created:
                report.fail(row.seq, "backdated", f"created_at {iso(created)} is before the previous row's {iso(last_created)}", max_failures)
            if row.seq in wanted:
                seen_at[row.seq] = row.row_hash
            prev, expected, last_created, cursor = row.row_hash, row.seq + 1, created, row.seq
            report.last_seq, report.last_hash = row.seq, row.row_hash
        session.expunge_all()
    head = (await session.execute(select(ChainState).where(ChainState.chain == chain))).scalar_one_or_none()
    if head is not None:
        report.head_seq, report.head_hash = head.last_seq, head.last_hash
        if (head.last_seq or 0) != (report.last_seq or 0) or (head.last_seq and head.last_hash != report.last_hash):
            report.fail(report.last_seq, "head_mismatch", f"the chain head says seq {head.last_seq}, the table ends at {report.last_seq} (rows removed from the end, or added outside the writer)", max_failures)
    elif report.rows:
        report.fail(None, "head_missing", "rows exist but the chain head does not", max_failures)
    for seq, digest in sorted(wanted.items()):
        report.anchors_checked += 1
        if seq not in seen_at:
            report.fail(seq, "anchor_missing", f"an external anchor names seq {seq}, which the chain no longer has", max_failures)
        elif seen_at[seq] != digest:
            report.fail(seq, "anchor_mismatch", "the chain was rewritten: this row's hash differs from the one anchored outside the database", max_failures)
    report.elapsed_ms = (time.perf_counter() - started) * 1000
    report.verified_at = datetime.now(UTC).isoformat()
    return report


# ---------------------------------------------------------------- external anchors
def anchor_path(archive_dir: str | Path, chain: str = SETTLEMENT_CHAIN) -> Path:
    return Path(archive_dir) / "anchors" / f"{chain}.jsonl"


def read_anchors(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    anchors = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                anchors.append(json.loads(line))
            except json.JSONDecodeError:
                anchors.append({"seq": -1, "hash": "unreadable anchor line"})
    return anchors


async def write_anchor(session: AsyncSession, path: Path, chain: str = SETTLEMENT_CHAIN) -> dict[str, Any] | None:
    """Append the chain head to the anchor file when it has moved since the last anchor."""
    head = (await session.execute(select(ChainState).where(ChainState.chain == chain))).scalar_one_or_none()
    if head is None or not head.last_seq:
        return None
    existing = read_anchors(path)
    if existing and int(existing[-1].get("seq", 0)) >= head.last_seq:
        return None
    anchor = {"chain": chain, "seq": head.last_seq, "hash": head.last_hash, "head_created_at": iso(head.last_created_at) if head.last_created_at else None, "anchored_at": datetime.now(UTC).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(anchor) + "\n")
    return anchor
