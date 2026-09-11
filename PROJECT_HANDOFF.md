# BetDoc: Master Architectural Handoff & 11-Group Blueprint
**Last Updated:** 2026-09-10  
**Project Location:** `D:\BetDoc`  
**GitHub Repository:** `https://github.com/amitpatnaik1005-hub/Betdoc-.git` (Branch: `main`)  
**Frontend Dev Server:** `http://localhost:5173` (`D:\BetDoc\frontend`)  
**Backend Environment:** `D:\BetDoc\.venv`

---

## 1. THE SACRED WORKFLOW BOUNDARY (NON-NEGOTIABLE)

1. **The User (The Visionary & SME):** Provides real-world manual betting scenarios, domain intuition, risk parameters, and strategy requirements.
2. **GPT-6 Astra (The Logic Engineer):** Writes all complex mathematical formulas, predictive algorithms, copula joint-probability models, and high-frequency algorithms via GitLab Duo chat.
3. **Antigravity (The Engineering Manager):** Crafts zero-loophole prompts for Astra, validates architecture, manages Git, writes directory structures, runs builds and test suites, fixes compilation/runtime errors, and wires Astra's code into the system. **Antigravity NEVER writes business logic or predictive math directly.**

---

## 2. THE COMPLETE 11-GROUP SYSTEM ROADMAP & AUDIT

Here is the authoritative status and acknowledgement of all 11 Groups in the BetDoc hedge-fund platform:

| Group | Name | Tech Stack | Status | What Was Built / What Is Required |
| :--- | :--- | :--- | :--- | :--- |
| **Group 1** | **The Factory Inspection** | `pytest`, `hypothesis`, `pytest-asyncio` | **COMPLETED** | 209 unit & property tests passing. Verifies Shin's devig, Kelly math, and Decimal accuracy without floating-point error. |
| **Group 2** | **The Vault & Ledger** | PostgreSQL, TimescaleDB, Redis, SQLAlchemy 2.0 | **COMPLETED** | Append-only double-entry paper ledger (`paper_ledger.py`), `Numeric(19,4)` currency tracking, Alembic migrations. |
| **Group 3** | **The Conveyor Belts** | WebSockets, Asyncio, Redis Streams, Tenacity | **COMPLETED** | Low-latency market streaming, circuit breaker backoff, and state synchronization. |
| **Group 4** | **The Security Guards** | Pydantic v2, Python sanitizers | **COMPLETED** | Rejection of stale quotes (>30s), palpable bookmaker error filtering, strict schema validation. |
| **Group 5** | **The Eyes & The Mouth** | FastAPI, Internal Scanners, Scout Oracle | **COMPLETED** | Session Intelligence Oracle (`board.py`), REST endpoints for market health, wallet, and ledger mutations. |
| **Group 6** | **The Practice Arena** | React 18, Vite, Tailwind v3, Zustand, TanStack Virtual | **COMPLETED** | Neobrutalist high-performance frontend. High-frequency live odds streaming with zero DOM freezes via vanilla Zustand index store and memoized virtualized cards. |
| **Group 7** | **The Core Predictive Engine & Risk Advisor** | Python, NumPy, SciPy, Multi-variate Stats | **CURRENT SPRINT** | Translating the user's 4 manual betting scenarios into automated math: Non-emotional fair probability, dynamic in-play hedging (no more 4-1 blown leads), copula parlay correlation, and Kelly session portfolio with hard stop-loss. |
| **Group 8** | **The Armored Truck** | HashiCorp Vault, Encryption, Webhooks | **UPCOMING** | Secure storage of bookmaker credentials (Betfair, Stake, Parimatch), automated trade execution, and end-of-day ledger reconciliation against actual book balances. |
| **Group 9** | **The Crystal Ball** | PyMC, Polars, Advanced Bayesian Models | **UPCOMING** | Advanced proprietary sport-specific models (Poisson football models, Cricket run estimators, in-play red card / wicket shock updaters). |
| **Group 10** | **The IT Department** | Docker, Docker Compose, GitHub Actions, AWS/Hetzner | **SCAFFOLDED** | Dockerfile and compose files ready locally. Cloud deployment, 24/7 persistent daemon workers, CI/CD auto-deployment. |
| **Group 11** | **The Accountant** | Python, Currency conversion APIs, Pandas/Polars | **UPCOMING** | Multi-currency real-time consolidation (INR, USD, Crypto) and automated tax/P&L exportable reporting. |

