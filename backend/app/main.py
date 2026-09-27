from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import contextlib

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
import asyncio

from app.core.config import settings
from app.core.database import engine
from app.api.deps import DbSession
from app.api.v1.router import api_router
from app.services.odds_client import OddsAPIClient
from app.services.odds_poller import poll_and_store_odds

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    poller_task = asyncio.create_task(poll_and_store_odds(), name="odds-poller")
    try:
        yield
    finally:
        poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller_task
            
        await OddsAPIClient.aclose()
        # Close pooled DB connections cleanly on shutdown
        await engine.dispose()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
app.include_router(api_router)

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
