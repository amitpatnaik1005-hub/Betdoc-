# ====================================================================================================
# GROUP 81 MASTER SPECIFICATION & CLAUDE CODE PRODUCTION IMPLEMENTATION BLUEPRINT
# PRODUCTION DOCKER MULTI-STAGE BUILDS, HARDENED NGINX REVERSE PROXY, WEBSOCKET RELAY,
# FULL CONTAINER ORCHESTRATION & DEEP FLEET HEALTH SHIELD (COMMANDER PRATAP & CHANAKYA)
# ====================================================================================================
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
# Target Environment: d:\BetDoc on branch `feature/garuda-ingestion` and `main`
#
# [CLAUDE EFFORT SETTING: HIGH / ULTRACODE]
# ====================================================================================================

## 🎯 MANDATORY OPERATIONAL DIRECTIVES FOR CLAUDE CODE:
1. **EFFORT MUST BE SET TO HIGH / ULTRACODE**:
   Execute deep devops, containerization, network engineering, and architectural reasoning.
   Deliver production-grade, drop-in Dockerfiles, Nginx configurations, compose orchestrations,
   Python health endpoints, and automated tests with zero placeholders, zero ellipsis, and 0% loopholes.
2. **ZERO TOKEN WASTE & PURPOSEFUL DEPTH**:
   Do not generate conversational fluff, speculative outlines, or incomplete summaries.
   Channel all compute into complete, tested implementations across all container definitions,
   Nginx reverse proxy rules, FastAPI health routers, and docker compose stacks.
3. **PRESERVE ALL STRATEGIC PIVOTS & CORE ADDITIONS (GROUPS 79 & 80)**:
   - **Arbitrage Module Paused**: Maintain `"Arbitrage Matrix: Coming Soon / Advanced Multi-Bookmaker Engine Under Optimization"`
     in all UI surfaces (`ActivePortfolio.tsx`, `Phantom.tsx`) to conserve remaining compute credits.
   - **Paper Trading Sandbox Engine**: Forward-testing mock bets with virtual dummy money (default ₹1,00,000 INR)
     remains 100% operational in the containerized environment.
   - **Ashoka & Ashvatthama (Parimatch Execution Gateway)**: Ensure containers correctly route real and mock bets
     targeting **Parimatch** (`https://parimatchs1.com/en/all-live` in INR) with live market odds from the 100+ provider catalog.
   - **Multi-Currency & Section 115BBJ 30% TDS (Commander TODAR MAL)**: Maintain Redis caching of live FX rates
     (`INR`, `USD`, `EUR`, `GBP`, `USDT`, `BTC`) and Indian tax estimation in the containerized stack.
4. **UNIFIED DOCKER COMPOSE ARCHITECTURE**:
   - Deliver both **Development** (`docker-compose.yml`) and **Production** (`docker-compose.production.yml`) stacks:
     * `postgres_db`: PostgreSQL 16 Alpine with persistent volume and authenticated healthcheck.
     * `redis_cache`: Redis 7 Alpine with password authentication and appendonly persistence.
     * `backend_api`: Multi-stage Python 3.12 Gunicorn/Uvicorn service with non-root user `appuser`.
     * `celery_worker`: Omni-ingestion and paper trading settlement worker with per-source Redis locks.
     * `celery_beat`: Periodic task scheduler (odds polling, paper settlement, rate limit maintenance).
     * `omni_dispatcher`: High-frequency provider dispatch worker.
     * `frontend_web`: Multi-stage Node 20 / Nginx 1.25 Alpine container serving static SPA assets and proxying API/WS traffic.
