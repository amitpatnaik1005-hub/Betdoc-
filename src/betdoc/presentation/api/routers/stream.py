"""Live WebSocket feed of Twin output.



Connection lifecycle, and why each step exists:



#. **Accept, then register.** The broadcaster adopts an already-accepted

   socket, which keeps the handshake in the router where the framework owns it

   and keeps the fan-out logic free of protocol details.

#. **Reject over the cap explicitly.** A refused connection is closed with

   policy code 1013 rather than silently accepted, so the client knows to back

   off instead of reconnecting in a tight loop.

#. **Keep receiving.** The receive loop is not decorative. A WebSocket peer

   that is never read from will fill the server's receive buffer, and a

   ``close`` frame from the client will never be observed, so the connection

   leaks a task and a queue until process exit.

#. **Always unregister.** The ``finally`` block runs on disconnect, on

   cancellation during shutdown, and on any protocol error, which is the only

   way the client table cannot grow monotonically.



Sending happens entirely in the broadcaster's per-client sender task. This

handler never sends application frames, so a slow client cannot block it.

"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from betdoc.presentation.api.broadcaster import (
    WebsocketBroadcaster,
    WsMessage,
    WsMessageType,
)
from betdoc.presentation.api.dependencies import get_broadcaster_from_websocket

__all__ = ["router"]


_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="presentation.api.stream"
)


_POLICY_VIOLATION: Final[int] = 1008

_TRY_AGAIN_LATER: Final[int] = 1013

_GOING_AWAY: Final[int] = 1001


router = APIRouter(prefix="/api/v1/stream", tags=["stream"])


@router.websocket("/recommendations")
async def stream_recommendations(
    websocket: WebSocket,
    client_name: str = Query(default="", max_length=64),
) -> None:
    """Subscribe to live recommendations, rejections and session forecasts.



    Args:

        websocket: The upgrading connection.

        client_name: Optional label carried in server logs, so a specific

            dashboard tab can be identified when investigating a slow client.

    """

    await websocket.accept()

    try:
        broadcaster: WebsocketBroadcaster = get_broadcaster_from_websocket(websocket)

    except Exception:
        _log.error("stream.broadcaster_unavailable")

        with contextlib.suppress(Exception):
            await websocket.close(code=_TRY_AGAIN_LATER, reason="service is still starting")

        return

    labels = {"client_name": client_name} if client_name else {}

    client_id = await broadcaster.register(websocket, labels=labels)

    if client_id is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=_TRY_AGAIN_LATER, reason="connection limit reached")

        return

    log = _log.bind(client_id=client_id, client_name=client_name or None)

    log.info("stream.client_connected")

    try:
        await _receive_loop(websocket, broadcaster, client_id, log)

    except WebSocketDisconnect as exc:
        log.info("stream.client_disconnected", code=exc.code)

    except asyncio.CancelledError:
        # Server shutdown. Close politely so the client reconnects rather than

        # treating an abrupt reset as an error condition.

        with contextlib.suppress(Exception):
            if websocket.client_state is WebSocketState.CONNECTED:
                await websocket.close(code=_GOING_AWAY, reason="server shutdown")

        raise

    except Exception as exc:
        log.warning(
            "stream.client_error",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )

    finally:
        # Unconditional. This is the only guard against an unbounded client

        # table across reconnect storms.

        await broadcaster.unregister(client_id)


async def _receive_loop(
    websocket: WebSocket,
    broadcaster: WebsocketBroadcaster,
    client_id: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Consume inbound frames until the client disconnects.



    The control surface is intentionally tiny: ``ping`` and ``stats``. This

    socket is a read-only projection, and accepting any command that could

    change engine behaviour would put a mutation path behind the least

    authenticated surface in the system.

    """

    while True:
        raw = await websocket.receive_text()

        command = _parse_command(raw)

        if command is None:
            continue

        if command == "ping":
            await _reply(
                websocket,
                WsMessage.of(
                    WsMessageType.SYSTEM,
                    extra={"reply_to": "ping", "result": "pong"},
                ),
            )

        elif command == "stats":
            await _reply(
                websocket,
                WsMessage.of(
                    WsMessageType.SYSTEM,
                    extra={
                        "reply_to": "stats",
                        "client_id": client_id,
                        "connected_clients": broadcaster.client_count,
                        "clients": [
                            item.model_dump(mode="json")
                            for item in broadcaster.snapshot()
                            if item.client_id == client_id
                        ],
                    },
                ),
            )

        else:
            log.debug("stream.unknown_command", command=command[:32])

            await _reply(
                websocket,
                WsMessage.of(
                    WsMessageType.SYSTEM,
                    extra={
                        "reply_to": command[:32],
                        "error": "unsupported command",
                        "supported": ["ping", "stats"],
                    },
                ),
            )


def _parse_command(raw: str) -> str | None:
    """Accept either a bare verb or ``{"command": "..."}``. Never raises.



    Malformed input from a browser is routine, not exceptional. Returning

    ``None`` keeps the connection alive instead of tearing down a healthy

    subscription over a stray keystroke.

    """

    text = raw.strip()

    if not text:
        return None

    if not text.startswith("{"):
        return text.lower()[:64]

    try:
        parsed: Any = json.loads(text)

    except (ValueError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    command = parsed.get("command")

    return command.lower()[:64] if isinstance(command, str) else None


async def _reply(websocket: WebSocket, message: WsMessage) -> None:
    """Send a small control response directly, bypassing the broadcast queue.



    Safe to await here because control replies are tiny and are only produced

    in response to a frame this client just sent, so the peer is demonstrably

    reading. Application data still goes exclusively through the queue.

    """

    if websocket.client_state is not WebSocketState.CONNECTED:
        return

    with contextlib.suppress(Exception):
        await websocket.send_text(message.encode())
import os
import time
import random
import asyncio
import httpx
from fastapi import WebSocket, WebSocketDisconnect

@router.websocket("/odds")
async def stream_odds(websocket: WebSocket) -> None:
    await websocket.accept()
    key = os.environ.get("BETDOC_INGESTOR_API_KEY", "")
    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?apiKey={key}&regions=eu&markets=h2h"
    
    try:
        async with httpx.AsyncClient() as client:
            while True:
                if not key:
                    await asyncio.sleep(60.0)
                    continue
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        data = response.json()
                        for game in data:
                            home_team = game.get("home_team", "Unknown")
                            away_team = game.get("away_team", "Unknown")
                            bookmakers = game.get("bookmakers", [])
                            if bookmakers:
                                markets = bookmakers[0].get("markets", [])
                                if markets:
                                    outcomes = markets[0].get("outcomes", [])
                                    if outcomes:
                                        # Broadcast the first outcome
                                        tick = {
                                            "type": "TICK",
                                            "data": {
                                                "market_id": game.get("id", f"{home_team}-{away_team}"),
                                                "team_home": home_team,
                                                "team_away": away_team,
                                                "market_type": "h2h",
                                                "bookmaker_odds": outcomes[0].get("price", 2.0),
                                                "fair_probability": 1.0 / outcomes[0].get("price", 2.0),
                                                "edge_percentage": 0.05, # Placeholder edge
                                                "timestamp": time.time(),
                                                "suspended": False
                                            }
                                        }
                                        await websocket.send_json(tick)
                except Exception as e:
                    _log.warning("odds_api_poll_error", error=str(e))
                
                await asyncio.sleep(15.0) # Poll every 15 seconds
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _log.error(f"stream_odds fatal error: {e}")
