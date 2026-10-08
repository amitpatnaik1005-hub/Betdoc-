"""The Omni-Sniper: live order routing behind the CFO ledger's two-phase execution.

``SniperGateway.place`` is the bookmaker leg ``TradeExecutor`` calls while the bankroll row is
locked and the stake is reserved. It routes the order to the execution venue for its bookmaker,
translates canonical ids into the venue's ids (no mapping: REJECTED, so the reservation rolls
back), and fires through the venue's adapter (session, outbound rate limit, slippage floor, 401
refresh-and-refire). Every step is streamed to the user's execution terminal.

Terminal feed (``SNIPER_PREFIX``):
    <p>:feed               pub/sub  every step, every user ({user_id, ts, step, message, level, ...})
    <p>:feed:<user_id>     list     the last ``SNIPER_FEED_LENGTH`` lines for that user (newest first)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.factory import BaseExecutionAdapter, build_adapter
from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.core.security_vault import VaultCrypto
from app.models.execution import ExecutionVenue
from app.services.bookmaker_gateway import BookmakerOrder, BookmakerOutcome, BookmakerResult
from app.services.id_mapper import IdMapper, RemoteIds, UnmappedEntityError
from app.services.omni_throttle import TokenBucket
from app.services.session_manager import SessionManager

logger = logging.getLogger("betdoc.sniper")

VENUE_CACHE_SECONDS = 15.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SniperFeed:
    """The execution terminal's stream: pub/sub for live sockets, a short list per user for history."""

    def __init__(self, redis: Redis | None, settings: Settings, clock: Callable[[], datetime] = _utcnow) -> None:
        self.redis = redis
        self.settings = settings
        self.clock = clock
        self.channel = f"{settings.SNIPER_PREFIX}:feed"

    def history_key(self, user_id: uuid.UUID | str) -> str:
        return f"{self.channel}:{user_id}"

    async def emit(self, user_id: uuid.UUID | None, step: str, message: str, *, level: str = "info", ref: str | None = None, **extra: Any) -> None:
        line = {"ts": self.clock().isoformat(), "step": step, "message": message, "level": level, "ref": ref, **extra}
        if user_id is not None:
            line["user_id"] = str(user_id)
        logger.info("sniper %s %s %s", step, ref or "", message)
        if self.redis is None:
            return
        raw = json.dumps(line, separators=(",", ":"), default=str)
        with contextlib.suppress(RedisError, OSError):
            pipe = self.redis.pipeline(transaction=False)
            pipe.publish(self.channel, raw)
            if user_id is not None:
                pipe.lpush(self.history_key(user_id), raw)
                pipe.ltrim(self.history_key(user_id), 0, self.settings.SNIPER_FEED_LENGTH - 1)
                pipe.expire(self.history_key(user_id), 86_400)
            await pipe.execute()

    async def history(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        if self.redis is None:
            return []
        try:
            raw = await self.redis.lrange(self.history_key(user_id), 0, self.settings.SNIPER_FEED_LENGTH - 1)
        except (RedisError, OSError):
            return []
        lines: list[dict[str, Any]] = []
        for item in raw:
            with contextlib.suppress(ValueError, TypeError):
                lines.append(json.loads(item))
        return lines


@dataclass(frozen=True, slots=True)
class Route:
    """Where an order goes and under which ids: resolved before the bankroll lock is taken."""

    venue: VenueConfig
    remote: RemoteIds


@dataclass(slots=True)
class _VenueCache:
    at: float = 0.0
    venues: tuple[VenueConfig, ...] = ()


class SniperGateway:
    """The live ``BookmakerGateway``: route, translate, fire."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis | None,
        settings: Settings,
        vault: VaultCrypto | None,
        http: httpx.AsyncClient,
        sandbox_http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.http = http
        self.sandbox_http = sandbox_http
        self.sessions = SessionManager(redis, settings, vault, self.http_for, clock)
        self.limiter = TokenBucket(redis, f"{settings.SNIPER_PREFIX}:outbound", max_wait_seconds=settings.SNIPER_RATE_MAX_WAIT_SECONDS)
        self.mapper = IdMapper(session_factory, redis, settings)
        self.feed = SniperFeed(redis, settings, clock)
        self._cache = _VenueCache()
        self._cache_lock = asyncio.Lock()

    def http_for(self, venue: VenueConfig) -> httpx.AsyncClient:
        if venue.is_sandbox:
            if self.sandbox_http is None:
                raise RuntimeError("The sandbox venue only runs where the sandbox bookmaker is mounted")
            return self.sandbox_http
        return self.http

    # -------------------------------------------------------------- venues
    async def venues(self, *, fresh: bool = False) -> tuple[VenueConfig, ...]:
        async with self._cache_lock:
            if fresh or time.monotonic() - self._cache.at > VENUE_CACHE_SECONDS:
                async with self.session_factory() as session:
                    rows = (await session.execute(select(ExecutionVenue).order_by(ExecutionVenue.is_sandbox, ExecutionVenue.id))).scalars().all()
                usable = [
                    VenueConfig.from_row(row)
                    for row in rows
                    if row.is_enabled and (not row.is_sandbox or (self.settings.sniper_sandbox_active and self.sandbox_http is not None))
                ]
                self._cache = _VenueCache(time.monotonic(), tuple(usable))
            return self._cache.venues

    async def venue_for(self, bookmaker_id: str) -> VenueConfig | None:
        venues = await self.venues()
        # The bookmaker's own venue first, then a venue that lists it, then the sandbox catch-all
        for rule in (lambda v: v.id == bookmaker_id, lambda v: bookmaker_id in v.routes, lambda v: v.handles(bookmaker_id)):
            for venue in venues:
                if rule(venue):
                    return venue
        return None

    def adapter(self, venue: VenueConfig) -> BaseExecutionAdapter:
        return build_adapter(venue, http=self.http_for(venue), sessions=self.sessions, limiter=self.limiter, settings=self.settings)

    # -------------------------------------------------------------- the shot
    def _emitter(self, order: BookmakerOrder):  # type: ignore[no-untyped-def]
        async def emit(step: str, message: str, level: str = "info") -> None:
            await self.feed.emit(order.user_id, step, message, level=level, ref=order.client_ref, bookmaker=order.bookmaker_id)

        return emit

    async def prepare(self, order: BookmakerOrder) -> Route | BookmakerResult:
        """Route and translate before any money moves: no venue or no mapping aborts the order here,
        with nothing reserved. (The locked section then does no database I/O at all.)"""
        emit = self._emitter(order)
        venue = await self.venue_for(order.bookmaker_id)
        if venue is None:
            await emit("route", f"No execution venue for {order.bookmaker_id}: aborted", "error")
            return BookmakerResult(BookmakerOutcome.REJECTED, "NO_EXECUTION_VENUE")
        await emit("route", f"Routing to {venue.display_name}{' (sandbox)' if venue.is_sandbox else ''}")
        await emit("map", "Translating IDs…")
        try:
            remote = await self.mapper.resolve(venue, order.fixture_id, order.market, order.selection)
        except UnmappedEntityError as exc:
            await emit("map", f"No {venue.display_name} id for this {exc.kind}: aborted", "error")
            return BookmakerResult(BookmakerOutcome.REJECTED, f"UNMAPPED_{exc.kind.upper()}", venue_id=venue.id)
        await emit("map", f"event {remote.event_id} · selection {remote.selection_id}")
        return Route(venue, remote)

    async def place(self, order: BookmakerOrder, route: Route | None = None) -> BookmakerResult:
        emit = self._emitter(order)
        if route is None:  # called without prepare(): route now (a failure still rolls the reservation back)
            prepared = await self.prepare(order)
            if isinstance(prepared, BookmakerResult):
                return prepared
            route = prepared
        venue = route.venue
        result = await self.adapter(venue).place(order, route.remote, lambda step, message: emit(step, message))
        if result.outcome is BookmakerOutcome.ACCEPTED:
            level = "warning" if result.reason == "SLIPPAGE_VIOLATION" else "success"
            await emit("result", f"{result.http_status} OK - remote_id: {result.reference}" + (f" @ {result.matched_odds}" if result.matched_odds else ""), level)
        elif result.outcome is BookmakerOutcome.REJECTED:
            status = f"{result.http_status} " if result.http_status else ""
            await emit("result", f"{status}{result.reason}: rejected, rolling back", "error")
        else:
            await emit("result", f"{result.reason}: no confirmation, stake held in exposure for reconciliation", "warning")
        return result
