"""Double-entry ledger. Every transaction sums to exactly zero or is refused.

Sign convention: positive is a debit, negative is a credit. The zero-sum check
runs **before** any ORM object is constructed, so an unbalanced transaction
never reaches the session and cannot be flushed by an unrelated later commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from betdoc.adapters.persistence.models import (
    AccountType,
    LedgerAccount,
    LedgerEntry,
    utc_now,
)

__all__ = [
    "DuplicateTransactionError",
    "LedgerError",
    "TransactionLeg",
    "UnbalancedTransactionError",
    "UnknownAccountError",
    "assert_ledger_balanced",
    "ensure_account",
    "get_account_balance",
    "get_balances",
    "get_transaction_legs",
    "record_transaction",
]


class LedgerError(Exception):
    """Base class for every refused ledger operation."""


class UnbalancedTransactionError(ValueError, LedgerError):
    """Debits and credits do not net to zero."""

    def __init__(self, total_paise: int, leg_count: int) -> None:
        super().__init__(
            f"transaction does not balance: legs sum to {total_paise} paise "
            f"across {leg_count} legs, expected exactly 0"
        )
        self.total_paise = total_paise
        self.leg_count = leg_count


class UnknownAccountError(ValueError, LedgerError):
    """A leg references an account that does not exist."""

    def __init__(self, account_ids: Sequence[uuid.UUID]) -> None:
        super().__init__(f"unknown ledger account(s): {list(account_ids)!r}")
        self.account_ids = tuple(account_ids)


class DuplicateTransactionError(ValueError, LedgerError):
    """The supplied ``transaction_id`` has already been recorded."""

    def __init__(self, transaction_id: uuid.UUID) -> None:
        super().__init__(f"transaction {transaction_id} has already been recorded")
        self.transaction_id = transaction_id


@dataclass(frozen=True, slots=True)
class TransactionLeg:
    """One side of a transaction. Positive debits, negative credits.

    Validated at construction, so an invalid leg cannot be carried around and
    discovered later.
    """

    account_id: uuid.UUID
    amount_paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, uuid.UUID):
            msg = f"account_id must be a UUID, got {type(self.account_id).__name__}"
            raise TypeError(msg)
        if isinstance(self.amount_paise, bool) or not isinstance(self.amount_paise, int):
            msg = (
                "amount_paise must be an int in minor units, got "
                f"{type(self.amount_paise).__name__}"
            )
            raise TypeError(msg)
        if self.amount_paise == 0:
            msg = "a zero-amount leg carries no information and is refused"
            raise ValueError(msg)

    @property
    def is_debit(self) -> bool:
        return self.amount_paise > 0


async def ensure_account(
    session: AsyncSession, name: str, account_type: AccountType
) -> LedgerAccount:
    """Fetch or create an account by name.

    Idempotent on ``name``, which carries a unique constraint. A concurrent
    creator therefore loses on the constraint rather than producing a duplicate
    chart-of-accounts row.
    """
    existing = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.name == name)
    )
    if existing is not None:
        if existing.account_type is not account_type:
            msg = (
                f"account {name!r} already exists as {existing.account_type.value!r}, "
                f"refusing to reinterpret it as {account_type.value!r}"
            )
            raise LedgerError(msg)
        return existing

    account = LedgerAccount(name=name, account_type=account_type)
    session.add(account)
    await session.flush()
    return account


async def record_transaction(
    session: AsyncSession,
    legs: list[TransactionLeg],
    transaction_id: uuid.UUID | None = None,
    *,
    timestamp: datetime | None = None,
) -> uuid.UUID:
    """Write a balanced transaction and return its ``transaction_id``.

    Order of operations is deliberate and must not be rearranged:

    #. Reject fewer than two legs. A one-sided entry is not double-entry.
    #. Sum the legs in exact integer arithmetic and reject any non-zero total.
    #. Verify every referenced account exists, so the failure is a clear
       domain error rather than a dialect-specific integrity error (SQLite
       does not enforce foreign keys unless the pragma is on).
    #. Reject a replayed ``transaction_id``, making the call idempotent-safe
       against at-least-once message delivery.
    #. Only then construct the rows and flush.

    Args:
        session: Active async session. The caller owns the commit.
        legs: Two or more legs summing to exactly zero paise.
        transaction_id: Supply the caller's idempotency key to make a replay
            detectable. Generated when omitted.
        timestamp: Shared timestamp for every leg. Defaults to now, in UTC.

    Returns:
        The ``transaction_id`` grouping the written entries.

    Raises:
        UnbalancedTransactionError: Legs do not net to zero.
        UnknownAccountError: A leg references a missing account.
        DuplicateTransactionError: ``transaction_id`` already exists.
        ValueError: Fewer than two legs supplied.
    """
    if len(legs) < 2:
        msg = f"double-entry requires at least two legs, got {len(legs)}"
        raise ValueError(msg)

    total = sum(leg.amount_paise for leg in legs)
    if total != 0:
        raise UnbalancedTransactionError(total, len(legs))

    account_ids = {leg.account_id for leg in legs}
    known = set(
        (
            await session.scalars(
                select(LedgerAccount.id).where(LedgerAccount.id.in_(account_ids))
            )
        ).all()
    )
    if missing := account_ids - known:
        raise UnknownAccountError(sorted(missing, key=str))

    resolved_id = transaction_id or uuid.uuid4()
    if transaction_id is not None:
        already = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.transaction_id == resolved_id)
        )
        if already:
            raise DuplicateTransactionError(resolved_id)

    stamp = timestamp or utc_now()
    session.add_all(
        [
            LedgerEntry(
                transaction_id=resolved_id,
                account_id=leg.account_id,
                amount_paise=leg.amount_paise,
                timestamp=stamp,
            )
            for leg in legs
        ]
    )
    await session.flush()
    return resolved_id


async def get_account_balance(session: AsyncSession, account_id: uuid.UUID) -> int:
    """Signed balance in paise. Aggregated in SQL, never in Python.

    Summing in the database keeps the operation O(index scan) and, critically,
    keeps it exact: ``BIGINT`` addition cannot lose a paisa the way a float
    accumulation in application code would.
    """
    balance = await session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return int(balance or 0)


async def get_balances(
    session: AsyncSession, account_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    """Balances for many accounts in one round trip."""
    statement = select(
        LedgerEntry.account_id,
        func.coalesce(func.sum(LedgerEntry.amount_paise), 0),
    ).group_by(LedgerEntry.account_id)
    if account_ids is not None:
        statement = statement.where(LedgerEntry.account_id.in_(list(account_ids)))
    rows = (await session.execute(statement)).all()
    return {row[0]: int(row[1]) for row in rows}


async def get_transaction_legs(
    session: AsyncSession, transaction_id: uuid.UUID
) -> tuple[LedgerEntry, ...]:
    """Every leg of one transaction, ordered deterministically for audit."""
    rows = await session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.transaction_id == transaction_id)
        .order_by(LedgerEntry.amount_paise.desc(), LedgerEntry.id)
    )
    return tuple(rows.all())


async def assert_ledger_balanced(session: AsyncSession) -> None:
    """Verify the global accounting identity: all entries sum to zero.

    A single query that must always return zero. Run it after every settlement
    batch and on a schedule. If it ever returns non-zero, something wrote to
    ``ledger_entries`` outside :func:`record_transaction`, and placement should
    halt until it is explained.

    Raises:
        UnbalancedTransactionError: The ledger as a whole does not balance.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount_paise), 0))
    )
    if int(total or 0) != 0:
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))
        raise UnbalancedTransactionError(int(total or 0), int(count or 0))
