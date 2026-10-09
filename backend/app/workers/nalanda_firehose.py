"""Nalanda's firehose: the live engines write to a Redis stream and move on; this worker stores it.

Producers (fire-and-forget, never raising, never waiting on PostgreSQL):

* ``emit_quotes``: every market frame ``publish_market_quotes`` sends to Aryabhata is also appended to
  ``<NALANDA_PREFIX>:firehose`` (one entry per frame, every book and selection in it);
* ``emit_record_soon``: an archive record (a bookmaker's response, as the executor got it) is
  scheduled onto the stream from a background task, so the order path does not even await Redis.

The consumer (``NalandaFirehose``, in the API process when ``NALANDA_ENABLED``, or
``python -m app.workers.nalanda_firehose``) reads the stream in bulk through the ``nalanda`` consumer
group. One process at a time holds the lease and consumes: the ghost-spike cleanser keeps per-cell
state, so it must see a cell's ticks in order. Each batch:

1. explodes frames into ticks and runs them through ``GhostSpikeFilter`` (a suspect waits up to 3s for
   its cell's next price; a revert flags it ``is_anomaly``);
2. inserts every decided tick in one ``executemany`` (``insert().values`` list: asyncpg's batched
   path) and appends the archive records to the hash chain, in one transaction;
3. acknowledges the entries whose ticks are all stored. An entry with a tick still held is
   acknowledged when that tick is decided, so a crash re-delivers it (at least once, never lost).

Entries left pending by a consumer that died are claimed after ``_CLAIM_IDLE_MS``. Ingestion
counters and a per-second velocity series live in Redis for the telemetry panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.nalanda_lake import NalandaTick
from app.schemas.aryabhata import MarketQuote
from app.services.nalanda_chain import ArchiveRecord, append_records, canonical
from app.services.nalanda_cleanser import GhostSpikeFilter, Tick

logger = logging.getLogger("betdoc.nalanda")

_EMIT_TIMEOUT_SECONDS = 0.5
_READ_BLOCK_MS = 1_000
_CLAIM_IDLE_MS = 60_000
_BACKOFF_MAX_SECONDS = 30.0
_VELOCITY_KEEP_SECONDS = 600
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""
_BACKGROUND: set[asyncio.Task[Any]] = set()


class NalandaKeys:
    def __init__(self, settings: Settings) -> None:
        p = settings.NALANDA_PREFIX
        self.stream = f"{p}:firehose"
        self.group = "nalanda"
        self.leader = f"{p}:leader"
        self.stats = f"{p}:stats"
        self.velocity = f"{p}:velocity"
        self.verify = f"{p}:verify:last"


# ---------------------------------------------------------------- producers
async def emit(redis: Redis | None, settings: Settings, entries: Sequence[dict[str, str]]) -> bool:
    """XADD the entries in one round trip. Never raises; False when they could not be appended."""
    if redis is None or not entries or not settings.NALANDA_ENABLED:
        return False
    keys = NalandaKeys(settings)
    try:
        async with asyncio.timeout(_EMIT_TIMEOUT_SECONDS):
            pipe = redis.pipeline(transaction=False)
            for entry in entries:
                pipe.xadd(keys.stream, entry, maxlen=settings.NALANDA_STREAM_MAXLEN, approximate=True)
            await pipe.execute()
    except (RedisError, OSError, TimeoutError):
        logger.warning("Nalanda: %d firehose entr(ies) not appended; Redis unavailable", len(entries))
        return False
    return True


async def emit_quotes(redis: Redis | None, settings: Settings, quotes: Sequence[MarketQuote]) -> bool:
    return await emit(redis, settings, [{"k": "q", "d": q.model_dump_json()} for q in quotes])


def record_entry(record: ArchiveRecord) -> dict[str, str]:
    body = {
        "kind": record.kind, "source": record.source, "source_id": record.source_id, "payload": canonical(dict(record.payload)),
        "user_id": None if record.user_id is None else str(record.user_id), "bot_id": None if record.bot_id is None else str(record.bot_id),
        "ledger_id": None if record.ledger_id is None else str(record.ledger_id), "fixture_id": record.fixture_id,
        "amount_inr": None if record.amount_inr is None else str(record.amount_inr),
        "occurred_at": None if record.occurred_at is None else record.occurred_at.isoformat(),
    }
    return {"k": "r", "d": json.dumps(body, separators=(",", ":"))}


def parse_record(raw: str) -> ArchiveRecord:
    body = json.loads(raw)

    def uid(value: str | None) -> uuid.UUID | None:
        return None if not value else uuid.UUID(value)

    return ArchiveRecord(
        kind=body["kind"], source=body["source"], source_id=body["source_id"], payload=body.get("payload") or {}, user_id=uid(body.get("user_id")),
        bot_id=uid(body.get("bot_id")), ledger_id=uid(body.get("ledger_id")), fixture_id=body.get("fixture_id"),
        amount_inr=None if body.get("amount_inr") is None else Decimal(body["amount_inr"]),
        occurred_at=None if not body.get("occurred_at") else datetime.fromisoformat(body["occurred_at"]),
    )


def emit_record_soon(redis: Redis | None, settings: Settings, record: ArchiveRecord) -> None:
    """Schedule one archive record onto the firehose and return at once (a strong reference keeps the
    task alive). Outside an event loop it is dropped: the mirror sweeper still archives the source rows."""
    if redis is None or not settings.NALANDA_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(emit(redis, settings, [record_entry(record)]))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# ---------------------------------------------------------------- the consumer
def ticks_of(entry_id: str, quote: MarketQuote) -> list[Tick]:
    out = []
    for book in quote.books:
        observed = book.observed_at or quote.fetched_at
        observed = observed if observed.tzinfo else observed.replace(tzinfo=UTC)
        for selection, odds in book.prices.items():
            if odds is None or not Decimal(odds).is_finite() or Decimal(odds) <= 1:
                continue
            out.append(Tick(quote.match_id, quote.market_type, selection[:64], quote.source[:64], book.bookmaker_id[:64], Decimal(odds).quantize(Decimal("0.0001")), observed, book.is_suspended, entry_id))
    return out


class NalandaFirehose:
    def __init__(self, redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.redis, self.session_factory, self.settings, self.clock = redis, session_factory, settings, clock
        self.keys = NalandaKeys(settings)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.token = uuid.uuid4().hex
        self.filter = GhostSpikeFilter(
            window=settings.NALANDA_ANOMALY_WINDOW, z_threshold=settings.NALANDA_ANOMALY_Z, min_jump=settings.NALANDA_ANOMALY_MIN_JUMP, hold_seconds=settings.NALANDA_ANOMALY_HOLD_SECONDS,
        )
        self._renew = redis.register_script(_RENEW)
        self._held: Counter[str] = Counter()  # entry -> its ticks still held by the filter
        self._claimed_at = 0.0
        self.stats: Counter[str] = Counter()

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.keys.stream, self.keys.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def lead(self) -> bool:
        lease_ms = int(_READ_BLOCK_MS * 15)
        if await self._renew(keys=[self.keys.leader], args=[self.token, lease_ms]):
            return True
        return bool(await self.redis.set(self.keys.leader, self.token, nx=True, px=lease_ms))

    async def _entries(self) -> list[tuple[str, dict[str, str]]]:
        entries: list[tuple[str, dict[str, str]]] = []
        if time.monotonic() - self._claimed_at > _CLAIM_IDLE_MS / 1000:
            self._claimed_at = time.monotonic()
            claimed = await self.redis.xautoclaim(self.keys.stream, self.keys.group, self.consumer, min_idle_time=_CLAIM_IDLE_MS, start_id="0-0", count=self.settings.NALANDA_BATCH)
            pending = claimed[1] if isinstance(claimed, list | tuple) and len(claimed) > 1 else []
            entries += [(entry_id, fields) for entry_id, fields in pending if fields and entry_id not in self._held]
        batches = await self.redis.xreadgroup(self.keys.group, self.consumer, {self.keys.stream: ">"}, count=self.settings.NALANDA_BATCH, block=_READ_BLOCK_MS)
        for _, items in batches or []:
            entries += list(items)
        return entries

    async def step(self) -> int:
        """One batch: read, cleanse, store, acknowledge. Returns how many entries it consumed."""
        if not await self.lead():
            await asyncio.sleep(_READ_BLOCK_MS / 1000)
            return 0
        entries = await self._entries()
        decided: list[Tick] = []
        records: list[ArchiveRecord] = []
        consumed: list[str] = []
        for entry_id, fields in entries:
            consumed.append(entry_id)
            kind, data = fields.get("k"), fields.get("d", "")
            try:
                if kind == "q":
                    for tick in ticks_of(entry_id, MarketQuote.model_validate_json(data)):
                        decided += self.filter.push(tick)
                elif kind == "r":
                    records.append(parse_record(data))
                else:
                    self.stats["malformed"] += 1
            except (ValidationError, ValueError, KeyError, json.JSONDecodeError):
                self.stats["malformed"] += 1
                logger.warning("Nalanda: firehose entry %s is malformed; acknowledged and skipped", entry_id)
        decided += self.filter.flush(self.clock())
        await self._store(decided, records)
        await self._acknowledge(consumed)
        return len(entries)

    def _still_held(self) -> Counter[str]:
        held: Counter[str] = Counter()
        for state in self.filter._cells.values():  # noqa: SLF001 - the firehose owns its filter
            for tick in state.held:
                held[tick.stream_id] += 1
        return held

    async def _acknowledge(self, consumed: Sequence[str]) -> None:
        held = self._still_held()
        ready = [e for e in {*consumed, *self._held} if held.get(e, 0) == 0]
        self._held = Counter({e: n for e, n in held.items()})
        if ready:
            await self.redis.xack(self.keys.stream, self.keys.group, *ready)

    async def _store(self, ticks: Sequence[Tick], records: Sequence[ArchiveRecord]) -> None:
        if not ticks and not records:
            return
        now = self.clock()
        async with self.session_factory() as session:
            if ticks:
                rows = [
                    {"created_at": now, "observed_at": t.observed_at, "fixture_id": t.fixture_id[:128], "market": t.market[:64], "selection": t.selection, "source": t.source,
                     "bookmaker_id": t.bookmaker_id, "odds": t.odds, "is_suspended": t.is_suspended, "is_anomaly": t.is_anomaly, "anomaly_z": t.anomaly_z, "stream_id": t.stream_id}
                    for t in ticks
                ]
                for i in range(0, len(rows), 5_000):
                    await session.execute(insert(NalandaTick), rows[i : i + 5_000])
            appended = await append_records(session, records, now=now) if records else None
            await session.commit()
        anomalies = sum(1 for t in ticks if t.is_anomaly)
        self.stats.update({"ticks": len(ticks), "anomalies": anomalies, "records": appended.appended if appended else 0, "batches": 1})
        await self._publish_stats(len(ticks), anomalies, appended.appended if appended else 0)

    async def _publish_stats(self, ticks: int, anomalies: int, records: int) -> None:
        second = int(time.time())
        try:
            pipe = self.redis.pipeline(transaction=False)
            pipe.hincrby(self.keys.stats, "ticks", ticks)
            pipe.hincrby(self.keys.stats, "anomalies", anomalies)
            pipe.hincrby(self.keys.stats, "records", records)
            pipe.hincrby(self.keys.stats, "batches", 1)
            pipe.hset(self.keys.stats, "last_batch_at", datetime.now(UTC).isoformat())
            pipe.hincrby(self.keys.velocity, str(second), ticks + records)
            await pipe.execute()
            if second % 60 == 0:
                fields = await self.redis.hkeys(self.keys.velocity)
                stale = [f for f in fields if f.isdigit() and int(f) < second - _VELOCITY_KEEP_SECONDS]
                if stale:
                    await self.redis.hdel(self.keys.velocity, *stale)
        except (RedisError, OSError):
            pass

    async def drain(self) -> None:
        """Shutdown: whatever the filter still holds is stored as ordinary prices, then acknowledged."""
        await self._store(list(self.filter.drain()), [])
        await self._acknowledge([])

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.ensure_group()
                while True:
                    await self.step()
                    backoff = 1.0
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.drain())
                raise
            except (RedisError, OSError) as exc:
                logger.warning("Nalanda firehose lost Redis (%s); retrying in %.0fs", type(exc).__name__, backoff)
            except Exception:  # noqa: BLE001 - a bad batch must not stop the lake
                logger.exception("Nalanda firehose step failed")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)


async def run_nalanda_firehose(redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    await NalandaFirehose(redis, session_factory, settings).run()


async def _main() -> None:
    from app.core.database import AsyncSessionLocal  # noqa: PLC0415

    settings = get_settings()
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        await run_nalanda_firehose(redis, AsyncSessionLocal, settings)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
