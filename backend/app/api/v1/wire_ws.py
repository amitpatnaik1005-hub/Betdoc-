"""The Wire, live: ``WebSocket /api/v1/ws/the-wire?token=...`` (Group 78). Developed for Amit Ashok Kumar Patnaik.

On connect: a greeting (``{"type": "connection_ack", "developer": ..., "status": "CONNECTED_TO_VIDUR_TACTICAL_WIRE"}``),
then the last ``WIRE_RECENT_FRAMES`` frames oldest first, then every new frame as the scans publish them (``news``,
``catalyst``, ``scores``, ``weather``). The channel is Redis pub/sub, so frames from the Celery workers arrive at
every API process. The client sends "ping" and gets ``{"type":"pong"}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import WsUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.services.the_wire.live import channel, recent

logger = logging.getLogger("betdoc.vidur.ws")

router = APIRouter(tags=["websockets"])


async def _developer(sessions: async_sessionmaker[AsyncSession]) -> str:
    from app.services.twin.vetting import developer_credit  # noqa: PLC0415 - pulls in the oracle engine

    try:
        async with sessions() as session:
            return await developer_credit(session)
    except Exception:  # noqa: BLE001 - the greeting never blocks the socket
        return "Amit Ashok Kumar Patnaik"


@router.websocket("/the-wire")
async def the_wire(websocket: WebSocket, user: WsUser, sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],  # noqa: ARG001 - WsUser authenticates
                   settings: Annotated[Settings, Depends(get_settings)]) -> None:
    redis = getattr(websocket.app.state, "redis", None)
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live wire unavailable")
        return
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(channel(settings))
        backlog = await recent(redis, settings)
    except Exception:  # noqa: BLE001 - Redis down: refuse; the page polls /the-wire/dashboard instead
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live wire unavailable")
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    await websocket.send_text(json.dumps({"type": "connection_ack", "topic": "connection_ack", "developer": await _developer(sessions),
                                          "status": "CONNECTED_TO_VIDUR_TACTICAL_WIRE"}))
    for raw in reversed(backlog):
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
            logger.debug("wire socket: pubsub already closed")
