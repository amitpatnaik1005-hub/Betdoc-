# ====================================================================================================
# GROUP 95 MASTER SPECIFICATION & CLAUDE CODE PRODUCTION IMPLEMENTATION BLUEPRINT
# LIVE REAL-MONEY BETTING GATEWAY, PARIMATCH LIVE SLIP TERMINAL, REAL BANKROLL LEDGER,
# ODDS DECAY SANITY SHIELD & ASSISTED EXECUTION DESK (COMMANDER ASHVATTHAMA & TODAR MAL)
# ====================================================================================================
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
# Target Environment: d:\BetDoc on branch `feature/garuda-ingestion` and `main`
#
# [CLAUDE EFFORT SETTING: HIGH / ULTRACODE]
# ====================================================================================================

## 🎯 MANDATORY OPERATIONAL DIRECTIVES FOR CLAUDE CODE:
1. **EFFORT MUST BE SET TO HIGH / ULTRACODE**:
   Channel deep architectural reasoning into building the live real-money execution bridge.
   Deliver production-grade, drop-in Python domain models, FastAPI routes, React components,
   and automated tests with zero placeholders, zero ellipsis, and 0% loopholes.
2. **ZERO TOKEN WASTE & PURPOSEFUL DEPTH**:
   Do not generate conversational fluff or speculative stubs. Every line of code written must be
   fully wired, strongly typed, AST-valid, and covered by automated tests.
3. **PRESERVE ALL STRATEGIC PIVOTS & CORE ADDITIONS (GROUPS 79 & 80)**:
   - **Arbitrage Module Paused**: Maintain `"Arbitrage Matrix: Coming Soon / Advanced Multi-Bookmaker Engine Under Optimization"`
     in all UI surfaces (`ActivePortfolio.tsx`, `Phantom.tsx`) to conserve remaining compute credits.
   - **Paper Trading Sandbox Preserved**: The virtual sandbox (₹1,00,000 INR dummy capital) remains active.
     The user can toggle seamlessly between `[PAPER TRADING]` and `[REAL MONEY LIVE DESK]`.
   - **Multi-Currency & Section 115BBJ 30% TDS (Commander TODAR MAL)**: Real money winnings are automatically
     tracked in Indian Rupees (`₹ INR`), with the 30% flat Indian TDS ledger recording gross vs net cash.
   - **17 Named Commanders**: All 17 historical commanders must be referenced in their authoritative roles.
4. **PRIMARY EXECUTION TARGET: PARIMATCH (INR)**:
   - Primary target: **Parimatch** (`https://parimatchs1.com/en/all-live` in INR).
   - Secondary target: **1xBet** (`https://1xbet.com`).
   - Every live bet ticket must generate an authenticated/direct event deep-link, exact market name,
     target decimal odds, and recommended Kelly stake in `₹ INR`.
5. **PRE-PLACEMENT ODDS DECAY & SLIPPAGE SHIELD (COMMANDER SHIVAJI & ASHVATTHAMA)**:
   - Before confirming placement, the system re-validates the live market odds against the minimum
     acceptable price ($Odds_{min} = \frac{1}{\text{True Prob}} \times (1 + \text{Min Edge})$).
   - If market odds have decayed below $Odds_{min}$, the terminal displays a high-priority warning:
     `"EDGE DECAYED: Market odds dropped from 2.10 to 1.85. Minimum required odds: 2.02. DO NOT PLACE."`
6. **DEVELOPER CREDIT**:
   The developer name `"Amit Ashok Kumar Patnaik"` MUST be embedded in all schemas, API headers,
   receipt metadata, and UI headers.
7. **ALL TESTS MUST PASS & FINAL PUSH TO GITHUB**:
   - Write comprehensive tests in `backend/tests/test_real_money_execution_gateway.py`.
   - Run tests with `pytest` and frontend build with `npm run build` (0 errors).
   - Commit and push all changes directly to git on branch `feature/garuda-ingestion` and `main`.

---

