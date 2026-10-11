"""
Paper Trading Sandbox, Multi-Currency Exchange Rates & Tax Ledger Models (Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

* ``paper_accounts``: one virtual bankroll per user (default ₹1,00,000), canonical in INR, with its running
  counters (bets, results, staked, returned, P&L, the peak equity and the deepest drawdown) and resets.
* ``paper_bets``: each mock wager executed by ASHVATTHAMA at the price it could get on the target book (Parimatch by
  default): requested and executed odds, slippage and latency, the stake, the result (WON, LOST, PUSH, VOID,
  HALF_WON, HALF_LOST, CASHED_OUT), payout, P&L and the Section 115BBJ tax withheld from a win. A parlay carries its
  legs (``legs``), each settled through the Group 79 sport adapters.
* ``paper_ledger_entries``: every balance movement (stake, payout, cashout, reset) with the balance and equity after
  it: the series the drawdown and the Sharpe ratio are measured on.
* ``currency_exchange_rates``: TODAR MAL's latest rate per currency pair (INR, USD, EUR, GBP, USDT, BTC), with its
  provider and observation time; a stale rate is never used for a conversion.
* ``tax_ledger_entries``: FA-6's Section 115BBJ / 194BA record per winning bet: gross payout, stake, net winnings,
  30% TDS, the post-tax payout, the Indian financial year it falls in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
ODDS = Numeric(12, 4)
RATE = Numeric(28, 12)
DEFAULT_PAPER_BALANCE = Decimal("100000.00")


class PaperBetStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    PUSH = "PUSH"
    VOID = "VOID"
    HALF_WON = "HALF_WON"
    HALF_LOST = "HALF_LOST"
    CASHED_OUT = "CASHED_OUT"


class PaperStructure(StrEnum):
    SINGLE = "SINGLE"
    PARLAY = "PARLAY"


class PaperBetSource(StrEnum):
    MANUAL = "MANUAL"
    ASHOKA = "ASHOKA"
    SPORTS_ENGINE = "SPORTS_ENGINE"
    BACKTEST = "BACKTEST"  # FA-7: a backtested strategy forward-tested


class LedgerKind(StrEnum):
    OPEN = "OPEN"  # the account's starting capital
    STAKE = "STAKE"
    PAYOUT = "PAYOUT"
    CASHOUT = "CASHOUT"
    RESET = "RESET"


SETTLED_STATUSES: tuple[str, ...] = tuple(s.value for s in PaperBetStatus if s is not PaperBetStatus.PENDING)


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class PaperAccount(Base):
    """
    Virtual Paper Trading Account for forward-testing value bets with dummy capital.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "paper_accounts"
    __table_args__ = (
        UniqueConstraint("user_id"),  # uq_paper_accounts_user_id: one sandbox per user
        CheckConstraint("virtual_balance >= 0", name="balance_not_negative"),
        CheckConstraint("initial_balance > 0", name="initial_positive"),
        CheckConstraint("currency = 'INR'", name="canonical_inr"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")  # canonical; other currencies are display conversions
    virtual_balance: Mapped[Decimal] = mapped_column(MONEY, default=DEFAULT_PAPER_BALANCE)
    initial_balance: Mapped[Decimal] = mapped_column(MONEY, default=DEFAULT_PAPER_BALANCE)
    total_paper_bets: Mapped[int] = mapped_column(Integer, default=0)
    won_bets: Mapped[int] = mapped_column(Integer, default=0)  # WON and HALF_WON
    lost_bets: Mapped[int] = mapped_column(Integer, default=0)  # LOST and HALF_LOST
    push_bets: Mapped[int] = mapped_column(Integer, default=0)  # PUSH and VOID
    cashed_out_bets: Mapped[int] = mapped_column(Integer, default=0)
    total_staked: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))  # settled bets only
    total_returned: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    total_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)  # total P&L / initial balance x 100
    total_tds: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    peak_equity: Mapped[Decimal] = mapped_column(MONEY, default=DEFAULT_PAPER_BALANCE)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    resets: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    bets: Mapped[list[PaperBet]] = relationship(back_populates="account", cascade="all, delete-orphan", passive_deletes=True)


