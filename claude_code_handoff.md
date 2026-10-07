# MEGA-HANDOFF DOCUMENT FOR CLAUDE CODE (BETDOC PROJECT)

## 1. PROJECT OVERVIEW AND ARCHITECTURE
Welcome, Claude Code! You are now the primary orchestrator for the **BetDoc** platform. BetDoc is a hyper-advanced, full-stack sports betting, quantitative prediction, and automated execution engine. It acts as a "True Digital Betting Twin".

### Tech Stack:
- **Backend:** Python, FastAPI, PostgreSQL (asyncpg), SQLAlchemy 2.0, Redis (Pub/Sub), Celery (Workers).
- **Frontend:** React, TypeScript, Vite, Tailwind CSS, Zustand (State Management), Framer Motion (Animations).
- **Architecture:** Microservices-inspired domain logic (Risk, Math, Omni-Ingestion, Execution) heavily relying on a custom `adapters/` and `services/` pattern.

### The 17 AI Commanders:
The entire system operates around 17 unique agent personas. The UI visually represents them using specific color themes and avatars.
1. **Ashoka** (Oracle - Prediction Engine)
2. **Kautilya** (Command Center - Master Control)
3. **Bajirao** (The Arena - Live Execution)
4. **Vidur** (The Hive / The Wire - Social/News Sentiment)
5. **Kumbha** (The Vault - Capital Management)
6. **Panini** (The Lab - Quantitative Research)
7. **Pratap** (Core - System Architecture)
8. **Garuda** (Phantom - Stealth Scraping)
9. **Todar Mal** (Archive - Data Warehouse)
10. **Aryabhata** (Math/Engine - Probability Distributions)
11. **Chanakya** (Risk Management - Kelly Criterion)
12. **Shivaji** (Security - VaultCrypto)
13. **Drona** (Training - ML Ops)
14. **Bheeshma** (Rules & Compliance - Rate Limiting)
15. **Karna** (Competitive Intel - Odds Shopping)
16. **Arjuna** (Sniper - High-Frequency Execution Bot)
17. **Devraya** (Visualization - UI rendering)

**RULE #1:** Whenever you write frontend code, map the correct Commander to the correct page using `useCommanderStore`.

## 2. THE CURRENT STATE OF THE REPOSITORY (CRITICAL)
The repository is currently split into two distinct states: **Tracked Files** (The Pristine Base) and **Untracked Files** (The Gold-Grade Frontier Code).

### A. The Tracked Files (Permanent UI Template)
The user has a **permanent, non-negotiable base template UI**. All tracked files in `frontend/src` are exactly as the user wants them.
- `App.tsx`: Contains the core `BrowserRouter`, the `StageHeader`, and the `SidebarNav`.
- **STRICT RULE #2:** NEVER rewrite, replace, or rip out the core routing layout in `App.tsx` or `AppShell.tsx`. Do NOT attempt to build a generic wrapper. 
- **Liberty to Tweak:** You DO have the liberty to make minor or major tweaks to the UI to make things look perfect, AS LONG AS you do not ruin or overwrite the base permanent UI structure.
- **Auth Mock:** Because the backend is currently offline for UI testing, there is a mock login bypass in `frontend/src/store/useAuthStore.ts` (`// MOCK LOGIN FOR TESTING`). Leave this intact unless the user explicitly starts the FastAPI backend.

### B. The Untracked Files (Group 60 Integration Targets)
The user has generated massive amounts of high-quality, "gold-grade" code using frontier models (Opus 5.5 / Astra). Because the repository was recently reset, these files are currently **untracked**. 
- **Backend:** `omni.py`, `omni_admin.py`, `omni_poller.py`, `omni_dispatcher.py`, `omni_normalizer.py`, `omni_vault.py`.
- **Frontend:** Bot scenes (`TheHiveScene.tsx`, `BotAvatar.tsx`), stores (`useCommanderStore.ts`).

## 3. YOUR SPECIFIC INTEGRATION MISSION (DO IT PROPERLY)
The previous agent (Antigravity) failed to integrate these properly. Your primary directive is to execute this flawlessly. Read the following checkpoints carefully and ensure they are all met:

### Checkpoint A: Frontend-Backend Connectivity
- Is the backend connected with the frontend completely? 
- Is the backend talking to the frontend through internal APIs across the ENTIRE BetDoc application (including all 11 sections)? 
- **Action:** If this is not done, you must add this provision. Wire up Axios clients, Zustand async actions, and WebSocket connections so data flows perfectly.

### Checkpoint B: Dynamic, Real-Time Permanence
- Are all the gold-grade quality codes, additions, and changes (even the smallest tweak) fully dynamic, real-time, permanent, and actually working? 
- **Action:** Verify everything. Nothing should be static. Data must flow from the backend, into Redis Pub/Sub, out through WebSockets, into Zustand, and render in React in real-time.

### Checkpoint C: API Internal Communication & Section Interoperability
- Are all the internal APIs of the BetDoc app connected with each other? 
- Are all the sections in the application communicating with each other properly?
- **Action:** If not, you must fix it. The Oracle should feed data to the Arena. The Arena should draw capital from the Vault. The systems must be deeply interconnected.

### Checkpoint D: No Hardcoding, Dummy Data, or Fake Buttons
- Inside the BetDoc application, ALL buttons must be working and have their own dynamics.
- **Action:** NOTHING should be hardcoded, dummy, or fake. Rip out mock arrays (e.g., placeholder bets, static charts) and replace them with actual state-driven rendering. If a button says "Place Bet", it must trigger an actual API/store call.

### Checkpoint E: Group 60 Completion Audit
- Are we really done up to Group 60?
- **Action:** Perform a comprehensive check. Ensure that every single bit of all the groups up to Group 60 has actually been implemented and integrated into the live codebase. Do not leave any Group half-finished.

## 4. NEXT STEPS
The user will now pass you instructions to execute the work the previous agent was supposed to do. You will do it exceptionally. Read this document, audit the untracked files, and proceed with surgical, precise, and fully dynamic integrations.