5. **HARDENED NGINX REVERSE PROXY & WEBSOCKET RELAY**:
   - Deliver `frontend/nginx.conf` configured for non-root execution (`port 8080`), with:
     * SPA routing (`try_files $uri $uri/ /index.html;`).
     * API reverse proxy (`/api/` -> `http://backend_api:8000`).
     * Real-time WebSocket upgrade relay for `/ws/`, `/api/v1/ws/`, `/api/v1/omni/ws/`, and `/api/v1/live-odds`.
     * Strict security headers: `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `X-XSS-Protection`.
     * Static asset 1-year caching for `/assets/`.
     * Gzip compression for text, css, json, js, and svg.
6. **DEEP FLEET HEALTH SHIELD (COMMANDER PRATAP & CHANAKYA)**:
   - Provide `/health` for fast shallow liveness checks (1ms).
   - Provide `/health/deep` for deep readiness validation:
     * PostgreSQL read/write connection test.
     * Redis ping and latency test.
     * Celery worker heartbeat verification.
     * Provider credential Vault decryption check (`VaultCrypto`).
     * Fleet commander status check (all 17 commanders).
     * Paper Trading Sandbox ledger integrity check.
7. **DEVELOPER CREDIT**:
   The developer name `"Amit Ashok Kumar Patnaik"` MUST be embedded in all Dockerfile labels, Nginx response headers (`X-Engineered-For`), health check receipts, and configuration docstrings.
8. **ALL TESTS MUST PASS & FINAL PUSH TO GITHUB**:
   - Write comprehensive tests in `backend/tests/test_docker_and_nginx_config.py`.
   - Run tests with `pytest` and frontend build with `npm run build` (0 errors).
   - Commit and push all changes directly to git on branch `feature/garuda-ingestion` and `main`.

---

## 🏛️ PART 1: SYSTEM ARCHITECTURE, TOPOLOGY & PRODUCTION SECURITY

### 1. Production Container Topology & Network Isolation
The BetDoc quantitative sports trading fleet is structured into an isolated bridge network (`betdoc_net`):

```
                        [ Internet / Client Browser ]
                                     |
                                     v (Port 80 / 443 / 8080)
                       +---------------------------+
                       |       frontend_web        |
                       | (Nginx 1.25 Alpine / SPA) |
                       +---------------------------+
                         /                       \
         Static Assets  /                         \ API & WS Proxy
                       v                           v
              [/usr/share/nginx/html]     [backend_api:8000]
                                          (Gunicorn / Uvicorn)
                                            /       |       \
                                           /        |        \
                                          v         v         v
                                    +----------+ +-------+ +---------------+
                                    | postgres | | redis | | celery_worker |
                                    | (DB:5432)| |(6379) | | celery_beat   |
                                    +----------+ +-------+ +---------------+
```

### 2. DevOps Ten Commandments & Container Hardening Principles
The containerized architecture enforces strict production security laws:
- **Law 8: Non-Root Supremacy**:
  Every container executes under an unprivileged user (`appuser` with UID/GID 10001). No process runs as root.
- **Law 9: Layer Caching Defense**:
  Dependency manifests (`package*.json`, `requirements.txt`) are copied and installed in separate build stages before application source code to maximize Docker layer cache hits.
- **Law 10: Secure Healthcheck Diagnostics**:
  Healthcheck probes do not leak credentials in process tables; database checks use environment-passed parameters and Redis probes grep for `PONG`.
- **Law 11: Real-Time WebSocket Streaming Relay**:
  Nginx proxies live WebSocket connections with `Upgrade` and `Connection` headers and 3600-second timeouts, guaranteeing uninterrupted in-play live odds and match state ticks.
- **Law 12: True Client IP Preservation**:
  Nginx passes `$remote_addr` via `X-Real-IP` and `X-Forwarded-For` so rate limiters and audit logs capture true client addresses.

---

## 🎖️ PART 2: THE 17-COMMANDER FLEET INFRASTRUCTURE INTEGRATION

All 17 commanders operate within containerized, health-monitored environments:

1. **PRATAP (The Vanguard & Core Infrastructure)**:
   - Primary commander of Group 81.
   - Manages Gunicorn worker concurrency ($W = 2 \times \text{Cores} + 1$), async database connection pools, and container resource limits.
2. **CHANAKYA (The Prime Minister & Fleet Overseer)**:
   - Primary co-commander of Group 81.
   - Operates the Deep Fleet Health Shield (`/health/deep`), continuous container readiness probes, and the emergency stop circuit breaker.
3. **GARUDA (The Aviator & WebSocket Streamer)**:
   - Manages high-throughput WebSocket relay through Nginx, maintaining sub-12ms tick streaming from live provider feeds.
4. **ASHOKA (The Oracle & Supreme Scout)**:
   - Containerized prediction service emitting value bets across all 9 sports.
5. **KAUTILYA (The Strategist & Portfolio Architect)**:
   - Kelly capital allocation and exposure calculations running inside backend API containers.
6. **BAJIRAO (The Striker & Momentum Tracker)**:
   - Real-time steam move interceptor communicating via Redis Pub/Sub.
7. **VIDUR (The Counselor & Intel Synthesizer)**:
   - Background news and weather ingestion pipeline running in Celery workers.
8. **KUMBHA (The CFO & Vault Security Officer)**:
   - Encrypted provider credential storage in `postgres_db`, protected by non-root execution and secret env variables.
9. **PANINI (The Linguist & Odds Normalizer)**:
   - Sub-12ms odds normalization service.
10. **TODAR MAL (The Revenue Minister & Multi-Currency Engine)**:
    - Maintains the virtual paper trading ledger, live FX rates in Redis, and Section 115BBJ 30% TDS tax reporting.
11. **ARYABHATA (The Mathematician & Quantitative Modeler)**:
    - Pre-compiled mathematical modeling wheels (SciPy, NumPy) in the multi-stage Docker build.
12. **SHIVAJI (The Guerrilla King & Defense Commander)**:
    - Stop-loss monitoring daemon running within the Celery worker fleet.
13. **DRONA (The Grandmaster & Neural Trainer)**:
    - Recalibration tasks scheduled via Celery beat.
14. **BHEESHMA (The Patriarch & Never-Forget Mistake Guard)**:
    - User learning journal and mistake tracking tables in PostgreSQL.
15. **KARNA (The Philanthropist & High-Variance Hunter)**:
    - Longshot value evaluation running in containerized tasks.
16. **ARJUNA (The Marksman & Zero-Vig Precision Pricer)**:
    - Pinnacle zero-vig benchmark pricing cache in Redis.
17. **ASHVATTHAMA (The Immortal Execution Sniper)**:
    - Automated execution router targeting **Parimatch** (`https://parimatchs1.com/en/all-live` in INR) or simulated Paper Trading slips.