## 🏛️ PART 1: SYSTEM ARCHITECTURE & REAL-MONEY TRADING TOPOLOGY

```
                                [ THE ORACLE: ASHOKA ]
                       Emits Vetted Value Bet (+EV & 9 Pillars)
                                          │
                                          ▼
                             [ RISK FORTRESS: KUMBHA ]
                     Sizes Exact Stake in ₹ INR (Kelly 0.20-0.50)
                                          │
                                          ▼
                       [ EXECUTION SNIPER: ASHVATTHAMA ]
                   Builds Parimatch Live Ticket & Direct URL
                                          │
                                          ▼
                 ┌──────────────────────────────────────────────────┐
                 │       PRE-PLACEMENT SANITY SHIELD (SHIVAJI)      │
                 │   Checks: Odds Decay? Drawdown Limit? Cooldown?  │
                 └────────────────────────┬─────────────────────────┘
                                          │
                                          ▼
                 ┌──────────────────────────────────────────────────┐
                 │           PARIMATCH LIVE TERMINAL MODAL          │
                 │   1. Click [Open Parimatch Live]                 │
                 │   2. Click [Copy Pre-Formatted Bet Slip]         │
                 │   3. User Confirms Real Ticket Placed            │
                 └────────────────────────┬─────────────────────────┘
                                          │
                                          ▼
                 ┌──────────────────────────────────────────────────┐
                 │          REAL BANKROLL LEDGER (TODAR MAL)        │
                 │   Deducts ₹ Stake from Real Cash Balance         │
                 │   Monitors Live Match via WebSocket              │
                 │   Auto-Settles Net Returns + 30% TDS Ledger      │
                 └──────────────────────────────────────────────────┘
```

---

## 🎖️ PART 2: THE 17-COMMANDER ROSTER & OPERATIONAL ASSIGNMENTS

1. **ASHVATTHAMA (The Execution Sniper & Parimatch Terminal Commander)**:
   - Primary commander of Group 95.
   - Formats execution orders for Parimatch live feeds, verifies market lines, and handles execution slips.
2. **TODAR MAL (The Imperial Accountant & Real Bankroll Ledger)**:
   - Maintains the Real Cash Vault (`real_bankroll_balance`, `real_cash_transactions`, `real_tax_ledger`).
   - Deducts Section 115BBJ 30% TDS from gross winnings.
3. **KAUTILYA (The Prime Minister & Chief Strategist)**:
   - Evaluates the 9-Pillar vetting filter before promoting any signal to the Real Money Desk.
4. **KUMBHA (The Royal Treasurer & Position Sizer)**:
   - Strictly governs fractional Kelly staking (0.20–0.50) based on live real bankroll.
5. **SHIVAJI (The Sovereign Shield & Stop-Loss Guardian)**:
   - Halts all real-money betting instantly if 15% daily drawdown, 3 consecutive losses, or session limit is reached.
6. **ASHOKA (The Oracle & Supreme Scout)**:
   - Discovers high-probability, positive-EV opportunities across 9 sports.
7. **ARYABHATA (The Chief Mathematician & Smallcase Master)**:
   - Runs 59 mathematical sub-models producing the consensus fair price.
8. **PRATAP (The Vanguard & Core Engine Room)**:
   - Orchestrates execution speed, latency tracking, and async database connections.
9. **VIDUR (The Intelligence Director & Wire Anchor)**:
   - Tracks live breaking news and lineups affecting open real wagers.
10. **BAJIRAO (The Field Marshal & Arena Master)**:
    - Oversees active unsettled real bets and displays live match progress.
11. **VIKRAMADITYA (The Supreme Judge & Settlement Arbiter)**:
    - Verifies official match scorecards and authorizes cash credit upon win.
12. **CHANAKYA (The Grand Tactician & Fleet Overseer)**:
    - Manages global health and operational integrity.
13. **BRIHASPATI (The Chief Counselor & CFO Advisor)**:
    - Provides real-time bankroll growth recommendations and rebalancing alerts.
