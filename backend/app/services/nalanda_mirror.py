"""The ledger mirror: the CFO's source tables, copied into Nalanda's hash chain; and the rebuild.

The firehose is fast but best-effort. The mirror is the guarantee: every minute it reads what was
committed to the CFO tables since its last pass (re-reading ``NALANDA_MIRROR_OVERLAP_SECONDS`` behind
its watermark, so a transaction that committed late is never skipped) and appends it to the chain.
The dedupe index makes the overlap free: a fact already archived is never archived twice.

    LEDGER_POSTING      cfo_ledger_entries   every leg of every double-entry posting
    SETTLEMENT_RECEIPT  cfo_phantom_ledger   every bet once it is WON / LOST / VOID / REJECTED
    AUDIT_EVENT         cfo_audit_log        every execution event, the bookmaker's request and response included
    MARKET_RESULT       cfo_market_results   every result (a re-grade is a new record)

``rebuild_financial_state`` needs nothing but the archive: it folds every LEDGER_POSTING in chain
order into balances per account (main or bot sub-account) and ledger account, checks each journal
sums to zero, and sets the result beside the live ``cfo_bankroll_accounts``. If the live ledger were
lost or corrupted, these balances are what it held.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cfo_vault import AuditLog, BankrollAccount, LedgerAccount, LedgerEntry, LedgerStatus, MarketResult, PhantomLedger
from app.models.nalanda_lake import MirrorCursor, SettlementArchive
from app.services.nalanda_chain import ArchiveRecord, append_records

ZERO = Decimal(0)
_SETTLED = (LedgerStatus.WON, LedgerStatus.LOST, LedgerStatus.VOID, LedgerStatus.REJECTED)


def _aware(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _posting(row: LedgerEntry) -> ArchiveRecord:
    return ArchiveRecord(
        "LEDGER_POSTING", "cfo_ledger_entries", str(row.id),
        {"journal_id": row.journal_id, "user_id": row.user_id, "bot_id": row.bot_id, "ledger_id": row.ledger_id, "kind": row.kind, "account": row.account, "amount": row.amount, "created_at": row.created_at},
        user_id=row.user_id, bot_id=row.bot_id, ledger_id=row.ledger_id, amount_inr=row.amount, occurred_at=_aware(row.created_at),
    )


def _receipt(row: PhantomLedger) -> ArchiveRecord:
    fields = (
        "user_id", "bot_id", "idempotency_key", "fixture_id", "market", "selection", "bookmaker_id", "remote_bet_id", "signal_id", "stake_inr", "requested_stake_inr",
        "odds", "true_prob", "commission_rate", "potential_pnl", "realized_pnl", "status", "commence_time", "strategy", "group_id", "currency", "stake_ccy",
        "created_at", "settled_at",
    )
    return ArchiveRecord(
        "SETTLEMENT_RECEIPT", "cfo_phantom_ledger", f"{row.id}:{row.status}", {"ledger_id": row.id, **{f: getattr(row, f) for f in fields}},
        user_id=row.user_id, bot_id=row.bot_id, ledger_id=row.id, fixture_id=row.fixture_id, amount_inr=row.realized_pnl, occurred_at=_aware(row.settled_at),
    )


def _audit(row: AuditLog) -> ArchiveRecord:
    return ArchiveRecord(
        "AUDIT_EVENT", "cfo_audit_log", str(row.id),
        {"event": row.event, "reason": row.reason, "idempotency_key": row.idempotency_key, "ledger_id": row.ledger_id, "fixture_id": row.fixture_id, "selection": row.selection,
         "stake_inr": row.stake_inr, "odds": row.odds, "pnl_inr": row.pnl_inr, "detail": row.detail or {}, "created_at": row.created_at},
        user_id=row.user_id, ledger_id=row.ledger_id, fixture_id=row.fixture_id, amount_inr=row.pnl_inr, occurred_at=_aware(row.created_at),
    )


def _result(row: MarketResult) -> ArchiveRecord:
    return ArchiveRecord(
        "MARKET_RESULT", "cfo_market_results", f"{row.id}:{row.winning_selection}:{row.is_void}",
        {"fixture_id": row.fixture_id, "market": row.market, "winning_selection": row.winning_selection, "is_void": row.is_void, "source": row.source, "recorded_by": row.recorded_by, "recorded_at": row.recorded_at},
        fixture_id=row.fixture_id, occurred_at=_aware(row.recorded_at),
    )


@dataclass(frozen=True, slots=True)
class MirrorSource:
    kind: str
    model: Any
    stamp: Any  # the column the watermark follows
    build: Callable[[Any], ArchiveRecord]
    where: Any = None


SOURCES: tuple[MirrorSource, ...] = (
    MirrorSource("LEDGER_POSTING", LedgerEntry, LedgerEntry.created_at, _posting),
    MirrorSource("SETTLEMENT_RECEIPT", PhantomLedger, PhantomLedger.settled_at, _receipt, PhantomLedger.status.in_(_SETTLED)),
    MirrorSource("AUDIT_EVENT", AuditLog, AuditLog.created_at, _audit),
    MirrorSource("MARKET_RESULT", MarketResult, MarketResult.recorded_at, _result),
)


async def sweep_source(session: AsyncSession, source: MirrorSource, *, now: datetime, overlap: timedelta, page: int, max_pages: int = 20) -> int:
    """Archive one source table's rows since the watermark (minus the overlap). The caller commits."""
    cursor = await session.get(MirrorCursor, source.kind)
    if cursor is None:
        cursor = MirrorCursor(record_kind=source.kind, watermark=None, mirrored=0)
        session.add(cursor)
        await session.flush()
    since = None if cursor.watermark is None else _aware(cursor.watermark) - overlap
    appended, last = 0, None
    for _ in range(max_pages):
        query = select(source.model).where(source.stamp.is_not(None))
        if source.where is not None:
            query = query.where(source.where)
        if last is not None:
            stamp, key = last
            query = query.where(or_(source.stamp > stamp, and_(source.stamp == stamp, source.model.id > key)))
        elif since is not None:
            query = query.where(source.stamp >= since)
        rows = (await session.execute(query.order_by(source.stamp, source.model.id).limit(page))).scalars().all()
        if not rows:
            break
        result = await append_records(session, [source.build(r) for r in rows], now=now)
        appended += result.appended
        last = (getattr(rows[-1], source.stamp.key), rows[-1].id)
        stamp_seen = _aware(last[0])
        if cursor.watermark is None or (stamp_seen is not None and stamp_seen > _aware(cursor.watermark)):
            cursor.watermark = stamp_seen
        if len(rows) < page:
            break
    cursor.mirrored = (cursor.mirrored or 0) + appended
    cursor.updated_at = now
    return appended