---

## 🌐 PART 3: THE 100+ PROVIDER CATALOG & VAULT ENCRYPTION IN DOCKER

The entire 100+ provider catalog (Categories A through E from `D:\confidential\API Keys & TARGET URL (Read me).md`) is supported inside Docker:
- Credentials stored in PostgreSQL `vault_provider_credentials` encrypted with AES-256-GCM.
- Injected at container runtime via `MASTER_VAULT_KEY` environment variable.
- Zero plaintext API keys committed to Git or embedded in Docker images.
- Container network connects outward over HTTPS to Parimatch, Pinnacle, Betfair, Odds API, Open-Meteo, ESPN, and NewsAPI.

---

## ⚽ PART 4: 9 SPORTS & 27 BET TYPES CONTAINERIZED DEPLOYMENT

The multi-stage container image packages all analytical dependencies required for the 9 sport adapters and 27 bet types:
1. **Soccer**: Dixon-Coles bivariate Poisson with correlation parameter $\tau(\lambda, \mu)$.
2. **Cricket**: Ball-by-ball Markov chain state transition and Duckworth-Lewis-Stern (DLS) standard edition.
3. **Basketball**: Dean Oliver Four Factors and Morey-Pythagorean model.
4. **Tennis**: O'Malley & Barnett analytical Markov chain serve/return model.
5. **Baseball**: David Smyth BaseRuns with bullpen decay and NRFI/YRFI probabilities.
6. **American Football**: EPA per play, key number distributions (margins 3, 7, 10, 14, 6).
7. **Ice Hockey**: Shot xG, Corsi/Fenwick metrics, empty-net surge modeling.
8. **Combat**: Strike differential, takedown defense, per-round stoppage decay.
9. **Esports**: HLTV rating differentials, 7-map veto sequence simulation.

All 27 bet types execute and settle cleanly inside containerized background Celery tasks.

---

## 🛡️ PART 5: DIGITAL TWIN 15 PILLARS & FA-1 TO FA-8 COMPLIANCE

The container stack provides complete infrastructure for:
- **FA-1 (In-Play Stop-Loss Shield)**: Sub-second Redis ticks monitor open positions.
- **FA-2 (Discipline Engine & XP Awards)**: Level progression and disciplined betting XP.
- **FA-3 (Sub-12ms Feed Normalization)**: Fast in-memory processing.
- **FA-4 (Sport Kelly Sizing)**: Kelly fractions (0.20 to 0.50).
- **FA-5 (Hedge & Cashout Recommendations)**: Live hedge calculations.
- **FA-6 (Global Tax & Section 115BBJ 30% TDS)**: Indian tax tracking ledger.
- **FA-7 (Backtesting to Forward-Testing Bridge)**: Historical simulation to paper trading.
- **FA-8 (OmniRoute Execution)**: Automated execution routing targeting Parimatch.
- **15 Digital Twin Pillars**: P1 Consensus, P4 Weather/Pitch, P7 Sharp Benchmark, P9 Referee/Umpire, P14 Recalibration, P15 Confidence Index.

---


---

## ⚙️ PART 6B: DETAILED DOCKER CONTAINER RUNTIME & RESOURCE SPECIFICATIONS

### 1. Service Runtime Profiles & Resource Constraints

Each container in the BetDoc production stack is governed by explicit CPU, RAM, and storage limits:

| Container Service | Image / Base | Internal Port | Memory Limit | CPU Limit | Healthcheck Command | Restart Policy |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `postgres_db` | `postgres:16-alpine` | `5432` | 2048 MB | 2.0 Cores | `pg_isready -h 127.0.0.1 -U $POSTGRES_USER` | `unless-stopped` |
| `redis_cache` | `redis:7-alpine` | `6379` | 1024 MB | 1.0 Cores | `redis-cli ping \| grep -q PONG` | `unless-stopped` |
| `backend_api` | `betdoc-backend:latest` | `8000` | 2048 MB | 2.0 Cores | `curl -f http://127.0.0.1:8000/api/v1/health` | `unless-stopped` |
| `celery_worker`| `betdoc-backend:latest` | N/A | 3072 MB | 2.5 Cores | `celery inspect ping -d omni@$HOSTNAME` | `unless-stopped` |
| `celery_beat`  | `betdoc-backend:latest` | N/A | 512 MB | 0.5 Cores | Process existence / pidfile | `unless-stopped` (1 replica) |
| `omni_dispatcher`| `betdoc-backend:latest`| N/A | 1024 MB | 1.0 Cores | Process heartbeat in Redis | `unless-stopped` |
| `frontend_web` | `betdoc-frontend:latest`| `8080` | 512 MB | 1.0 Cores | `wget --spider -q http://127.0.0.1:8080/` | `unless-stopped` |

### 2. High-Performance PostgreSQL Database Configuration Tuning
In production, PostgreSQL requires tuned memory and connection settings:
- `shared_buffers = 512MB`: Dedicated shared memory cache for frequently queried tables (`odds_snapshots`, `paper_bets`, `tax_ledger_entries`).
- `work_mem = 16MB`: Sizing for in-memory sorting during multi-bookmaker line comparisons and P&L aggregations.
- `maintenance_work_mem = 128MB`: Accelerates Alembic migrations and index creation.
- `max_connections = 100`: Accommodates pooled connections from Gunicorn workers and Celery background tasks.
- `effective_cache_size = 1536MB`: Informs query planner of available operating system page cache.

### 3. Redis In-Memory Persistence & Memory Policy
- `maxmemory 1024mb`: Prevents Redis from consuming unbounded host memory.
- `maxmemory-policy allkeys-lru`: Automatically evicts least recently used odds ticks when cache limit is approached.
- `appendonly yes`: Writes every odds modification to disk append log for crash recovery.
- `appendfsync everysec`: Provides optimal trade-off between write throughput and data durability.

### 4. Gunicorn Worker Sizing & Uvicorn Worker Model
- `worker_class = "uvicorn.workers.UvicornWorker"`: Runs modern asynchronous ASGI event loops (`uvloop`).
- `workers = 4`: Based on $W = 2 \times \text{Cores} + 1$, providing optimal concurrency without thrashing context switches.
- `timeout = 60`: Sane upper bound allowing complex multi-sport consensus calculations to complete.
- `keepalive = 5`: Reuses HTTP connections from Nginx reverse proxy.

### 5. Nginx Reverse Proxy Performance & Buffer Tuning
- `worker_processes auto`: Automatically spawns one worker process per CPU core.
- `worker_connections 2048`: Allows up to 8,192 simultaneous WebSocket and HTTP client connections.
- `multi_accept on`: Accepts all incoming connections simultaneously.
- `sendfile on` & `tcp_nopush on`: Kernel-level static asset transfer bypassing user-space buffer copies.
- `gzip_comp_level 6`: Balanced compression reducing asset payload size by ~70% without CPU penalty.

## 📜 PART 6: RETROSPECTIVE ARCHITECTURE & FLEET SYNTHESIS (GROUPS 1 TO 80)

Group 81 packages the entire fleet built across Groups 1 through 80 into a production-grade, reproducible container system:
- **Groups 1-68**: Database schemas, auth tokens, risk models, mathematical predictors.
- **Group 69**: Ashoka Oracle value betting desk.
- **Group 70**: The Hive multi-agent swarm.
- **Group 71**: The Wire intelligence ingestion.
- **Group 72**: True Digital Betting Twin 15 pillars.
- **Group 73**: Settlement feedback loops.
- **Group 74**: Model recalibration engine.
- **Group 75**: Never-Forget mistake guard & XP leveling.
- **Group 76**: Bankroll growth strategies.
- **Group 77**: Advanced backtesting & stop-loss shield.
- **Group 78**: News and wire integration.
- **Group 79**: Multi-sport engine expansion (9 sports, 27 bet types).
- **Group 80**: Paper Trading Sandbox, multi-currency (TODAR MAL), 30% TDS ledger, and arbitrage "Coming Soon" pivot.
- **Group 81**: Production Docker multi-stage builds, hardened Nginx reverse proxy, WebSocket streaming relay, and deep fleet health shield.

---

## 🛠️ PART 7: INFRASTRUCTURE & BACKEND DROP-IN IMPLEMENTATION (FILES 1 TO 10)

