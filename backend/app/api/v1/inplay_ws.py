"""The in-play stop-loss shield, live: ``WebSocket /api/v1/ws/inplay-shield?token=...`` (Group 77).

Every tick of the twin's watch (``TWIN_INPLAY_POLL_SECONDS``) publishes one frame per watched bet to the user's
channel: the live win probability, fair value, the offer read in, the stop-loss floor, and the cashout ticket
when the shield fires. On connect the socket sends the latest frame of each bet, then relays the channel.
The client sends "ping" to keep the socket alive and gets ``{"type":"pong"}``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, status

from app.api.deps import WsUser
from app.core.config import settings
from app.services.twin.inplay import last_frames_key, live_channel

logger = logging.getLogger("betdoc.inplay_ws")

router = APIRouter(tags=["websockets"])


@router.websocket("/inplay-shield")
async def inplay_shield(websocket: WebSocket, user: WsUser) -> None:
    redis = getattr(websocket.app.state, "redis", None)
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(live_channel(settings, user.id))
        latest = await redis.hgetall(last_frames_key(settings, user.id))
    except Exception:  # noqa: BLE001 - Redis down: refuse the socket, the client polls /manual-parlay/live-shields instead
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    for raw in latest.values():
        await websocket.send_text(raw)

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") == "message":
                async with send_lock:
                    await websocket.send_text(message["data"])

    async def drain() -> None:
        while True:
            if await websocket.receive_text() == "ping":
                async with send_lock:
                    await websocket.send_text('{"type":"pong"}')

    tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("in-play shield socket: pubsub already closed")
