from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import contextlib

import httpx
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
import asyncio
import logging

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.events import mutation_event_middleware
from app.core.logging import RequestContextMiddleware, setup_json_logging
from app.core.rate_limit import limiter, rate_limit_middleware
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.api.deps import DbSession, get_current_user
from app.api.endpoints import health as deep_health
from app.api.endpoints import telemetry
from app.api.v1.router import api_router
from app.services.commander_supervisor import ProbeContext, run_supervisor
from app.services.omni_fleet import FleetDeps, run_inprocess_fallback
from app.domain.the_hive import HiveOrchestrator
from app.core.live_odds import run_live_odds_relay
from app.services.aryabhata_pipeline import run_aryabhata
from app.services.bookmaker_gateway import BookmakerConfigurationError, build_gateway
from app.core.websockets import manager as live_odds_manager

logger = logging.getLogger(__name__)

if settings.ENVIRONMENT == "production":
    setup_json_logging()


def _build_vault() -> VaultCrypto | None:
    try:
        return VaultCrypto.from_settings(settings)
    except VaultConfigurationError as exc:
        # Omni admin answers 503 until a valid MASTER_VAULT_KEY is configured
        logger.warning("Omni vault disabled: %s", exc)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Shared clients for routers that read app.state (Omni stream/admin, deep health, rate limiter)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        await redis.ping()
    except (RedisError, OSError):
        logger.warning("Redis unreachable at startup; live stream and rate limiting are degraded until it returns")
    app.state.settings = settings
    app.state.redis = redis
    app.state.vault = _build_vault()
    limiter.initialize(redis)

    # Ingestion fleet: shared HTTP client for in-process runs (fallback loop, Fleet Command "Run now")
    fleet_http = httpx.AsyncClient(
        timeout=settings.omni_http_timeout_seconds,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=settings.omni_http_max_connections),
    )
    app.state.fleet_deps = FleetDeps(
        redis=redis,
        session_factory=AsyncSessionLocal,
        http=fleet_http,
        vault=app.state.vault,
        settings=settings,
        local_sink=live_odds_manager,  # Redis down: in-process runs still reach this worker's sockets
    )

    # CFO two-phase execution: the bookmaker leg (paper unless CFO_EXECUTION_MODE=live is configured)
    cfo_http = httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=20))
    try:
        app.state.bookmaker = build_gateway(settings, cfo_http)
    except BookmakerConfigurationError as exc:
        app.state.bookmaker = None  # /omni/execute-trade answers 503 instead of guessing
        logger.error("CFO execution disabled: %s", exc)

    background = [
        # Every worker relays the Redis live-odds channel to the sockets it holds (cross-worker fan-out)
        asyncio.create_task(run_live_odds_relay(redis, live_odds_manager), name="live-odds-relay"),
    ]
    if settings.ARYABHATA_ENABLED:
        # Each worker joins the Aryabhata consumer group: every frame is priced once, wherever it lands
        background.append(asyncio.create_task(run_aryabhata(redis, settings), name="aryabhata"))
    if settings.OMNI_FLEET_INPROCESS_FALLBACK:
        # Runs ingestion here only while no Celery worker heartbeats; replaces the old odds poller loop
        background.append(asyncio.create_task(run_inprocess_fallback(app.state.fleet_deps), name="fleet-fallback"))
    if settings.HIVE_SUPERVISOR_INTERVAL_SECONDS > 0:
        probe_ctx = ProbeContext(
            redis=redis,
            vault_configured=app.state.vault is not None,
            ws_clients=lambda: len(live_odds_manager.active_connections),
        )
        background.append(
            asyncio.create_task(
                run_supervisor(AsyncSessionLocal, HiveOrchestrator(), probe_ctx, settings.HIVE_SUPERVISOR_INTERVAL_SECONDS),
                name="commander-supervisor",
            )
        )
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await fleet_http.aclose()
        await cfo_http.aclose()
        await redis.aclose()
        # Close pooled DB connections cleanly on shutdown
        await engine.dispose()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
app.include_router(api_router)
app.include_router(deep_health.router, prefix="/api/v1/health", tags=["health"])
app.include_router(telemetry.router, dependencies=[Depends(get_current_user)])

app.middleware("http")(mutation_event_middleware)
app.middleware("http")(rate_limit_middleware)
app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=False,  # Bearer tokens travel in a header, not cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(errors)},
    )


@app.get("/health")
async def health(db: DbSession) -> dict[str, str]:
    # FIX: Check if database is actually reachable
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "unreachable"
        
    return {
        "status": "ok", 
        "project": settings.PROJECT_NAME,
        "database": db_status
    }