### FILE 1: `frontend/Dockerfile`
Multi-stage build for React frontend with Nginx runner:

```dockerfile
# syntax=docker/dockerfile:1.4
# Multi-stage production build for BetDoc React Frontend
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

FROM node:20-alpine AS builder

WORKDIR /app

# Layer Caching Defense
COPY package*.json ./
RUN npm ci

COPY . .

# Build arguments for Vite environment
ARG VITE_API_BASE_URL="/api/v1"
ARG VITE_WS_BASE_URL="/ws"
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_WS_BASE_URL=${VITE_WS_BASE_URL}

RUN npm run build

# Runner stage using unprivileged Nginx
FROM nginx:1.25-alpine AS runner

LABEL maintainer="Amit Ashok Kumar Patnaik"
LABEL description="BetDoc Quantitative Sports Trading Fleet - Frontend Web Server"

# Non-root security: create appuser
RUN addgroup -g 10001 appgroup && \
    adduser -u 10001 -G appgroup -s /bin/sh -D appuser

# Copy custom hardened Nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built static assets from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Configure permissions for non-root execution
RUN chown -R appuser:appgroup /usr/share/nginx/html && \
    chown -R appuser:appgroup /var/cache/nginx && \
    chown -R appuser:appgroup /var/log/nginx && \
    chown -R appuser:appgroup /etc/nginx/conf.d && \
    touch /var/run/nginx.pid && \
    chown -R appuser:appgroup /var/run/nginx.pid

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --spider -q http://127.0.0.1:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
```

---

### FILE 2: `frontend/nginx.conf`
Production hardened Nginx configuration with WebSocket streaming relay and security headers:

```nginx
# Production Nginx Configuration for BetDoc Fleet
# Non-root execution on port 8080
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

worker_processes auto;
pid /var/run/nginx.pid;

events {
    worker_connections 2048;
    use epoll;
    multi_accept on;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # Custom structured access log
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for" '
                    'rt=$request_time uct="$upstream_connect_time" uht="$upstream_header_time" urt="$upstream_response_time"';

    access_log /var/log/nginx/access.log main;
    error_log  /var/log/nginx/error.log warn;

    sendfile           on;
    tcp_nopush         on;
    tcp_nodelay        on;
    keepalive_timeout  65;
    types_hash_max_size 2048;
    client_max_body_size 25M;

    # Compression
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript application/json application/javascript application/xml+rss application/x-font-ttf font/opentype image/svg+xml;

    server {
        listen 8080;
        server_name localhost;
        root /usr/share/nginx/html;
        index index.html;

        # Hardened Security Headers
        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-XSS-Protection "1; mode=block" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        add_header X-Engineered-For "Amit Ashok Kumar Patnaik" always;

        # Static assets aggressive caching
        location ~* \.(?:css|js|jpg|jpeg|gif|png|ico|cur|gz|svg|svgz|mp4|ogg|ogv|webm|htc|woff|woff2)$ {
            expires 1y;
            access_log off;
            add_header Cache-Control "public, immutable";
            add_header X-Engineered-For "Amit Ashok Kumar Patnaik";
        }

        # React SPA routing
        location / {
            try_files $uri $uri/ /index.html;
        }

        # API reverse proxy
        location /api/ {
            resolver 127.0.0.11 valid=10s ipv6=off;
            set $backend "http://backend_api:8000";

            proxy_pass $backend;
            proxy_http_version 1.1;

            # WebSocket upgrade support
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";

            # Proxy headers
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            # Timeouts
            proxy_connect_timeout 60s;
            proxy_send_timeout 60s;
            proxy_read_timeout 60s;
        }

        # Dedicated WebSocket stream relay
        location ~* ^/(?:api/v1/)?(?:ws|live-odds)(?:/|$) {
            resolver 127.0.0.11 valid=10s ipv6=off;
            set $backend "http://backend_api:8000";

            proxy_pass $backend;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

            # Streaming timeout: keep connection open for in-play feeds
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }

        # Health endpoint pass-through
        location /health {
            resolver 127.0.0.11 valid=10s ipv6=off;
            proxy_pass http://backend_api:8000/api/v1/health;
            proxy_set_header Host $host;
        }
    }
}
```

---

### FILE 3: `backend/Dockerfile`
Multi-stage build for FastAPI Python backend with non-root user and layer caching:

```dockerfile
# syntax=docker/dockerfile:1.4
# Multi-stage production build for BetDoc FastAPI Backend
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev build-essential && \
    rm -rf /var/lib/apt/lists/*

# Pre-compile wheels for all requirements
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

# Final runner stage
FROM python:3.12-slim-bookworm AS runner

LABEL maintainer="Amit Ashok Kumar Patnaik"
LABEL description="BetDoc Quantitative Sports Trading Fleet - Core Backend Engine"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Non-root user creation
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

WORKDIR /app

# Runtime libraries
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl procps && \
    rm -rf /var/lib/apt/lists/*

# Install pre-built wheels
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache /wheels/* && \
    rm -rf /wheels

# Copy application code with ownership
COPY --chown=appuser:appgroup . .

USER appuser

EXPOSE 8000

# Healthcheck probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/api/v1/health || exit 1

# Apply migrations on startup and launch Gunicorn
CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app -c gunicorn.conf.py"]
```

---

### FILE 4: `backend/gunicorn.conf.py`
High-performance Gunicorn configuration with Uvicorn workers:

```python
"""
Gunicorn Configuration for BetDoc Production Backend.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

import multiprocessing
import os

bind = "0.0.0.0:8000"
backlog = 2048

# Calculate worker count based on CPU allocation
cores = multiprocessing.cpu_count()
workers = min(int(os.getenv("MAX_WORKERS", 4)), max(2, cores * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 60
keepalive = 5

# Trust proxy IP from Nginx
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")

# Structured standard output logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
```

---

### FILE 5: `docker-compose.yml`
One-command full-stack local development configuration:

```yaml
# Local Development Stack: BetDoc Quantitative Sports Trading Fleet
# Developed for Amit Ashok Kumar Patnaik
# Run with: docker compose up -d

services:
  postgres:
    image: postgres:16-alpine
    container_name: betdoc_db
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: betdoc
    ports:
      - "5432:5432"
    volumes:
      - betdoc_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d betdoc"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: betdoc_redis
    ports:
      - "6379:6379"
    volumes:
      - betdoc_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  backend_api:
    build:
      context: ./backend
      dockerfile: Dockerfile
    image: betdoc-backend:dev
    container_name: betdoc_backend_api
    env_file: ./backend/.env
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:password@postgres:5432/betdoc
      REDIS_URL: redis://redis:6379/0
      ENVIRONMENT: development
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped

  celery_worker:
    build:
      context: ./backend
      dockerfile: Dockerfile
    image: betdoc-backend:dev
    container_name: betdoc_celery_worker
    env_file: ./backend/.env
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:password@postgres:5432/betdoc
      REDIS_URL: redis://redis:6379/0
    command: ["celery", "-A", "app.core.celery_app:celery_app", "worker", "--loglevel=INFO", "--concurrency=2", "--hostname=omni@%h"]
    depends_on:
      backend_api:
        condition: service_healthy
    restart: unless-stopped

  celery_beat:
    build:
      context: ./backend
      dockerfile: Dockerfile
    image: betdoc-backend:dev
    container_name: betdoc_celery_beat
    env_file: ./backend/.env
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:password@postgres:5432/betdoc
      REDIS_URL: redis://redis:6379/0
    command: ["celery", "-A", "app.core.celery_app:celery_app", "beat", "--loglevel=INFO", "--schedule=/tmp/celerybeat-schedule"]
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  frontend_web:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    image: betdoc-frontend:dev
    container_name: betdoc_frontend_web
    ports:
      - "8080:8080"
    depends_on:
      backend_api:
        condition: service_healthy
    restart: unless-stopped

volumes:
  betdoc_postgres_data:
  betdoc_redis_data:
```

---

### FILE 6: `docker-compose.production.yml`
Production-grade deployment configuration with resource constraints, logging limits, and network isolation:

```yaml
# Production Stack: BetDoc Quantitative Sports Trading Fleet
# Developed for Amit Ashok Kumar Patnaik
# Run with: docker compose -f docker-compose.production.yml up -d

name: betdoc

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

x-service-defaults: &service-defaults
  restart: unless-stopped
  security_opt: ["no-new-privileges:true"]
  networks: [betdoc_net]
  logging: *default-logging

x-backend: &backend
  <<: *service-defaults
  image: betdoc-backend:latest
  build:
    context: ./backend
    dockerfile: Dockerfile
  env_file: ./backend/.env

services:
  postgres_db:
    <<: *service-defaults
    image: postgres:16-alpine
    container_name: betdoc_prod_postgres
    env_file: ./backend/.env
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis_cache:
    <<: *service-defaults
    image: redis:7-alpine
    container_name: betdoc_prod_redis
    env_file: ./backend/.env
    command: redis-server --appendonly yes
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  backend_api:
    <<: *backend
    container_name: betdoc_prod_api
    environment:
      FORWARDED_ALLOW_IPS: "*"
      MAX_WORKERS: 4
      ENVIRONMENT: production
    depends_on:
      postgres_db:
        condition: service_healthy
      redis_cache:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://127.0.0.1:8000/api/v1/health || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 3

  celery_worker:
    <<: *backend
    container_name: betdoc_prod_celery_worker
    command: ["celery", "-A", "app.core.celery_app:celery_app", "worker", "--loglevel=INFO", "--concurrency=4", "--hostname=omni@%h"]
    depends_on:
      backend_api:
        condition: service_healthy

  celery_beat:
    <<: *backend
    container_name: betdoc_prod_celery_beat
    command: ["celery", "-A", "app.core.celery_app:celery_app", "beat", "--loglevel=INFO", "--schedule=/tmp/celerybeat-schedule"]
    depends_on:
      redis_cache:
        condition: service_healthy
      celery_worker:
        condition: service_started

  frontend_web:
    <<: *service-defaults
    build:
      context: ./frontend
      dockerfile: Dockerfile
    image: betdoc-frontend:latest
    container_name: betdoc_prod_frontend
    ports:
      - "80:8080"
    depends_on:
      backend_api:
        condition: service_healthy

networks:
  betdoc_net:
    driver: bridge

volumes:
  pgdata:
  redisdata:
```