14. **SAMUDRAGUPTA (The Imperial Conqueror & Multi-Bookmaker Arbitrageur)**:
    - Badged as "Coming Soon / Under Optimization" (Paused).
15. **HARSHA (The Grand Harmonizer & Human Touch Director)**:
    - Manages user manual overrides and intuitive weighting adjustments.
16. **PANINI (The Master Grammarian & Historical Archivist)**:
    - Indexes settled real trades for permanent compliance reporting.
17. **GARUDA (The Aviator & WebSocket Streamer)**:
    - Streams live score updates and price shifts to the user terminal.

---

## 💾 PART 3: DATABASE SCHEMA & MIGRATIONS

Create an Alembic migration (`add_real_money_execution_tables.py`) with down_revision pointing to `a83e1207d94f`:

```python
# backend/app/models/real_money.py

from datetime import datetime
import uuid
from sqlalchemy import Column, String, Float, DateTime, Boolean, ForeignKey, Integer, Text, Enum
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base

class RealBankroll(Base):
    __tablename__ = "real_bankrolls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, unique=True, index=True)
    currency = Column(String, default="INR", nullable=False)
    cash_balance = Column(Float, default=0.0, nullable=False)
    locked_exposure = Column(Float, default=0.0, nullable=False)
    total_deposited = Column(Float, default=0.0, nullable=False)
    total_withdrawn = Column(Float, default=0.0, nullable=False)
    realized_pnl = Column(Float, default=0.0, nullable=False)
    gross_winnings = Column(Float, default=0.0, nullable=False)
    total_tds_deducted = Column(Float, default=0.0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RealBetTicket(Base):
    __tablename__ = "real_bet_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    ticket_reference = Column(String, unique=True, nullable=False, index=True) # e.g. PM-REAL-20261011-001
    bookmaker = Column(String, default="parimatch", nullable=False) # parimatch, onexbet
    sport = Column(String, nullable=False)
    match_id = Column(String, nullable=False)
    event_name = Column(String, nullable=False)
    market_type = Column(String, nullable=False)
    selection = Column(String, nullable=False)
    
    # Financials
    recommended_stake = Column(Float, nullable=False)
    placed_stake = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)
    target_odds = Column(Float, nullable=False)
    placed_odds = Column(Float, nullable=False)
    minimum_profitable_odds = Column(Float, nullable=False)
    model_edge_pct = Column(Float, nullable=False)
    
    # Execution Tracking
    deep_link_url = Column(Text, nullable=False)
    bookmaker_slip_id = Column(String, nullable=True) # User's external Parimatch slip ID
    status = Column(String, default="PENDING_PLACEMENT", nullable=False) # PENDING_PLACEMENT, PLACED, WON, LOST, PUSH, VOID, CANCELLED
    settlement_payout = Column(Float, default=0.0, nullable=False)
    net_profit = Column(Float, default=0.0, nullable=False)
    tds_deducted = Column(Float, default=0.0, nullable=False)
    
    # Sanity & Vetting Notes
    vetting_pillar_score = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)
    placed_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RealCashTransaction(Base):
    __tablename__ = "real_cash_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    transaction_type = Column(String, nullable=False) # DEPOSIT, WITHDRAWAL, BET_STAKE, BET_PAYOUT, TDS_WITHHOLDING
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)
    balance_after = Column(Float, nullable=False)
    reference_id = Column(String, nullable=True) # Link to ticket_reference or external deposit ref
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

---

## ⚡ PART 4: FASTAPI BACKEND ROUTES (`app/api/v1/real_money.py`)

1. `GET /api/v1/real-money/bankroll`:
   - Returns real cash balance, locked exposure, net P&L, and Section 115BBJ 30% TDS withholdings.
2. `POST /api/v1/real-money/deposit`:
   - Records a real cash deposit (e.g. ₹10,000 INR) to seed the live bankroll.
3. `POST /api/v1/real-money/withdraw`:
   - Records a real cash withdrawal from the available balance.
4. `POST /api/v1/real-money/create-ticket`:
   - Ashvatthama builds a Parimatch live execution ticket from an Ashoka recommendation.
   - Calculates deep-link URL: `https://parimatchs1.com/en/all-live` (or direct event URL).
   - Generates one-click clipboard text payload.
   - Computes minimum profitable odds $Odds_{min}$.
