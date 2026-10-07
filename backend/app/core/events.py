"""Cross-section event bus: Redis pub/sub fanned out to browsers over WebSockets.

Every successful write anywhere in the API publishes a small ``mutation`` event naming the section
it touched, so open dashboards refresh the affected panels instantly instead of waiting to poll.
Background services publish their own typed events (e.g. ``commanders`` heartbeats) the same way.

Publishing never blocks or fails a request: Redis being down only costs real-time freshness.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response, WebSocket, WebSocketDisconnect, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "betdoc:events"
API_PREFIX = "/api/v1/"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Auth and raw ingestion are high-volume or sensitive and change nothing a dashboard renders.
_SILENT_SECTIONS = frozenset({"auth", "ingest"})
_PUBLISH_TIMEOUT_SECONDS = 0.5
_background: set[asyncio.Task[None]] = set()


def section_of(path: str) -> str | None:
    """``/api/v1/the-vault/cfo/alerts`` -> ``the-vault``; non-API paths -> None."""
    if not path.startswith(API_PREFIX):
        return None
    head = path[len(API_PREFIX):].split("/", 1)[0]
    return head or None


async def publish_event(redis: Redis | None, event: dict[str, Any]) -> None:
    if redis is None:
        return
    payload = json.dumps({**event, "at": datetime.now(UTC).isoformat()}, default=str, separators=(",", ":"))
    try:
        await asyncio.wait_for(redis.publish(EVENTS_CHANNEL, payload), timeout=_PUBLISH_TIMEOUT_SECONDS)
    except (RedisError, OSError, TimeoutError):
        logger.debug("Event publish skipped (Redis unavailable): %s", event.get("type"))


def _publish_in_background(redis: Redis, event: dict[str, Any]) -> None:
    task = asyncio.create_task(publish_event(redis, event))
    _background.add(task)  # keep a reference so the task isn't garbage-collected mid-flight
    task.add_done_callback(_background.discard)


async def mutation_event_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    if request.method in _MUTATING_METHODS and 200 <= response.status_code < 300:
        section = section_of(request.url.path)
        redis: Redis | None = getattr(request.app.state, "redis", None)
        if section and section not in _SILENT_SECTIONS and redis is not None:
            _publish_in_background(
                redis,
                {
                    "type": "mutation",
                    "section": section,
                    "path": request.url.path,
                    "method": request.method,
                    "status": response.status_code,
                },
            )
    return response


async def relay_channel(websocket: WebSocket, redis: Redis | None, channel: str) -> None:
    """Forward one Redis channel to an (already authenticated) WebSocket until either side closes."""
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
    except (RedisError, OSError):
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    await websocket.accept()

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") == "message":
                await websocket.send_text(message["data"])

    async def drain() -> None:
        # Keeps the socket alive, answers client pings, and notices disconnects.
        while True:
            if await websocket.receive_text() == "ping":
                await websocket.send_text('{"type":"pong"}')

    tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("Relay for %s ended: %s", channel, exc)
    finally:
        for task in tasks:
            task.cancel()
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except (RedisError, OSError):
            pass
