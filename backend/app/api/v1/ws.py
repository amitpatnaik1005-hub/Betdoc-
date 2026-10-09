import asyncio
import logging
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Query, Request, Response, WebSocket, WebSocketDisconnect, status

from app.api.deps import CurrentUser, WsUser
from app.core.config import settings
from app.core.events import EVENTS_CHANNEL, relay_channel
from app.core.database import AsyncSessionLocal
from app.core.live_odds import encode_ticks, read_snapshot
from app.core.websockets import manager
from app.models import User
from app.schemas.aryabhata import TradeSignal  # noqa: F401  (payload contract for /signals)
from app.schemas.market import MarketTick  # noqa: F401  (payload contract for this channel)
from app.services.aryabhata_pipeline import run_signal_socket
from app.services.portfolio_stream import PortfolioKeys, watch
from app.services.sentinel_bus import SentinelKeys

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websockets"])

# The client must send "ping" at least this often, or the connection is treated as dead
IDLE_TIMEOUT_SECONDS = 90.0


async def _authenticate(token: str | None) -> float | None:
    """Return the token's expiry timestamp, or None if the client must be rejected."""
    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[settings.ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except ValueError:
        return None

    # A valid signature isn't enough: deleted or deactivated users must not stream odds
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None

    return float(payload["exp"])


async def _safe_close(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass  # Already closed by the client or evicted by the manager


@router.websocket("/live-odds")
async def live_odds(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    # FIX: Check CORS explicitly since WebSockets bypass standard browser CORS checks
    origin = websocket.headers.get("origin")
    if origin and origin not in settings.BACKEND_CORS_ORIGINS:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
        return

    token_exp = await _authenticate(token)
    if token_exp is None:
        # Closing before accept() rejects the handshake (the client sees HTTP 403)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Ticks arrive through the Redis relay (app.core.live_odds), so this socket sees every tick
    # whichever worker produced it; the snapshot is the whole board, not just this worker's view.
    await manager.connect(websocket, snapshot=await read_snapshot(getattr(websocket.app.state, "redis", None)))

    try:
        while True:
            # A connection must not outlive its JWT
            seconds_to_expiry = token_exp - datetime.now(timezone.utc).timestamp()
            if seconds_to_expiry <= 0:
                await _safe_close(websocket, status.WS_1008_POLICY_VIOLATION, "Token expired")
                break

            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=min(IDLE_TIMEOUT_SECONDS, seconds_to_expiry),
                )
            except TimeoutError:
                if token_exp <= datetime.now(timezone.utc).timestamp():
                    await _safe_close(websocket, status.WS_1008_POLICY_VIOLATION, "Token expired")
                else:
                    await _safe_close(websocket, status.WS_1000_NORMAL_CLOSURE, "Idle timeout")
                break

            if data == "ping":
                # Goes through the manager so it can't interleave with a broadcast
                await manager.send_personal(websocket, "pong")

    except WebSocketDisconnect:
        pass
    except Exception:
        # Binary frames, a failed pong, or a socket the manager already evicted
        logger.debug("WebSocket connection ended unexpectedly", exc_info=True)
        await _safe_close(websocket, status.WS_1011_INTERNAL_ERROR)
    finally:
        # Runs on every exit path, so dead sockets never stay in the broadcast set
        manager.disconnect(websocket)


@router.websocket("/events")
async def events_stream(websocket: WebSocket, user: WsUser) -> None:  # noqa: ARG001 - auth gate
    """Cross-section event bus: every section's writes, commander heartbeats, system halts."""
    await relay_channel(websocket, getattr(websocket.app.state, "redis", None), EVENTS_CHANNEL)


@router.websocket("/signals")
async def signals_stream(websocket: WebSocket, user: WsUser) -> None:
    """Aryabhata's live +EV lines as ``TradeSignal``s, each staked for this user's live bankroll.

    Frames: ``snapshot`` on connect and whenever the risk limits change (every live signal re-sized),
    then ``signals`` with new or updated lines plus the ``withdrawn`` keys of edges that closed.
    """
    await run_signal_socket(websocket, user.id, getattr(websocket.app.state, "redis", None), AsyncSessionLocal, settings)


@router.websocket("/portfolio")
async def portfolio_stream(websocket: WebSocket, user: WsUser) -> None:
    """The Active Portfolio, live: this user's books marked to the live books 5x a second, and the
    arbitrage scan whenever it changes. The socket heartbeats its user into the publisher's watch set."""
    redis = getattr(websocket.app.state, "redis", None)
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    keys = PortfolioKeys(settings)
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(keys.channel(user.id), keys.arbitrage)
        await watch(redis, settings, user.id)
        first = [await redis.get(keys.last(user.id)), await redis.get(keys.arbitrage_last)]
    except Exception:  # noqa: BLE001 - Redis down: refuse the socket, the client falls back to polling
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    for raw in first:
        if raw:
            await websocket.send_text(raw)

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") == "message":
                async with send_lock:
                    await websocket.send_text(message["data"])

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(1.0, settings.PORTFOLIO_WATCH_TTL_SECONDS / 3))
            try:
                await watch(redis, settings, user.id)
            except Exception:  # noqa: BLE001 - a missed beat only pauses the stream until the next one
                logger.debug("Portfolio watch heartbeat failed")

    async def drain() -> None:
        while True:
            if await websocket.receive_text() == "ping":
                async with send_lock:
                    await websocket.send_text('{"type":"pong"}')

    tasks = [asyncio.create_task(pump()), asyncio.create_task(heartbeat()), asyncio.create_task(drain())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/sniper")
async def sniper_stream(websocket: WebSocket, user: WsUser) -> None:
    """The execution terminal, live: every step of this user's orders (an admin sees every user's)."""
    redis = getattr(websocket.app.state, "redis", None)
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    channel = f"{settings.SNIPER_PREFIX}:feed"
    try:
        await pubsub.subscribe(channel)
    except Exception:  # noqa: BLE001 - Redis down: refuse the socket, the client falls back to history polling
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable")
        return
    await websocket.accept()
    mine, admin = str(user.id), user.role == "ADMIN"
    send_lock = asyncio.Lock()

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message["data"]
            if not admin and f'"user_id":"{mine}"' not in data:
                continue  # another user's order
            async with send_lock:
                await websocket.send_text(data)

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
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


@router.get("/live-odds/snapshot")
async def live_odds_snapshot(request: Request, user: CurrentUser) -> Response:  # noqa: ARG001 - auth gate
    """The whole live board over plain HTTP, in the same camelCase shape as /ws/live-odds frames.

    The browser falls back to polling this while its socket is down, so a dropped WebSocket costs
    seconds of freshness, never an empty board. Redis down: this worker's own view of the board.
    """
    snapshot = await read_snapshot(getattr(request.app.state, "redis", None))
    ticks = snapshot if snapshot is not None else manager.snapshot()
    return Response(content=encode_ticks(ticks), media_type="application/json", headers={"Cache-Control": "no-store"})


@router.websocket("/sentinel")
async def sentinel_stream(websocket: WebSocket, user: WsUser) -> None:
    """The Sentinel's live feed (Group 68), administrators only: every alert the moment it is emitted
    (undebounced, for the tab's sirens), digests as they go out, health and debouncer updates."""
    if user.role != "ADMIN":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Administrators only")
        return
    await relay_channel(websocket, getattr(websocket.app.state, "redis", None), SentinelKeys(settings).live)