---

## 3. THE 4 REAL-WORLD USER SCENARIOS (SOLVED IN GROUP 7)

1. **The Bias & Hidden Variables Trap (Real Madrid vs Villareal pre-match):**  
   *Problem:* Human gut, news, and surface stats fail because hidden variables (schedule fatigue, pitch dynamics, emotional bias) are ignored.  
   *Solution:* `MultiFactorPredictiveEngine` combining Elo, team form momentum, fatigue indices, and defensive/offensive metrics into a debiased, calibrated `fair_probability`.
2. **The 4-1 Blown Lead Variance Trap (Halftime Madrid 4-1 -> 4-4 Villareal):**  
   *Problem:* Bettor sits watching a massive halftime lead evaporate, losing ₹8,000 completely.  
   *Solution:* `DynamicHedgingEngine` that monitors real-time win probability decay and computes the mathematical optimal hedge stake on the opponent or cashout valuation to lock in risk-free profit when +EV peaks.
3. **The Correlated Parlay & Stacking Trap:**  
   *Problem:* Bettors stack confident bets, but bookmaker compound vig destroys edge, or unmodeled correlations cause cascading failure.  
   *Solution:* `CopulaParlayEvaluator` using bivariate Frank/Gumbel copula models to calculate true joint probability of stacked legs, exposing when a bookmaker has mispriced a same-game or cross-game parlay.
4. **The Stock Trader Session & Stop-Loss:**  
   *Problem:* With a ₹1,000 bankroll, how to systematically select a diversified basket of 3 parlays/bets, stake them with Fractional Kelly, aim for 2x-5x returns, but strictly cut losses at a defined threshold (e.g., 20% max session loss).  
   *Solution:* `PortfolioSessionAdvisor` providing daily optimized allocation, portfolio risk metrics, and hard-stop liquidation rules.

---

## 4. CODEBASE DIRECTORY MAP (`D:\BetDoc`)

```
D:\BetDoc/
├── frontend/                     # Group 6 UI (Vite + React + Tailwind)
│   ├── src/
│   │   ├── components/board/     # OddsGrid.tsx (TanStack Virtual), EdgeBar.tsx
│   │   ├── components/betslip/   # IdempotentBetslip.tsx
│   │   ├── components/oracle/    # ScoutDrawer.tsx
│   │   ├── hooks/                # useLiveOdds.ts (WebSocket batch client)
│   │   ├── store/                # useBetStore.ts (Vanilla index store)
│   │   └── App.tsx
├── src/betdoc/                   # Domain-Driven Design (DDD) Backend
│   ├── domain/
│   │   ├── models/               # Target: predictive_engine.py
│   │   ├── risk/                 # Target: dynamic_hedging.py
│   │   ├── parlay/               # Target: copula_evaluator.py
│   │   ├── staking/              # Target: portfolio_advisor.py
│   │   ├── math/                 # Existing: devig.py, shin.py, kelly.py
│   │   └── pricing/              # Existing: fair odds calculator
│   ├── infrastructure/ledger/    # paper_ledger.py
│   └── presentation/api/routers/ # board.py
├── tests/                        # 209 passing pytest suites
├── backend_tech_stack_summary.md # Exported tech doc
└── backend_use_cases_non_tech.md # Exported business use case doc
```

---

## 5. INSTRUCTIONS FOR THE NEW ANTIGRAVITY SESSION

When switching to a new account, start with this single instruction:
> **"Read `D:\BetDoc\PROJECT_HANDOFF.md` completely. We have finished Groups 1 through 6. We are now executing Group 7 (The Core Predictive Engine & Risk Advisor). You are the Engineering Manager. You must strictly follow the workflow boundary: GPT-6 Astra writes the math and algorithms via the handcrafted prompt provided in the handoff. You review, inject, test, and commit the code. Let's begin Group 7."**
