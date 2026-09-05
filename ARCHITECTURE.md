# BetDoc: System Architecture & Tech Stack

## 1. System Overview
BetDoc is an **Event-Driven, High-Frequency Quantitative Betting Engine**. It is designed to ingest live sports odds from multiple sources in real-time, store them in ultra-fast memory, and run complex mathematical models (Arbitrage, +EV, Kelly Criterion) to identify market inefficiencies before bookmakers can correct them.

## 2. The Tech Stack (Gold-Grade)

### **Core Backend**
*   **Language:** Python 3.11+ 
    *   *Why:* The industry standard for quantitative finance and machine learning. We enforce strict typing (`mypy`) and 100% asynchronous execution (`asyncio`) to prevent bottlenecking.
*   **Framework:** FastAPI
    *   *Why:* Lightning-fast backend framework. It natively supports asynchronous endpoints and WebSockets, which is mandatory for pushing live alerts to a dashboard.

### **Database & State Management**
*   **Hot Storage (The Live Brain):** Redis (`redis.asyncio`)
    *   *Why:* When a match is live, odds change every second. Standard databases (like SQL) are too slow to read/write this fast. Redis stores the live odds directly in your computer's RAM, allowing the math engine to access them in sub-milliseconds.
*   **Cold Storage (The Archive):** PostgreSQL + SQLAlchemy
    *   *Why:* Used for long-term storage. This tracks our betting history, bankroll growth, and Closing Line Value (CLV) to prove our mathematical edge over months of data.

### **The Math & Data Layer**
*   **Libraries:** `numpy`, `pandas`, `itertools`
    *   *Why:* We use vectorized math operations to cross-reference thousands of odds combinations instantly without slowing down the server.

---

## 3. The Architecture (How Data Flows)

The system is broken into strictly separated "Engines" so that if one piece breaks, the whole system doesn't crash.

1. **Data Sources / APIs** stream data into the system.
2. **Ingestion Engine (`base_api.py`)** normalizes the messy data into a clean, standard format.
3. **Redis (Hot State)** catches the clean data and stores it in RAM.
4. **Quant Engine** constantly listens to Redis. The microsecond new odds arrive, it runs:
    *   *De-Vig Math:* Removes the bookmaker's margin to find the True Probability.
    *   *+EV Scanner:* Compares True Probability against soft bookmakers to find profitable gaps.
    *   *Kelly Criterion:* Calculates exactly what percentage of the bankroll to risk.
5. **FastAPI Server (`main.py`)** takes the profit signals and pushes them to the user via WebSockets, while simultaneously saving the record to **PostgreSQL** for historical tracking.
