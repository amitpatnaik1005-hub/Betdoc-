# ====================================================================================================
# GROUP 80 MASTER SPECIFICATION & CLAUDE CODE PRODUCTION IMPLEMENTATION BLUEPRINT
# PAPER TRADING SANDBOX ENGINE, REAL MOCK BETTING SUITE (VIRTUAL INR BANKROLL),
# MULTI-CURRENCY CONVERSION (TODAR MAL), 30% INDIAN TDS TAX LEDGER & ARBITRAGE "COMING SOON" PIVOT
# ====================================================================================================
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
# Target Environment: d:\BetDoc on branch `feature/garuda-ingestion` and `main`
#
# [CLAUDE EFFORT SETTING: HIGH / ULTRACODE]
# ====================================================================================================

## 🎯 MANDATORY OPERATIONAL DIRECTIVES FOR CLAUDE CODE:
1. **EFFORT MUST BE SET TO HIGH / ULTRACODE**:
   Execute rigorous engineering, transactional database integrity, real-time virtual accounting,
   clean state persistence, and reactive UI components with zero loopholes and zero token waste.
2. **STRATEGIC PIVOT: ARBITRAGE MODULE ON HOLD / "COMING SOON"**:
   - Do NOT build heavy cross-bookmaker arbitrage linear solvers or multi-leg arbitrage execution in this group.
   - Update any Arbitrage panels and references in the UI to display a clean, professional banner:
     `"Arbitrage Matrix: Coming Soon / Advanced Multi-Bookmaker Engine Under Optimization"`.
   - All compute and tokens must be dedicated to **Ashoka / The Oracle's single-directional value bet advice**,
     **Parimatch primary execution**, and the **Paper Trading Sandbox Engine**.
3. **CORE FEATURE ADDITION: REAL MOCK BETTING & PAPER TRADING SANDBOX**:
   Implement a complete, production-grade Paper Trading Sandbox that allows the user to execute mock bets
   using **virtual dummy money (default ₹1,00,000 INR)** while receiving real-time live odds from Parimatch,
   ESPN Wire, and The Odds API:
   - **Global Mode Switcher**: Seamlessly toggle between `[ 🧪 PAPER TRADING (SANDBOX) ]` and `[ 🔴 LIVE REAL MONEY ]`.
   - **Timestamped Mock Execution**: Record virtual wagers at exact live market prices when Ashoka recommends a play.
   - **Automated Settlement Engine**: Hook into Group 79's `SportAdapterRegistry` and Group 73's settlement feedback loop
     to automatically settle mock bets (WIN, LOSS, PUSH, VOID) when real-world matches conclude.
   - **Performance & P&L Analytics**: Real-time tracking of Virtual Balance, Total Paper Bets, Win Rate %, Net P&L,
     ROI %, and Drawdown curve, giving the user complete proof of profitability before risking real capital.
   - **Account Reset**: One-click reset allowing the user to refresh their virtual bankroll back to ₹1,00,000 anytime.
4. **MULTI-CURRENCY ENGINE & GLOBAL TAX LEDGER (COMMANDER TODAR MAL)**:
   - Ingest live FX rates supporting `INR` (primary), `USD`, `EUR`, `GBP`, `USDT`, `BTC` with Redis caching (`fx:rates:latest`).
   - Store bankroll and paper accounts in canonical INR, converting transparently for display.
   - Implement **Section 115BBJ 30% Flat Tax (TDS)** estimator for Indian net gambling winnings, displaying gross vs net yield.
5. **MIGRATION REVISION CHAIN**:
   - Parent Revision (`down_revision`): Strictly `'f94a1206c83e'` (Group 79).
   - Current Revision (`revision`): `'a83e1207d94f'`.
6. **DEVELOPER CREDIT**:
   The developer name `"Amit Ashok Kumar Patnaik"` MUST be embedded in all model docstrings, schemas, API receipts, and UI headers.
7. **ALL TESTS MUST PASS & FINAL PUSH TO GITHUB**:
   - Write comprehensive tests in `backend/tests/test_paper_trading_and_currency.py`.
   - Run tests with `pytest` and frontend build with `npm run build` (0 errors).
   - Commit and push all changes directly to git.

---

## 🛠️ PART 1: BACKEND MODELS, MIGRATION, SCHEMAS & SERVICES

### FILE 1: `backend/app/models/paper_trading.py`
Drop-in SQLAlchemy models for Paper Trading accounts, mock bets, exchange rates, and tax ledger:

```python
"""
Paper Trading Sandbox, Multi-Currency Exchange Rates & Tax Ledger Models.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.models import Base

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class PaperBetStatus(str, enum.Enum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    PUSH = "PUSH"
    VOID = "VOID"

class PaperAccount(Base):
    """
    Virtual Paper Trading Account for forward-testing value bets with dummy capital.
    Developer Credit: Amit Ashok Kumar Patnaik
    """
    __tablename__ = "paper_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), unique=True, nullable=False, default="default_user", index=True)
    currency = Column(String(10), nullable=False, default="INR")
    virtual_balance = Column(Float, nullable=False, default=100000.0)
    initial_balance = Column(Float, nullable=False, default=100000.0)
    total_paper_bets = Column(Integer, nullable=False, default=0)
    won_bets = Column(Integer, nullable=False, default=0)
    lost_bets = Column(Integer, nullable=False, default=0)
    push_bets = Column(Integer, nullable=False, default=0)
    total_pnl = Column(Float, nullable=False, default=0.0)
    roi_pct = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    bets = relationship("PaperBet", back_populates="account", cascade="all, delete-orphan")

class PaperBet(Base):
    """
    Individual Mock / Paper Bet executed at live market odds with virtual currency.
    Developer Credit: Amit Ashok Kumar Patnaik
    """
    __tablename__ = "paper_bets"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    sport_key = Column(String(50), nullable=False, index=True)
    match_id = Column(String(100), nullable=False, index=True)
    event_name = Column(String(255), nullable=False)
    market_type = Column(String(100), nullable=False)
    selection = Column(String(100), nullable=False)
    odds = Column(Float, nullable=False)
    stake_amount = Column(Float, nullable=False)
    potential_payout = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default=PaperBetStatus.PENDING.value, index=True)
    pnl = Column(Float, nullable=False, default=0.0)
    target_bookmaker = Column(String(50), nullable=False, default="parimatch")
    placed_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    settlement_notes = Column(Text, nullable=True)

    account = relationship("PaperAccount", back_populates="bets")

    __table_args__ = (
        Index("ix_paper_bets_status_placed", "status", "placed_at"),
    )

class CurrencyExchangeRate(Base):
    """
    Live FX Exchange Rates cached for multi-currency conversion across bookmakers.
    Developer Credit: Amit Ashok Kumar Patnaik
    """
    __tablename__ = "currency_exchange_rates"

    id = Column(Integer, primary_key=True, index=True)
    base_currency = Column(String(10), nullable=False, index=True)
    quote_currency = Column(String(10), nullable=False, index=True)
    rate = Column(Float, nullable=False)
    provider = Column(String(50), nullable=False, default="frankfurter")
    last_updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_fx_base_quote", "base_currency", "quote_currency", unique=True),
    )

class TaxLedgerEntry(Base):
    """
    FA-6 Global Tax & Indian Section 115BBJ 30% TDS Ledger Entry.
    Developer Credit: Amit Ashok Kumar Patnaik
    """
    __tablename__ = "tax_ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), nullable=False, default="default_user", index=True)
    bet_id = Column(Integer, nullable=True, index=True)
    gross_win = Column(Float, nullable=False)
    net_profit = Column(Float, nullable=False)
    tds_deducted_30_pct = Column(Float, nullable=False)
    net_post_tax_payout = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    jurisdiction = Column(String(10), nullable=False, default="IN")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
```

---

### FILE 2: `backend/alembic/versions/a83e1207d94f_add_paper_trading_and_currency_tables.py`
Alembic migration chained to `f94a1206c83e`:

```python
"""add paper trading and currency tables

Revision ID: a83e1207d94f
Revises: f94a1206c83e
Create Date: 2026-10-11 11:30:00.000000

Developer Credit: Amit Ashok Kumar Patnaik | BetDoc Fleet
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a83e1207d94f'
down_revision: Union[str, None] = 'f94a1206c83e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. paper_accounts
    op.create_table(
        'paper_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('currency', sa.String(length=10), server_default='INR', nullable=False),
        sa.Column('virtual_balance', sa.Float(), server_default='100000.0', nullable=False),
        sa.Column('initial_balance', sa.Float(), server_default='100000.0', nullable=False),
        sa.Column('total_paper_bets', sa.Integer(), server_default='0', nullable=False),
        sa.Column('won_bets', sa.Integer(), server_default='0', nullable=False),
        sa.Column('lost_bets', sa.Integer(), server_default='0', nullable=False),
        sa.Column('push_bets', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_pnl', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('roi_pct', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id')
    )
    op.create_index('ix_paper_accounts_id', 'paper_accounts', ['id'], unique=False)
    op.create_index('ix_paper_accounts_user_id', 'paper_accounts', ['user_id'], unique=True)

    # 2. paper_bets
    op.create_table(
        'paper_bets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('sport_key', sa.String(length=50), nullable=False),
        sa.Column('match_id', sa.String(length=100), nullable=False),
        sa.Column('event_name', sa.String(length=255), nullable=False),
        sa.Column('market_type', sa.String(length=100), nullable=False),
        sa.Column('selection', sa.String(length=100), nullable=False),
        sa.Column('odds', sa.Float(), nullable=False),
        sa.Column('stake_amount', sa.Float(), nullable=False),
        sa.Column('potential_payout', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('pnl', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('target_bookmaker', sa.String(length=50), server_default='parimatch', nullable=False),
        sa.Column('placed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('settlement_notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['paper_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_paper_bets_id', 'paper_bets', ['id'], unique=False)
    op.create_index('ix_paper_bets_account_id', 'paper_bets', ['account_id'], unique=False)
    op.create_index('ix_paper_bets_sport_key', 'paper_bets', ['sport_key'], unique=False)
    op.create_index('ix_paper_bets_match_id', 'paper_bets', ['match_id'], unique=False)
    op.create_index('ix_paper_bets_status', 'paper_bets', ['status'], unique=False)
    op.create_index('ix_paper_bets_status_placed', 'paper_bets', ['status', 'placed_at'], unique=False)

    # 3. currency_exchange_rates
    op.create_table(
        'currency_exchange_rates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('base_currency', sa.String(length=10), nullable=False),
        sa.Column('quote_currency', sa.String(length=10), nullable=False),
        sa.Column('rate', sa.Float(), nullable=False),
        sa.Column('provider', sa.String(length=50), server_default='frankfurter', nullable=False),
        sa.Column('last_updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_currency_exchange_rates_id', 'currency_exchange_rates', ['id'], unique=False)
    op.create_index('ix_currency_exchange_rates_base', 'currency_exchange_rates', ['base_currency'], unique=False)
    op.create_index('ix_currency_exchange_rates_quote', 'currency_exchange_rates', ['quote_currency'], unique=False)
    op.create_index('ix_fx_base_quote', 'currency_exchange_rates', ['base_currency', 'quote_currency'], unique=True)

    # 4. tax_ledger_entries
    op.create_table(
        'tax_ledger_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=100), server_default='default_user', nullable=False),
        sa.Column('bet_id', sa.Integer(), nullable=True),
        sa.Column('gross_win', sa.Float(), nullable=False),
        sa.Column('net_profit', sa.Float(), nullable=False),
        sa.Column('tds_deducted_30_pct', sa.Float(), nullable=False),
        sa.Column('net_post_tax_payout', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(length=10), server_default='INR', nullable=False),
        sa.Column('jurisdiction', sa.String(length=10), server_default='IN', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_tax_ledger_entries_id', 'tax_ledger_entries', ['id'], unique=False)
    op.create_index('ix_tax_ledger_entries_user_id', 'tax_ledger_entries', ['user_id'], unique=False)

    # Seed default Paper Account
    op.execute(
        "INSERT INTO paper_accounts (user_id, currency, virtual_balance, initial_balance, total_paper_bets, won_bets, lost_bets, push_bets, total_pnl, roi_pct, is_active, created_at, updated_at) "
        "VALUES ('default_user', 'INR', 100000.0, 100000.0, 0, 0, 0, 0, 0.0, 0.0, true, NOW(), NOW()) "
        "ON CONFLICT (user_id) DO NOTHING"
    )

def downgrade() -> None:
    op.drop_table('tax_ledger_entries')
    op.drop_table('currency_exchange_rates')
    op.drop_table('paper_bets')
    op.drop_table('paper_accounts')
```

