"""ARYABHATA over Redis: market frames stream in, edges fan out, each socket gets its own stakes.

Flow::

    fleet run / POST /ingest --XADD--> <prefix>:frames          (stream, approx MAXLEN)
    every API worker  --XREADGROUP (group "aryabhata")-->       (each frame handled by one worker)
        merge the source's books into <prefix>:books:<market>   (Lua: atomic replace + version)
        evaluate_market() off the event loop                    (Decimal maths, aryabhata_engine)
        commit edges to <prefix>:active + PUBLISH <prefix>:signals   (Lua: version-checked, atomic)
    /ws/signals (per socket) --SUBSCRIBE--> size each edge for that user's live bankroll -> browser

Ingestion never waits on the engine: publishing a frame is one pipelined XADD, and a slow or
absent consumer only lets the bounded stream fill. Frames are seconds-lived facts, so nothing is
replayed after a restart: the group starts at new entries and abandoned deliveries are dropped.

Keys (``ARYABHATA_PREFIX``):
    <p>:frames          stream   one MarketQuote per entry (field "q")
    <p>:books:<market>  hash     "<source>|<bookmaker>" -> book JSON; "~c" = last committed version
    <p>:active          hash     "<fixture>|<selection>" -> live EdgeSignal JSON
    <p>:active:exp      zset     the same fields scored by expiry (pruned on every commit)
    <p>:risk            hash     mirror of the Control Panel risk limits
    <p>:signals         pub/sub  {"type": "market", ...} edges + withdrawals, {"type": "risk"}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.domain.dashboard.summary_builder import SETTLED_STATUSES
from app.models import BetLedger, ExchangeAccount
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.schemas.aryabhata import (
    SIGNAL_TTL_SECONDS,
    BookQuote,
    EdgeSignal,
    MarketQuote,
    RiskConfigRead,
    TradeSignal,
)
from app.schemas.market import MarketTick
from app.services.aryabhata_engine import (
    BookLine,
    EmaState,
    MarketEvaluation,
    MarketState,
    RiskLimits,
    evaluate_market,
    ordered_labels,
    recommend_stake,
    to_decimal,
)
from app.services.venue_costs import TermsTable
from app.workers.nalanda_firehose import emit_quotes

logger = logging.getLogger("betdoc.aryabhata")

_REDIS_TIMEOUT_SECONDS = 2.0
_READ_BLOCK_MS = 2_000
_READ_COUNT = 200
_HYGIENE_SECONDS = 60.0
_ABANDONED_IDLE_MS = 60_000
_IDLE_CONSUMER_MS = 3_600_000
_BACKOFF_MAX_SECONDS = 30.0
_BANKROLL_REFRESH_SECONDS = 15.0
_RISK_REFRESH_SECONDS = 30.0
_EMA_FIELD = "~e:"  # books-hash fields holding each selection's steam EMA state

# Replace everything one source said about a market with its newest frame, atomically, and stamp
# the merge with Redis' own clock so commits from racing consumers can be ordered.
# KEYS[1] books hash. ARGV[1] "<source>|" prefix, ARGV[2] ttl, ARGV[3..] field/value pairs.
_MERGE_BOOKS = """
local prefix = ARGV[1]
local fresh = {}
for i = 3, #ARGV, 2 do fresh[ARGV[i]] = true end
for _, f in ipairs(redis.call('HKEYS', KEYS[1])) do
  if string.sub(f, 1, #prefix) == prefix and not fresh[f] then redis.call('HDEL', KEYS[1], f) end
end
for i = 3, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
local t = redis.call('TIME')
return {t[1] .. string.format('%06d', tonumber(t[2])), redis.call('HGETALL', KEYS[1])}
"""

# Commit one market's evaluation unless a newer merge already committed, prune expired edges, and
# publish the change in the same atomic step (so subscribers see commits in version order).
# KEYS: books hash, active hash, expiry zset.
# ARGV: version, now, channel, market key, n, n fields, n edge JSONs ("" = none), n expiries,
#       m, then m field/value pairs of EMA state written into the books hash.
_COMMIT_EDGES = """
local version = tonumber(ARGV[1])
if version < tonumber(redis.call('HGET', KEYS[1], '~c') or '0') then return 0 end
redis.call('HSET', KEYS[1], '~c', ARGV[1])
for _, f in ipairs(redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[2])) do
  redis.call('HDEL', KEYS[2], f)
  redis.call('ZREM', KEYS[3], f)
