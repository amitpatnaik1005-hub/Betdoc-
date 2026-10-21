"""The live portfolio over Redis pub/sub, five times a second (Group 96 retired the arbitrage scan).

Keys (``PORTFOLIO_CHANNEL_PREFIX``, default ``betdoc:live_portfolio``):
    <p>:<user_id>          pub/sub  the user's portfolio: every open book marked to the live books
    <p>:last:<user_id>     string   the latest payload, so a new socket starts from it
    <p>:watchers           zset     user_id -> last heartbeat; only watched portfolios are computed
    <p>:leader             string   the API worker publishing right now (a renewed lease)

One API worker at a time holds the lease and does the work, so N workers never publish N copies.
A tick reads Redis only: the books Aryabhata keeps, the FX rates, and each user's open positions
from their Redis snapshot (rebuilt from PostgreSQL only when an execution or settlement marks it
dirty, or every ``PORTFOLIO_POSITIONS_REFRESH_SECONDS``). The Decimal maths runs off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.portfolio_manager import MarketMeta, PortfolioManager, VenueSource, board_markets, portfolio_payload
from app.services.portfolio_positions import cached_open_bets

logger = logging.getLogger("betdoc.portfolio")

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""
_BACKOFF_MAX_SECONDS = 10.0


class PortfolioKeys:
    __slots__ = ("leader", "prefix", "watchers")

    def __init__(self, settings: Settings) -> None:
        self.prefix = settings.PORTFOLIO_CHANNEL_PREFIX
        self.watchers = f"{self.prefix}:watchers"
        self.leader = f"{self.prefix}:leader"

    def channel(self, user_id: uuid.UUID | str) -> str:
        return f"{self.prefix}:{user_id}"

    def last(self, user_id: uuid.UUID | str) -> str:
        return f"{self.prefix}:last:{user_id}"


async def watch(redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """A socket's heartbeat: keeps its user's portfolio being computed."""
    await redis.zadd(PortfolioKeys(settings).watchers, {str(user_id): time.time()})


def _dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)


class PortfolioPublisher:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        venues: VenueSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings
        self.keys = PortfolioKeys(settings)
        self.manager = PortfolioManager(redis, session_factory, settings, venues, clock)
        self.token = uuid.uuid4().hex
        self.lease_ms = max(2_000, int(settings.PORTFOLIO_TICK_SECONDS * 15_000))
        self.seq = 0
        self.metas: dict[str, MarketMeta] = {}
        self._board_read_at: float | None = None
        self._renew = redis.register_script(_RENEW)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                if await self.lead():
                    await self.tick()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError, TimeoutError) as exc:
                logger.warning("Portfolio publisher lost Redis (%s); retrying in %.0fs", type(exc).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                continue
            except Exception:  # noqa: BLE001 - one bad tick must not stop the stream
                logger.exception("Portfolio tick failed")
            await asyncio.sleep(max(0.0, self.settings.PORTFOLIO_TICK_SECONDS - (time.monotonic() - started)))

    async def lead(self) -> bool:
        if await self._renew(keys=[self.keys.leader], args=[self.token, self.lease_ms]):
            return True
        return bool(await self.redis.set(self.keys.leader, self.token, nx=True, px=self.lease_ms))

    async def tick(self) -> int:
        """One round: the board's market names when they are due, then every watched portfolio. Returns how many were sent."""
        if self._board_read_at is None or time.monotonic() - self._board_read_at >= self.settings.PORTFOLIO_BOARD_REFRESH_SECONDS:
            await self.refresh_board()
        now = time.time()
        await self.redis.zremrangebyscore(self.keys.watchers, "-inf", now - self.settings.PORTFOLIO_WATCH_TTL_SECONDS)
        users: list[str] = await self.redis.zrangebyscore(self.keys.watchers, now - self.settings.PORTFOLIO_WATCH_TTL_SECONDS, "+inf")
        if not users:
            return 0
        bets = {}
        for user in users:
            try:
                bets[user] = await cached_open_bets(self.redis, self.session_factory, self.settings, uuid.UUID(user), now=now)
            except ValueError:
                continue
        ctx = await self.manager.context()
        books = await self.manager.books([b.market_key for group in bets.values() for b in group])
        self.seq += 1
        seq, metas = self.seq, self.metas
        payloads = await asyncio.to_thread(lambda: {user: _dumps(portfolio_payload(user, group, books, ctx, metas, seq)) for user, group in bets.items()})
        pipe = self.redis.pipeline(transaction=False)
        for user, raw in payloads.items():
            pipe.publish(self.keys.channel(user), raw)
            pipe.set(self.keys.last(user), raw, ex=5)
        await pipe.execute()
        return len(payloads)

    async def refresh_board(self) -> dict[str, MarketMeta]:
        """Fixture names, kickoffs and quoted outcomes per market: the rows' labels and in-play flag."""
        self._board_read_at = time.monotonic()
        self.metas = await board_markets(self.redis)
        return self.metas


async def run_portfolio_publisher(
    redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings, venues: VenueSource | None = None
) -> None:
    await PortfolioPublisher(redis, session_factory, settings, venues).run()