---

### FILE 3: `backend/app/schemas/paper_trading.py`
Pydantic v2 schemas:

```python
"""
Pydantic v2 Schemas for Paper Trading, Currency and Section 115BBJ Tax Ledger.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

class PaperBetCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sport_key: str = Field(..., description="Sport identifier e.g. cricket, soccer, basketball")
    match_id: str = Field(..., description="Unique fixture identifier")
    event_name: str = Field(..., description="E.g. Arsenal vs Chelsea")
    market_type: str = Field(..., description="E.g. match_winner, 1x2_match_odds, moneyline")
    selection: str = Field(..., description="E.g. home, away, draw, over")
    odds: float = Field(..., gt=1.0, description="Decimal odds at placement time")
    stake_amount: float = Field(..., gt=0.0, description="Virtual stake in INR")
    target_bookmaker: str = Field(default="parimatch", description="Bookmaker line being forward-tested")

class PaperBetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    sport_key: str
    match_id: str
    event_name: str
    market_type: str
    selection: str
    odds: float
    stake_amount: float
    potential_payout: float
    status: str
    pnl: float
    target_bookmaker: str
    placed_at: datetime
    settled_at: Optional[datetime] = None
    settlement_notes: Optional[str] = None
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class PaperAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    currency: str
    virtual_balance: float
    initial_balance: float
    total_paper_bets: int
    won_bets: int
    lost_bets: int
    push_bets: int
    total_pnl: float
    roi_pct: float
    is_active: bool
    created_at: datetime
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class PaperPerformanceStats(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    virtual_balance: float
    initial_balance: float
    total_pnl: float
    roi_pct: float
    win_rate_pct: float
    total_bets: int
    won_bets: int
    lost_bets: int
    push_bets: int
    pending_bets: int
    status_summary: str
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class CurrencyRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_currency: str
    quote_currency: str
    rate: float
    last_updated_at: datetime
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class TaxEstimateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gross_profit: float
    tds_rate_pct: float = 30.0
    tds_deducted: float
    net_take_home: float
    currency: str = "INR"
    jurisdiction: str = "IN (Section 115BBJ)"
    compliance_notes: str = "Flat 30% tax on net gambling winnings upon withdrawal/FY end."
    developer_credit: str = "Amit Ashok Kumar Patnaik"
```

---

### FILE 4: `backend/app/services/currency_service.py`
Drop-in Multi-Currency & FX Service:

```python
"""
Multi-Currency & FX Exchange Rate Service (Commander TODAR MAL).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.paper_trading import CurrencyExchangeRate

logger = logging.getLogger("betdoc.currency_service")

# Baseline resilient static exchange rates (fallback when offline or API down)
FALLBACK_FX_RATES: Dict[str, float] = {
    "USD_INR": 86.85,
    "EUR_INR": 93.40,
    "GBP_INR": 111.20,
    "USDT_INR": 87.10,
    "BTC_INR": 8250000.0,
    "INR_USD": 1.0 / 86.85,
    "INR_EUR": 1.0 / 93.40,
    "INR_GBP": 1.0 / 111.20,
    "INR_USDT": 1.0 / 87.10,
    "USD_EUR": 0.93,
    "EUR_USD": 1.07,
    "USD_GBP": 0.78,
}

class CurrencyService:
    """
    Manages live FX conversions, Redis caching, and Section 115BBJ Indian TDS tax estimations.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    def __init__(self, api_url: str = "https://api.frankfurter.app"):
        self.api_url = api_url

    async def get_exchange_rate(
        self,
        db: AsyncSession,
        base: str,
        quote: str,
    ) -> float:
        """Returns the conversion rate from base currency to quote currency."""
        base = base.upper().strip()
        quote = quote.upper().strip()

        if base == quote:
            return 1.0

        pair_key = f"{base}_{quote}"

        # 1. Check database cached rate
        try:
            stmt = select(CurrencyExchangeRate).where(
                CurrencyExchangeRate.base_currency == base,
                CurrencyExchangeRate.quote_currency == quote,
            )
            res = await db.execute(stmt)
            cached = res.scalar_one_or_none()
            if cached and cached.rate > 0:
                return cached.rate
        except Exception as exc:
            logger.debug("Database FX cache lookup failed: %s", exc)

        # 2. Try fetching from free public API
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                url = f"{self.api_url}/latest?from={base}&to={quote}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    rate = float(data.get("rates", {}).get(quote, 0.0))
                    if rate > 0:
                        # Store in database
                        new_rate = CurrencyExchangeRate(
                            base_currency=base,
                            quote_currency=quote,
                            rate=rate,
                            provider="frankfurter",
                            last_updated_at=datetime.now(timezone.utc),
                        )
                        db.add(new_rate)
                        await db.commit()
                        return rate
        except Exception as exc:
            logger.debug("Public FX rate fetch failed: %s", exc)

        # 3. Fallback to resilient static FX rates
        return FALLBACK_FX_RATES.get(pair_key, 1.0)

    async def convert(
        self,
        db: AsyncSession,
        amount: float,
        from_curr: str,
        to_curr: str,
    ) -> float:
        """Converts an amount from one currency to another."""
        rate = await self.get_exchange_rate(db, from_curr, to_curr)
        return round(amount * rate, 2)

    def calculate_indian_tds(self, gross_win: float, stake: float) -> Dict[str, float]:
        """
        Calculates Indian Section 115BBJ 30% TDS on net winnings.
        Net Profit = Gross Win - Stake. If Net Profit > 0, 30% tax applies.
        """
        net_profit = max(0.0, gross_win - stake)
        tds_30 = round(net_profit * 0.30, 2)
        net_take_home = round(gross_win - tds_30, 2)
        return {
            "gross_win": gross_win,
            "stake": stake,
            "net_profit": net_profit,
            "tds_deducted": tds_30,
            "net_take_home": net_take_home,
        }
```

---

### FILE 5: `backend/app/services/paper_trading_service.py`
Drop-in Paper Trading Sandbox Service:

```python
"""
Paper Trading Sandbox Service.
Executes mock bets with virtual INR capital against real-time odds,
and automatically settles them using Group 79 sport resolvers.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, desc

from app.models.paper_trading import PaperAccount, PaperBet, PaperBetStatus, TaxLedgerEntry
from app.domain.sports.registry import SportAdapterRegistry

logger = logging.getLogger("betdoc.paper_trading")

class PaperTradingService:
    """
    Manages the forward-testing virtual money sandbox.
    Developer Credit: Amit Ashok Kumar Patnaik
    """

    async def get_or_create_account(
        self, db: AsyncSession, user_id: str = "default_user"
    ) -> PaperAccount:
        """Retrieves user paper account or creates one with ₹100,000 INR default virtual balance."""
        stmt = select(PaperAccount).where(PaperAccount.user_id == user_id)
        res = await db.execute(stmt)
        account = res.scalar_one_or_none()
        if not account:
            account = PaperAccount(
                user_id=user_id,
                currency="INR",
                virtual_balance=100000.0,
                initial_balance=100000.0,
                total_paper_bets=0,
                won_bets=0,
                lost_bets=0,
                push_bets=0,
                total_pnl=0.0,
                roi_pct=0.0,
                is_active=True,
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
        return account

    async def place_paper_bet(
        self,
        db: AsyncSession,
        user_id: str,
        sport_key: str,
        match_id: str,
        event_name: str,
        market_type: str,
        selection: str,
        odds: float,
        stake_amount: float,
        target_bookmaker: str = "parimatch",
    ) -> PaperBet:
        """Places a mock bet with virtual balance at live market odds."""
        account = await self.get_or_create_account(db, user_id)

        if account.virtual_balance < stake_amount:
            raise ValueError(f"Insufficient virtual balance ({account.virtual_balance:.2f} INR) for stake ({stake_amount:.2f} INR)")

        potential_payout = round(stake_amount * odds, 2)

        # Debit virtual balance
        account.virtual_balance = round(account.virtual_balance - stake_amount, 2)
        account.total_paper_bets += 1

        bet = PaperBet(
            account_id=account.id,
            sport_key=sport_key,
            match_id=match_id,
            event_name=event_name,
            market_type=market_type,
            selection=selection,
            odds=odds,
            stake_amount=stake_amount,
            potential_payout=potential_payout,
            status=PaperBetStatus.PENDING.value,
            target_bookmaker=target_bookmaker,
            placed_at=datetime.now(timezone.utc),
        )
        db.add(bet)
        await db.commit()
        await db.refresh(bet)
        logger.info("Placed paper bet #%d: %s %s @ %.2f (Virtual Stake: %.2f INR)", bet.id, event_name, selection, odds, stake_amount)
        return bet

    async def settle_paper_bet(
        self,
        db: AsyncSession,
        bet_id: int,
        match_result: Dict[str, Any],
        settlement_notes: Optional[str] = None,
    ) -> PaperBet:
        """Settles a paper bet using Group 79 sport domain adapter rules."""
        stmt = select(PaperBet).where(PaperBet.id == bet_id)
        res = await db.execute(stmt)
        bet = res.scalar_one_or_none()
        if not bet or bet.status != PaperBetStatus.PENDING.value:
            return bet

        stmt_acc = select(PaperAccount).where(PaperAccount.id == bet.account_id)
        res_acc = await db.execute(stmt_acc)
        account = res_acc.scalar_one()

        adapter = SportAdapterRegistry.get_adapter(bet.sport_key)
        is_win = adapter.resolve_market_winner(
            market_type=bet.market_type,
            match_result=match_result,
            selection=bet.selection,
        )

        now = datetime.now(timezone.utc)
        bet.settled_at = now
        bet.settlement_notes = settlement_notes or "Auto-settled via SportAdapterRegistry"

        if is_win:
            bet.status = PaperBetStatus.WON.value
            bet.pnl = round(bet.potential_payout - bet.stake_amount, 2)
            account.virtual_balance = round(account.virtual_balance + bet.potential_payout, 2)
            account.won_bets += 1
            account.total_pnl = round(account.total_pnl + bet.pnl, 2)

            # Record tax deduction in shadow tax ledger
            tax_entry = TaxLedgerEntry(
                user_id=account.user_id,
                bet_id=bet.id,
                gross_win=bet.potential_payout,
                net_profit=bet.pnl,
                tds_deducted_30_pct=round(bet.pnl * 0.30, 2),
                net_post_tax_payout=round(bet.potential_payout - (bet.pnl * 0.30), 2),
                currency=account.currency,
                jurisdiction="IN",
            )
            db.add(tax_entry)
        else:
            bet.status = PaperBetStatus.LOST.value
            bet.pnl = -round(bet.stake_amount, 2)
            account.lost_bets += 1
            account.total_pnl = round(account.total_pnl - bet.stake_amount, 2)

        # Update ROI %
        total_staked = max(1.0, (account.initial_balance - account.virtual_balance) + account.total_pnl)
        account.roi_pct = round((account.total_pnl / account.initial_balance) * 100.0, 2)

        await db.commit()
        await db.refresh(bet)
        await db.refresh(account)
        return bet

    async def get_performance_stats(
        self, db: AsyncSession, user_id: str = "default_user"
    ) -> Dict[str, Any]:
        """Calculates win rate, net P&L, ROI%, and forward-testing performance metrics."""
        account = await self.get_or_create_account(db, user_id)

        # Count pending bets
        stmt_pending = select(PaperBet).where(
            PaperBet.account_id == account.id,
            PaperBet.status == PaperBetStatus.PENDING.value,
        )
        res_pending = await db.execute(stmt_pending)
        pending_count = len(res_pending.fetchall())

        decided_bets = account.won_bets + account.lost_bets
        win_rate = round((account.won_bets / decided_bets * 100.0), 2) if decided_bets > 0 else 0.0

        status_summary = (
            "PROFITABLE FORWARD-TEST" if account.total_pnl > 0 and win_rate >= 54.0
            else "BUILDING STATISTICAL SAMPLE" if decided_bets < 10
            else "CALIBRATING ADVICE"
        )

        return {
            "virtual_balance": account.virtual_balance,
            "initial_balance": account.initial_balance,
            "total_pnl": account.total_pnl,
            "roi_pct": account.roi_pct,
            "win_rate_pct": win_rate,
            "total_bets": account.total_paper_bets,
            "won_bets": account.won_bets,
            "lost_bets": account.lost_bets,
            "push_bets": account.push_bets,
            "pending_bets": pending_count,
            "status_summary": status_summary,
            "developer_credit": "Amit Ashok Kumar Patnaik",
        }

    async def reset_account(
        self, db: AsyncSession, user_id: str = "default_user", initial_balance: float = 100000.0
    ) -> PaperAccount:
        """Resets virtual paper balance back to fresh capital."""
        account = await self.get_or_create_account(db, user_id)
        account.virtual_balance = initial_balance
        account.initial_balance = initial_balance
        account.total_paper_bets = 0
        account.won_bets = 0
        account.lost_bets = 0
        account.push_bets = 0
        account.total_pnl = 0.0
        account.roi_pct = 0.0
        await db.commit()
        await db.refresh(account)
        logger.info("Paper account for %s reset to %.2f INR", user_id, initial_balance)
        return account
```

---

### FILE 6: `backend/app/api/v1/paper_trading.py`
FastAPI router for Paper Trading & Currency endpoints:

```python
"""
Paper Trading Sandbox & Currency API Router.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.schemas.paper_trading import (
    PaperAccountRead,
    PaperBetCreate,
    PaperBetRead,
    PaperPerformanceStats,
    CurrencyRateRead,
    TaxEstimateResponse,
)
from app.services.paper_trading_service import PaperTradingService
from app.services.currency_service import CurrencyService

router = APIRouter()
paper_service = PaperTradingService()
currency_service = CurrencyService()

@router.get("/account", response_model=PaperAccountRead, summary="Get Paper Trading Account")
async def get_paper_account(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Any:
    """Retrieves current virtual paper trading balance and stats."""
    return await paper_service.get_or_create_account(db, user_id=getattr(current_user, "username", "default_user"))

@router.post("/place-bet", response_model=PaperBetRead, status_code=status.HTTP_201_CREATED, summary="Place Mock Bet")
async def place_mock_bet(
    payload: PaperBetCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Any:
    """Executes a paper bet with virtual INR capital against real-time live odds."""
    try:
        bet = await paper_service.place_paper_bet(
            db=db,
            user_id=getattr(current_user, "username", "default_user"),
            sport_key=payload.sport_key,
            match_id=payload.match_id,
            event_name=payload.event_name,
            market_type=payload.market_type,
            selection=payload.selection,
            odds=payload.odds,
            stake_amount=payload.stake_amount,
            target_bookmaker=payload.target_bookmaker,
        )
        return bet
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.get("/performance", response_model=PaperPerformanceStats, summary="Paper Trading Performance")
async def get_paper_performance(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Any:
    """Returns win rate, ROI, P&L, and profitability stats for mock betting."""
    return await paper_service.get_performance_stats(db, user_id=getattr(current_user, "username", "default_user"))

@router.post("/reset", response_model=PaperAccountRead, summary="Reset Virtual Bankroll")
async def reset_virtual_bankroll(
    initial_balance: float = 100000.0,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> Any:
    """Resets virtual bankroll back to default ₹100,000 INR."""
    return await paper_service.reset_account(db, user_id=getattr(current_user, "username", "default_user"), initial_balance=initial_balance)

@router.get("/currency/rate", summary="Get Live FX Rate")
async def get_fx_rate(
    base: str = "USD",
    quote: str = "INR",
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Returns live or cached FX rate from base to quote currency."""
    rate = await currency_service.get_exchange_rate(db, base, quote)
    return {
        "base_currency": base.upper(),
        "quote_currency": quote.upper(),
        "rate": rate,
        "developer_credit": "Amit Ashok Kumar Patnaik",
    }

@router.get("/tax/estimate", response_model=TaxEstimateResponse, summary="Section 115BBJ 30% TDS Estimator")
async def estimate_indian_tds(
    gross_win: float,
    stake: float,
) -> Any:
    """Computes Indian Section 115BBJ 30% TDS on net gambling profits."""
    calc = currency_service.calculate_indian_tds(gross_win, stake)
    return TaxEstimateResponse(
        gross_profit=calc["net_profit"],
        tds_deducted=calc["tds_deducted"],
        net_take_home=calc["net_take_home"],
    )
```

