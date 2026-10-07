import asyncio
import logging
import json
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from redis.asyncio import Redis

from app.api.v1.ws import _authenticate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omni", tags=["omni"])

@router.websocket("/ws/stream")
async def omni_ws_proxy(websocket: WebSocket, token: str | None = Query(default=None)):
    """
    Forward the omni_live_stream Redis channel to the frontend browser OmniSocket.
    """
    settings = websocket.app.state.settings
    # Same handshake rules as /ws/live-odds: browsers skip CORS for WebSockets, and the token rides in the query.
    origin = websocket.headers.get("origin")
    if origin and origin not in settings.BACKEND_CORS_ORIGINS:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
        return
    if await _authenticate(token) is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    redis: Redis | None = websocket.app.state.redis
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return

    await websocket.accept()
    
    pubsub = redis.pubsub()
    await pubsub.subscribe(settings.omni_live_channel)
    
    # We need a task to read from redis and send to websocket
    async def redis_to_ws():
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    # Optionally filter by topic if the frontend requested it
                    await websocket.send_text(data)
        except Exception as e:
            logger.error(f"Error reading from Redis Pub/Sub: {e}")
            raise

    # We also need to read from websocket in case the client disconnects or sends filters
    async def ws_to_redis():
        try:
            while True:
                data = await websocket.receive_text()
                # For now, just keep connection alive. Could handle {"action": "subscribe", "topic": "..."} here
        except WebSocketDisconnect:
            logger.info("Client disconnected from Omni stream")
        except Exception as e:
            logger.error(f"Error reading from WS client: {e}")

    task1 = asyncio.create_task(redis_to_ws())
    task2 = asyncio.create_task(ws_to_redis())
    
    done, pending = await asyncio.wait(
        [task1, task2],
        return_when=asyncio.FIRST_COMPLETED,
    )
    
    for task in pending:
        task.cancel()
        
    await pubsub.unsubscribe(settings.omni_live_channel)
    await pubsub.close()
