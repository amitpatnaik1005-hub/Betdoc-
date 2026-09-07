"""SQLAlchemy 2.0 schema for the BetDoc persistence vault.

Invariants encoded at the schema level rather than in application code:

* Money is **integer paise** in ``BigInteger``. No float, no numeric drift.
* Every timestamp is timezone-aware on every backend, guaranteed by
  :class:`UtcDateTime` rather than by convention.
* Primary keys are :class:`uuid.UUID` through the generic ``Uuid`` type, so the
  identical DDL runs on PostgreSQL and on the SQLite test suite.
* ``OddsTick`` carries a composite ``(id, timestamp)`` primary key because
  TimescaleDB requires the partitioning column in every unique index.
* ``LedgerEntry`` is append-only by contract: no update or delete path is
  exposed anywhere in this package.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Dialect,
    Enum,
    Float,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, Uuid

__all__ = [
    "AccountType",
    "Base",
    "BetRecord",
    "BetStatus",
    "LedgerAccount",
    "LedgerEntry",
    "OddsTick",
    "UtcDateTime",
    "utc_now",
]

_STRING_ENUM_KWARGS: Final[dict[str, Any]] = {
    # VARCHAR + CHECK instead of a native PG ENUM: portable to SQLite, and
    # adding a member later is a CHECK change rather than an ALTER TYPE that
    # cannot run inside a transaction.
    "native_enum": False,
    "validate_strings": True,
    "values_callable": lambda enum_class: [member.value for member in enum_class],
}


def utc_now() -> datetime:
    """Timezone-aware UTC now. The only accepted default for a timestamp."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware ``DateTime`` that is actually timezone-aware everywhere.

    ``DateTime(timezone=True)`` is honoured by PostgreSQL and silently ignored
    by SQLite, which drops the offset on write and returns a naive value on
    read. That asymmetry means a test suite on SQLite cannot verify the
    timezone guarantee it is supposed to protect.

    This decorator normalises every bound value to UTC and re-attaches UTC on
    every result, so the invariant "no naive datetime ever crosses the ORM
    boundary" holds on both backends.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetime rejected: attach a timezone before persisting"
            raise ValueError(msg)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base. Modern 2.0 style, no legacy ``declarative_base()``."""

    type_annotation_map = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: UtcDateTime(),
        int: BigInteger(),
    }

    def __repr__(self) -> str:
        identity = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identity!r}>"


class BetStatus(enum.Enum):
    """Terminal and non-terminal settlement states of a placed bet."""

    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    VOID = "void"
    HALF_WON = "half_won"
    HALF_LOST = "half_lost"

    @property
    def is_terminal(self) -> bool:
        return self is not BetStatus.PENDING

    @property
    def win_fraction(self) -> float:
        """Portion of stake that settled at full odds."""
        return {
            BetStatus.WON: 1.0,
            BetStatus.HALF_WON: 0.5,
            BetStatus.PENDING: 0.0,
            BetStatus.LOST: 0.0,
            BetStatus.HALF_LOST: 0.0,
            BetStatus.VOID: 0.0,
        }[self]

    @property
    def stake_refund_fraction(self) -> float:
        """Portion of stake returned unstaked (the pushed part)."""
        return {
            BetStatus.WON: 0.0,
            BetStatus.HALF_WON: 0.5,
            BetStatus.PENDING: 0.0,
            BetStatus.LOST: 0.0,
            BetStatus.HALF_LOST: 0.5,
            BetStatus.VOID: 1.0,
        }[self]


class AccountType(enum.Enum):
    """Standard accounting classification, driving the normal balance sign."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"

    @property
    def normal_balance_sign(self) -> int:
        """``+1`` for debit-normal accounts, ``-1`` for credit-normal ones."""
        return 1 if self in (AccountType.ASSET, AccountType.EXPENSE) else -1


class OddsTick(Base):
    """One bookmaker's price for one outcome at one instant.

    Composite ``(id, timestamp)`` primary key is mandatory, not stylistic:
    ``create_hypertable('odds_ticks', 'timestamp')`` fails if the partitioning
    column is absent from every unique index, so a bare ``id`` primary key
    would make the table impossible to convert.

    The natural-key unique constraint makes ingestion idempotent, which lets
    concurrent pollers race safely with ``ON CONFLICT DO NOTHING`` instead of
    coordinating.
    """

    __tablename__ = "odds_ticks"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(default=utc_now)

    bookmaker: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    market_key: Mapped[str] = mapped_column(String(128))
    outcome_key: Mapped[str] = mapped_column(String(64))
    decimal_odds: Mapped[float] = mapped_column(Float)

    __table_args__ = (
        PrimaryKeyConstraint("id", "timestamp", name="pk_odds_ticks"),
        UniqueConstraint(
            "bookmaker",
            "event_id",
            "market_key",
            "outcome_key",
            "timestamp",
            name="uq_odds_ticks_natural_key",
        ),
        Index(
            "ix_odds_ticks_lookup",
            "event_id",
            "market_key",
            "outcome_key",
            "timestamp",
        ),
        Index("ix_odds_ticks_bookmaker_timestamp", "bookmaker", "timestamp"),
        CheckConstraint("decimal_odds > 1.0", name="ck_odds_ticks_odds_above_one"),
    )


class BetRecord(Base):
    """A placed bet. Immutable except for its settlement transition."""

    __tablename__ = "bet_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128))
    market_key: Mapped[str] = mapped_column(String(128))
    outcome_key: Mapped[str] = mapped_column(String(64))
    bookmaker: Mapped[str] = mapped_column(String(64))
    odds: Mapped[float] = mapped_column(Float)
    stake_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[BetStatus] = mapped_column(
        Enum(BetStatus, name="bet_status", **_STRING_ENUM_KWARGS),
        default=BetStatus.PENDING,
    )
    placed_at: Mapped[datetime] = mapped_column(default=utc_now)
    settled_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint("stake_paise > 0", name="ck_bet_records_stake_positive"),
        CheckConstraint("odds > 1.0", name="ck_bet_records_odds_above_one"),
        CheckConstraint(
            "(status = 'pending' AND settled_at IS NULL)"
            " OR (status <> 'pending' AND settled_at IS NOT NULL)",
            name="ck_bet_records_settlement_coherent",
        ),
        Index("ix_bet_records_status_placed_at", "status", "placed_at"),
        Index("ix_bet_records_event", "event_id", "market_key", "outcome_key"),
        Index("ix_bet_records_bookmaker_placed_at", "bookmaker", "placed_at"),
    )


class LedgerAccount(Base):
    """A named account in the double-entry chart of accounts."""

    __tablename__ = "ledger_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type", **_STRING_ENUM_KWARGS)
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    entries: Mapped[list[LedgerEntry]] = relationship(
        back_populates="account", lazy="raise", passive_deletes=False
    )

    __table_args__ = (Index("ix_ledger_accounts_type", "account_type"),)


class LedgerEntry(Base):
    """One leg of a balanced transaction. Append-only.

    Sign convention: positive is a **debit**, negative is a **credit**. Every
    ``transaction_id`` group must sum to exactly zero, which is enforced in
    :mod:`ledger` before any row is written.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), index=True
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    timestamp: Mapped[datetime] = mapped_column(default=utc_now)

    account: Mapped[LedgerAccount] = relationship(back_populates="entries", lazy="raise")

    __table_args__ = (
        CheckConstraint("amount_paise <> 0", name="ck_ledger_entries_amount_nonzero"),
        Index("ix_ledger_entries_account_timestamp", "account_id", "timestamp"),
        Index("ix_ledger_entries_transaction", "transaction_id", "account_id"),
    )