async def sweep(session_factory: async_sessionmaker[AsyncSession], *, now: datetime, overlap_seconds: int, page: int, sources: Sequence[MirrorSource] = SOURCES) -> dict[str, int]:
    """Every source, each in its own transaction (a failing source never holds back the others)."""
    done: dict[str, int] = {}
    for source in sources:
        async with session_factory() as session:
            done[source.kind] = await sweep_source(session, source, now=now, overlap=timedelta(seconds=overlap_seconds), page=page)
            await session.commit()
    return done


# ---------------------------------------------------------------- the rebuild
async def rebuild_financial_state(session: AsyncSession, *, batch: int = 5_000) -> dict[str, Any]:
    """Every account's balances from the archive's LEDGER_POSTING records alone, beside the live ledger."""
    balances: dict[tuple[str, str | None], dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    journals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    postings, last_seq, cursor = 0, None, 0
    while True:
        rows = (
            await session.execute(
                select(SettlementArchive.seq, SettlementArchive.payload).where(SettlementArchive.record_kind == "LEDGER_POSTING", SettlementArchive.seq > cursor)
                .order_by(SettlementArchive.seq).limit(batch)
            )
        ).all()
        if not rows:
            break
        for seq, payload in rows:
            amount = Decimal(payload["amount"])
            balances[(payload["user_id"], payload.get("bot_id"))][payload["account"]] += amount
            journals[payload["journal_id"]] += amount
            postings += 1
            cursor = last_seq = seq
    live = {(str(a.user_id), None if a.bot_id is None else str(a.bot_id)): a for a in (await session.execute(select(BankrollAccount))).scalars()}
    accounts = []
    for key in sorted(set(balances) | set(live), key=lambda k: (k[0], k[1] or "")):
        derived = balances.get(key, {})
        account = live.get(key)
        available, exposure = derived.get(LedgerAccount.AVAILABLE.value, ZERO), derived.get(LedgerAccount.EXPOSURE.value, ZERO)
        row: dict[str, Any] = {
            "user_id": key[0], "bot_id": key[1], "derived": {k: str(v) for k, v in sorted(derived.items())}, "derived_available": str(available), "derived_exposure": str(exposure),
            "live_available": None if account is None else str(account.available_balance), "live_exposure": None if account is None else str(account.exposure_balance),
        }
        if account is None:
            row["status"] = "MISSING_LIVE"  # the archive knows an account the live ledger lost
        elif key not in balances:
            row["status"] = "NOT_ARCHIVED"  # nothing mirrored yet for it (the sweep runs every minute)
        elif account.available_balance == available and account.exposure_balance == exposure:
            row["status"] = "MATCH"
        else:
            row["status"] = "DRIFT"
            row["drift_available"] = str(account.available_balance - available)
            row["drift_exposure"] = str(account.exposure_balance - exposure)
        accounts.append(row)
    unbalanced = [{"journal_id": j, "sum": str(v)} for j, v in journals.items() if v != ZERO]
    statuses = {a["status"] for a in accounts}
    return {
        "postings": postings, "as_of_seq": last_seq, "accounts": accounts, "unbalanced_journals": unbalanced[:50],
        "ok": not unbalanced and statuses <= {"MATCH"}, "rebuilt_at": datetime.now(UTC).isoformat(),
    }


def bankroll_snapshot(rebuild: dict[str, Any]) -> dict[tuple[uuid.UUID, uuid.UUID | None], tuple[Decimal, Decimal]]:
    """``(user, bot) -> (available, exposure)`` from a rebuild: what to restore a lost ledger to."""
    out = {}
    for a in rebuild["accounts"]:
        if a["status"] != "MISSING_LIVE" and a["derived"] == {}:
            continue
        out[(uuid.UUID(a["user_id"]), None if a["bot_id"] is None else uuid.UUID(a["bot_id"]))] = (Decimal(a["derived_available"]), Decimal(a["derived_exposure"]))
    return out
