"""The neural event bus (Group 108, point b.9). Developed for Amit Ashok Kumar Patnaik.

    POST /api/v1/neural-bus/chains         run the chain on one item of VIDUR's board (with your slip, the Consultant re-scores it)
    GET  /api/v1/neural-bus/chains?limit=  the recent chains: the Wire's own (system) and yours, newest first
    GET  /api/v1/neural-bus/modules        the five modules in chain order, the channel and the transport
    WS   /api/v1/ws/neural-bus?token=...   live frames: chain_started, step (RUNNING, then its end), chain

The REST routes are mounted for signed-in users (``router.py``); the socket checks its token in the handshake and
forwards the system's frames and the viewer's own, never another user's. Frames come from Redis pub/sub
(``<NEURAL_BUS_PREFIX>:pipeline``) and from this process's hub (chains run here while Redis was unreachable).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, WsUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.neural_bus.chain import MODULES
from app.schemas.neural_bus import ChainRequest
from app.services import neural_bus as bus
from app.services import neural_chain
from app.services.twin.vetting import developer_credit

logger = logging.getLogger("betdoc.neural.api")

router = APIRouter(tags=["neural_bus"])
ws_router = APIRouter(tags=["websockets"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request | WebSocket) -> Redis | None:
    return getattr(request.app.state, "redis", None)


async def _credit(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as session:
        return await developer_credit(session)


@router.post("/chains", status_code=status.HTTP_201_CREATED)
async def start_chain(body: ChainRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    redis = _redis(request)
    lock = f"user:{user.id}"
    if not await bus.claim(redis, settings, lock, settings.NEURAL_BUS_USER_LOCK_SECONDS):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, {"reason": "CHAIN_RUNNING", "message": "Your last chain is still running: its frames are on the bar"})
    try:
        chain = await neural_chain.run_item(sessions, redis, settings, body.item_id, user_id=user.id, leg_ids=body.leg_ids, skin=body.skin,
                                            now=datetime.now(UTC))
    except neural_chain.ChainRefusal as exc:
        raise HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message}) from exc
    finally:
        await bus.release(redis, settings, lock)
    return {**chain, "mine": True, "developer_credit": await _credit(sessions)}


@router.get("/chains")
async def recent_chains(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings,
                        limit: Annotated[int, Query(ge=1, le=500)] = 10) -> dict[str, Any]:
    mine = str(user.id)
    chains = await bus.recent(_redis(request), settings, [bus.SYSTEM, mine], min(limit, settings.NEURAL_BUS_HISTORY))
    # a chain's "mine" is the viewer's: the stored record was written for its audience, not for this viewer
    return {"chains": [{**c, "mine": c.get("origin") == "USER"} for c in chains], "developer_credit": await _credit(sessions)}


@router.get("/modules")
async def modules(request: Request, user: CurrentUser, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001 - user enforces authentication
    return {"modules": [{"module": m.value, "name": name, "role": role} for m, name, role in MODULES], "channel": bus.channel(settings),
            "transport": "redis" if _redis(request) is not None else "in-process"}


@ws_router.websocket("/neural-bus")
async def neural_bus_socket(websocket: WebSocket, user: WsUser, settings: AppSettings) -> None:
    viewer = str(user.id)
    redis = _redis(websocket)
    pubsub = None
    if redis is not None:
        try:
            pubsub = redis.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(bus.channel(settings))
        except Exception:  # noqa: BLE001 - Redis down: the hub still carries this process's chains
            pubsub = None
    queue = bus.HUB.listen()
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        audience = frame.pop("audience", None)
        if audience not in (bus.SYSTEM, viewer):
            return  # another user's chain
        frame["mine"] = audience == viewer
        async with send_lock:
            await websocket.send_text(json.dumps(frame, separators=(",", ":")))

    async with send_lock:
        await websocket.send_text(json.dumps({"type": "connection_ack", "channel": bus.channel(settings), "transport": "redis" if pubsub is not None else "in-process",
                                              "status": "LISTENING_TO_THE_NEURAL_BUS"}))

    async def from_redis() -> None:
        if pubsub is None:
            await asyncio.Event().wait()  # nothing to read: wait for the socket to end
        async for message in pubsub.listen():  # type: ignore[union-attr]
            if message.get("type") == "message":
                await send(message["data"])

    async def from_hub() -> None:
        while True:
            await send(await queue.get())

    async def drain() -> None:
        while True:
            if await websocket.receive_text() == "ping":
                async with send_lock:
                    await websocket.send_text('{"type":"pong"}')

    tasks = [asyncio.create_task(from_redis()), asyncio.create_task(from_hub()), asyncio.create_task(drain())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        bus.HUB.drop(queue)
        if pubsub is not None:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("neural bus socket: pubsub already closed")
