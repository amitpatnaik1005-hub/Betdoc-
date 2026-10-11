# ====================================================================================================
# GROUP 77 MASTER SPECIFICATION & CLAUDE CODE EXECUTION PROMPT
# ADVANCED STRATEGY BACKTESTING ENGINE, LIVE IN-PLAY STOP-LOSS SHIELD &
# MULTI-SPORT PARLAY WORKBENCH (WITH CHAMELEON BOOKMAKER UI SKINNING)
# ====================================================================================================
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
# Target Environment: d:\BetDoc on branch `feature/garuda-ingestion`
#
# [CLAUDE EFFORT SETTING: HIGH / ULTRACODE]
# ====================================================================================================

## 🎯 MISSION OBJECTIVE
Claude, you are the principal quantitative systems architect and lead engineer for BetDoc. Your mission is to implement **Group 77** in its entirety with **ultra-high effort, zero stubs, and production-grade completeness**. 

Group 77 establishes the quantitative backbone for strategy validation, real-time live stop-loss capital preservation, and manual parlay craftsmanship with dynamic bookmaker skinning.

---

## 🏛️ CORE PILLARS & ARCHITECTURAL REQUIREMENTS

### 1. Multi-Asset Walk-Forward Strategy Backtesting Engine
- **Files**:
  - `backend/app/domain/backtesting/engine_math.py`
  - `backend/app/services/backtesting/backtest_engine.py`
- **Requirements**:
  - **Dynamic Walk-Forward Analysis**: Strict temporal separation of In-Sample (IS: 70%) and Out-of-Sample (OOS: 30%) rolling windows to prevent look-ahead bias.
  - **Execution Slippage & Liquidity Decay**: Model market impact using square-root stake-to-liquidity scaling:
    $$p_{\text{exec}} = p_{\text{quote}} \cdot \left(1 - \text{SlippageRate} \cdot \sqrt{\frac{\text{Stake}}{\text{MarketLiquidity}}}\right)$$
  - **Bookmaker Vigorish / Margin Modeling**:
    - BetDoc Institutional: 2.5%
    - Stake: 4.2%
    - Parimatch: 4.5%
    - 1xBet: 3.8%
  - **Metrics**: Compound Annual Growth Rate (CAGR), Sharpe Ratio ($R_f=0.04$), Sortino Ratio (downside variance only), Calmar Ratio, Maximum Peak-to-Trough Drawdown ($D_{\max}$), Brier Skill Score (BSS).
  - **Monte Carlo VaR**: 10,000-path bootstrap trajectory generating 95% and 99% Value-at-Risk (VaR) and CVaR.

---

### 2. Live In-Play Stop-Loss Shield (Zero Capital Wipeout)
- **Files**:
  - `backend/app/domain/backtesting/inplay_stoploss_math.py`
  - `backend/app/adapters/bookmakers/base_cashout_adapter.py`
  - `backend/app/adapters/bookmakers/parimatch_adapter.py`
  - `backend/app/adapters/bookmakers/one_xbet_adapter.py`
  - `backend/app/adapters/bookmakers/stake_adapter.py`
  - `backend/app/workers/inplay_stoploss_worker.py`
- **The Core Rule**: **Money placed on a bet must NEVER go to 0!**
  - When live bets are active on external bookmaker sites (**Parimatch**, **1xBet**, **Stake**), unexpected in-game swings (early red card, early underdog goal, sudden momentum collapse) cause win probability to crash.
  - The In-Play Stop-Loss worker continuously polls live odds and bookmaker cashout offers every 3–5 seconds.
  - **Emergency Cashout Trigger Criteria**:
    1. Cashout value dips to/below threshold: $V_{\text{cashout}} \le (1.0 - \text{StopLossPct}) \cdot \text{Stake}$ (default $\text{StopLossPct} = 0.25$).
    2. Live model probability collapse: $P_{\text{live}} < 0.35 \cdot P_{\text{entry}}$.
    3. Severe adverse in-game event (red card, star player injury, cricket wicket collapse).
  - **Immediate Action**: Dispatches an automated emergency cashout execution order to the bookmaker adapter session. Capital is liquidated in milliseconds, recovering 65%–75% of stake and salvaging user capital.

---

### 3. Manual Multi-Sport Parlay Workbench (Match KPI Cards)
- **File**: `frontend/src/components/parlay/ManualParlayWorkbench.tsx`
- **Requirements**:
  - **9 Sports Filter Carousel**: Football (Soccer), Basketball, Tennis, Cricket, Ice Hockey, Baseball, American Football, MMA/UFC, Esports.
  - **Interactive Match KPI Cards**: Live and pre-match fixtures rendered as cards showing live score/clock, team form, model win probabilities, best market odds, liquidity, and momentum steam.
  - **Drag-and-Drop / One-Click Slip Assembly**: Users can drag any Match KPI Card or select markets into their manual parlay slip.
  - **All 27 Bet Types**: Moneyline, Point Spread, Over/Under Totals, Both Teams to Score (BTTS), Asian Handicap, Player Props, In-Play Next Event, Teasers, Pleasers, Draw No Bet, Double Chance, and full System / Round Robin combinations (Trixie, Yankee, Patent, Canadian, Heinz, Super Heinz, Goliath).