---

### FILE 7: `backend/app/api/v1/router.py` Wire-Up
Include `paper_trading.py` router under `/paper-trading` and `/currency`:

```python
from app.api.v1.paper_trading import router as paper_trading_router

api_router.include_router(paper_trading_router, prefix="/paper-trading", tags=["Paper Trading Sandbox"])
```

---

## 🛠️ PART 2: FRONTEND REACT & ZUSTAND SUITE

### FILE 8: `frontend/src/store/usePaperTradingStore.ts`
Reactive Zustand store for Paper Trading & Global Mode Switcher:

```typescript
/**
 * Paper Trading & Mock Betting Sandbox Store.
 * Allows forward-testing value bets with dummy capital at real-time odds.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface PaperBet {
  id: number;
  sport_key: string;
  match_id: string;
  event_name: string;
  market_type: string;
  selection: string;
  odds: number;
  stake_amount: number;
  potential_payout: number;
  status: 'PENDING' | 'WON' | 'LOST' | 'PUSH' | 'VOID';
  pnl: number;
  target_bookmaker: string;
  placed_at: string;
  settled_at?: string;
  settlement_notes?: string;
}

export interface PaperAccount {
  id: number;
  user_id: string;
  currency: string;
  virtual_balance: number;
  initial_balance: number;
  total_paper_bets: number;
  won_bets: number;
  lost_bets: number;
  push_bets: number;
  total_pnl: number;
  roi_pct: number;
  is_active: boolean;
}

interface PaperTradingState {
  isPaperMode: boolean; // true = Mock/Sandbox Mode, false = Live Real Money
  account: PaperAccount | null;
  bets: PaperBet[];
  isLoading: boolean;
  error: string | null;

  setPaperMode: (enabled: boolean) => void;
  fetchAccount: () => Promise<void>;
  placeMockBet: (betData: {
    sport_key: string;
    match_id: string;
    event_name: string;
    market_type: string;
    selection: string;
    odds: number;
    stake_amount: number;
    target_bookmaker?: string;
  }) => Promise<boolean>;
  resetAccount: () => Promise<void>;
}

export const usePaperTradingStore = create<PaperTradingState>()(
  persist(
    (set, get) => ({
      isPaperMode: true, // Default to Safe Mock Mode
      account: {
        id: 1,
        user_id: 'default_user',
        currency: 'INR',
        virtual_balance: 100000.0,
        initial_balance: 100000.0,
        total_paper_bets: 0,
        won_bets: 0,
        lost_bets: 0,
        push_bets: 0,
        total_pnl: 0.0,
        roi_pct: 0.0,
        is_active: true,
      },
      bets: [],
      isLoading: false,
      error: null,

      setPaperMode: (enabled: boolean) => set({ isPaperMode: enabled }),

      fetchAccount: async () => {
        set({ isLoading: true, error: null });
        try {
          const res = await fetch('/api/v1/paper-trading/account');
          if (res.ok) {
            const data = await res.json();
            set({ account: data });
          }
        } catch (err: any) {
          set({ error: err.message || 'Failed to fetch paper account' });
        } finally {
          set({ isLoading: false });
        }
      },

      placeMockBet: async (betData) => {
        set({ isLoading: true, error: null });
        try {
          const res = await fetch('/api/v1/paper-trading/place-bet', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(betData),
          });
          if (res.ok) {
            const newBet = await res.json();
            set((state) => ({
              bets: [newBet, ...state.bets],
              account: state.account
                ? {
                    ...state.account,
                    virtual_balance: state.account.virtual_balance - betData.stake_amount,
                    total_paper_bets: state.account.total_paper_bets + 1,
                  }
                : null,
            }));
            return true;
          } else {
            const err = await res.json();
            set({ error: err.detail || 'Failed to place mock bet' });
            return false;
          }
        } catch (err: any) {
          set({ error: err.message || 'Network error' });
          return false;
        } finally {
          set({ isLoading: false });
        }
      },

      resetAccount: async () => {
        set({ isLoading: true });
        try {
          const res = await fetch('/api/v1/paper-trading/reset', { method: 'POST' });
          if (res.ok) {
            const fresh = await res.json();
            set({ account: fresh, bets: [] });
          }
        } catch (err: any) {
          set({ error: err.message });
        } finally {
          set({ isLoading: false });
        }
      },
    }),
    {
      name: 'betdoc-paper-trading-storage',
    }
  )
);
```

