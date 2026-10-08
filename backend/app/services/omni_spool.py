"""In-process spool for the fleet's Redis writes while Redis is unreachable.

Ingestion must survive a Redis outage: a run that cannot reach Redis keeps its results here instead
of failing, and the next run (or fallback tick) that reaches Redis flushes them. Everything is
latest-wins per key and bounded, so a long outage costs memory proportional to the board, not to
time: stale prices are superseded, never replayed in bulk.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.adapters.base_adapter import StandardizedEvent
from app.core.config import Settings
from app.core.live_odds import live_odds_keys, publish_board_ticks
from app.core.omni_keys import OmniRedisKeys
from app.schemas.market import MarketTick
from app.services.omni_quorum_buffer import buffer_events

logger = logging.getLogger("betdoc.omni.spool")


class LocalSpool:
    def __init__(self, max_cells: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._max = max_cells
        self._board: OrderedDict[str, MarketTick] = OrderedDict()
        self._sources: OrderedDict[tuple[str, str], MarketTick] = OrderedDict()
        self._events: OrderedDict[tuple[str, str], tuple[str, StandardizedEvent]] = OrderedDict()
        self._metrics: dict[str, dict[str, str]] = {}

    @staticmethod
    def _put(store: OrderedDict, key: object, value: object, limit: int) -> None:  # type: ignore[type-arg]
        store.pop(key, None)
        store[key] = value
        while len(store) > limit:
            store.popitem(last=False)  # oldest first

    def ticks(self, source_ticks: list[MarketTick], board_ticks: list[MarketTick]) -> None:
        with self._lock:
            for tick in source_ticks:
                self._put(self._sources, (tick.board_key, tick.source or "unknown"), tick, self._max)
            for tick in board_ticks:
                self._put(self._board, tick.board_key, tick, self._max)

    def events(self, items: list[tuple[str, StandardizedEvent]]) -> None:
        with self._lock:
            for topic, event in items:
                self._put(self._events, (topic, str(event.provider_id)), (topic, event), self._max)

    def metrics(self, source_id: str, mapping: dict[str, object]) -> None:
        with self._lock:
            self._metrics.setdefault(source_id, {}).update({k: str(v) for k, v in mapping.items()})

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._board) + len(self._sources) + len(self._events) + len(self._metrics)

    def _drain(self) -> tuple[list[MarketTick], list[MarketTick], list[tuple[str, StandardizedEvent]], dict[str, dict[str, str]]]:
        with self._lock:
            drained = list(self._board.values()), list(self._sources.values()), list(self._events.values()), dict(self._metrics)
            self._board.clear()
            self._sources.clear()
            self._events.clear()
            self._metrics.clear()
            return drained

    async def flush(self, redis: Redis, settings: Settings) -> int:
        """Write everything spooled. On failure the items go back, newest state preserved."""
        if not self.pending:
            return 0
        board, sources, events, metrics = self._drain()
        try:
            keys = live_odds_keys()
            pipe = redis.pipeline(transaction=False)
            for tick in sources:
                pipe.hset(keys.sources(tick.board_key), tick.source or "unknown", tick.model_dump_json(by_alias=True))
                pipe.expire(keys.sources(tick.board_key), settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS)
            fleet = OmniRedisKeys(settings.omni_redis_prefix)
            for source_id, mapping in metrics.items():
                pipe.hset(fleet.fleet_metrics(source_id), mapping=mapping)
            await pipe.execute()
            if events:
                await buffer_events(redis, fleet, events, settings.omni_quorum_max_age_seconds)
            if board and not await publish_board_ticks(redis, board):
                raise RedisError("board publish failed")
        except (RedisError, OSError):
            self.ticks(sources, board)
            self.events(events)
            for source_id, mapping in metrics.items():
                self.metrics(source_id, dict(mapping))
            return 0
        flushed = len(board) + len(sources) + len(events) + len(metrics)
        logger.info("Spool flushed %d item(s) to Redis after an outage", flushed)
        return flushed


SPOOL = LocalSpool()