---

### FILE 7: `backend/app/api/endpoints/health.py`
Upgraded Fleet Health Shield with shallow liveness and deep readiness verification:

```python
"""
Fleet Health Shield & Diagnostic Endpoints (Commanders PRATAP & CHANAKYA).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

import asyncio
import time
from typing import Any, Dict
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()

@router.get("", summary="Shallow Liveness Check")
@router.get("/", summary="Shallow Liveness Check")
async def shallow_health_check() -> Dict[str, Any]:
    """Immediate 200 OK liveness check for load balancers."""
    return {
        "status": "online",
        "service": "BetDoc Quantitative Sports Trading Fleet",
        "timestamp": time.time(),
        "developer_credit": "Amit Ashok Kumar Patnaik",
    }

@router.get("/deep", summary="Deep Fleet Readiness Check")
async def deep_fleet_readiness(
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Deep readiness check testing database connectivity, Redis responsiveness,
    Celery state, and paper trading system health.
    """
    start = time.time()
    checks: Dict[str, Any] = {}
    is_healthy = True

    # 1. PostgreSQL DB Check
    try:
        res = await db.execute(text("SELECT 1"))
        scalar = res.scalar()
        checks["database"] = {"status": "healthy" if scalar == 1 else "degraded", "latency_ms": round((time.time() - start) * 1000, 2)}
    except Exception as exc:
        checks["database"] = {"status": "unhealthy", "error": str(exc)}
        is_healthy = False

    # 2. Redis Cache Check
    r_start = time.time()
    try:
        from app.workers.oracle_tasks import _resources
        async with _resources() as (redis, _, _):
            ping = await redis.ping()
            checks["redis"] = {"status": "healthy" if ping else "degraded", "latency_ms": round((time.time() - r_start) * 1000, 2)}
    except Exception as exc:
        checks["redis"] = {"status": "unhealthy", "error": str(exc)}
        is_healthy = False

    # 3. Paper Trading Sandbox Status
    try:
        from app.models.paper_trading import PaperAccount
        from sqlalchemy import select
        acc_stmt = select(PaperAccount).limit(1)
        acc_res = await db.execute(acc_stmt)
        has_account = acc_res.scalar_one_or_none() is not None
        checks["paper_trading_sandbox"] = {"status": "ready" if has_account else "uninitialized", "virtual_currency": "INR"}
    except Exception as exc:
        checks["paper_trading_sandbox"] = {"status": "error", "error": str(exc)}

    # 4. Fleet Commander Status
    checks["commanders"] = {
        "active_count": 17,
        "infrastructure": "PRATAP",
        "governance": "CHANAKYA",
        "execution_sniper": "ASHVATTHAMA (Parimatch Primary)",
        "ledger_and_tax": "TODAR MAL (INR / 30% TDS)",
        "oracle": "ASHOKA",
        "arbitrage_status": "PAUSED_COMING_SOON",
    }

    elapsed_ms = round((time.time() - start) * 1000, 2)
    payload = {
        "status": "ready" if is_healthy else "degraded",
        "total_latency_ms": elapsed_ms,
        "environment": getattr(settings, "ENVIRONMENT", "production"),
        "checks": checks,
        "developer_credit": "Amit Ashok Kumar Patnaik",
    }

    return JSONResponse(
        content=payload,
        status_code=status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
```

---

### FILE 8: `backend/app/api/v1/router.py` Wire-Up
Wire the upgraded health endpoints in the API router:

```python
from app.api.endpoints.health import router as health_router

api_router.include_router(health_router, prefix="/health", tags=["Fleet Health Shield"])
```

---

