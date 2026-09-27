import asyncio
import logging

from fastapi import WebSocket

from app.schemas.market import MarketTick

logger = logging.getLogger(__name__)

# A client that can't take a message within this window is evicted. Without it, one
# stalled client makes asyncio.gather wait for that client, and every other client's
# ticks are held up behind it.
SEND_TIMEOUT_SECONDS = 2.0
CLOSE_TIMEOUT_SECONDS = 1.0
SLOW_CONSUMER_CLOSE_CODE = 1013  # "Try Again Later"


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        self._cache: dict[tuple[str, str, str], MarketTick] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._send_locks[websocket] = asyncio.Lock()
        self.active_connections.add(websocket)
        # Send a snapshot of the current state immediately
        if self._cache:
            snapshot = list(self._cache.values())
            # Convert to camelCase payload (by_alias=True)
            payload_str = "[" + ",".join(t.model_dump_json(by_alias=True) for t in snapshot) + "]"
            try:
                await asyncio.wait_for(self._send(websocket, payload_str), timeout=SEND_TIMEOUT_SECONDS)
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
        # Update the cache
        key = (tick.match_id, tick.market_type, tick.selection)
        self._cache[key] = tick

        if not self.active_connections:
            return

        # Serialize once for every client. by_alias sends camelCase keys to the frontend.
        payload_str = tick.model_dump_json(by_alias=True)

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
