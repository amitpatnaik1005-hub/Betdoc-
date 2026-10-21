"""TODAR MAL's exchange rates and the Section 115BBJ tax ledger (Group 80; moved here when Group 97 retired paper trading).

Developed for Amit Ashok Kumar Patnaik.

* ``currency_exchange_rates``: the latest rate per currency pair (INR, USD, EUR, GBP, USDT, BTC), with its provider and
  observation time; a stale rate is never used for a conversion.
* ``tax_ledger_entries``: the Section 115BBJ / 194BA record per winning bet: gross payout, stake, net winnings, 30% TDS,
  the post-tax payout and the Indian financial year it falls in. Real-money entries are ``mode='LIVE'``. Rows written
  by the retired sandbox keep ``mode='PAPER'`` and their ``paper_bet_id`` (a plain historical id: the paper tables are
  no longer mapped, so the foreign key was dropped in revision a97c5e1d2f30).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
RATE = Numeric(28, 12)


class CurrencyExchangeRate(Base):
    """Live FX rates cached for conversion across bookmakers (TODAR MAL). Developer Credit: Amit Ashok Kumar Patnaik."""

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
    """FA-6 Section 115BBJ 30% TDS ledger entry. Developer Credit: Amit Ashok Kumar Patnaik."""

    __tablename__ = "tax_ledger_entries"
    __table_args__ = (
        UniqueConstraint("paper_bet_id"),  # uq_tax_ledger_entries_paper_bet_id: one entry per (historical) paper bet
        CheckConstraint("net_winnings > 0 AND tds_amount >= 0", name="amounts_valid"),
        CheckConstraint("mode IN ('PAPER', 'LIVE')", name="mode_known"),  # PAPER: history from the retired sandbox
        Index("ix_tax_ledger_entries_user_fy", "user_id", "financial_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    paper_bet_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="LIVE")
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