---

### 4. Cognitive 15-Pillar Oracle Bet Rater
- **File**: `backend/app/domain/manual_parlay/cognitive_parlay_rater.py`
- **Requirements**:
  - When a user constructs a manual parlay, it is submitted to the Cognitive Rater.
  - Evaluates the bet across **15 Fortress Pillars**:
    1. Mathematical Edge ($E$)
    2. Bookmaker Vig / Hold ($M$)
    3. Leg Correlation Matrix & Penalty ($C$)
    4. Market Liquidity Depth ($L$)
    5. Steam Move & Sharp Money Alignment ($S$)
    6. Information Asymmetry & News Sentiment ($N$)
    7. Brier Calibration History ($B$)
    8. Venue & Surface Dynamics ($V$)
    9. Weather & Altitude Impacts ($W$)
    10. Head-to-Head & Tactical Matchups ($H$)
    11. Rest & Fatigue Differential ($F$)
    12. Motivation & Tournament Stakes ($T$)
    13. Referee & Official Bias Tendencies ($R$)
    14. Volatility & Downside Tail Risk ($\text{VaR}$)
    15. Execution Timing & Line Shopping Spread ($\Omega$)
  - **Composite Score ($0 - 100$) & Qualitative Tiers**:
    - **95 – 100**: `PERFECT`
    - **85 – 94**: `EXTRAORDINARY`
    - **75 – 84**: `BRILLIANT`
    - **60 – 74**: `GOOD`
    - **45 – 59**: `AVERAGE`
    - **0 – 44**: `POOR`
  - Generates clear tactical adjustments, correlation warnings, and recommended stop-loss threshold.

---

### 5. Chameleon Bookmaker UI Skinning (Digital Twin Account Emulation)
- **Files**:
  - `frontend/src/lib/backtest_and_parlay.ts`
  - `frontend/src/components/parlay/ManualParlayWorkbench.tsx`
- **Requirements**:
  - The Bet Builder dynamically morphs its entire UI palette, coupon ergonomics, typography, and margin assumptions to match the user's active betting account:
    - **Parimatch Skin (`parimatch`)**: Electric Yellow (`#FFEE00`), Jet Black (`#121212`), high-contrast buttons, Parimatch coupon ergonomics, 4.5% margin.
    - **1xBet Skin (`one_xbet`)**: Cyan (`#00A3E0`), Royal Navy (`#163963`), accumulator boost badges, 1xBet betslip layout, 3.8% margin.
    - **Stake Skin (`stake`)**: Neon Emerald (`#00E701`), Dark Slate Navy (`#1A2C38`), crypto sportsbook minimalism, 4.2% margin.
    - **BetDoc Institutional Skin (`betdoc`)**: Institutional Emerald (`#10B981`) & Amber (`#F59E0B`), Obsidian (`#030712`), monospace quantitative analytics, 2.5% margin.
  - The user can select their active account from a switcher in the workbench header. The UI instantly updates styles, displays their live account balance mirror, and calculates odds with that bookmaker's real margin.

---

## 🗄️ DATABASE MODELS & MIGRATION SPECIFICATION

