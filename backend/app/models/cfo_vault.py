"""CFO vault (Group 62): the cash ledger every Omni execution and settlement moves money through.

* ``BankrollAccount``: the row every money movement locks (``SELECT ... FOR UPDATE NOWAIT``). Each
  user has one main account (``bot_id`` NULL) and one isolated sub-account per Hive trading bot
  (Group 65): a bot's bets reserve, settle and lose only inside its own sub-account.
  ``available_balance`` is free cash, ``exposure_balance`` is cash riding on open bets,
  ``peak_balance`` the equity high-water mark the drawdown guard uses.
* ``LedgerEntry``: the double-entry journal behind those balances. Every posting writes legs that
  sum to exactly zero across AVAILABLE, EXPOSURE, PNL and EQUITY, so the materialised balances can
  always be re-derived and checked. Append-only.
* ``PhantomLedger``: one row per bet placed through the CFO ledger.
* ``AuditLog``: every execution attempt, block, outcome and settlement with its exact reason.
  Append-only: a database trigger refuses UPDATE and DELETE, and the ORM refuses them first.
* ``RiskGuardSettings``: each user's guard limits (Control Panel, Risk management).
* ``MarketResult``: the graded outcome of a market, which ``cfo.settle_markets`` settles against.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text
from sqlalchemy.types import JSON

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
ODDS = Numeric(10, 4)
PROBABILITY = Numeric(12, 10)
PERCENT = Numeric(5, 2)
RATE = Numeric(6, 4)  # a commission on net winnings, 0.0500 = 5%
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class LedgerStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    REJECTED = "REJECTED"
    VOID = "VOID"
    # Dead-letter queue: the resolver gave up (bookmaker unreachable, or no outcome long after
    # kick-off). The stake stays in exposure and nothing retries it until a person resolves it.
    REQUIRES_MANUAL_INTERVENTION = "REQUIRES_MANUAL_INTERVENTION"


OPEN_STATUSES = (LedgerStatus.PENDING, LedgerStatus.REQUIRES_MANUAL_INTERVENTION)  # stake still in exposure


class LedgerAccount(StrEnum):
    AVAILABLE = "AVAILABLE"  # free cash
    EXPOSURE = "EXPOSURE"  # cash riding on open bets
    PNL = "PNL"  # realised profit and loss (the income side of every settlement)
    EQUITY = "EQUITY"  # the opening balance's source


class PostingKind(StrEnum):
    OPEN = "OPEN"
    RESERVE = "RESERVE"  # execution: AVAILABLE -> EXPOSURE
    RELEASE = "RELEASE"  # rejected or void: EXPOSURE -> AVAILABLE
    SETTLE_WON = "SETTLE_WON"
    SETTLE_LOST = "SETTLE_LOST"
    ALLOCATE = "ALLOCATE"  # capital into a bot's sub-account (from the main account, or virtual for paper)
    DEALLOCATE = "DEALLOCATE"  # capital back out of it


class AuditEvent(StrEnum):
    EXECUTION_ATTEMPT = "EXECUTION_ATTEMPT"  # funds reserved, about to call the bookmaker
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"  # a guard or check refused the trade; nothing was reserved
    BOOKMAKER_REJECTED = "BOOKMAKER_REJECTED"  # definitive refusal: the reservation was rolled back
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"  # the bookmaker may have the bet: funds stay in exposure
    COMMIT_FAILED = "COMMIT_FAILED"  # placed at the bookmaker, ledger write failed: reconcile by hand
    SETTLED = "SETTLED"
    DEAD_LETTERED = "DEAD_LETTERED"  # moved to REQUIRES_MANUAL_INTERVENTION by the order resolver


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK rather than a native type: new members never need an ALTER TYPE migration
    return Enum(enum_cls, name=name, native_enum=False, create_constraint=True, length=32, validate_strings=True)


class AccountFunding(StrEnum):
    TRANSFER = "TRANSFER"  # real capital moved over from the main account (live bots)
    VIRTUAL = "VIRTUAL"  # notional capital (paper bots): never flows back into the main account


class BankrollAccount(Base):
    __tablename__ = "cfo_bankroll_accounts"
    __table_args__ = (
        CheckConstraint("available_balance >= 0", name="available_non_negative"),
        CheckConstraint("exposure_balance >= 0", name="exposure_non_negative"),
        CheckConstraint("peak_balance >= 0", name="peak_non_negative"),
        CheckConstraint("(bot_id IS NULL AND funding IS NULL) OR (bot_id IS NOT NULL AND funding IS NOT NULL)", name="funding_for_bots"),
        # One main account per user; one sub-account per bot
        Index("uq_cfo_bankroll_accounts_main", "user_id", unique=True, postgresql_where=text("bot_id IS NULL"), sqlite_where=text("bot_id IS NULL")),
        Index("uq_cfo_bankroll_accounts_bot", "bot_id", unique=True, postgresql_where=text("bot_id IS NOT NULL"), sqlite_where=text("bot_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="RESTRICT"), nullable=True)
    funding: Mapped[str | None] = mapped_column(String(16), nullable=True)  # AccountFunding, sub-accounts only
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    available_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    exposure_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    peak_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    postings: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # bumps on every movement
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    @property
    def equity(self) -> Decimal:
        return self.available_balance + self.exposure_balance


class PhantomLedger(Base):
    __tablename__ = "cfo_phantom_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_cfo_phantom_ledger_user_idempotency"),
        CheckConstraint("stake_inr > 0", name="stake_positive"),
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint("potential_pnl >= 0", name="potential_pnl_non_negative"),
        CheckConstraint("commission_rate >= 0 AND commission_rate < 0.5", name="commission_rate_range"),
        CheckConstraint(
            "(status IN ('PENDING', 'REQUIRES_MANUAL_INTERVENTION') AND settled_at IS NULL)"
            " OR (status NOT IN ('PENDING', 'REQUIRES_MANUAL_INTERVENTION') AND settled_at IS NOT NULL)",
            name="settlement_consistent",
        ),
        CheckConstraint("resolve_attempts >= 0", name="resolve_attempts_non_negative"),
        CheckConstraint("requested_stake_inr IS NULL OR requested_stake_inr >= stake_inr", name="fill_within_request"),
        Index("ix_cfo_phantom_ledger_user_status", "user_id", "status"),
        Index("ix_cfo_phantom_ledger_user_fixture_status", "user_id", "fixture_id", "status"),
        Index("ix_cfo_phantom_ledger_fixture_market_status", "fixture_id", "market", "status"),
        Index("ix_cfo_phantom_ledger_status_next_resolve", "status", "next_resolve_at"),
        Index("ix_cfo_phantom_ledger_user_group", "user_id", "group_id"),
        Index("ix_cfo_phantom_ledger_bot_status", "bot_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(64))
    bookmaker_id: Mapped[str] = mapped_column(String(64))
    remote_bet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the bookmaker's own bet id
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    stake_inr: Mapped[Decimal] = mapped_column(MONEY)
    odds: Mapped[Decimal] = mapped_column(ODDS)
    true_prob: Mapped[Decimal | None] = mapped_column(PROBABILITY, nullable=True)
    potential_pnl: Mapped[Decimal] = mapped_column(MONEY)  # profit if it wins: stake * (odds - 1) * (1 - commission_rate)
    commission_rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal(0), server_default="0")  # the venue's cut of the net win
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[LedgerStatus] = mapped_column(_enum(LedgerStatus, "cfo_ledger_status"), default=LedgerStatus.PENDING)
    # The bookmaker's answer never arrived: the bet may be live, so its stake stays in exposure
    reconcile_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    commence_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Order resolver (sniper.resolve_pending_orders): failed polls in a row, the next poll, the last error
    resolve_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_resolve_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_resolve_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Group 64: a partly matched order keeps what was asked for; the legs of one arbitrage or hedge share a group
    requested_stake_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)  # arbitrage | hedge (None: a single bet)
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # A bet at a foreign-currency venue: its stake there (stake_inr is the rupees it cost); None = INR
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    stake_ccy: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    # Group 65: the Hive bot whose sub-account holds this bet (None: the user's main account)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LedgerEntry(Base):
    __tablename__ = "cfo_ledger_entries"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        Index("ix_cfo_ledger_entries_user_account", "user_id", "account"),
        Index("ix_cfo_ledger_entries_journal", "journal_id"),
        Index("ix_cfo_ledger_entries_user_bot_account", "user_id", "bot_id", "account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))  # the legs of one posting share it
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cfo_phantom_ledger.id", ondelete="RESTRICT"), nullable=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hive_trading_bots.id", ondelete="RESTRICT"), nullable=True)  # whose sub-ledger
    kind: Mapped[PostingKind] = mapped_column(_enum(PostingKind, "cfo_posting_kind"))
    account: Mapped[LedgerAccount] = mapped_column(_enum(LedgerAccount, "cfo_ledger_account"))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # signed: debits to an asset account are positive
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "cfo_audit_log"
    __table_args__ = (
        Index("ix_cfo_audit_log_user_created", "user_id", "created_at"),
        Index("ix_cfo_audit_log_user_event_created", "user_id", "event", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    event: Mapped[AuditEvent] = mapped_column(_enum(AuditEvent, "cfo_audit_event"))
    reason: Mapped[str] = mapped_column(String(64))  # e.g. BLOCKED_BY_DRAWDOWN, BOOKMAKER_HTTP_500
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fixture_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stake_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)
    pnl_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)  # SETTLED rows: the drawdown guard sums these
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


class RiskGuardSettings(Base):
    __tablename__ = "cfo_risk_settings"
    __table_args__ = (
        CheckConstraint("daily_drawdown_pct >= 1 AND daily_drawdown_pct <= 50", name="drawdown_range"),
        CheckConstraint("max_market_exposure_pct >= 1 AND max_market_exposure_pct <= 50", name="market_exposure_range"),
        CheckConstraint("max_loss_streak >= 1 AND max_loss_streak <= 20", name="loss_streak_range"),
        CheckConstraint("velocity_max_cv_pct >= 0.5 AND velocity_max_cv_pct <= 20", name="velocity_range"),
        CheckConstraint("max_slippage_pct >= 0 AND max_slippage_pct <= 5", name="slippage_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    daily_drawdown_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("10.00"), server_default="10.00")
    max_market_exposure_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("10.00"), server_default="10.00")
    max_loss_streak: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    # Odds volatility ceiling: std dev of the last 60s of prices as a % of their mean
    velocity_max_cv_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("3.00"), server_default="3.00")
    # How far below the requested odds a fill may land (never below the +EV floor): min_acceptable_odds
    max_slippage_pct: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal("0.50"), server_default="0.50")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MarketResult(Base):
    __tablename__ = "cfo_market_results"
    __table_args__ = (
        UniqueConstraint("fixture_id", "market", name="uq_cfo_market_results_fixture_market"),
        CheckConstraint("(is_void AND winning_selection IS NULL) OR (NOT is_void AND winning_selection IS NOT NULL)", name="outcome_consistent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    winning_selection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_void: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())


# ---- Append-only tables: DB-level (authoritative) + ORM-level (fast fail) ----
APPEND_ONLY_TABLES = ("cfo_audit_log", "cfo_ledger_entries")

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION cfo_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% blocked)', TG_TABLE_NAME, TG_OP;
END;
$$;
"""


def append_only_trigger(table: str) -> str:
    return f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION cfo_append_only();"


class ImmutableRecordError(PermissionError):
    """Raised when code tries to change or delete an audit or journal row."""


for _model in (AuditLog, LedgerEntry):
    # DDL() %-formats its statement: the function body's literal % signs must be doubled
    event.listen(_model.__table__, "after_create", DDL(APPEND_ONLY_FUNCTION.replace("%", "%%")).execute_if(dialect="postgresql"))
    event.listen(_model.__table__, "after_create", DDL(append_only_trigger(_model.__tablename__)).execute_if(dialect="postgresql"))

    @event.listens_for(_model, "before_update")
    def _block_update(_mapper: Any, _connection: Any, target: Any) -> None:
        raise ImmutableRecordError(f"{type(target).__name__} rows are append-only")

    @event.listens_for(_model, "before_delete")
    def _block_delete(_mapper: Any, _connection: Any, target: Any) -> None:
        raise ImmutableRecordError(f"{type(target).__name__} rows are append-only")