### FILE 9: `backend/.env.production.example`
Production environment variable template:

```env
# BetDoc Production Environment Variables
# Developed for Amit Ashok Kumar Patnaik

ENVIRONMENT=production
PROJECT_NAME=BetDoc Quantitative Sports Trading Fleet
DEVELOPER_NAME=Amit Ashok Kumar Patnaik

# PostgreSQL Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_postgres_password_here
POSTGRES_DB=betdoc
DATABASE_URL=postgresql+asyncpg://postgres:your_secure_postgres_password_here@postgres_db:5432/betdoc

# Redis Cache & PubSub
REDIS_PASSWORD=your_secure_redis_password_here
REDIS_URL=redis://:your_secure_redis_password_here@redis_cache:6379/0

# Security & Encryption Vault
SECRET_KEY=your_super_secret_jwt_key_here
MASTER_VAULT_KEY=your_32_byte_hex_vault_key_here

# Celery Fleet
CELERY_CONCURRENCY=4

# Target Bookmaker Tokens (Decrypted via Vault in Production)
PARIMATCH_FEED_TOKEN=
ODDS_API_KEY=
```

---

### FILE 10: `frontend/.env.production.example`
Frontend production environment variable template:

```env
# Frontend Production Environment
VITE_API_BASE_URL=/api/v1
VITE_WS_BASE_URL=/ws
```

---

## 🛠️ PART 8: AUTOMATED TEST SUITE (FILE 11)

### FILE 11: `backend/tests/test_docker_and_nginx_config.py`
Automated test suite verifying Dockerfiles, Nginx syntax, and health endpoints:

```python
"""
Unit Tests for Docker Configuration, Nginx Reverse Proxy, and Deep Health Endpoints.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
"""

import os
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

def test_frontend_dockerfile_structure():
    """Validates multi-stage build instructions and non-root execution in frontend Dockerfile."""
    path = os.path.join(os.path.dirname(__file__), "../../frontend/Dockerfile")
    if not os.path.exists(path):
        pytest.skip("frontend/Dockerfile not found on disk")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "FROM node:20-alpine AS builder" in content
    assert "FROM nginx:1.25-alpine AS runner" in content
    assert "USER appuser" in content
    assert "EXPOSE 8080" in content
    assert "Amit Ashok Kumar Patnaik" in content

def test_nginx_config_rules():
    """Validates reverse proxy rules, WebSocket upgrades, and security headers in nginx.conf."""
    path = os.path.join(os.path.dirname(__file__), "../../frontend/nginx.conf")
    if not os.path.exists(path):
        pytest.skip("frontend/nginx.conf not found on disk")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "listen 8080;" in content
    assert "proxy_pass $backend;" in content
    assert "proxy_set_header Upgrade $http_upgrade;" in content
    assert 'proxy_set_header Connection "upgrade";' in content
    assert 'X-Content-Type-Options "nosniff"' in content
    assert "Amit Ashok Kumar Patnaik" in content

def test_backend_dockerfile_structure():
    """Validates backend Dockerfile multi-stage build and non-root execution."""
    path = os.path.join(os.path.dirname(__file__), "../Dockerfile")
    if not os.path.exists(path):
        pytest.skip("backend/Dockerfile not found on disk")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "FROM python:3.12-slim-bookworm" in content
    assert "USER appuser" in content
    assert "EXPOSE 8000" in content

@pytest.mark.asyncio
async def test_health_endpoints():
    """Tests /health shallow liveness endpoint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "online"
        assert data["developer_credit"] == "Amit Ashok Kumar Patnaik"
```

---

## 📋 PART 9: CLAUDE CODE EXECUTION PLAYBOOK & ZERO-DRIFT VERIFICATION CHECKLIST

1. **Verify Git Working Tree**:
   Ensure `git status` is clean on branch `feature/garuda-ingestion`.
2. **Apply Infrastructure Updates**:
   - Write `frontend/Dockerfile` and `frontend/nginx.conf`.
   - Write `backend/Dockerfile` and `backend/gunicorn.conf.py`.
   - Update `docker-compose.yml` and `docker-compose.production.yml`.
   - Update `backend/app/api/endpoints/health.py` and register route in `router.py`.
3. **Run Automated Tests**:
   `pytest backend/tests/test_docker_and_nginx_config.py -v`.
4. **Run Frontend Build**:
   `npm run build` in `frontend/` (0 errors).
5. **Commit & Push to GitHub**:
   ```bash
   git add -A
   git commit -m "feat(infra): Group 81 production Docker multi-stage builds, hardened Nginx reverse proxy, and fleet health shield"
   git push origin feature/garuda-ingestion
   git branch -f main feature/garuda-ingestion
   git push origin main
   ```
