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
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.portfolio_positions import mark_positions_dirty
from app.models.cfo_vault import (
    AccountFunding,
    OPEN_STATUSES,
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


def potential_profit(stake: Decimal, odds: Decimal, commission: Decimal = ZERO) -> Decimal:
    """Profit if the bet wins, after the venue's commission on the net win, rounded down to the paisa
    (the ledger never credits a fraction it can't pay)."""
    return (stake * (odds - 1) * (1 - commission)).quantize(PAISA, rounding=ROUND_DOWN)


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
        .on_conflict_do_nothing(index_elements=["user_id"], index_where=text("bot_id IS NULL"))
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


def account_scope(column: Any, bot_id: uuid.UUID | None) -> Any:
    """``bot_id`` NULL is the user's main account; anything else is that bot's isolated sub-account."""
    return column.is_(None) if bot_id is None else column == bot_id


async def lock_bankroll(
    session: AsyncSession, user_id: uuid.UUID, settings: Settings, *, nowait: bool = True, bot_id: uuid.UUID | None = None
) -> BankrollAccount:
    """``SELECT ... FOR UPDATE NOWAIT`` on one bankroll row: the user's main account (opened on first
    use) or a Hive bot's sub-account (which exists only once capital was allocated to it)."""
    stmt = (
        select(BankrollAccount)
        .where(BankrollAccount.user_id == user_id, account_scope(BankrollAccount.bot_id, bot_id))
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
        if bot_id is not None:
            raise CfoError("NO_SUB_ACCOUNT", "This bot has no capital allocated to it", status_code=409)
        await _open_account(session, user_id, settings)
    raise LedgerInvariantError("ACCOUNT_UNAVAILABLE", "The bankroll account could not be opened")


async def read_account(session: AsyncSession, user_id: uuid.UUID, bot_id: uuid.UUID | None = None) -> BankrollAccount | None:
    """A lock-free read for dashboards and pre-lock guard checks."""
    stmt = select(BankrollAccount).where(BankrollAccount.user_id == user_id, account_scope(BankrollAccount.bot_id, bot_id))
    return (await session.execute(stmt)).scalar_one_or_none()


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
            session.add(
                LedgerEntry(journal_id=journal, user_id=account.user_id, bot_id=account.bot_id, ledger_id=ledger_id, kind=kind, account=ledger_account, amount=amount)
            )
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
    commence_time: datetime | None = None
    strategy: str | None = None  # arbitrage | hedge: one leg of a multi-leg order
    group_id: uuid.UUID | None = None
    currency: str = "INR"  # the venue account's currency
    stake_ccy: Decimal | None = None  # the stake in that currency (None: an INR venue, stake_inr itself)
    bot_id: uuid.UUID | None = None  # a Hive bot's order: reserved and settled inside its sub-account
    commission: Decimal | None = None  # the venue's cut of net winnings (None: the executor resolves it from the venue terms)


async def reserve(session: AsyncSession, account: BankrollAccount, ticket: OrderTicket) -> PhantomLedger:
    """Execution sync: record the bet PENDING and move its stake from AVAILABLE into EXPOSURE."""
    stake = to_money(ticket.stake_inr, "stake_inr")
    if stake <= ZERO:
        raise CfoError("INVALID_STAKE", "Stake must be positive")
    if ticket.odds <= 1:
        raise CfoError("INVALID_ODDS", "Odds must be greater than 1")
    commission = ticket.commission if ticket.commission is not None else ZERO
    if not ZERO <= commission < Decimal("0.5"):
        raise CfoError("INVALID_COMMISSION", "A commission rate must be in [0, 0.5)")
    if account.bot_id != ticket.bot_id or account.user_id != ticket.user_id:
        raise LedgerInvariantError("WRONG_ACCOUNT", "An order reserves only in its own account")
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
        potential_pnl=potential_profit(stake, ticket.odds, commission),
        commission_rate=commission,
        status=LedgerStatus.PENDING,
        commence_time=ticket.commence_time,
        strategy=ticket.strategy,
        group_id=ticket.group_id,
        currency=None if ticket.currency == "INR" else ticket.currency,
        stake_ccy=ticket.stake_ccy if ticket.currency != "INR" else None,
        bot_id=ticket.bot_id,
    )
    session.add(entry)
    try:
        await session.flush([entry])
    except IntegrityError as exc:
        raise DuplicateExecutionError("DUPLICATE_REQUEST", "This order was already submitted") from exc
    _post(session, account, PostingKind.RESERVE, {LedgerAccount.AVAILABLE: -stake, LedgerAccount.EXPOSURE: stake}, entry.id)
    await session.flush()
    return entry


def reduce_to_fill(session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, filled_inr: Decimal, filled_ccy: Decimal | None = None) -> Decimal:
    """A partial fill: the unmatched part of the reserved stake goes back EXPOSURE -> AVAILABLE and
    the bet is what actually matched. Returns the amount released."""
    filled = to_money(filled_inr, "filled_inr")
    if entry.status is not LedgerStatus.PENDING:
        raise LedgerInvariantError("NOT_PENDING", "Only a pending bet can be partly filled")
    if not ZERO < filled <= entry.stake_inr:
        raise LedgerInvariantError("BAD_FILL", "A fill must be positive and no more than the reserved stake")
    unfilled = entry.stake_inr - filled
    if unfilled == ZERO:
        return ZERO
    _post(session, account, PostingKind.RELEASE, {LedgerAccount.EXPOSURE: -unfilled, LedgerAccount.AVAILABLE: unfilled}, entry.id)
    entry.requested_stake_inr = entry.requested_stake_inr or entry.stake_inr
    entry.stake_inr = filled
    if entry.stake_ccy is not None and filled_ccy is not None:
        entry.stake_ccy = filled_ccy
    entry.potential_pnl = potential_profit(filled, entry.odds, entry.commission_rate or ZERO)
    return unfilled


def release(session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, status: LedgerStatus, now: datetime | None = None) -> None:
    """Return a stake to AVAILABLE (a definitive rejection found by reconciliation, or a void market)."""
    if entry.status not in OPEN_STATUSES:
        raise LedgerInvariantError("NOT_PENDING", "Only an open bet can be released")
    if status not in (LedgerStatus.REJECTED, LedgerStatus.VOID):
        raise LedgerInvariantError("BAD_RELEASE", "A release ends a bet as REJECTED or VOID")
    _post(session, account, PostingKind.RELEASE, {LedgerAccount.EXPOSURE: -entry.stake_inr, LedgerAccount.AVAILABLE: entry.stake_inr}, entry.id)
    entry.status, entry.settled_at, entry.realized_pnl, entry.reconcile_required = status, now or _utcnow(), ZERO, False


def settle(session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, won: bool, now: datetime | None = None) -> Decimal:
    """Grade a pending bet. WON credits stake + profit to AVAILABLE; LOST books the stake to PNL.
    Either way its exposure clears. Returns the realised P&L."""
    if entry.status not in OPEN_STATUSES:
        raise LedgerInvariantError("NOT_PENDING", "Only an open bet can be settled")
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


async def verify_account(session: AsyncSession, user_id: uuid.UUID, bot_id: uuid.UUID | None = None) -> dict[str, Decimal]:
    """Re-derive every balance of one account (main, or a bot's) from its journal; raise if the
    materialised row disagrees. Each account's own journal sums to zero."""
    sums = dict(
        (
            await session.execute(
                select(LedgerEntry.account, func.coalesce(func.sum(LedgerEntry.amount), 0))
                .where(LedgerEntry.user_id == user_id, account_scope(LedgerEntry.bot_id, bot_id))
                .group_by(LedgerEntry.account)
            )
        ).all()
    )
    derived = {str(account): Decimal(str(sums.get(account, 0))) for account in LedgerAccount}
    if sum(derived.values(), ZERO) != ZERO:
        raise LedgerInvariantError("JOURNAL_UNBALANCED", "The journal does not sum to zero")
    account = await read_account(session, user_id, bot_id)
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
def streak_key(settings: Settings, user_id: uuid.UUID, bot_id: uuid.UUID | None = None) -> str:
    base = f"{settings.CFO_STREAK_KEY_PREFIX}:{user_id}"
    return base if bot_id is None else f"{base}:bot:{bot_id}"


async def loss_streak_from_db(session: AsyncSession, user_id: uuid.UUID, limit: int = 100, bot_id: uuid.UUID | None = None) -> int:
    """Consecutive LOST bets, newest first, ignoring voids. The truth the Redis counter mirrors."""
    rows = (
        await session.execute(
            select(PhantomLedger.status)
            .where(
                PhantomLedger.user_id == user_id,
                account_scope(PhantomLedger.bot_id, bot_id),
                PhantomLedger.status.in_((LedgerStatus.WON, LedgerStatus.LOST)),
            )
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


async def apply_streak(redis: Redis | None, settings: Settings, user_id: uuid.UUID, outcomes: list[bool], bot_id: uuid.UUID | None = None) -> None:
    """After a committed settlement: INCR per loss, reset per win. On failure, drop the key so the
    guard recomputes it from the ledger rather than trusting a stale count."""
    if redis is None or not outcomes:
        return
    key = streak_key(settings, user_id, bot_id)
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


Grade = Literal["WON", "LOST", "VOID"]


@dataclass(frozen=True, slots=True)
class BookmakerGrade:
    """A bookmaker's own statement of how one bet ended (the order resolver reads "my bets")."""

    grade: Grade
    venue_id: str


def _grade_entry(
    session: AsyncSession, account: BankrollAccount, entry: PhantomLedger, grade: Grade, now: datetime, summary: SettlementSummary
) -> tuple[Decimal, bool | None]:
    if grade == "VOID":
        release(session, account, entry, LedgerStatus.VOID, now)
        summary.void += 1
        return ZERO, None
    won = grade == "WON"
    pnl = settle(session, account, entry, won, now)
    summary.won += int(won)
    summary.lost += int(not won)
    return pnl, won


async def settle_markets(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    settings: Settings,
    clock: Callable[[], datetime] = _utcnow,
    *,
    bookmaker_grades: Mapping[uuid.UUID, BookmakerGrade] | None = None,
) -> SettlementSummary:
    """Settle pending bets, one locked transaction per user. Two sources of truth feed one engine:

    * graded markets (``MarketResult``): every pending bet on the market;
    * the bookmaker's own statement of a bet (``bookmaker_grades``, from the order resolver), which
      wins over a market result for that bet: it is what the counterparty will actually pay.

    A bet whose placement is still unconfirmed (``reconcile_required``) is never graded: until the
    bookmaker confirms it exists, paying it out could pay a bet that was never struck.
    """
    summary = SettlementSummary()
    grades = dict(bookmaker_grades or {})
    by_user: dict[tuple[uuid.UUID, uuid.UUID | None], set[uuid.UUID]] = defaultdict(set)
    async with session_factory() as session:
        due = (
            await session.execute(
                select(PhantomLedger.user_id, PhantomLedger.bot_id, PhantomLedger.id)
                .join(MarketResult, (MarketResult.fixture_id == PhantomLedger.fixture_id) & (MarketResult.market == PhantomLedger.market))
                .where(PhantomLedger.status == LedgerStatus.PENDING, PhantomLedger.reconcile_required.is_(False))
            )
        ).all()
        if grades:
            due += (
                await session.execute(
                    select(PhantomLedger.user_id, PhantomLedger.bot_id, PhantomLedger.id).where(
                        PhantomLedger.id.in_(list(grades)), PhantomLedger.status == LedgerStatus.PENDING, PhantomLedger.reconcile_required.is_(False)
                    )
                )
            ).all()
    for user_id, bot_id, ledger_id in due:
        by_user[(user_id, bot_id)].add(ledger_id)

    for (user_id, bot_id), ids in by_user.items():
        outcomes: list[bool] = []
        async with session_factory() as session:
            try:
                account = await lock_bankroll(session, user_id, settings, nowait=True, bot_id=bot_id)
                rows = (
                    await session.execute(
                        select(PhantomLedger, MarketResult)
                        .outerjoin(MarketResult, (MarketResult.fixture_id == PhantomLedger.fixture_id) & (MarketResult.market == PhantomLedger.market))
                        .where(PhantomLedger.id.in_(list(ids)), PhantomLedger.status == LedgerStatus.PENDING)  # re-checked under the lock
                        .order_by(PhantomLedger.created_at)
                        .with_for_update(of=PhantomLedger)
                    )
                ).all()
                now = clock()
                for entry, result in rows:
                    statement = grades.get(entry.id)
                    if statement is not None:
                        grade: Grade = statement.grade
                        source = {"source": f"bookmaker:{statement.venue_id}", "remote_bet_id": entry.remote_bet_id}
                    elif result is not None:
                        grade = "VOID" if result.is_void else ("WON" if entry.selection == result.winning_selection else "LOST")
                        source = {"source": result.source, "winning_selection": result.winning_selection}
                    else:
                        continue
                    pnl, won = _grade_entry(session, account, entry, grade, now, summary)
                    if won is not None:
                        outcomes.append(won)
                    summary.pnl += pnl
                    session.add(
                        audit_row(
                            AuditEvent.SETTLED,
                            f"SETTLED_{grade}",
                            user_id=user_id,
                            ledger_id=entry.id,
                            idempotency_key=entry.idempotency_key,
                            fixture_id=entry.fixture_id,
                            selection=entry.selection,
                            stake_inr=entry.stake_inr,
                            odds=entry.odds,
                            pnl_inr=pnl,
                            detail=source,
                        )
                    )
                mark_peak(account)
                await session.commit()
                summary.users += 1
                if bot_id is None:
                    await mark_positions_dirty(redis, settings, user_id)
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
        await apply_streak(redis, settings, user_id, outcomes, bot_id)
    return summary


# ---------------------------------------------------------------- Hive sub-accounts (Group 65)
async def _lock_sub_account(session: AsyncSession, user_id: uuid.UUID, bot_id: uuid.UUID, funding: AccountFunding, settings: Settings) -> BankrollAccount:
    """Lock a bot's sub-account, opening it (empty) on its first allocation."""
    try:
        return await lock_bankroll(session, user_id, settings, nowait=True, bot_id=bot_id)
    except CfoError as exc:
        if exc.reason != "NO_SUB_ACCOUNT":
            raise
    now = _utcnow()
    session.add(
        BankrollAccount(
            user_id=user_id, bot_id=bot_id, funding=funding.value, currency="INR", available_balance=ZERO, exposure_balance=ZERO,
            peak_balance=ZERO, postings=0, created_at=now, updated_at=now,
        )
    )
    await session.flush()
    return await lock_bankroll(session, user_id, settings, nowait=True, bot_id=bot_id)


async def allocate(
    session: AsyncSession, settings: Settings, *, user_id: uuid.UUID, bot_id: uuid.UUID, amount: object, funding: AccountFunding
) -> tuple[BankrollAccount | None, BankrollAccount]:
    """Capital into a bot's sub-account. TRANSFER (a live bot) moves it out of the main account
    (locked first: one lock order everywhere); VIRTUAL (a paper bot) is notional and touches no
    real balance. Either way each account's journal still sums to zero, and moving capital is not
    performance: both peaks move by the same amount, so no drawdown guard reads it as a loss.
    The caller commits."""
    cash = to_money(amount, "amount")
    if cash <= ZERO:
        raise CfoError("INVALID_AMOUNT", "Allocate a positive amount", status_code=422)
    main = await lock_bankroll(session, user_id, settings, nowait=True) if funding is AccountFunding.TRANSFER else None
    sub = await _lock_sub_account(session, user_id, bot_id, funding, settings)
    if sub.funding != funding.value:
        if sub.available_balance != ZERO or sub.exposure_balance != ZERO:
            raise CfoError("FUNDING_MISMATCH", f"This bot's sub-account is {sub.funding.lower()}-funded; release its capital first", status_code=409)
        sub.funding = funding.value  # empty: it may change hands between paper and live
    if main is not None:
        if cash > main.available_balance:
            raise InsufficientFundsError("INSUFFICIENT_BALANCE", "Not enough available balance to allocate", detail={"available": str(main.available_balance)})
        _post(session, main, PostingKind.ALLOCATE, {LedgerAccount.AVAILABLE: -cash, LedgerAccount.EQUITY: cash})
        main.peak_balance = max(ZERO, main.peak_balance - cash)
    _post(session, sub, PostingKind.ALLOCATE, {LedgerAccount.AVAILABLE: cash, LedgerAccount.EQUITY: -cash})
    sub.peak_balance += cash
    await session.flush()
    return main, sub


async def deallocate(session: AsyncSession, settings: Settings, *, user_id: uuid.UUID, bot_id: uuid.UUID, amount: object) -> tuple[BankrollAccount | None, BankrollAccount]:
    """Capital back out of a bot's free balance (never its open exposure). A transfer-funded
    account returns it to the main account; virtual capital simply ceases to exist. The caller commits."""
    cash = to_money(amount, "amount")
    if cash <= ZERO:
        raise CfoError("INVALID_AMOUNT", "Release a positive amount", status_code=422)
    probe = await read_account(session, user_id, bot_id)
    if probe is None:
        raise CfoError("NO_SUB_ACCOUNT", "This bot has no capital allocated to it", status_code=409)
    funding = AccountFunding(probe.funding)
    main = await lock_bankroll(session, user_id, settings, nowait=True) if funding is AccountFunding.TRANSFER else None
    sub = await lock_bankroll(session, user_id, settings, nowait=True, bot_id=bot_id)
    if cash > sub.available_balance:
        raise InsufficientFundsError("INSUFFICIENT_BALANCE", "Only the bot's free balance can be released", detail={"available": str(sub.available_balance)})
    _post(session, sub, PostingKind.DEALLOCATE, {LedgerAccount.AVAILABLE: -cash, LedgerAccount.EQUITY: cash})
    sub.peak_balance = max(ZERO, sub.peak_balance - cash)
    if main is not None:
        _post(session, main, PostingKind.DEALLOCATE, {LedgerAccount.AVAILABLE: cash, LedgerAccount.EQUITY: -cash})
        main.peak_balance += cash
    await session.flush()
    return main, sub


# ---------------------------------------------------------------- reconciliation (UNKNOWN outcomes)
ManualOutcome = Literal["WON", "LOST", "VOID", "NOT_PLACED", "OPEN"]


async def resolve_manually(
    session: AsyncSession,
    settings: Settings,
    ledger_id: uuid.UUID,
    *,
    outcome: ManualOutcome,
    remote_bet_id: str | None,
    actor: uuid.UUID | None,
) -> tuple[PhantomLedger, bool | None]:
    """A person's ruling on an unconfirmed or dead-lettered bet, from the bookmaker's own records.

    WON / LOST / VOID settle it now; NOT_PLACED returns the stake (REJECTED); OPEN puts it back in
    the resolver's queue as an ordinary pending bet. Returns the entry and, for WON/LOST, whether it
    won (for the loss-streak counter). The caller commits.
    """
    found = (await session.execute(select(PhantomLedger.user_id, PhantomLedger.bot_id).where(PhantomLedger.id == ledger_id))).first()
    if found is None:
        raise CfoError("NOT_FOUND", "No such position", status_code=404)
    owner, bot_id = found
    account = await lock_bankroll(session, owner, settings, nowait=True, bot_id=bot_id)
    entry = (await session.execute(select(PhantomLedger).where(PhantomLedger.id == ledger_id).with_for_update())).scalar_one()
    if entry.status not in OPEN_STATUSES:
        raise CfoError("NOT_OPEN", "This bet is already settled", status_code=409)
    if entry.status is LedgerStatus.PENDING and not entry.reconcile_required and outcome in ("NOT_PLACED", "OPEN"):
        raise CfoError("NOT_RECONCILABLE", "This bet is confirmed and still open: nothing to reconcile", status_code=409)
    if remote_bet_id:
        entry.remote_bet_id = remote_bet_id
    now = _utcnow()
    won: bool | None = None
    pnl: Decimal | None = None
    if outcome == "NOT_PLACED":
        release(session, account, entry, LedgerStatus.REJECTED, now)
        event, reason = AuditEvent.BOOKMAKER_REJECTED, "RESOLVED_NOT_PLACED"
    elif outcome == "OPEN":
        if not entry.remote_bet_id:
            raise CfoError("REMOTE_ID_REQUIRED", "An open bet needs the bookmaker's bet id", status_code=422)
        entry.status, entry.reconcile_required = LedgerStatus.PENDING, False
        entry.resolve_attempts, entry.last_resolve_error, entry.next_resolve_at = 0, None, now
        event, reason = AuditEvent.EXECUTED, "RESOLVED_OPEN"
    elif outcome == "VOID":
        release(session, account, entry, LedgerStatus.VOID, now)
        pnl = ZERO
        event, reason = AuditEvent.SETTLED, "SETTLED_VOID"
    else:
        won = outcome == "WON"
        pnl = settle(session, account, entry, won, now)
        event, reason = AuditEvent.SETTLED, f"SETTLED_{outcome}"
    entry.reconcile_required = False
    mark_peak(account)
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
            pnl_inr=pnl,
            detail={"remote_bet_id": entry.remote_bet_id, "resolved_by": str(actor) if actor else None, "manual": True},
        )
    )
    await session.flush()
    return entry, won


async def reconcile(
    session: AsyncSession,
    settings: Settings,
    ledger_id: uuid.UUID,
    *,
    placed: bool,
    remote_bet_id: str | None,
    actor: uuid.UUID | None,
) -> PhantomLedger:
    """Resolve a bet whose placement was never confirmed: placed keeps its reserved stake as an
    ordinary pending bet; not placed returns the stake (REJECTED). The caller commits."""
    unconfirmed = await session.scalar(select(PhantomLedger.reconcile_required).where(PhantomLedger.id == ledger_id))
    if unconfirmed is None:
        raise CfoError("NOT_FOUND", "No such position", status_code=404)
    if unconfirmed is False:
        raise CfoError("NOT_RECONCILABLE", "Only an unconfirmed pending bet can be reconciled", status_code=409)
    entry, _ = await resolve_manually(session, settings, ledger_id, outcome="OPEN" if placed else "NOT_PLACED", remote_bet_id=remote_bet_id, actor=actor)
    return entry
