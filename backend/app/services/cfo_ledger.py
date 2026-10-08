"""The CFO ledger: every rupee an Omni execution or settlement moves, under a row lock, double-entry.

Rules this module holds to:

* Money is ``Decimal`` at 2 dp. A stake with more precision is refused, never rounded.
* Every movement first locks the user's ``BankrollAccount`` row with ``SELECT ... FOR UPDATE NOWAIT``.
  A row already locked by another execution fails fast as ``BankrollLockedError`` (HTTP 409) instead
  of queueing behind a bookmaker call.
* Every movement is a posting: legs across AVAILABLE, EXPOSURE, PNL and EQUITY that sum to exactly
  zero, written to the append-only journal and applied to the materialised balances in the same
  transaction. ``verify_account`` re-derives the balances from the journal.
* Functions here never commit. The caller owns the transaction, so a reservation and the bookmaker
  call that depends on it commit or roll back together.

Postings (signed: positive debits an asset)::

    OPEN         AVAILABLE +B                      EQUITY -B
    RESERVE      AVAILABLE -s   EXPOSURE +s
    RELEASE      AVAILABLE +s   EXPOSURE -s                      (rejected / void)
    SETTLE_WON   AVAILABLE +s+p EXPOSURE -s   PNL -p
    SETTLE_LOST                 EXPOSURE -s   PNL +s
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.cfo_vault import (
    AuditEvent,
    AuditLog,
    BankrollAccount,
    LedgerAccount,
    LedgerEntry,
    LedgerStatus,
    MarketResult,
    PhantomLedger,
    PostingKind,
)

logger = logging.getLogger("betdoc.cfo")

PAISA = Decimal("0.01")
ZERO = Decimal("0")
LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL: FOR UPDATE NOWAIT found the row locked


# ---------------------------------------------------------------- errors
class CfoError(Exception):
    """A refusal with a machine-readable reason (the audit log's ``reason``) and an HTTP status."""

    status_code = 400

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status_code: int | None = None,
        detail: Mapping[str, Any] | None = None,
        audited: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = dict(detail or {})
        self.audited = audited  # True once its own audit row is written (no second BLOCKED row)
        if status_code is not None:
            self.status_code = status_code


class BankrollLockedError(CfoError):
    status_code = 409


class InsufficientFundsError(CfoError):
    status_code = 409


class DuplicateExecutionError(CfoError):
    status_code = 409


class LedgerInvariantError(CfoError):
    status_code = 500


# ---------------------------------------------------------------- money
def to_money(value: object, field_name: str = "amount") -> Decimal:
    """A finite Decimal with at most 2 decimal places. Never rounds a rupee amount silently."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be finite")
    quantized = number.quantize(PAISA)
    if quantized != number:
        raise ValueError(f"{field_name} has more than 2 decimal places")
    return quantized


def potential_profit(stake: Decimal, odds: Decimal) -> Decimal:
    """Profit if the bet wins, rounded down to the paisa (the ledger never credits a fraction it can't pay)."""
    return (stake * (odds - 1)).quantize(PAISA, rounding=ROUND_DOWN)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_lock_unavailable(exc: DBAPIError) -> bool:
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if candidate is not None and LOCK_NOT_AVAILABLE in (getattr(candidate, "sqlstate", None), getattr(candidate, "pgcode", None)):
            return True
    return False


# ---------------------------------------------------------------- accounts
async def opening_balance(session: AsyncSession, user_id: uuid.UUID, settings: Settings) -> Decimal:
    """Where a new account starts: the dashboard's live bankroll (starting capital plus realised P&L)."""
    from app.services.aryabhata_pipeline import live_bankroll  # local: the pipeline imports this module's peers

    bankroll = await live_bankroll(session, user_id, settings.starting_bankroll)
    return max(bankroll.quantize(PAISA, rounding=ROUND_DOWN), ZERO)


async def _open_account(session: AsyncSession, user_id: uuid.UUID, settings: Settings) -> None:
    balance = await opening_balance(session, user_id, settings)
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    now = _utcnow()
    stmt = (
        insert(BankrollAccount)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            currency="INR",
            available_balance=balance,
            exposure_balance=ZERO,
            peak_balance=balance,
            postings=1 if balance > 0 else 0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
        .returning(BankrollAccount.id)
    )
    created = (await session.execute(stmt)).scalar_one_or_none()
    if created is not None and balance > 0:
        journal = uuid.uuid4()
        session.add_all(
            [
                LedgerEntry(journal_id=journal, user_id=user_id, kind=PostingKind.OPEN, account=LedgerAccount.AVAILABLE, amount=balance),
                LedgerEntry(journal_id=journal, user_id=user_id, kind=PostingKind.OPEN, account=LedgerAccount.EQUITY, amount=-balance),
            ]
        )
        await session.flush()


async def lock_bankroll(session: AsyncSession, user_id: uuid.UUID, settings: Settings, *, nowait: bool = True) -> BankrollAccount:
    """``SELECT ... FOR UPDATE NOWAIT`` on the user's bankroll row, opening the account on first use."""
    stmt = (
        select(BankrollAccount)
        .where(BankrollAccount.user_id == user_id)
        .with_for_update(nowait=nowait)
        .execution_options(populate_existing=True)
    )
    for _ in range(2):
        try:
            account = (await session.execute(stmt)).scalar_one_or_none()
        except DBAPIError as exc:
            if is_lock_unavailable(exc):
                raise BankrollLockedError("BANKROLL_LOCKED", "Another execution holds this bankroll; try again in a moment") from exc
            raise
        if account is not None:
            return account
        await _open_account(session, user_id, settings)
    raise LedgerInvariantError("ACCOUNT_UNAVAILABLE", "The bankroll account could not be opened")


async def read_account(session: AsyncSession, user_id: uuid.UUID) -> BankrollAccount | None:
    """A lock-free read for dashboards and pre-lock guard checks."""
    return (await session.execute(select(BankrollAccount).where(BankrollAccount.user_id == user_id))).scalar_one_or_none()


# ---------------------------------------------------------------- postings
def _post(
    session: AsyncSession,
    account: BankrollAccount,
    kind: PostingKind,
    legs: Mapping[LedgerAccount, Decimal],
    ledger_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write one balanced posting and apply it to the locked account's balances."""
    if sum(legs.values(), ZERO) != ZERO:
        raise LedgerInvariantError("UNBALANCED_POSTING", f"{kind} legs do not sum to zero")
    available = account.available_balance + legs.get(LedgerAccount.AVAILABLE, ZERO)
    exposure = account.exposure_balance + legs.get(LedgerAccount.EXPOSURE, ZERO)
    if available < ZERO:
        raise InsufficientFundsError("INSUFFICIENT_BALANCE", "Stake exceeds the available balance", detail={"available": str(account.available_balance)})
    if exposure < ZERO:
        raise LedgerInvariantError("NEGATIVE_EXPOSURE", f"{kind} would take exposure below zero")
    journal = uuid.uuid4()
    for ledger_account, amount in legs.items():
        if amount != ZERO:
            session.add(LedgerEntry(journal_id=journal, user_id=account.user_id, ledger_id=ledger_id, kind=kind, account=ledger_account, amount=amount))
    account.available_balance = available
    account.exposure_balance = exposure
    account.postings += 1
    return journal


def mark_peak(account: BankrollAccount) -> Decimal:
    """Raise the equity high-water mark to the current equity. Called once a batch of postings is
    complete, so the order in which one sweep settles its bets can never mint a peak in between."""
    account.peak_balance = max(account.peak_balance, account.equity)
    return account.peak_balance


@dataclass(frozen=True, slots=True)
class OrderTicket:
    user_id: uuid.UUID
    idempotency_key: uuid.UUID
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    stake_inr: Decimal
    odds: Decimal
    true_prob: Decimal | None = None
    signal_id: uuid.UUID | None = None


async def reserve(session: AsyncSession, account: BankrollAccount, ticket: OrderTicket) -> PhantomLedger:
    """Execution sync: record the bet PENDING and move its stake from AVAILABLE into EXPOSURE."""
    stake = to_money(ticket.stake_inr, "stake_inr")
    if stake <= ZERO:
        raise CfoError("INVALID_STAKE", "Stake must be positive")
    if ticket.odds <= 1:
        raise CfoError("INVALID_ODDS", "Odds must be greater than 1")
    if stake > account.available_balance:
        raise InsufficientFundsError(
            "INSUFFICIENT_BALANCE",
            "Stake exceeds the available balance",
            detail={"available": str(account.available_balance), "stake": str(stake)},
        )
    # Race-free under the bankroll lock (every reservation for this user holds it); the unique
    # constraint stays as the backstop, and a hit there poisons the transaction for the caller to roll back
    duplicate = await session.scalar(
        select(PhantomLedger.id).where(PhantomLedger.user_id == ticket.user_id, PhantomLedger.idempotency_key == ticket.idempotency_key)
    )
    if duplicate is not None:
        raise DuplicateExecutionError("DUPLICATE_REQUEST", "This order was already submitted")
    entry = PhantomLedger(
        user_id=ticket.user_id,
        idempotency_key=ticket.idempotency_key,
        fixture_id=ticket.fixture_id,
        market=ticket.market,
        selection=ticket.selection,
        bookmaker_id=ticket.bookmaker_id,
        signal_id=ticket.signal_id,
        stake_inr=stake,
        odds=ticket.odds,
        true_prob=ticket.true_prob,
        potential_pnl=potential_profit(stake, ticket.odds),
        status=LedgerStatus.PENDING,
    )
    session.add(entry)
    try:
        await session.flush([entry])
    except IntegrityError as exc:
        raise DuplicateExecutionError("DUPLICATE_REQUEST", "This order was already submitted") from exc
    _post(session, account, PostingKind.RESERVE, {LedgerAccount.AVAILABLE: -stake, LedgerAccount.EXPOSURE: stake}, entry.id)
    await session.flush()
    return entry


def release(session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, status: LedgerStatus, now: datetime | None = None) -> None:
    """Return a stake to AVAILABLE (a definitive rejection found by reconciliation, or a void market)."""
    if entry.status is not LedgerStatus.PENDING:
        raise LedgerInvariantError("NOT_PENDING", "Only a pending bet can be released")
    if status not in (LedgerStatus.REJECTED, LedgerStatus.VOID):
        raise LedgerInvariantError("BAD_RELEASE", "A release ends a bet as REJECTED or VOID")
    _post(session, account, PostingKind.RELEASE, {LedgerAccount.EXPOSURE: -entry.stake_inr, LedgerAccount.AVAILABLE: entry.stake_inr}, entry.id)
    entry.status, entry.settled_at, entry.realized_pnl, entry.reconcile_required = status, now or _utcnow(), ZERO, False


def settle(session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, won: bool, now: datetime | None = None) -> Decimal:
    """Grade a pending bet. WON credits stake + profit to AVAILABLE; LOST books the stake to PNL.
    Either way its exposure clears. Returns the realised P&L."""
    if entry.status is not LedgerStatus.PENDING:
        raise LedgerInvariantError("NOT_PENDING", "Only a pending bet can be settled")
    stake = entry.stake_inr
    if won:
        profit = entry.potential_pnl
        _post(
            session,
            account,
            PostingKind.SETTLE_WON,
            {LedgerAccount.EXPOSURE: -stake, LedgerAccount.AVAILABLE: stake + profit, LedgerAccount.PNL: -profit},
            entry.id,
        )
        entry.status, pnl = LedgerStatus.WON, profit
    else:
        _post(session, account, PostingKind.SETTLE_LOST, {LedgerAccount.EXPOSURE: -stake, LedgerAccount.PNL: stake}, entry.id)
        entry.status, pnl = LedgerStatus.LOST, -stake
    entry.settled_at, entry.realized_pnl = now or _utcnow(), pnl
    return pnl


async def verify_account(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Decimal]:
    """Re-derive every balance from the journal; raise if the materialised row disagrees."""
    sums = dict(
        (
            await session.execute(
                select(LedgerEntry.account, func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.user_id == user_id).group_by(LedgerEntry.account)
            )
        ).all()
    )
    derived = {str(account): Decimal(str(sums.get(account, 0))) for account in LedgerAccount}
    if sum(derived.values(), ZERO) != ZERO:
        raise LedgerInvariantError("JOURNAL_UNBALANCED", "The journal does not sum to zero")
    account = await read_account(session, user_id)
    if account is not None and (
        derived[LedgerAccount.AVAILABLE] != account.available_balance or derived[LedgerAccount.EXPOSURE] != account.exposure_balance
    ):
        raise LedgerInvariantError("BALANCE_DRIFT", "Materialised balances differ from the journal", detail={k: str(v) for k, v in derived.items()})
    return derived


# ---------------------------------------------------------------- audit
def audit_row(event: AuditEvent, reason: str, **fields: Any) -> AuditLog:
    return AuditLog(event=event, reason=reason[:64], detail=fields.pop("detail", None) or {}, **fields)


async def write_audit(session_factory: async_sessionmaker[AsyncSession], row: AuditLog, *, required: bool = False) -> bool:
    """Commit one audit row in its own transaction, so it survives the caller's rollback.

    ``required``: the caller must not proceed without this record (the pre-bookmaker intent row).
    """
    try:
        async with session_factory() as session:
            session.add(row)
            await session.commit()
        return True
    except Exception:
        logger.exception("CFO audit write failed (%s %s)", row.event, row.reason)
        if required:
            raise
        return False


# ---------------------------------------------------------------- loss streak (Redis counter, DB truth)
def streak_key(settings: Settings, user_id: uuid.UUID) -> str:
    return f"{settings.CFO_STREAK_KEY_PREFIX}:{user_id}"


async def loss_streak_from_db(session: AsyncSession, user_id: uuid.UUID, limit: int = 100) -> int:
    """Consecutive LOST bets, newest first, ignoring voids. The truth the Redis counter mirrors."""
    rows = (
        await session.execute(
            select(PhantomLedger.status)
            .where(PhantomLedger.user_id == user_id, PhantomLedger.status.in_((LedgerStatus.WON, LedgerStatus.LOST)))
            .order_by(PhantomLedger.settled_at.desc(), PhantomLedger.id.desc())
            .limit(limit)
        )
    ).scalars()
    streak = 0
    for status in rows:
        if status is not LedgerStatus.LOST:
            break
        streak += 1
    return streak


async def apply_streak(redis: Redis | None, settings: Settings, user_id: uuid.UUID, outcomes: list[bool]) -> None:
    """After a committed settlement: INCR per loss, reset per win. On failure, drop the key so the
    guard recomputes it from the ledger rather than trusting a stale count."""
    if redis is None or not outcomes:
        return
    key = streak_key(settings, user_id)
    try:
        for won in outcomes:
            if won:
                await redis.set(key, 0)
            else:
                await redis.incr(key)
    except (RedisError, OSError):
        logger.warning("Loss-streak counter update failed for a user; it will be recomputed from the ledger")
        try:
            await redis.delete(key)
        except (RedisError, OSError):
            pass


# ---------------------------------------------------------------- settlement engine
@dataclass(slots=True)
class SettlementSummary:
    users: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    skipped_locked: int = 0
    failed: int = 0
    pnl: Decimal = ZERO
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"users": self.users, "won": self.won, "lost": self.lost, "void": self.void, "skipped_locked": self.skipped_locked, "failed": self.failed, "pnl": str(self.pnl)}


async def settle_markets(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    settings: Settings,
    clock: Callable[[], datetime] = _utcnow,
) -> SettlementSummary:
    """Settle every PENDING bet whose market has a recorded result, one user transaction at a time.

    A bet whose placement is still unconfirmed (``reconcile_required``) is never graded: until the
    bookmaker confirms it exists, paying it out could pay a bet that was never struck.
    """
    summary = SettlementSummary()
    async with session_factory() as session:
        due = (
            await session.execute(
                select(PhantomLedger.user_id, PhantomLedger.id)
                .join(MarketResult, (MarketResult.fixture_id == PhantomLedger.fixture_id) & (MarketResult.market == PhantomLedger.market))
                .where(PhantomLedger.status == LedgerStatus.PENDING, PhantomLedger.reconcile_required.is_(False))
            )
        ).all()
    by_user: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for user_id, ledger_id in due:
        by_user[user_id].append(ledger_id)

    for user_id, ids in by_user.items():
        outcomes: list[bool] = []
        async with session_factory() as session:
            try:
                account = await lock_bankroll(session, user_id, settings, nowait=True)
                rows = (
                    await session.execute(
                        select(PhantomLedger, MarketResult)
                        .join(MarketResult, (MarketResult.fixture_id == PhantomLedger.fixture_id) & (MarketResult.market == PhantomLedger.market))
                        .where(PhantomLedger.id.in_(ids), PhantomLedger.status == LedgerStatus.PENDING)  # re-checked under the lock
                        .order_by(PhantomLedger.created_at)
                        .with_for_update(of=PhantomLedger)
                    )
                ).all()
                now = clock()
                for entry, result in rows:
                    if result.is_void:
                        release(session, account, entry, LedgerStatus.VOID, now)
                        pnl = ZERO
                        summary.void += 1
                    else:
                        won = entry.selection == result.winning_selection
                        pnl = settle(session, account, entry, won, now)
                        outcomes.append(won)
                        summary.won += int(won)
                        summary.lost += int(not won)
                    summary.pnl += pnl
                    session.add(
                        audit_row(
                            AuditEvent.SETTLED,
                            "SETTLED_VOID" if result.is_void else ("SETTLED_WON" if pnl > 0 else "SETTLED_LOST"),
                            user_id=user_id,
                            ledger_id=entry.id,
                            idempotency_key=entry.idempotency_key,
                            fixture_id=entry.fixture_id,
                            selection=entry.selection,
                            stake_inr=entry.stake_inr,
                            odds=entry.odds,
                            pnl_inr=pnl,
                            detail={"result_source": result.source, "winning_selection": result.winning_selection},
                        )
                    )
                mark_peak(account)
                await session.commit()
                summary.users += 1
            except BankrollLockedError:
                await session.rollback()
                summary.skipped_locked += 1  # an execution holds the row: the next sweep settles it
                continue
            except Exception as exc:  # noqa: BLE001 - one user's failure must not block everyone else's payout
                await session.rollback()
                summary.failed += 1
                summary.errors.append(type(exc).__name__)
                logger.exception("Settlement failed for a user; rolled back")
                continue
        await apply_streak(redis, settings, user_id, outcomes)
    return summary


# ---------------------------------------------------------------- reconciliation (UNKNOWN outcomes)
async def reconcile(
    session: AsyncSession,
    settings: Settings,
    ledger_id: uuid.UUID,
    *,
    placed: bool,
    bookmaker_ref: str | None,
    actor: uuid.UUID | None,
) -> PhantomLedger:
    """Resolve a bet whose placement was never confirmed, with the bookmaker's word on it.

    Placed: it keeps its reserved stake and becomes an ordinary pending bet. Not placed: the stake
    returns to AVAILABLE and the bet ends REJECTED. The caller commits.
    """
    owner = await session.scalar(select(PhantomLedger.user_id).where(PhantomLedger.id == ledger_id))
    if owner is None:
        raise CfoError("NOT_FOUND", "No such position", status_code=404)
    account = await lock_bankroll(session, owner, settings, nowait=True)
    entry = (await session.execute(select(PhantomLedger).where(PhantomLedger.id == ledger_id).with_for_update())).scalar_one()
    if entry.status is not LedgerStatus.PENDING or not entry.reconcile_required:
        raise CfoError("NOT_RECONCILABLE", "Only an unconfirmed pending bet can be reconciled", status_code=409)
    if placed:
        entry.bookmaker_ref, entry.reconcile_required = bookmaker_ref, False
        event, reason = AuditEvent.EXECUTED, "RECONCILED_PLACED"
    else:
        release(session, account, entry, LedgerStatus.REJECTED)
        event, reason = AuditEvent.BOOKMAKER_REJECTED, "RECONCILED_NOT_PLACED"
    session.add(
        audit_row(
            event,
            reason,
            user_id=owner,
            ledger_id=entry.id,
            idempotency_key=entry.idempotency_key,
            fixture_id=entry.fixture_id,
            selection=entry.selection,
            stake_inr=entry.stake_inr,
            odds=entry.odds,
            detail={"bookmaker_ref": bookmaker_ref, "reconciled_by": str(actor) if actor else None},
        )
    )
    await session.flush()
    return entry