### 1. Models: `backend/app/models/backtest_and_parlay.py`
Must import `Base` and `utc_now` from `app.models`. Define:
- `BacktestRun`:
  - `id` (String UUID PK), `strategy_name`, `sport`, `date_from`, `date_to`, `initial_capital`, `final_equity`, `cagr`, `sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `win_rate`, `brier_score`, `slippage_rate`, `var_95`, `var_99`, `equity_curve_json`, `monte_carlo_json`, `created_at`.
- `LiveInPlayShield`:
  - `id` (String UUID PK), `bet_id`, `bookmaker_name` (parimatch, one_xbet, stake, betdoc), `sport`, `match_title`, `market_type`, `staked_amount`, `entry_odds`, `entry_prob`, `stop_loss_pct` (default 0.25), `status` (ACTIVE, MONITORED, CASHOUT_EXECUTED, SETTLED_NORMAL, VOID), `current_cashout_value`, `salvaged_amount`, `trigger_reason`, `triggered_at`, `created_at`.
- `ManualParlaySubmission`:
  - `id` (String UUID PK), `legs_count`, `legs_json`, `total_odds`, `staked_amount`, `bookmaker_skin`, `rating_score` (Float 0-100), `rating_tier` (PERFECT, EXTRAORDINARY, BRILLIANT, GOOD, AVERAGE, POOR), `pillar_breakdown_json`, `tactical_advice`, `stop_loss_recommended_pct`, `created_at`.
- `BookmakerSkinAccount`:
  - `id` (String UUID PK), `user_id`, `bookmaker_name`, `account_identifier`, `balance_amount`, `currency`, `is_active_skin`, `created_at`.

### 2. Export Models: `backend/app/models/__init__.py`
Import and export all 4 models so Alembic autogenerates or recognizes them.

### 3. Alembic Migration: `backend/alembic/versions/d71e3092f89c_backtest_stoploss_parlay.py`
- `revision: str = 'd71e3092f89c'`
- `down_revision: Union[str, None] = 'c59d2013f48b'` (Group 76 parent migration)
- Create all 4 tables with appropriate indexes on `bookmaker_name`, `status`, and `created_at`.

---

## 🔌 API ENDPOINTS & SCHEMAS

### 1. Schemas: `backend/app/schemas/backtest_and_parlay.py`
- Pydantic models for Backtest execution requests, Walk-Forward responses, Stop-Loss registration, Cashout execution status, Manual Parlay inspection requests, and 15-Pillar rating responses.

### 2. Routers:
- `backend/app/api/v1/backtest.py`:
  - `POST /api/v1/backtest/run`: Run walk-forward backtest.
  - `GET /api/v1/backtest/runs`: List historical backtest runs.
  - `GET /api/v1/backtest/{run_id}`: Get backtest details & equity curve.
  - *CRITICAL*: Always import `get_db` from `app.api.deps`.
- `backend/app/api/v1/manual_parlay.py`:
  - `POST /api/v1/manual-parlay/inspect`: Rate a custom user parlay against the 15 Fortress Pillars, return 0-100 score, tier, and advice.
  - `POST /api/v1/manual-parlay/submit`: Persist manual parlay and arm live stop-loss shield.
  - `GET /api/v1/manual-parlay/live-shields`: Get all active stop-loss monitored bets.
  - `POST /api/v1/manual-parlay/emergency-cashout/{shield_id}`: Manually force cashout.
- `backend/app/api/v1/inplay_ws.py`:
  - `WebSocket /api/v1/ws/inplay-shield`: Push real-time odds fluctuations, live cashout valuations, and cashout alerts.
- Register all routers in `backend/app/api/v1/router.py`.

---

## 🎨 FRONTEND IMPLEMENTATION

### 1. Types & Client: `frontend/src/lib/backtest_and_parlay.ts`
- Define TypeScript types for all models, metrics, and `BOOKMAKER_THEMES` dictionary for `parimatch`, `one_xbet`, `stake`, and `betdoc`.

### 2. Components:
- `frontend/src/components/backtesting/BacktestDashboard.tsx`:
  - Strategy selector, IS/OOS split sliders, slippage configuration, Monte Carlo density plot, drawdown chart.
- `frontend/src/components/parlay/ManualParlayWorkbench.tsx`:
  - **Chameleon Skin Switcher**: Seamless visual shift between Parimatch (Yellow/Black), 1xBet (Cyan/Navy), Stake (Neon Green/Navy), and BetDoc (Emerald/Dark Obsidian).
  - **Match KPI Cards**: 9 sports feed with live probability, steam indicators, and drag-and-drop addition.
  - **Interactive Betslip**: Real-time parlay calculation, stop-loss trigger slider (-15% to -40%), and "Inspect Parlay" button.
  - **Cognitive Rating Badge**: Renders 0-100 score with glowing tier color (e.g. Amber for Brilliant, Neon Gold for Perfect), pillar radar breakdown, and tactical advice callouts.
- Connect into `frontend/src/pages/TheLab.tsx` (Panini) and `frontend/src/pages/TheOracle.tsx` (Ashoka).

---

## 🧪 TESTING & VERIFICATION REQUIREMENTS

- `backend/tests/test_backtest_and_parlay.py`:
  - Must use `httpx.ASGITransport(app=app)`.
  - Test Walk-Forward math, slippage decay, and Monte Carlo confidence intervals.
  - Test In-Play Stop-Loss threshold trigger and emergency cashout execution.
  - Test 15-Pillar Cognitive Parlay Rater across good, average, and poor parlays.
  - Test Chameleon bookmaker margin calculations.
  - Test API endpoints with 100% passing assertions.

---

## ⚠️ STRICT MANDATORY RULES

1. **DEVELOPER CREDIT**: The name `"Amit Ashok Kumar Patnaik"` MUST be embedded in all model table docstrings, API receipts, and UI workbench headers.
2. **ZERO STUBS**: No `pass`, no `TODO`, no placeholder returns. All math, adapters, routers, and React components must be fully implemented.
3. **MIGRATION CHAIN**: `down_revision = "c59d2013f48b"`.
4. **SESSION IMPORT**: Use `from app.api.deps import get_db`.
5. **MODELS IMPORT**: Use `from app.models import Base, utc_now`.