end
local n = tonumber(ARGV[5])
local m_at = 6 + 3 * n
for j = 1, tonumber(ARGV[m_at] or '0') do
  redis.call('HSET', KEYS[1], ARGV[m_at + 2 * j - 1], ARGV[m_at + 2 * j])
end
local signals, withdrawn = {}, {}
for i = 1, n do
  local f, edge = ARGV[5 + i], ARGV[5 + n + i]
  if edge ~= '' then
    redis.call('HSET', KEYS[2], f, edge)
    redis.call('ZADD', KEYS[3], ARGV[5 + 2 * n + i], f)
    table.insert(signals, edge)
  elseif redis.call('HDEL', KEYS[2], f) == 1 then
    redis.call('ZREM', KEYS[3], f)
    table.insert(withdrawn, cjson.encode(f))
  end
end
if #signals > 0 or #withdrawn > 0 then
  redis.call('PUBLISH', ARGV[3], '{"type":"market","market":' .. cjson.encode(ARGV[4]) .. ',"version":' .. ARGV[1]
    .. ',"signals":[' .. table.concat(signals, ',') .. '],"withdrawn":[' .. table.concat(withdrawn, ',') .. ']}')
end
return 1
"""


_MARKET_INDEX_TTL_SECONDS = 3 * 86_400  # a fixture's market index outlives its books


class AryabhataKeys:
    __slots__ = ("active", "active_exp", "channel", "frames", "group", "prefix", "risk")

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.frames = f"{prefix}:frames"
        self.group = "aryabhata"
        self.active = f"{prefix}:active"
        self.active_exp = f"{prefix}:active:exp"
        self.risk = f"{prefix}:risk"
        self.channel = f"{prefix}:signals"

    def books(self, market_key: str) -> str:
        return f"{self.prefix}:books:{market_key}"

    def markets(self, fixture_id: str) -> str:
        """Set: every market type a fixture has been quoted in (Ashoka's index; no keyspace scans)."""
        return f"{self.prefix}:markets:{fixture_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _token(value: str) -> str:
    """Hash-field segment: '|' separates source from bookmaker, so it never appears inside one."""
    return value.replace("|", "_")[:64] or "unknown"


# ---------------------------------------------------------------- producers
async def publish_market_quotes(redis: Redis | None, quotes: Sequence[MarketQuote], settings: Settings) -> bool:
    """XADD frames for the engine, and the same frames to Nalanda's firehose (Group 67: the tick lake).
    Never raises: a missed frame costs one signal, not an ingestion run."""
    if redis is None or not quotes:
        return redis is not None
    try:
        return await _publish_frames(redis, quotes, settings)
    finally:
        await emit_quotes(redis, settings, quotes)  # its own round trip, after the engine's; never raises


async def _publish_frames(redis: Redis, quotes: Sequence[MarketQuote], settings: Settings) -> bool:
    if not settings.ARYABHATA_ENABLED:
        return True
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            pipe = redis.pipeline(transaction=False)
            for quote in quotes:
                pipe.xadd(keys.frames, {"q": quote.model_dump_json()}, maxlen=settings.ARYABHATA_STREAM_MAXLEN, approximate=True)
                pipe.sadd(keys.markets(quote.match_id), quote.market_type)
                pipe.expire(keys.markets(quote.match_id), _MARKET_INDEX_TTL_SECONDS)
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        logger.warning("Aryabhata: %d market frame(s) not published; Redis unavailable", len(quotes))
        return False
    return True


def quotes_from_ticks(ticks: Sequence[MarketTick], fetched_at: datetime | None = None) -> list[MarketQuote]:
    """Frames for pushed ticks (``POST /ingest``): each source's prices for a market form one book."""
    fetched_at = fetched_at or _utcnow()
    grouped: dict[tuple[str, str, str], list[MarketTick]] = {}
    for tick in ticks:
        source = tick.source or "ingest"
        grouped.setdefault((tick.match_id, tick.market_type, source), []).append(tick)
    quotes: list[MarketQuote] = []
    for (_, _, source), group in grouped.items():
        prices = {t.selection: t.odds for t in group}
        if len(prices) < 2:
            continue  # one selection is not a market: nothing to de-vig
        first = group[-1]
        quotes.append(
            MarketQuote(
                match_id=first.match_id,
                market_type=first.market_type,
                home_team=first.home_team,
                away_team=first.away_team,
                sport_key=first.sport_key,
                commence_time=first.commence_time,
                source=source,
                fetched_at=fetched_at,
                books=(
                    BookQuote(
                        bookmaker_id=_token(source),
                        prices=prices,
                        observed_at=first.observed_at,
                        is_suspended=any(t.is_suspended for t in group),
                    ),
                ),
            )
        )
    return quotes


# ---------------------------------------------------------------- risk limits
def _column_default(name: str) -> Any:
    default = SystemSettingsModel.__table__.c[name].default
    return getattr(default, "arg", None)


def limits_from_row(row: SystemSettingsModel | None) -> RiskLimits:
    """Mirror execution's trading gate: no row means defaults and no absolute max bet yet."""
    if row is None:
        return RiskLimits(
            kelly_multiplier=to_decimal(_column_default("default_kelly_fraction")) or Decimal("0.25"),
            max_stake_pct=to_decimal(_column_default("max_stake_pct")) or Decimal("5"),
            max_bet_size=None,
            halted=False,
        )
    return RiskLimits(
        kelly_multiplier=to_decimal(row.default_kelly_fraction) or Decimal(0),
        max_stake_pct=to_decimal(row.max_stake_pct) or Decimal(1),
        max_bet_size=to_decimal(row.max_bet_size),
        halted=row.max_daily_exposure <= 0,
    )


def _limits_mapping(limits: RiskLimits) -> dict[str, str]:
    return {
        "kelly_multiplier": str(limits.kelly_multiplier),
        "max_stake_pct": str(limits.max_stake_pct),
        "max_bet_size": "" if limits.max_bet_size is None else str(limits.max_bet_size),
        "halted": "1" if limits.halted else "0",
    }


def _limits_from_mapping(mapping: dict[str, str]) -> RiskLimits | None:
    multiplier, pct = to_decimal(mapping.get("kelly_multiplier")), to_decimal(mapping.get("max_stake_pct"))
    if multiplier is None or pct is None or mapping.get("halted") not in ("0", "1"):
        return None
    return RiskLimits(multiplier, pct, to_decimal(mapping.get("max_bet_size") or None), mapping["halted"] == "1")


# When neither Redis nor the database can say what the limits are, nothing is staked.
FAIL_CLOSED = RiskLimits(Decimal(0), Decimal(1), Decimal(0), halted=True)


async def load_risk_limits(redis: Redis | None, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> RiskLimits:
    """Redis mirror first; on a miss the Control Panel row (written back to Redis); else fail closed."""
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    if redis is not None:
        try:
            async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
                cached = _limits_from_mapping(await redis.hgetall(keys.risk))
            if cached is not None:
                return cached
        except (RedisError, OSError, TimeoutError):
            pass
    try:
        async with session_factory() as session:
            limits = limits_from_row(await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID))
    except Exception:  # noqa: BLE001 - any database failure: stake nothing rather than guess
        logger.warning("Aryabhata: risk limits unreadable from Redis and the database; staking nothing")
        return FAIL_CLOSED
    await _store_limits(redis, keys, limits, settings)
    return limits


async def _store_limits(redis: Redis | None, keys: AryabhataKeys, limits: RiskLimits, settings: Settings) -> None:
    if redis is None:
        return
    with contextlib.suppress(RedisError, OSError, TimeoutError):
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            pipe = redis.pipeline(transaction=True)
            pipe.delete(keys.risk)
            pipe.hset(keys.risk, mapping=_limits_mapping(limits))
            pipe.expire(keys.risk, settings.ARYABHATA_RISK_CACHE_SECONDS)
            await pipe.execute()


async def publish_risk_limits(redis: Redis | None, row: SystemSettingsModel, settings: Settings) -> None:
    """Write-through after a Control Panel change; every open Arena re-sizes its stakes at once."""
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    await _store_limits(redis, keys, limits_from_row(row), settings)
    if redis is not None:
        with contextlib.suppress(RedisError, OSError, TimeoutError):
            async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
                await redis.publish(keys.channel, '{"type":"risk"}')


def risk_read(limits: RiskLimits) -> RiskConfigRead:
    return RiskConfigRead(
        kelly_multiplier=limits.kelly_multiplier,
        max_stake_pct=limits.max_stake_pct,
        max_bet_size=limits.max_bet_size,
        halted=limits.halted,
    )


# ---------------------------------------------------------------- bankroll and personalisation
async def live_bankroll(session: AsyncSession, user_id: UUID, starting_bankroll: object) -> Decimal:
    """Starting capital plus everything realised since: the dashboard's total bankroll, in Decimal."""
    pnl = func.coalesce(BetLedger.payout, 0) - BetLedger.stake
    realised = await session.scalar(
        select(func.coalesce(func.sum(case((BetLedger.status.in_(SETTLED_STATUSES), pnl), else_=0)), 0))
        .select_from(BetLedger)
        .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
        .where(ExchangeAccount.user_id == user_id)
    )
    return (to_decimal(starting_bankroll) or Decimal(0)) + (to_decimal(realised) or Decimal(0))


def personalize(edge: EdgeSignal, bankroll: Decimal | None, limits: RiskLimits) -> TradeSignal:
    """The edge as one user's TradeSignal: stake sized now, with the limits in force now."""
    decision = recommend_stake(edge.full_kelly, bankroll, limits)
    return TradeSignal(
        signal_id=edge.signal_id,
        fixture_id=edge.fixture_id,
        market_id=edge.market_id,
        selection=edge.selection,
        odds=edge.odds,
        true_prob=edge.true_prob,
        ev_percent=edge.ev_percent,
        kelly_stake_inr=decision.stake_inr,
        bookmaker_id=edge.bookmaker_id,
        timestamp=edge.timestamp,
        expires_at=edge.expires_at,
        market_type=edge.market_type,
        home_team=edge.home_team,
        away_team=edge.away_team,
        sport_key=edge.sport_key,
        commence_time=edge.commence_time,
        source=edge.source,
        devig_method=edge.devig_method,
        overround=edge.overround,
        books=edge.books,
        stake_fraction=decision.fraction,
        stake_binding=decision.binding,
        is_steam_move=edge.is_steam_move,
    )


async def read_active_edges(redis: Redis, settings: Settings, now: datetime | None = None) -> list[EdgeSignal]:
    """Every live edge (newest first), for a socket's opening snapshot."""
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    now = now or _utcnow()
    async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
        raw: dict[str, str] = await redis.hgetall(keys.active)
    edges: list[EdgeSignal] = []
    for value in raw.values():
        try:
            edge = EdgeSignal.model_validate_json(value)
        except ValidationError:
            continue
        if edge.expires_at > now:
            edges.append(edge)
    return sorted(edges, key=lambda e: e.timestamp, reverse=True)


# ---------------------------------------------------------------- the consumer
@dataclass(slots=True)
class ConsumerStats:
    frames: int = 0
    markets: int = 0
    edges: int = 0
    malformed: int = 0
    stale_commits: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


def _book_field(source: str, book: BookQuote) -> str:
    return f"{_token(source)}|{_token(book.bookmaker_id)}"


def _book_value(source: str, book: BookQuote, seen_at: datetime) -> str:
    return json.dumps(
        {
            "s": source,
            "b": book.bookmaker_id,
            "p": {label: str(price) for label, price in book.prices.items()},
            "t": seen_at.timestamp(),
            "x": book.is_suspended,
        },
        separators=(",", ":"),
    )


def _book_line(raw: str) -> BookLine | None:
    try:
        data = json.loads(raw)
        prices = {str(label): to_decimal(price) for label, price in data["p"].items()}
        if any(price is None for price in prices.values()):
            return None
        return BookLine(
            source=str(data["s"]),
            bookmaker_id=str(data["b"]),
            prices=prices,  # type: ignore[arg-type] - None ruled out above
            seen_at=datetime.fromtimestamp(float(data["t"]), UTC),
            is_suspended=bool(data.get("x", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _pairs(flat: Sequence[str]) -> dict[str, str]:
    return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}


async def read_market_books(redis: Redis, settings: Settings, market_keys: Sequence[str]) -> dict[str, tuple[BookLine, ...]]:
    """Every book the stream holds for each market (``"<fixture>|<market type>"``), one pipelined
    read. Stale and suspended books are included: callers filter by age for their own purpose."""
    if not market_keys:
        return {}
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
        pipe = redis.pipeline(transaction=False)
        for market_key in market_keys:
            pipe.hgetall(keys.books(market_key))
        results: list[dict[str, str]] = await pipe.execute()
    return {
        market_key: tuple(line for name, raw in (fields or {}).items() if not name.startswith("~") and (line := _book_line(raw)) is not None)
        for market_key, fields in zip(market_keys, results, strict=True)
    }


class AryabhataConsumer:
    """One member of the ``aryabhata`` consumer group. ``run`` loops until cancelled."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        name: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
        venues: Callable[[], Awaitable[Sequence[VenueConfig]]] | None = None,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.venues = venues  # the execution venues: a venue row's own commission overrides the settings table
        self.keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        self.stats = ConsumerStats()
        self._merge = redis.register_script(_MERGE_BOOKS)
        self._commit = redis.register_script(_COMMIT_EDGES)
        self._last_hygiene = 0.0

    # -------------------------------------------------------------- loop
    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.ensure_group()
                logger.info("Aryabhata consumer %s joined %s", self.name, self.keys.frames)
                backoff = 1.0
                while True:
                    await self.step()
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    logger.warning("Aryabhata stream error: %s", exc)
            except (RedisError, OSError) as exc:
                logger.warning("Aryabhata lost Redis (%s); retrying in %.0fs", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def ensure_group(self) -> None:
        try:
            # "$": frames are seconds-lived, so a fresh group starts at new ones, never a backlog
            await self.redis.xgroup_create(self.keys.frames, self.keys.group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def step(self) -> int:
        """One read-evaluate-ack cycle. Returns the number of frames handled."""
        if time.monotonic() - self._last_hygiene >= _HYGIENE_SECONDS:
            await self.hygiene()
        response = await self.redis.xreadgroup(
            self.keys.group, self.name, {self.keys.frames: ">"}, count=_READ_COUNT, block=_READ_BLOCK_MS
        )
        entries = [entry for _, batch in response or [] for entry in batch]
        if not entries:
            return 0
        quotes: list[MarketQuote] = []
        for _, fields in entries:
            try:
                quotes.append(MarketQuote.model_validate_json(fields.get("q", "")))
            except ValidationError:
                self.stats.malformed += 1
        try:
            await self.handle_quotes(quotes)
        except (RedisError, OSError):
            raise
        except Exception:  # noqa: BLE001 - one poisoned batch must not stop the engine
            logger.exception("Aryabhata: batch of %d frame(s) failed", len(quotes))
        finally:
            # At-most-once: a signal is worth seconds, so a failed frame is dropped, never retried late
            await self.redis.xack(self.keys.frames, self.keys.group, *[entry_id for entry_id, _ in entries])
        return len(entries)

    async def hygiene(self) -> None:
        """Drop deliveries abandoned by dead consumers (stale by now) and forget long-idle consumers."""
        self._last_hygiene = time.monotonic()
        with contextlib.suppress(ResponseError):
            claimed = await self.redis.xautoclaim(
                self.keys.frames, self.keys.group, self.name, _ABANDONED_IDLE_MS, start_id="0-0", count=1000, justid=True
            )
            ids = claimed[1] if isinstance(claimed, list | tuple) and len(claimed) > 1 else []
            if ids:
                await self.redis.xack(self.keys.frames, self.keys.group, *ids)
            for consumer in await self.redis.xinfo_consumers(self.keys.frames, self.keys.group):
                if consumer.get("name") != self.name and not consumer.get("pending") and consumer.get("idle", 0) > _IDLE_CONSUMER_MS:
                    await self.redis.xgroup_delconsumer(self.keys.frames, self.keys.group, consumer["name"])

    # -------------------------------------------------------------- the work
    async def handle_quotes(self, quotes: Sequence[MarketQuote]) -> list[MarketEvaluation]:
        """Merge each frame's books, evaluate every touched market, commit and publish the edges."""
        latest: dict[tuple[str, str], MarketQuote] = {}
        for quote in quotes:  # stream order: within one batch the newest frame per source wins
            latest[(quote.market_key, quote.source)] = quote
        if not latest:
            return []
        self.stats.frames += len(quotes)

        merged: list[tuple[MarketQuote, str, MarketState, dict[str, EmaState]]] = []
        for quote in latest.values():
            # A price is as old as its fetch, not its arrival here (a backlog must not freshen it)
            fetched = quote.fetched_at if quote.fetched_at.tzinfo else quote.fetched_at.replace(tzinfo=UTC)
            seen_at = min(self.clock(), fetched)
            args: list[str] = [f"{_token(quote.source)}|", str(int(self.settings.ARYABHATA_BOOK_MAX_AGE_SECONDS * 3))]
            for book in quote.books:
                args += [_book_field(quote.source, book), _book_value(quote.source, book, seen_at)]
            version, flat = await self._merge(keys=[self.keys.books(quote.market_key)], args=args)
            fields = _pairs(flat)
            books = tuple(line for name, raw in fields.items() if not name.startswith("~") and (line := _book_line(raw)) is not None)
            ema = {
                name[len(_EMA_FIELD):]: ema_state
                for name, raw in fields.items()
                if name.startswith(_EMA_FIELD) and (ema_state := EmaState.decode(raw)) is not None
            }
            state = MarketState(
                fixture_id=quote.match_id,
                market_type=quote.market_type,
                home_team=quote.home_team,
                away_team=quote.away_team,
                books=books,
                sport_key=quote.sport_key,
                commence_time=quote.commence_time,
            )
            merged.append((quote, str(version), state, ema))

        commissions = await self.commissions(state for _, _, state, _ in merged)
        now = self.clock()
        line_age = timedelta(seconds=self.settings.ARYABHATA_LINE_MAX_AGE_SECONDS)
        book_age = timedelta(seconds=self.settings.ARYABHATA_BOOK_MAX_AGE_SECONDS)
        period, periods = self.settings.ARYABHATA_STEAM_PERIOD_SECONDS, self.settings.ARYABHATA_STEAM_PERIODS
        evaluations = await asyncio.to_thread(
            lambda: [
                evaluate_market(
                    state,
                    now=now,
                    line_max_age=line_age,
                    book_max_age=book_age,
                    ema=ema,
                    steam_period_seconds=period,
                    steam_periods=periods,
                    commissions=commissions,
                )
                for _, _, state, ema in merged
            ]
        )

        for (quote, version, state, _), evaluation in zip(merged, evaluations, strict=True):
            labels = evaluation.labels or ordered_labels(frozenset(label for book in state.books for label in book.prices))
            by_label = {edge.selection: edge for edge in evaluation.edges}
            fields = [f"{quote.match_id}|{label}" for label in labels]
            payloads = [by_label[label].model_dump_json() if label in by_label else "" for label in labels]
            expiries = [str(by_label[label].expires_at.timestamp()) if label in by_label else "0" for label in labels]
            ema_args = [item for label, ema_state in (evaluation.ema or {}).items() for item in (f"{_EMA_FIELD}{label}", ema_state.encode())]
            committed = await self._commit(
                keys=[self.keys.books(quote.market_key), self.keys.active, self.keys.active_exp],
                args=[
                    version, str(now.timestamp()), self.keys.channel, quote.market_key, str(len(labels)),
                    *fields, *payloads, *expiries, str(len(ema_args) // 2), *ema_args,
                ],
            )
            self.stats.markets += 1
            if int(committed) == 1:
                self.stats.edges += len(evaluation.edges)
                await publish_hive_signals(self.redis, self.settings, evaluation.edges)
            else:
                self.stats.stale_commits += 1
            for reason, count in (evaluation.skipped or {}).items():
                self.stats.skipped[reason] = self.stats.skipped.get(reason, 0) + count
        return evaluations


    async def commissions(self, states: Iterable[MarketState]) -> dict[str, Decimal]:
        """Each quoting bookmaker's commission on net winnings, resolved once per batch. Venues that
        cannot be read leave the settings table in force: an exchange is never priced as free."""
        venues: Sequence[VenueConfig] = ()
        if self.venues is not None:
            try:
                venues = await self.venues()
            except Exception:  # noqa: BLE001 - the venue table is an override, the settings rates still apply
                logger.warning("Aryabhata: execution venues unreadable; commissions from settings", exc_info=True)
        terms = TermsTable(self.settings, venues)
        return {book.bookmaker_id: terms(book.bookmaker_id).commission for state in states for book in state.books}


async def publish_hive_signals(redis: Redis, settings: Settings, edges: Sequence[EdgeSignal]) -> None:
    """Committed edges onto the Hive's stream (Group 65): its consumer group decides each once,
    whichever worker reads it. A missed append costs the bots one frame, never the commit."""
    if not edges or not settings.HIVE_ENABLED:
        return
    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            pipe = redis.pipeline(transaction=False)
            for edge in edges:
                pipe.xadd(f"{settings.HIVE_PREFIX}:signals", {"e": edge.model_dump_json()}, maxlen=settings.HIVE_STREAM_MAXLEN, approximate=True)
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        logger.warning("Aryabhata: %d edge(s) not handed to the Hive; Redis unavailable", len(edges))


async def run_aryabhata(redis: Redis, settings: Settings, venues: Callable[[], Awaitable[Sequence[VenueConfig]]] | None = None) -> None:
    await AryabhataConsumer(redis, settings, venues=venues).run()


# ---------------------------------------------------------------- /ws/signals
class SignalView:
    """One socket's view: its user's bankroll and the current limits, refreshed on a short clock."""

    def __init__(self, user_id: UUID, redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.user_id = user_id
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings
        self.bankroll: Decimal | None = None
        self.limits: RiskLimits = FAIL_CLOSED
        self._bankroll_at = float("-inf")
        self._limits_at = float("-inf")

    async def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._limits_at >= _RISK_REFRESH_SECONDS:
            self.limits = await load_risk_limits(self.redis, self.session_factory, self.settings)
            self._limits_at = now
        if force or now - self._bankroll_at >= _BANKROLL_REFRESH_SECONDS:
            try:
                async with self.session_factory() as session:
                    self.bankroll = await live_bankroll(session, self.user_id, self.settings.starting_bankroll)
                self._bankroll_at = now
            except Exception:  # noqa: BLE001 - keep the last known bankroll (None: nothing is staked)
                logger.warning("Aryabhata: bankroll refresh failed for a signals socket")

    def signals(self, edges: Sequence[EdgeSignal], now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for edge in edges:
            if edge.expires_at <= now:
                continue
            try:
                out.append(personalize(edge, self.bankroll, self.limits).model_dump(mode="json"))
            except ValidationError:
                logger.warning("Aryabhata: dropped an edge that fails the TradeSignal contract (%s)", edge.key)
        return out

    def snapshot(self, edges: Sequence[EdgeSignal], now: datetime) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "signals": self.signals(edges, now),
            "risk": risk_read(self.limits).model_dump(mode="json"),
            "bankroll": float(self.bankroll) if self.bankroll is not None else None,
            "ttl_seconds": SIGNAL_TTL_SECONDS,
            "server_time": now.isoformat(),
        }


async def run_signal_socket(
    websocket: WebSocket,
    user_id: UUID,
    redis: Redis | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Relay edges to one (already authenticated) socket, staked for its user, until either side closes."""
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Signal stream unavailable")
        return
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(keys.channel)
    except (RedisError, OSError):
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Signal stream unavailable")
        return
    await websocket.accept()
    view = SignalView(user_id, redis, session_factory, settings)
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:  # the pump and the ping handler both write to this socket
            await websocket.send_text(json.dumps(payload, separators=(",", ":")))

    async def send_snapshot() -> None:
        await view.refresh(force=True)
        try:
            edges = await read_active_edges(redis, settings)
        except (RedisError, OSError, TimeoutError):
            edges = []
        await send(view.snapshot(edges, _utcnow()))

    async def pump() -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                data = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "risk":
                await send_snapshot()  # every card re-sized with the new limits
            elif kind == "market":
                edges: list[EdgeSignal] = []
                for item in data.get("signals") or []:
                    with contextlib.suppress(ValidationError):
                        edges.append(EdgeSignal.model_validate(item))
                await view.refresh()
                withdrawn = [str(key) for key in data.get("withdrawn") or []]
                now = _utcnow()
                signals = view.signals(edges, now)
                if signals or withdrawn:
                    await send({"type": "signals", "signals": signals, "withdrawn": withdrawn, "server_time": now.isoformat()})

    async def drain() -> None:
        while True:
            if await websocket.receive_text() == "ping":
                await send({"type": "pong"})

    tasks: list[asyncio.Task[None]] = []
    try:
        await send_snapshot()
        tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("Signals socket ended: %s", exc)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(RedisError, OSError, RuntimeError):
            await pubsub.unsubscribe(keys.channel)
            await pubsub.aclose()
