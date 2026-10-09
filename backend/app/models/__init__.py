"""
BetDoc: Todar Mal ledger schema (SQLAlchemy 2.0, PostgreSQL).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Called on each insert. Passing datetime.now(timezone.utc) directly as the
    default would freeze one timestamp at import time and reuse it for every row."""
    return datetime.now(timezone.utc)


# Fixed constraint names keep Alembic autogenerate migrations stable
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """AsyncAttrs adds `await obj.awaitable_attrs.<rel>` for lazy relationships
    under AsyncSession."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


BET_STATUSES: tuple[str, ...] = (
    "PENDING", "PENDING_NETWORK", "ACCEPTED", "REJECTED", "UNKNOWN",
    "WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "CASH_OUT",
)
RESOLVED_BET_STATUSES: tuple[str, ...] = (
    "WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "CASH_OUT",
)


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="QUANT", server_default="QUANT")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())

    exchange_accounts: Mapped[List["ExchangeAccount"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    risk_mandate: Mapped[Optional["RiskMandate"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class ExchangeAccount(Base):
    __tablename__ = "exchange_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "exchange_name", name="uix_user_exchange"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    exchange_name: Mapped[str] = mapped_column(String(64))
    
    # Ciphertext only. Encrypt and decrypt in the service layer.
    api_key_encrypted: Mapped[str] = mapped_column(String)
    api_secret_encrypted: Mapped[str] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(default=1, server_default="1") # Added for KMS rotation
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    user: Mapped["User"] = relationship(back_populates="exchange_accounts")

    # FIXED: Never cascade delete the ledger! If an account is deleted, RESTRICT it to preserve financial history.
    bets: Mapped[List["BetLedger"]] = relationship(
        back_populates="exchange_account",
        passive_deletes=False,
    )

    def __repr__(self) -> str:
        return f"<ExchangeAccount id={self.id} exchange={self.exchange_name!r} active={self.is_active}>"


class RiskMandate(Base):
    __tablename__ = "risk_mandates"
    __table_args__ = (
        CheckConstraint("max_stake_per_bet > 0", name="max_stake_positive"),
        CheckConstraint("max_daily_exposure > 0", name="max_exposure_positive"),
        CheckConstraint("max_stake_per_bet <= max_daily_exposure", name="stake_within_exposure"),
        CheckConstraint("kill_threshold_pct >= 0 AND kill_threshold_pct <= 100", name="kill_threshold_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    max_stake_per_bet: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=4))
    max_daily_exposure: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=4))
    kill_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=4))

    user: Mapped["User"] = relationship(back_populates="risk_mandate")


class BetLedger(Base):
    __tablename__ = "bet_ledger"
    __table_args__ = (
        CheckConstraint("stake > 0", name="stake_positive"),
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint("true_probability >= 0 AND true_probability <= 1", name="true_probability_range"),
        CheckConstraint(f"status IN ({_sql_in_list(BET_STATUSES)})", name="status_valid"),
        CheckConstraint(
            f"status NOT IN ({_sql_in_list(RESOLVED_BET_STATUSES)}) OR resolved_at IS NOT NULL",
            name="resolution_consistent",
        ),
        CheckConstraint("payout IS NULL OR payout >= 0", name="payout_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        UniqueConstraint("exchange_account_id", "exchange_bet_id", name="uq_bet_ledger_account_exchange_bet"),
        Index("ix_bet_ledger_account_placed_at", "exchange_account_id", "placed_at"),
        Index("ix_bet_ledger_account_status", "exchange_account_id", "status"),
        Index(
            "ix_bet_ledger_sweep_pending",
            "status",
            "placed_at",
            postgresql_where=text("status = 'PENDING_NETWORK'")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    
    # FIXED: ondelete="RESTRICT" to prevent accidental ledger wiping
    exchange_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="RESTRICT"))
    
    # ADDED: External exchange reference ID for reconciliation
    exchange_bet_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    
    match_id: Mapped[str] = mapped_column(String(128), index=True)
    
    # ADDED: market_type to differentiate O/U vs Match Odds for the same match
    market_type: Mapped[str] = mapped_column(String(64)) 
    selection: Mapped[str] = mapped_column(String(255))
    
    # ADDED: Currency tracking
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    
    odds: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=4))
    stake: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=4))
    
    # ADDED: payout tracking for settled bets
    payout: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=16, scale=4), nullable=True)
    
    # FIXED: Increased precision to scale=6 to prevent edge calculation drift
    true_probability: Mapped[Decimal] = mapped_column(Numeric(precision=16, scale=6))
    
    status: Mapped[str] = mapped_column(String(32), default="PENDING", server_default="PENDING", index=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    exchange_account: Mapped["ExchangeAccount"] = relationship(back_populates="bets")

from app.models.risk import StopLossConfigModel, StopLossEventModel  # noqa: E402,F401
from app.models.market_signals import MarketTickModel  # noqa: E402,F401
from app.models.the_lab import ResearchReportModel, ExperimentModel  # noqa: E402,F401
# Must stay at the bottom: odds.py imports Base from this package
from app.models.odds import OddsSnapshot  # noqa: E402,F401
from app.models.the_hive import BotProfileModel, HiveTaskModel, HiveTaskDependencyModel, SelfLearningLogModel  # noqa: E402,F401

from app.models.the_core import CoreEngineMetricsModel, SmallcaseRegistryModel, TestBenchRunModel, BacktestJobModel

from app.models.popular_picks import PopularParlayModel, ParlayReviewGateModel

from app.models.competitive_intel import CompetitorBotModel, FeatureGapAlertModel, DevSuggestionModel

from app.models.oracle_scout import OracleScoutHistoryModel

from app.models.archive import ArchiveAccessLogModel

from app.models.control_panel import SystemSettingsModel

# Groups 54-58: registered so Alembic autogenerate sees them (otherwise it emits DROP TABLE).
from app.models.bookmakers import BookmakerConfigModel  # noqa: E402,F401
from app.models.cfo import CfoAlertModel, TaxRecordModel, StressTestResultModel, CfoAdvisoryModel  # noqa: E402,F401
from app.models.human_touch import HumanTouchConfigModel, MatchNarrativeModel, HumanOverrideLogModel  # noqa: E402,F401
from app.models.phantom import ArbitrageOpportunityModel, PhantomCalculationLogModel  # noqa: E402,F401
from app.models.sports import SportConfigModel  # noqa: E402,F401
from app.models.omni_vault import OmniFleetSource, OmniProviderConfig, OmniProviderEndpoint, OmniRawPayload, OmniQuarantineLog  # noqa: E402,F401
from app.models.canonical import CanonicalEntity  # noqa: E402,F401
# Module import, not names: integration.models imports Base from here, so it may still be initialising.
import app.domain.integration.models  # noqa: E402,F401
from app.models.cfo_vault import AuditLog, BankrollAccount, LedgerEntry, MarketResult, PhantomLedger, RiskGuardSettings  # noqa: E402,F401
from app.models.execution import EntityMapping, ExecutionVenue  # noqa: E402,F401
from app.models.hive_bots import HiveBotEvent, HiveOrderPlan, HiveShadowPosition, TradingBot  # noqa: E402,F401
from app.models.lab_quant import LabBacktestRun, LabFixture, LabFixtureResult, LabFxRate, LabOddsTick  # noqa: E402,F401
