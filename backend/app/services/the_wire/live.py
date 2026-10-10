"""The Wire's live channel (``/ws/the-wire``): one Redis pub/sub channel for every viewer, and the last
``WIRE_RECENT_FRAMES`` frames for a socket's opening burst. Frames: ``news``, ``catalyst``, ``scores``, ``weather``.
Publishing never raises: the record in the database is the truth, the channel only hurries it along."""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings

logger = logging.getLogger("betdoc.vidur.live")


def channel(settings: Settings) -> str:
    return f"{settings.WIRE_PREFIX}:live"


def recent_key(settings: Settings) -> str:
    return f"{settings.WIRE_PREFIX}:recent"


async def publish(redis: Redis | None, settings: Settings, frames: list[dict[str, Any]]) -> int:
    if redis is None or not frames:
        return 0
    try:
        pipe = redis.pipeline(transaction=False)
        for frame in frames:
            raw = json.dumps(frame, separators=(",", ":"), default=str)
            pipe.publish(channel(settings), raw)
            pipe.lpush(recent_key(settings), raw)
        pipe.ltrim(recent_key(settings), 0, settings.WIRE_RECENT_FRAMES - 1)
        await pipe.execute()
        return len(frames)
    except (RedisError, OSError):
        logger.warning("VIDUR: %d live frames could not reach Redis", len(frames))
        return 0


async def recent(redis: Redis, settings: Settings) -> list[str]:
    """Newest first."""
    return list(await redis.lrange(recent_key(settings), 0, settings.WIRE_RECENT_FRAMES - 1))
