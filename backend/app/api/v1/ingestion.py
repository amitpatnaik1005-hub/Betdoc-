import asyncio
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from redis.asyncio import Redis

from app.core.config import settings
from app.core.live_odds import publish_board_ticks
from app.core.websockets import manager
from app.schemas.market import MarketTick

logger = logging.getLogger(__name__)

MAX_TICKS_PER_BATCH = 1000

api_key_header = APIKeyHeader(name="X-Ingestion-Key", auto_error=True)


async def verify_ingestion_key(api_key: str = Security(api_key_header)) -> None:
    expected = settings.INGESTION_API_KEY.get_secret_value()
    # Constant-time comparison: `==` returns sooner the earlier the strings differ,
    # which lets an attacker recover the key one character at a time
    if not secrets.compare_digest(api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid ingestion key")


router = APIRouter(tags=["ingestion"])

# Broadcasts run one batch at a time, in arrival order. Without this, two background tasks
# from back-to-back requests interleave, and an OLDER price can reach clients after a
# newer one, leaving stale odds on screen. asyncio.Lock hands out access first-come, first-served.
_broadcast_lock = asyncio.Lock()


def _latest_per_market(ticks: list[MarketTick]) -> list[MarketTick]:
    """Keep only the final tick for each (match, market) in a batch. Earlier ones are
    already outdated, and sending them costs every client bandwidth and re-renders."""
    latest: dict[tuple[str, str, str], MarketTick] = {}
    for tick in ticks:
        key = (tick.match_id, tick.market_type, tick.selection)
        latest.pop(key, None)  # Re-insert at the end so order reflects the latest update
        latest[key] = tick
    return list(latest.values())


async def process_ticks(ticks: list[MarketTick], redis: Redis | None = None) -> None:
    """Publish through Redis so every API worker's sockets get the batch. Only when Redis is down
    does this worker broadcast on its own (its clients still update; other workers' clients don't)."""
    latest = [t.model_copy(update={"source": t.source or "ingest"}) for t in _latest_per_market(ticks)]
    async with _broadcast_lock:
        if await publish_board_ticks(redis, latest):
            return
        try:
            await manager.broadcast_market_ticks(latest)
        except Exception:
            # A background task has no caller to report to: log, never raise.
            logger.exception("Local broadcast failed for %d tick(s)", len(latest))


# Body(...) instead of pydantic Field(...): FastAPI only reads request-parameter metadata
# from its own param classes. Body subclasses Field's FieldInfo, so the list-length
# limits are enforced the same way.
@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_ingestion_key)],
)
async def ingest_ticks(
    ticks: Annotated[list[MarketTick], Body(min_length=1, max_length=MAX_TICKS_PER_BATCH)],
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, str | int]:
    background_tasks.add_task(process_ticks, ticks, getattr(request.app.state, "redis", None))
    return {"status": "ingested", "count": len(ticks)}
