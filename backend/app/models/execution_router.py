"""Smart Order Router persistence (Group 71): a routed order, its venue slices, each venue's circuit breaker.

A ``RoutedOrder`` is the parent: the stake the user asked for at one price, across the bookmakers it
may go to. Its ``RoutedSlice`` rows are the children: one per venue (one Vault account per bookmaker:
an order is never split across two accounts of the same book), each with a deterministic idempotency
key ``{order_id}_{venue_id}_{slice_index}`` and the UUID derived from it, which is both the Vault
reservation's ``order_ref`` and the CFO ledger's idempotency key, so a retried slice holds once and
bets once. ``VenueCircuitBreaker`` is one row per venue: consecutive failures and the pause they buy.

Every status moves forward only, by compare-and-set (``UPDATE ... WHERE status = <expected>``), so two
workers racing on the same slice (a dispatch and a release, a trip and a dispatch) cannot both win.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
ROUTER_MONEY = Numeric(18, 4)
ROUTER_ODDS = Numeric(10, 4)


class RoutedOrderStatus(StrEnum):
    ROUTING = "ROUTING"  # accepted, planning and reserving
    RESERVED = "RESERVED"  # every slice's stake held, the slippage guard not yet run
    DISPATCHING = "DISPATCHING"  # slices on their way to the venues
    FILLED = "FILLED"  # the whole stake matched
    PARTIAL = "PARTIAL"  # some of it matched: the position is legged
    UNCONFIRMED = "UNCONFIRMED"  # a venue never answered: held for reconciliation
    REJECTED = "REJECTED"  # nothing matched (refused at planning, or every venue said no)
    ABORTED = "ABORTED"  # the slippage guard stopped it before anything left


class SliceStatus(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # sent, no trustworthy answer: the stake stays held (it may be live)
    RELEASED = "RELEASED"  # never sent: the hold was given back


TERMINAL_ORDER_STATUSES = frozenset({RoutedOrderStatus.FILLED, RoutedOrderStatus.PARTIAL, RoutedOrderStatus.REJECTED, RoutedOrderStatus.ABORTED})
OPEN_SLICE_STATUSES = frozenset({SliceStatus.RESERVED, SliceStatus.DISPATCHED, SliceStatus.UNKNOWN})


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in enum)


class RoutedOrder(Base):
    __tablename__ = "routed_orders"
    __table_args__ = (
        CheckConstraint("desired_total_stake > 0", name="stake_positive"),
        CheckConstraint("odds > 1 AND min_acceptable_odds > 1 AND min_acceptable_odds <= odds", name="price_floor"),
        CheckConstraint("max_slippage_pct >= 0 AND max_slippage_pct <= 50", name="slippage_bounded"),
        CheckConstraint("filled_stake >= 0 AND filled_stake <= desired_total_stake", name="filled_bounded"),
        CheckConstraint(f"status IN ({_values(RoutedOrderStatus)})", name="status_known"),
        Index("ix_routed_orders_status_created", "status", "created_at"),
        Index("ix_routed_orders_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(String(96), unique=True)  # the caller's parent order id: a retry is the same order
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    match_id: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(128))
    odds: Mapped[Decimal] = mapped_column(ROUTER_ODDS)
    min_acceptable_odds: Mapped[Decimal] = mapped_column(ROUTER_ODDS)
    max_slippage_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    true_prob: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    desired_total_stake: Mapped[Decimal] = mapped_column(ROUTER_MONEY)
    currency: Mapped[str] = mapped_column(String(8))
    target_bookmakers: Mapped[list[str]] = mapped_column(JsonColumn)
    status: Mapped[str] = mapped_column(String(16), default=RoutedOrderStatus.ROUTING.value)
    filled_stake: Mapped[Decimal] = mapped_column(ROUTER_MONEY, default=Decimal(0))
    blended_odds: Mapped[Decimal | None] = mapped_column(ROUTER_ODDS, nullable=True)  # stake-weighted matched price
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # INSUFFICIENT_FLEET_BALANCE, SLIPPAGE_GUARD, ...
    detail: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # the deficit report, the guard's verdicts
    hedge_state: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ELIGIBLE: legged, for the portfolio's hedger
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nalanda_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nalanda_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoutedSlice(Base):
    __tablename__ = "routed_order_slices"
    __table_args__ = (
        UniqueConstraint("routed_order_id", "slice_index", name="uq_routed_order_slices_index"),
        CheckConstraint("stake > 0", name="stake_positive"),
        CheckConstraint("filled_stake >= 0 AND filled_stake <= stake", name="filled_bounded"),
        CheckConstraint(f"status IN ({_values(SliceStatus)})", name="status_known"),
        Index("ix_routed_order_slices_status_venue", "status", "venue_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    routed_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("routed_orders.id", ondelete="CASCADE"), index=True)
    slice_index: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(192), unique=True)  # {order_id}_{venue_id}_{slice_index}
    client_ref: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)  # uuid5 of the key: the reservation and ledger key
    venue_id: Mapped[str] = mapped_column(String(64))  # the bookmaker
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vault_bookmaker_accounts.id", ondelete="SET NULL"), nullable=True)
    stake: Mapped[Decimal] = mapped_column(ROUTER_MONEY)
    currency: Mapped[str] = mapped_column(String(8))
    quoted_odds: Mapped[Decimal | None] = mapped_column(ROUTER_ODDS, nullable=True)  # the venue's price when it was planned
    guard_odds: Mapped[Decimal | None] = mapped_column(ROUTER_ODDS, nullable=True)  # the price the slippage guard re-read
    commission: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal(0))
    net_ev: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=SliceStatus.RESERVED.value)
    filled_stake: Mapped[Decimal] = mapped_column(ROUTER_MONEY, default=Decimal(0))
    matched_odds: Mapped[Decimal | None] = mapped_column(ROUTER_ODDS, nullable=True)
    remote_bet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the bookmaker's confirmation
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VenueCircuitBreaker(Base):
    """One venue's breaker: ``ROUTER_BREAKER_FAILURES`` consecutive rejects or timeouts inside
    ``ROUTER_BREAKER_WINDOW_SECONDS`` pause it until ``paused_until``. A fill closes the streak."""

    __tablename__ = "venue_circuit_breakers"
    __table_args__ = (CheckConstraint("consecutive_failures >= 0 AND trips >= 0", name="counts_non_negative"),)

    venue_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    streak_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trips: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
