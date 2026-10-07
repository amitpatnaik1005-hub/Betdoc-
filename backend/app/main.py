from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import contextlib

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
import asyncio
import logging

from app.core.config import settings
from app.core.database import engine
from app.core.logging import RequestContextMiddleware, setup_json_logging
from app.core.rate_limit import limiter, rate_limit_middleware
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.api.deps import DbSession, get_current_user
from app.api.endpoints import health as deep_health
from app.api.endpoints import telemetry
from app.api.v1.router import api_router
from app.services.odds_client import OddsAPIClient
from app.services.odds_poller import poll_and_store_odds

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

    poller_task = asyncio.create_task(poll_and_store_odds(), name="odds-poller")
    try:
        yield
    finally:
        poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task

        await OddsAPIClient.aclose()
        await redis.aclose()
        # Close pooled DB connections cleanly on shutdown
        await engine.dispose()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
app.include_router(api_router)
app.include_router(deep_health.router, prefix="/api/v1/health", tags=["health"])
app.include_router(telemetry.router, dependencies=[Depends(get_current_user)])

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