5. `POST /api/v1/real-money/confirm-placement`:
   - User verifies bet placed on Parimatch.
   - Validates live price hasn't decayed below $Odds_{min}$.
   - Deducts stake from `cash_balance` into `locked_exposure`.
   - Records `RealCashTransaction`.
6. `POST /api/v1/real-money/settle-ticket`:
   - Settles the ticket upon official scorecard confirmation (WON/LOST/PUSH/VOID).
   - On WON: releases exposure, deposits gross payout, deducts 30% TDS, adds net profit to cash balance.
7. `GET /api/v1/real-money/active-tickets`:
   - Returns all live placed tickets currently awaiting match outcome.

---

## 🎨 PART 5: FRONTEND LIVE TRADING DESK & PARIMATCH SLIP TERMINAL

1. **Global Mode Switcher Header**:
   - In `AppShell.tsx` and `TradingModeToggle.tsx`:
     * `[🟡 PAPER TRADING (₹1,00,000 VIRTUAL)]`
     * `[🟢 REAL MONEY LIVE DESK (₹ CASH)]`
   - Switching to Real Money prompts a confirmation dialog reminding user of stop-loss protections.
2. **Ashvatthama's Parimatch Live Slip Terminal Modal (`ParimatchExecutionModal.tsx`)**:
   - Displays:
     * Match: Arsenal vs Chelsea (EPL)
     * Market: Match Odds (Home Win)
     * Recommended Stake: **₹2,500 INR** (0.35 Kelly)
     * Target Odds: **2.10** | Minimum Required: **2.02**
     * Expected Value (+EV): **+6.2%**
     * Direct Link Button: **[🎯 Open Match on Parimatch]** (opens new tab to `parimatchs1.com`)
     * One-Click Copy: **[📋 Copy Bet Details to Clipboard]**
     * Form: "Actual Odds Placed" (default 2.10) & "Parimatch Slip ID" (optional)
     * Confirmation Button: **[✅ Confirm Bet Placed with Real Money]**
3. **Odds Decay Sanity Banner**:
   - Dynamic real-time calculation: if user types in placed odds lower than minimum profitable odds, button turns amber with warning:
     `"⚠️ CAUTION: Placed odds (1.95) are below profitable model floor (2.02). Expected Value is negative."`
4. **Real Money Vault & TDS Drawer (`RealBankrollCard.tsx`)**:
   - Displays Real Cash Balance, Open Exposure, Total TDS Withheld under Section 115BBJ (30%), and Net Realized Cash Profit.

---

## 🧪 PART 6: COMPREHENSIVE TEST SUITE (`tests/test_real_money_execution_gateway.py`)

Must provide 100% test coverage for:
1. Bankroll creation, deposits, and withdrawal validations.
2. Parimatch ticket generation with deep-link URL formatting.
3. Odds decay detection (rejecting or warning on sub-threshold odds).
4. Stake deduction from cash into locked exposure.
5. Win settlement: gross return, 30% TDS deduction, net balance update.
6. Loss settlement: exposure reduction, realized loss logging, stop-loss trigger check.
7. Simultaneous paper trading and real money balance independence (zero cross-contamination).

---

## 🚀 PART 7: GIT COMMIT & PUSH SPECIFICATION

Once all tests pass and frontend builds cleanly:
```bash
git -C d:\BetDoc add .
git -C d:\BetDoc commit -m "feat(real-money): Group 95 Live Real-Money Betting Gateway, Parimatch Execution Terminal, and 30% TDS Cash Ledger"
git -C d:\BetDoc push origin feature/garuda-ingestion
```