class PaperBet(Base):
    """
    Individual Mock / Paper Bet executed at live market odds with virtual currency.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "paper_bets"
    __table_args__ = (
        CheckConstraint("stake_amount > 0", name="stake_positive"),
        CheckConstraint("odds >= 1 AND executed_odds >= 1", name="odds_valid"),
        CheckConstraint(_in("status", PaperBetStatus), name="status_known"),
        CheckConstraint(_in("structure", PaperStructure), name="structure_known"),
        CheckConstraint(_in("source", PaperBetSource), name="source_known"),
        Index("ix_paper_bets_status_placed", "status", "placed_at"),
        Index("ix_paper_bets_account_placed", "account_id", "placed_at"),
        Index("ix_paper_bets_match", "match_id"),
        UniqueConstraint("account_id", "client_ref"),  # uq_paper_bets_account_id: one execution per client ref
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"))
    client_ref: Mapped[str] = mapped_column(String(64))  # idempotency key (the client's, or the bet id)
    structure: Mapped[str] = mapped_column(String(8), default=PaperStructure.SINGLE.value)
    sport_key: Mapped[str] = mapped_column(String(64))  # the first leg's sport for a parlay
    match_id: Mapped[str] = mapped_column(String(128))
    event_name: Mapped[str] = mapped_column(String(255))
    market_type: Mapped[str] = mapped_column(String(100))  # a Group 79 market key ("1x2_match_odds", "point_spread:h1"...)
    selection: Mapped[str] = mapped_column(String(128))
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # parlays: every leg and its status
    odds: Mapped[Decimal] = mapped_column(ODDS)  # what the trader asked for
    executed_odds: Mapped[Decimal] = mapped_column(ODDS)  # what ASHVATTHAMA got on the target book
    stake_amount: Mapped[Decimal] = mapped_column(MONEY)
    potential_payout: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(12), default=PaperBetStatus.PENDING.value, index=True)
    payout: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    tds_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    net_post_tax_payout: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    target_bookmaker: Mapped[str] = mapped_column(String(32), default="parimatch")
    source: Mapped[str] = mapped_column(String(16), default=PaperBetSource.MANUAL.value)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)  # FA-7: the strategy being forward-tested
    fair_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # pillar 15's 0-100
    kelly_stake: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)  # FA-4's sport Kelly stake at placement
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # ASHVATTHAMA's receipt
    discipline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # FA-2's read at placement
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settlement_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[PaperAccount] = relationship(back_populates="bets")


class PaperLedgerEntry(Base):
    """
    One movement of a paper account's virtual balance, with the balance and equity after it.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "paper_ledger_entries"
    __table_args__ = (
        CheckConstraint(_in("kind", LedgerKind), name="kind_known"),
        Index("ix_paper_ledger_entries_account_created", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"))
    bet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_bets.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # signed: a stake is negative
    balance_after: Mapped[Decimal] = mapped_column(MONEY)
    equity_after: Mapped[Decimal] = mapped_column(MONEY)  # balance + open stakes at cost
    epoch: Mapped[int] = mapped_column(Integer, default=0)  # the account's reset count when it was written
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CurrencyExchangeRate(Base):
    """
    Live FX Exchange Rates cached for multi-currency conversion across bookmakers (Commander TODAR MAL).
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "currency_exchange_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency"),  # uq_currency_exchange_rates_base_currency
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    base_currency: Mapped[str] = mapped_column(String(8))
    quote_currency: Mapped[str] = mapped_column(String(8))
    rate: Mapped[Decimal] = mapped_column(RATE)  # quote units per one base unit
    provider: Mapped[str] = mapped_column(String(64))
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TaxLedgerEntry(Base):
    """
    FA-6 Global Tax & Indian Section 115BBJ 30% TDS Ledger Entry.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    __tablename__ = "tax_ledger_entries"
    __table_args__ = (
        UniqueConstraint("paper_bet_id"),  # uq_tax_ledger_entries_paper_bet_id: one entry per winning bet
        CheckConstraint("net_winnings > 0 AND tds_amount >= 0", name="amounts_valid"),
        CheckConstraint("mode IN ('PAPER', 'LIVE')", name="mode_known"),
        Index("ix_tax_ledger_entries_user_fy", "user_id", "financial_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    paper_bet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_bets.id", ondelete="SET NULL"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="PAPER")
    gross_payout: Mapped[Decimal] = mapped_column(MONEY)
    stake: Mapped[Decimal] = mapped_column(MONEY)
    net_winnings: Mapped[Decimal] = mapped_column(MONEY)
    tds_rate: Mapped[float] = mapped_column(Float)
    tds_amount: Mapped[Decimal] = mapped_column(MONEY)
    net_post_tax_payout: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    jurisdiction: Mapped[str] = mapped_column(String(4), default="IN")
    section: Mapped[str] = mapped_column(String(16), default="115BBJ")
    financial_year: Mapped[str] = mapped_column(String(7))  # "2026-27": April to March
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