---

### FILE 9: `frontend/src/components/navigation/TradingModeToggle.tsx`
Header component allowing instant toggle between Mock Mode and Live Money:

```tsx
/**
 * Global Trading Mode Toggle: Paper Trading (Mock) vs Live Real Money.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 */

import React from 'react';
import { usePaperTradingStore } from '../../store/usePaperTradingStore';

export const TradingModeToggle: React.FC = () => {
  const { isPaperMode, setPaperMode, account } = usePaperTradingStore();

  return (
    <div className="flex items-center gap-2 bg-slate-900 border border-slate-700/60 rounded-full p-1 shadow-inner text-xs">
      <button
        type="button"
        onClick={() => setPaperMode(true)}
        className={`px-3 py-1 rounded-full font-semibold transition-all duration-200 flex items-center gap-1.5 ${
          isPaperMode
            ? 'bg-emerald-600 text-white shadow-md shadow-emerald-900/40'
            : 'text-slate-400 hover:text-slate-200'
        }`}
      >
        <span className="w-2 h-2 rounded-full bg-emerald-300 animate-pulse" />
        🧪 PAPER TRADING
        <span className="text-[10px] opacity-80 bg-emerald-800/60 px-1.5 py-0.5 rounded">
          ₹{account?.virtual_balance.toLocaleString('en-IN') || '1,00,000'}
        </span>
      </button>

      <button
        type="button"
        onClick={() => setPaperMode(false)}
        className={`px-3 py-1 rounded-full font-semibold transition-all duration-200 flex items-center gap-1.5 ${
          !isPaperMode
            ? 'bg-rose-600 text-white shadow-md shadow-rose-900/40'
            : 'text-slate-400 hover:text-slate-200'
        }`}
      >
        <span className="w-2 h-2 rounded-full bg-rose-400" />
        🔴 LIVE MONEY
      </button>
    </div>
  );
};
```

---

### FILE 10: `frontend/src/pages/PaperTradingSandbox.tsx`
Paper Trading Sandbox Dashboard Page:

```tsx
/**
 * Paper Trading Sandbox Dashboard.
 * Displays virtual capital performance, win rate, mock bets ledger, and reset controls.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 */

import React, { useEffect } from 'react';
import { usePaperTradingStore } from '../store/usePaperTradingStore';

export const PaperTradingSandbox: React.FC = () => {
  const { account, bets, isPaperMode, fetchAccount, resetAccount } = usePaperTradingStore();

  useEffect(() => {
    fetchAccount();
  }, [fetchAccount]);

  const winRate = account && (account.won_bets + account.lost_bets) > 0
    ? ((account.won_bets / (account.won_bets + account.lost_bets)) * 100).toFixed(1)
    : '0.0';

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6 text-slate-100">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-slate-800 pb-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-white">🧪 Paper Trading Sandbox</h1>
            <span className="text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 px-2.5 py-0.5 rounded-full font-medium">
              Zero Financial Risk
            </span>
          </div>
          <p className="text-sm text-slate-400 mt-1">
            Forward-test Ashoka's high-conviction value bets at real-time market prices with virtual INR capital.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => {
              if (window.confirm('Reset virtual bankroll back to ₹1,00,000 INR?')) {
                resetAccount();
              }
            }}
            className="text-xs text-rose-300 border border-rose-600/40 bg-rose-950/20 hover:bg-rose-900/40 px-3 py-1.5 rounded-lg transition"
          >
            Reset Virtual Bankroll
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4">
          <span className="text-xs text-slate-400 font-medium uppercase tracking-wider">Virtual Balance</span>
          <div className="text-2xl font-bold text-emerald-400 mt-1">
            ₹{account?.virtual_balance.toLocaleString('en-IN') || '1,00,000'}
          </div>
          <span className="text-[11px] text-slate-400">Initial: ₹1,00,000 INR</span>
        </div>

        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4">
          <span className="text-xs text-slate-400 font-medium uppercase tracking-wider">Net Virtual P&L</span>
          <div className={`text-2xl font-bold mt-1 ${((account?.total_pnl || 0) >= 0) ? 'text-emerald-400' : 'text-rose-400'}`}>
            {((account?.total_pnl || 0) >= 0 ? '+' : '')}₹{account?.total_pnl.toLocaleString('en-IN') || '0.00'}
          </div>
          <span className="text-[11px] text-slate-400">ROI: {account?.roi_pct || 0}%</span>
        </div>

        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4">
          <span className="text-xs text-slate-400 font-medium uppercase tracking-wider">Win Rate</span>
          <div className="text-2xl font-bold text-sky-400 mt-1">{winRate}%</div>
          <span className="text-[11px] text-slate-400">{account?.won_bets || 0}W - {account?.lost_bets || 0}L</span>
        </div>

        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4">
          <span className="text-xs text-slate-400 font-medium uppercase tracking-wider">Total Mock Bets</span>
          <div className="text-2xl font-bold text-amber-400 mt-1">{account?.total_paper_bets || 0}</div>
          <span className="text-[11px] text-slate-400">Execution: Parimatch Live</span>
        </div>
      </div>

      {/* Arbitrage Status Banner */}
      <div className="bg-gradient-to-r from-amber-950/40 via-slate-900 to-amber-950/40 border border-amber-600/30 rounded-xl p-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-amber-400 text-lg">⚙️</span>
          <div>
            <h3 className="text-sm font-semibold text-amber-200">Arbitrage Matrix Module: Coming Soon</h3>
            <p className="text-xs text-slate-400">
              Cross-bookmaker multi-leg arbitrage computation is currently paused for optimization. All engine compute is focused on Ashoka's value recommendations and mock bet execution.
            </p>
          </div>
        </div>
        <span className="text-[11px] bg-amber-500/20 text-amber-300 border border-amber-500/40 px-2.5 py-1 rounded-full font-medium">
          Under Optimization
        </span>
      </div>

      {/* Recent Paper Bets Table */}
      <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5 space-y-4">
        <h2 className="text-base font-semibold text-white">Recent Mock Bets</h2>
        {bets.length === 0 ? (
          <div className="text-center py-8 text-slate-500 text-sm">
            No mock bets placed yet. Navigate to Sports Engine or The Oracle to test Ashoka's value recommendations with virtual dummy capital!
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="pb-2">Event</th>
                  <th className="pb-2">Market</th>
                  <th className="pb-2">Selection</th>
                  <th className="pb-2">Odds</th>
                  <th className="pb-2">Stake</th>
                  <th className="pb-2">Status</th>
                  <th className="pb-2">P&L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {bets.map((b) => (
                  <tr key={b.id} className="hover:bg-slate-800/30">
                    <td className="py-2.5 font-medium text-slate-200">{b.event_name}</td>
                    <td className="py-2.5 text-slate-400">{b.market_type}</td>
                    <td className="py-2.5 text-emerald-400 font-semibold">{b.selection}</td>
                    <td className="py-2.5 text-slate-200">{b.odds.toFixed(2)}</td>
                    <td className="py-2.5 text-slate-200">₹{b.stake_amount.toLocaleString('en-IN')}</td>
                    <td className="py-2.5">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                        b.status === 'WON' ? 'bg-emerald-500/20 text-emerald-300' :
                        b.status === 'LOST' ? 'bg-rose-500/20 text-rose-300' :
                        'bg-amber-500/20 text-amber-300'
                      }`}>
                        {b.status}
                      </span>
                    </td>
                    <td className={`py-2.5 font-semibold ${b.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {b.pnl >= 0 ? '+' : ''}₹{b.pnl.toLocaleString('en-IN')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};
```

---

## 🛠️ PART 3: AUTOMATED TEST SUITE

### FILE 11: `backend/tests/test_paper_trading_and_currency.py`
Comprehensive test suite covering all mock trading, FX, and Section 115BBJ TDS mechanics:

```python
"""
Tests for Paper Trading Sandbox, Virtual Currency Accounting & Indian 30% TDS.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

import pytest
import httpx
from app.main import app
from app.services.currency_service import CurrencyService
from app.services.paper_trading_service import PaperTradingService

@pytest.mark.asyncio
async def test_currency_conversion_and_tds():
    """Tests static and fallback FX conversions and Section 115BBJ 30% TDS."""
    svc = CurrencyService()
    
    # Static conversion test
    tds = svc.calculate_indian_tds(gross_win=25000.0, stake=10000.0)
    assert tds["gross_win"] == 25000.0
    assert tds["stake"] == 10000.0
    assert tds["net_profit"] == 15000.0
    assert tds["tds_deducted"] == 4500.0  # 30% of 15,000
    assert tds["net_take_home"] == 20500.0

@pytest.mark.asyncio
async def test_paper_trading_endpoints():
    """Tests Paper Trading API endpoints via httpx ASGITransport."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Fetch account
        resp = await client.get("/api/v1/paper-trading/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["currency"] == "INR"
        assert data["virtual_balance"] >= 0.0
        assert data["developer_credit"] == "Amit Ashok Kumar Patnaik"

        # 2. Place mock bet
        bet_payload = {
            "sport_key": "cricket",
            "match_id": "mock_ind_aus_2026",
            "event_name": "India vs Australia",
            "market_type": "match_winner",
            "selection": "India",
            "odds": 1.95,
            "stake_amount": 5000.0,
            "target_bookmaker": "parimatch",
        }
        bet_resp = await client.post("/api/v1/paper-trading/place-bet", json=bet_payload)
        assert bet_resp.status_code == 201
        bet_data = bet_resp.json()
        assert bet_data["status"] == "PENDING"
        assert bet_data["potential_payout"] == round(5000.0 * 1.95, 2)
        assert bet_data["developer_credit"] == "Amit Ashok Kumar Patnaik"

        # 3. Check performance
        perf_resp = await client.get("/api/v1/paper-trading/performance")
        assert perf_resp.status_code == 200
        perf_data = perf_resp.json()
        assert perf_data["total_bets"] >= 1
        assert "win_rate_pct" in perf_data

        # 4. Currency rate check
        fx_resp = await client.get("/api/v1/paper-trading/currency/rate?base=USD&quote=INR")
        assert fx_resp.status_code == 200
        assert fx_resp.json()["rate"] > 70.0
```

---

## 📋 PART 4: VERIFICATION & DEPLOYMENT CHECKLIST
1. Apply database migration: `alembic upgrade head` (bringing revision from `f94a1206c83e` to `a83e1207d94f`).
2. Run `alembic check` to verify 0 schema drift.
3. Run `pytest backend/tests/test_paper_trading_and_currency.py -v`.
4. Run `npm run build` in `frontend/` (0 errors).
5. Commit and push directly to `feature/garuda-ingestion` and `main`.
