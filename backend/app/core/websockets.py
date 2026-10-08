import asyncio
import logging
from collections.abc import Sequence

from fastapi import WebSocket

from app.core.live_odds import encode_ticks
from app.schemas.market import MarketTick

logger = logging.getLogger(__name__)

# A client that can't take a message within this window is evicted. Without it, one
# stalled client makes asyncio.gather wait for that client, and every other client's
# ticks are held up behind it.
SEND_TIMEOUT_SECONDS = 2.0
CLOSE_TIMEOUT_SECONDS = 1.0
SLOW_CONSUMER_CLOSE_CODE = 1013  # "Try Again Later"


class ConnectionManager:
    """The sockets held by THIS worker. Ticks reach it through the Redis relay (app.core.live_odds),
    so every worker delivers every tick; it never needs to know about the others."""

    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        # Last tick per board cell seen by this worker: the snapshot when Redis can't provide one
        self._cache: dict[str, MarketTick] = {}

    async def connect(self, websocket: WebSocket, snapshot: Sequence[MarketTick] | None = None) -> None:
        """Accept, register, then send the current board (Redis snapshot, else this worker's cache)."""
        await websocket.accept()
        self._send_locks[websocket] = asyncio.Lock()
        self.active_connections.add(websocket)
        board = list(snapshot) if snapshot is not None else list(self._cache.values())
        if board:
            try:
                await asyncio.wait_for(self._send(websocket, encode_ticks(board)), timeout=SEND_TIMEOUT_SECONDS)
            except Exception:
                pass

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)
        self._send_locks.pop(websocket, None)

    async def _send(self, websocket: WebSocket, message: str) -> None:
        lock = self._send_locks.get(websocket)
        if lock is None:
            raise ConnectionError("WebSocket is no longer registered")
        async with lock:
            await websocket.send_text(message)

    async def send_personal(self, websocket: WebSocket, message: str) -> None:
        await asyncio.wait_for(self._send(websocket, message), timeout=SEND_TIMEOUT_SECONDS)

    async def _evict(self, websocket: WebSocket, code: int = SLOW_CONSUMER_CLOSE_CODE) -> None:
        self.disconnect(websocket)
        try:
            await asyncio.wait_for(websocket.close(code=code), timeout=CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def broadcast_market_tick(self, tick: MarketTick) -> None:
        await self.broadcast_market_ticks([tick])

    async def broadcast_market_ticks(self, ticks: Sequence[MarketTick]) -> None:
        """One frame (a JSON array) per batch to every local client; unresponsive clients are evicted."""
        if not ticks:
            return
        for tick in ticks:
            self._cache[tick.board_key] = tick

        if not self.active_connections:
            return

        # Serialize once for every client. by_alias sends camelCase keys to the frontend.
        payload_str = encode_ticks(ticks)

        # Snapshot: connect/disconnect can change the set while we await
        connections = list(self.active_connections)

        tasks = [
            asyncio.wait_for(self._send(conn, payload_str), timeout=SEND_TIMEOUT_SECONDS)
            for conn in connections
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        failed = [
            connections[index]
            for index, result in enumerate(results)
            if isinstance(result, Exception)
        ]
        if failed:
            logger.info("Evicting %d unresponsive WebSocket client(s)", len(failed))
            await asyncio.gather(*(self._evict(conn) for conn in failed), return_exceptions=True)


manager = ConnectionManager()
