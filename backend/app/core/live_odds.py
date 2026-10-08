"""Live odds over Redis, so ``/ws/live-odds`` works across every API worker.

Publishers (the Omni fleet, the ``/ingest`` endpoint) never touch WebSockets. They store the tick
in Redis and PUBLISH it. Each API worker runs one relay that subscribes to the channel and fans
every message out to the sockets that worker holds, so a tick produced anywhere reaches every
client, whichever gunicorn worker accepted its socket. A board snapshot in Redis means a socket
opened on any worker starts from the full board, not just what that worker has seen.

Keys (prefix ``LIVE_ODDS_CHANNEL``):
    <channel>             pub/sub; each message is a JSON array of camelCase MarketTicks
    <channel>:board       hash   board_key -> latest MarketTick JSON
    <channel>:board:ts    zset   board_key -> unix time of its last update (staleness + pruning)
    <channel>:src:<key>   hash   source id -> that source's own tick (the fleet merges these)
    <channel>:hist:<key>  zset   "<ts>|<odds>|<true_prob>" scored by time: the last few minutes of
                                 each cell, for the CFO velocity lock
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.schemas.market import MarketTick

logger = logging.getLogger(__name__)

_PUBLISH_TIMEOUT_SECONDS = 2.0
_RELAY_BACKOFF_MAX_SECONDS = 30.0


class TickSink(Protocol):
    async def broadcast_market_ticks(self, ticks: Sequence[MarketTick]) -> None: ...


class LiveOddsKeys:
    __slots__ = ("board", "board_ts", "channel")

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.board = f"{channel}:board"
        self.board_ts = f"{channel}:board:ts"

    def sources(self, board_key: str) -> str:
        """Hash: source id -> that source's latest tick for one board cell (the merge input)."""
        return f"{self.channel}:src:{board_key}"


def live_odds_keys() -> LiveOddsKeys:
    return LiveOddsKeys(get_settings().LIVE_ODDS_CHANNEL)


def tick_history_key(settings: Settings, board_key: str) -> str:
    return f"{settings.LIVE_ODDS_CHANNEL}:hist:{board_key}"


def encode_ticks(ticks: Iterable[MarketTick]) -> str:
    """camelCase JSON array, the shape the frontend market store accepts."""
    return "[" + ",".join(t.model_dump_json(by_alias=True) for t in ticks) + "]"


def decode_ticks(raw: str | bytes) -> list[MarketTick]:
    """Parse one channel message. Malformed items are dropped, never forwarded to browsers."""
    try:
        parsed: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("Dropped non-JSON live-odds message")
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    ticks: list[MarketTick] = []
    for item in items:
        try:
            ticks.append(MarketTick.model_validate(item))
        except ValidationError:
            logger.debug("Dropped invalid live-odds tick")
    return ticks


async def publish_board_ticks(redis: Redis | None, ticks: Sequence[MarketTick]) -> bool:
    """Store and broadcast board ticks. Returns False when Redis is unavailable (caller decides the fallback)."""
    if redis is None or not ticks:
        return redis is not None
    settings = get_settings()
    keys = live_odds_keys()
    now = time.time()
    cutoff = now - settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS
    history = settings.CFO_TICK_HISTORY_SECONDS
    try:
        async with asyncio.timeout(_PUBLISH_TIMEOUT_SECONDS):
            stale: list[str] = await redis.zrangebyscore(keys.board_ts, "-inf", cutoff)
            pipe = redis.pipeline(transaction=False)
            if stale:
                pipe.hdel(keys.board, *stale)
                pipe.zrem(keys.board_ts, *stale)
            pipe.hset(keys.board, mapping={t.board_key: t.model_dump_json(by_alias=True) for t in ticks})
            pipe.zadd(keys.board_ts, {t.board_key: now for t in ticks})
            for tick in ticks:
                if tick.odds > 1:
                    hist = tick_history_key(settings, tick.board_key)
                    pipe.zadd(hist, {f"{now:.3f}|{tick.odds}|{tick.true_probability}": now})
                    pipe.zremrangebyscore(hist, "-inf", now - history)
                    pipe.expire(hist, history)
            pipe.publish(keys.channel, encode_ticks(ticks))
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        logger.warning("Live-odds publish failed for %d tick(s); Redis unavailable", len(ticks))
        return False
    return True


async def read_snapshot(redis: Redis | None) -> list[MarketTick] | None:
    """Every board tick updated within the snapshot TTL, or None when Redis can't answer."""
    if redis is None:
        return None
    keys = live_odds_keys()
    cutoff = time.time() - get_settings().LIVE_ODDS_SNAPSHOT_TTL_SECONDS
    try:
        async with asyncio.timeout(_PUBLISH_TIMEOUT_SECONDS):
            fresh: list[str] = await redis.zrangebyscore(keys.board_ts, cutoff, "+inf")
            values: list[str | None] = await redis.hmget(keys.board, fresh) if fresh else []
    except (RedisError, OSError, TimeoutError):
        return None
    return [tick for value in values if value for tick in decode_ticks(value)]


async def run_live_odds_relay(redis: Redis, sink: TickSink) -> None:
    """Forward the live-odds channel to this worker's sockets until cancelled. Reconnects on Redis loss."""
    channel = live_odds_keys().channel
    backoff = 1.0
    while True:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(channel)
            logger.info("Live-odds relay subscribed to %s", channel)
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                ticks = decode_ticks(message["data"])
                if ticks:
                    await sink.broadcast_market_ticks(ticks)
        except (RedisError, OSError) as exc:
            logger.warning("Live-odds relay lost Redis (%s); retrying in %.0fs", type(exc).__name__, backoff)
        finally:
            with contextlib.suppress(RedisError, OSError, RuntimeError):
                await pubsub.aclose()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _RELAY_BACKOFF_MAX_SECONDS)
