"""
Real-Money Betting Gateway Database Models (Group 95).
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

The live desk is strictly isolated from the Paper Trading Sandbox (Group 80): its own bankroll, tickets and cash
ledger, never a row of ``paper_*``. Money is exact (``Numeric(18, 2)`` rupees), never a float.

* ``real_bankrolls``: one real cash vault per user in INR (TODAR MAL): liquid cash, cash locked in open wagers,
  deposits and withdrawals, realized P&L after TDS, gross winnings, Section 115BBJ TDS withheld, the high-water mark,
  and SHIVAJI's circuit-breaker state for the current Indian trading day (00:00 IST): the day's starting bankroll,
  its realized losses, the consecutive-loss streak, the 15% daily stop-loss latch and the 30-minute cool-down.
* ``real_bet_tickets``: each assisted execution ticket ASHVATTHAMA builds for Parimatch (or 1xBet): the deep link,
  the one-click clipboard slip, the target odds and the minimum profitable floor, the Kelly stake (KUMBHA), the
  pillar score (KAUTILYA), then what the trader actually placed (odds, stake, the book's slip id, a sub-floor
  override) and the settlement (gross payout, 30% TDS, net profit).
* ``real_cash_transactions``: the immutable cash ledger: every movement of liquid cash with its exact balance before
  and after (deposit, withdrawal, stake locked, payout credit, TDS withheld, refund, stake forfeited).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
ODDS = Numeric(12, 4)
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


class RealBookmaker(StrEnum):
    PARIMATCH = "parimatch"  # primary: the INR capital account
    ONEXBET = "onexbet"  # secondary


class RealTicketStatus(StrEnum):
    PENDING_PLACEMENT = "PENDING_PLACEMENT"  # built, nothing placed, no money moved
    PLACED = "PLACED"  # the trader placed it on the book: the stake is locked
    WON = "WON"
    HALF_WON = "HALF_WON"  # quarter-ball Asian handicaps
    LOST = "LOST"
    HALF_LOST = "HALF_LOST"
    PUSH = "PUSH"
    VOID = "VOID"
    CANCELLED = "CANCELLED"
    CASHED_OUT = "CASHED_OUT"  # Group 114: the trader took the book's cashout; the amount realised is the return


OPEN_STATUSES = frozenset({RealTicketStatus.PENDING_PLACEMENT.value, RealTicketStatus.PLACED.value})


class RealTransactionType(StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    BET_STAKE_LOCKED = "BET_STAKE_LOCKED"
    BET_PAYOUT_CREDIT = "BET_PAYOUT_CREDIT"
    TDS_WITHHOLDING_DEBIT = "TDS_WITHHOLDING_DEBIT"
    BET_REFUND = "BET_REFUND"
    BET_STAKE_FORFEITED = "BET_STAKE_FORFEITED"  # a loss: the locked stake leaves exposure; liquid cash is unchanged


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class RealBankroll(Base):
    """
    Real money cash bankroll per user in INR (Commander TODAR MAL), with SHIVAJI's circuit-breaker state.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "real_bankrolls"
    __table_args__ = (
        UniqueConstraint("user_id"),  # uq_real_bankrolls_user_id: one vault per user
        CheckConstraint("cash_balance >= 0", name="cash_not_negative"),
        CheckConstraint("locked_exposure >= 0", name="exposure_not_negative"),
        CheckConstraint("currency = 'INR'", name="canonical_inr"),
        CheckConstraint("consecutive_losses >= 0", name="streak_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    locked_exposure: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    total_deposited: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    total_withdrawn: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    gross_winnings: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    total_tds_deducted: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    high_water_mark: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    daily_starting_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    daily_realized_loss: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_loss_triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_loss_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trading_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class RealBetTicket(Base):
    """
    Real-money bet ticket constructed by ASHVATTHAMA for Parimatch (or 1xBet).
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "real_bet_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_reference"),
        UniqueConstraint("verdict_id"),  # uq_real_bet_tickets_verdict_id: a signed slip is placed once (Group 114)
        CheckConstraint("placed_stake >= 0", name="ticket_stake_positive"),
        CheckConstraint("target_odds > 1", name="target_odds_valid"),
        CheckConstraint("placed_odds IS NULL OR placed_odds > 1", name="placed_odds_valid"),
        CheckConstraint(_in("bookmaker", RealBookmaker), name="bookmaker_valid"),
        CheckConstraint(_in("status", RealTicketStatus), name="status_valid"),
        Index("ix_real_bet_tickets_user_status", "user_id", "status"),
        Index("ix_real_bet_tickets_match", "match_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticket_reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    bookmaker: Mapped[str] = mapped_column(String(16), default=RealBookmaker.PARIMATCH.value, nullable=False)
    sport: Mapped[str] = mapped_column(String(64), nullable=False)
    league: Mapped[str] = mapped_column(String(128), nullable=False)
    match_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_name: Mapped[str] = mapped_column(String(255), nullable=False)
    market_type: Mapped[str] = mapped_column(String(100), nullable=False)
    selection: Mapped[str] = mapped_column(String(128), nullable=False)

    recommended_stake: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    placed_stake: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    target_odds: Mapped[Decimal] = mapped_column(ODDS, nullable=False)
    placed_odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)
    minimum_profitable_odds: Mapped[Decimal] = mapped_column(ODDS, nullable=False)
    true_probability: Mapped[float] = mapped_column(Float, nullable=False)
    model_edge_pct: Mapped[float] = mapped_column(Float, nullable=False)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25, nullable=False)

    deep_link_url: Mapped[str] = mapped_column(Text, nullable=False)
    clipboard_payload: Mapped[str] = mapped_column(Text, nullable=False)
    bookmaker_slip_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_selection: Mapped[str | None] = mapped_column(String(32), nullable=True)
    settleable_from_score: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Group 114: a slip placed from the Bet-Slip keeps its signed verdict and the Oracle bet that tracks its legs; the
    # ticket then settles (or is cashed out) as that bet does, so a parlay settles leg by leg from the scores
    verdict_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("consultant_verdicts.id", ondelete="SET NULL"), nullable=True)
    user_placed_bet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="SET NULL"), nullable=True, index=True)
    # Group 115: a multi-leg ticket the desk built (the system splitter), its legs as priced when it was recorded; on
    # placement they become the Oracle bet that settles it. None: a single-market ticket, or one placed from the Bet-Slip
    legs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=RealTicketStatus.PENDING_PLACEMENT.value, nullable=False)
    gross_return: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    settlement_payout: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    net_profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    tds_deducted: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)

    vetting_pillar_score: Mapped[float] = mapped_column(Float, default=90.0, nullable=False)
    odds_decay_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RealCashTransaction(Base):
    """
    Immutable double-entry cash ledger for real funds (Commander TODAR MAL).
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "real_cash_transactions"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_positive"),
        CheckConstraint(_in("transaction_type", RealTransactionType), name="transaction_type_valid"),
        Index("ix_real_cash_transactions_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("real_bet_tickets.id", ondelete="SET NULL"), nullable=True)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
