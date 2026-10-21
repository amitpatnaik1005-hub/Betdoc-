"""Pre-match rate persistence and the 3-tier quota optimizer (Group 101, point b.13a). Developed for Amit Ashok Kumar Patnaik.

The ingestion fleet's Odds API run (``app.services.omni_fleet``) already buys one board per sport. This module

* keeps every board it buys as one ``pre_match_odds_snapshots`` row per fixture (``persist_fleet_batch``): markets merged
  per market, the h2h board de-vigged with the Group 79 consensus, rows long past kickoff pruned;
* tells the fleet which sports are due (``due_scope``): one call prices every fixture of a sport, so a sport is bought
  again only when its most urgent fixture's tier (``app.domain.pre_match.cadence``) says its stored board is too old;
* serves the stored boards to the desk with no network call (``list_fixtures`` / ``get_fixture``);
* buys one sport on demand (``force_refresh``), behind a cooldown and the fleet's own breaker, quota reserve and the
  Group 92 cache shield, so a click never spends a credit the fleet would refuse to spend.

Prices are only ever what the provider quoted; consensus and fair odds are derived from them, never filled in.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.pre_match.cadence import aware, due_sports
from app.domain.sports.twin_bridge import canonical_quotes, sharp_benchmark
from app.models.pre_match_odds import PreMatchOddsSnapshotModel

if TYPE_CHECKING:
    from app.adapters.ingestion.base import SourcePayload
    from app.services.omni_fleet import FleetDeps

logger = logging.getLogger("betdoc.pre_match")
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
SOURCE_ID = "odds_api"  # the fleet source whose boards this module keeps
CONSENSUS_METHOD = "h2h: every complete book de-vigged proportionally, then averaged (Group 79)"
_IN_CHUNK = 500

Window = Literal["3h", "12h", "24h", "72h", "all"]
WINDOW_HOURS: dict[str, float | None] = {"3h": 3.0, "12h": 12.0, "24h": 24.0, "72h": 72.0, "all": None}  # all: PRE_MATCH_HORIZON_HOURS


# ================================================================ one board -> rows (pure)
def _iso(moment: datetime) -> str:
    return aware(moment).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if 1.0 < price < 1e6 else None


def _consensus(bookmaker_odds: Mapping[str, Mapping[str, float]], outcomes: Sequence[str]) -> dict[str, Any]:
    """The h2h board's de-vigged consensus (the same definition as the Group 79 sports desk) and its sharp benchmark."""
    venues = canonical_quotes(bookmaker_odds)
    rows = []
    for quotes in venues.values():
        if all(o in quotes for o in outcomes):
            implied = [1.0 / quotes[o] for o in outcomes]
            total = sum(implied)
            rows.append([p / total for p in implied])
    if not rows:
        return {}
    probabilities = {o: sum(r[i] for r in rows) / len(rows) for i, o in enumerate(outcomes)}
    bench = sharp_benchmark(venues, list(outcomes))
    return {
        "market": "h2h",
        "method": CONSENSUS_METHOD,
        "books": len(rows),
        "outcomes": {o: {"probability": round(p, 6), "fair_odds": round(1.0 / p, 4)} for o, p in probabilities.items() if p > 0},
        "sharp": None if bench is None else {
            "book": bench.book,
            "overround": round(bench.overround, 6),
            "fair_odds": {o: round(1.0 / p, 4) for o, p in bench.fair_probabilities.items() if p > 0},
        },
    }


def event_row(raw: Mapping[str, Any], sport_key: str, polled_at: datetime) -> dict[str, Any] | None:
    """One Odds API event as a snapshot row. None when it has no id, teams or kickoff. Every market the call carried is
    kept as quoted; h2h prices are also keyed home / draw / away and de-vigged."""
    match_id, home, away = str(raw.get("id") or ""), str(raw.get("home_team") or ""), str(raw.get("away_team") or "")
    commence = _parse_time(raw.get("commence_time"))
    if not match_id or not home or not away or commence is None or len(match_id) > 128:
        return None
    stamp = _iso(polled_at)
    markets: dict[str, dict[str, Any]] = {}
    h2h: dict[str, dict[str, float]] = {}
    for book in raw.get("bookmakers") or []:
        book_key = str(book.get("key") or "")
        if not book_key:
            continue
        for market in book.get("markets") or []:
            market_key = str(market.get("key") or "")
            outcomes = []
            for outcome in market.get("outcomes") or []:
                price = _price(outcome.get("price"))
                if price is None:
                    continue
                row: dict[str, Any] = {"name": str(outcome.get("name") or ""), "price": price}
                if outcome.get("point") is not None:
                    try:
                        row["point"] = float(outcome["point"])
                    except (TypeError, ValueError):
                        continue
                outcomes.append(row)
                if market_key == "h2h":
                    side = "home" if row["name"] == home else "away" if row["name"] == away else "draw" if row["name"].casefold() == "draw" else None
                    if side is not None:
                        h2h.setdefault(book_key, {})[side] = price
            if market_key and outcomes:
                books = markets.setdefault(market_key, {"polled_at": stamp, "books": {}})["books"]
                books[book_key] = {"last_update": market.get("last_update") or book.get("last_update"), "outcomes": outcomes}
    sides = ("home", "draw", "away") if any("draw" in q for q in h2h.values()) else ("home", "away")
    return {
        "match_id": match_id,
        "sport_key": sport_key[:64],
        "home_team": home[:128],
        "away_team": away[:128],
        "commence_time": commence,
        "markets_json": markets,
        "bookmaker_odds": h2h,
        "consensus_fair_odds": _consensus(h2h, sides) if h2h else {},
        "last_polled_at": aware(polled_at),
    }


def merge(existing: Mapping[str, Any] | None, fresh: dict[str, Any]) -> dict[str, Any]:
    """A fetch replaces the markets it carried and keeps the others (with their own polled_at); without an h2h market
    the stored h2h prices and consensus stay too."""
    if not existing:
        return fresh
    merged = dict(fresh)
    merged["markets_json"] = {**(existing.get("markets_json") or {}), **fresh["markets_json"]}
    if "h2h" not in fresh["markets_json"]:
        merged["bookmaker_odds"] = existing.get("bookmaker_odds") or {}
        merged["consensus_fair_odds"] = existing.get("consensus_fair_odds") or {}
    return merged


# ================================================================ writing (the fleet's Odds API run)
def _insert(session: AsyncSession):  # noqa: ANN202 - the dialect's insert construct
    return pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert


async def store_board(session: AsyncSession, raw_events: Sequence[Mapping[str, Any]], sport_key: str, polled_at: datetime, settings: Settings) -> int:
    """Upsert one sport's board (one row per fixture) and prune rows long past kickoff. The caller commits."""
    fresh: dict[str, dict[str, Any]] = {}
    for raw in raw_events:
        row = event_row(raw, sport_key, polled_at) if isinstance(raw, Mapping) else None
        if row is not None:
            fresh[row["match_id"]] = row
    ids = list(fresh)
    stored: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), _IN_CHUNK):
        result = await session.execute(
            select(PreMatchOddsSnapshotModel.match_id, PreMatchOddsSnapshotModel.markets_json, PreMatchOddsSnapshotModel.bookmaker_odds,
                   PreMatchOddsSnapshotModel.consensus_fair_odds).where(PreMatchOddsSnapshotModel.match_id.in_(ids[i : i + _IN_CHUNK]))
        )
        stored.update({m: {"markets_json": mk, "bookmaker_odds": bo, "consensus_fair_odds": co} for m, mk, bo, co in result.all()})
    if fresh:
        now = datetime.now(UTC)
        insert = _insert(session)
        values = [{"id": uuid.uuid4(), "created_at": now, "updated_at": now, **merge(stored.get(m), row)} for m, row in fresh.items()]
        for i in range(0, len(values), _IN_CHUNK):
            stmt = insert(PreMatchOddsSnapshotModel).values(values[i : i + _IN_CHUNK])
            updated = ("sport_key", "home_team", "away_team", "commence_time", "markets_json", "bookmaker_odds", "consensus_fair_odds", "last_polled_at", "updated_at")
            await session.execute(stmt.on_conflict_do_update(index_elements=["match_id"], set_={c: stmt.excluded[c] for c in updated}))
    cutoff = aware(polled_at) - timedelta(hours=settings.PRE_MATCH_RETENTION_HOURS)
    await session.execute(delete(PreMatchOddsSnapshotModel).where(PreMatchOddsSnapshotModel.commence_time < cutoff))
    return len(fresh)


def _polled_key(settings: Settings) -> str:
    return f"{settings.PRE_MATCH_REDIS_PREFIX}:sport_polled"


async def persist_fleet_batch(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    settings: Settings,
    payloads: Sequence[SourcePayload],
    fetched_at: datetime,
) -> int:
    """Keep every sport board of one fleet run. Never raises: the price history (odds_snapshots) is already stored, and a
    sport whose board is not kept stays due, so the next tick buys it again (the pre-Group 101 cadence)."""
    kept: list[str] = []
    fixtures = 0
    for payload in payloads:
        if not isinstance(payload.data, list):
            continue
        try:
            async with session_factory() as session:
                fixtures += await store_board(session, payload.data, str(payload.key), fetched_at, settings)
                await session.commit()
            kept.append(str(payload.key))
        except SQLAlchemyError:
            logger.warning("pre-match: the %s board was not kept (table missing or DB down?)", payload.key, exc_info=True)
    if kept and redis is not None:
        try:
            await redis.hset(_polled_key(settings), mapping={s: aware(fetched_at).timestamp() for s in kept})
        except (RedisError, OSError):
            logger.info("pre-match: Redis down; sport fetch times fall back to the stored rows")
    return fixtures


# ================================================================ the cadence gate (the fleet's beat)
@dataclass(frozen=True, slots=True)
class CadenceInputs:
    nearest_kickoff: dict[str, datetime]
    last_polled: dict[str, datetime]


async def cadence_inputs(session: AsyncSession, redis: Redis | None, settings: Settings, sports: Sequence[str], now: datetime) -> CadenceInputs:
    """Per sport: when its board was last bought (Redis, else the newest stored row) and its most urgent fixture: one in
    play (kicked off within PRE_MATCH_INPLAY_WINDOW_HOURS and still on the latest board) else the next kickoff."""
    now = aware(now)
    live_from = now - timedelta(hours=settings.PRE_MATCH_INPLAY_WINDOW_HOURS)
    t = PreMatchOddsSnapshotModel
    rows = (await session.execute(
        select(t.sport_key, func.min(case((t.commence_time > now, t.commence_time))), func.max(t.last_polled_at))
        .where(t.sport_key.in_(list(sports)), t.commence_time >= live_from).group_by(t.sport_key)
    )).all()
    nearest = {s: aware(k) for s, k, _ in rows if k is not None}
    last_polled = {s: aware(p) for s, _, p in rows if p is not None}
    if redis is not None and sports:
        try:
            values = await redis.hmget(_polled_key(settings), list(sports))
            for sport, value in zip(sports, values, strict=True):
                if value not in (None, ""):
                    stamp = datetime.fromtimestamp(float(value), tz=UTC)
                    last_polled[sport] = max(stamp, last_polled.get(sport, stamp))
        except (RedisError, OSError, ValueError):
            pass
    started = (await session.execute(
        select(t.sport_key, t.commence_time, t.last_polled_at).where(t.sport_key.in_(list(sports)), t.commence_time >= live_from, t.commence_time <= now)
    )).all()
    for sport, kickoff, polled in started:
        latest = last_polled.get(sport)
        # Dropped from the latest board: finished (or withdrawn), so it no longer holds its sport on tier 3
        if latest is not None and aware(polled) >= latest - timedelta(seconds=1):
            nearest[sport] = min(aware(kickoff), nearest.get(sport, aware(kickoff)))
    return CadenceInputs(nearest, last_polled)


async def due_scope(session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, sports: Sequence[str],
                    now: datetime, stretch: float = 1.0) -> list[str]:
    """The sports of the fleet's Odds API scope that are due now. Fails open (every sport) if the store can't be read."""
    if not settings.PRE_MATCH_TIERED_POLLING_ENABLED or not sports:
        return list(sports)
    try:
        async with session_factory() as session:
            inputs = await cadence_inputs(session, redis, settings, sports, now)
    except SQLAlchemyError:
        logger.warning("pre-match: cadence store unreadable; every sport is due this tick", exc_info=True)
        return list(sports)
    return due_sports(list(sports), inputs.nearest_kickoff, inputs.last_polled, now, settings, stretch)


# ================================================================ reading (the desk)
async def list_fixtures(db: AsyncSession, settings: Settings, *, sport: str | None, window: Window, now: datetime, limit: int) -> list[PreMatchOddsSnapshotModel]:
    """Stored boards from fixtures in play (kicked off within PRE_MATCH_INPLAY_WINDOW_HOURS) to the window's end, soonest first."""
    now = aware(now)
    hours = WINDOW_HOURS[window] or settings.PRE_MATCH_HORIZON_HOURS
    t = PreMatchOddsSnapshotModel
    stmt = select(t).where(t.commence_time >= now - timedelta(hours=settings.PRE_MATCH_INPLAY_WINDOW_HOURS), t.commence_time <= now + timedelta(hours=hours))
    if sport:
        stmt = stmt.where(t.sport_key == sport)
    return list((await db.execute(stmt.order_by(t.commence_time, t.match_id).limit(limit))).scalars().all())


async def get_fixture(db: AsyncSession, match_id: str) -> PreMatchOddsSnapshotModel | None:
    return (await db.execute(select(PreMatchOddsSnapshotModel).where(PreMatchOddsSnapshotModel.match_id == match_id))).scalar_one_or_none()


# ================================================================ force refresh (one sport, on demand)
RefreshStatus = Literal["SYNCED", "FRESH", "NOT_OFFERED"]
_SKIPPED = {
    "locked": "an Odds API run is in progress; its board lands in a moment",
    "throttled": "the fleet's own rate limiter deferred the call; try again shortly",
    "needs_key": "no Odds API key is configured",
    "disabled": "the Odds API source is disabled in Fleet Command",
    "paused": "the Odds API source is paused in Fleet Command",
}


class RefreshRefused(Exception):
    """The fleet would not spend a credit now (breaker, quota reserve, cache shield, sport not covered)."""


class RefreshFailed(Exception):
    """The provider call was made and failed."""


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    status: RefreshStatus
    snapshot: PreMatchOddsSnapshotModel
    source: Literal["the_odds_api", "cache"]
    detail: str


async def force_refresh(deps: FleetDeps, match_id: str, now: datetime) -> RefreshOutcome | None:
    """Buy the fixture's sport board now. None when the fixture is not stored. Within the cooldown the stored board is
    returned (FRESH, no credit spent); NOT_OFFERED when the provider's board no longer lists the fixture."""
    from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor  # noqa: PLC0415 - the fleet imports this module
    from app.services.omni_fleet import read_shield_mode, refusal_reason, run_source  # noqa: PLC0415 - the fleet imports this module

    settings = deps.settings
    async with deps.session_factory() as db:
        snapshot = await get_fixture(db, match_id)
        if snapshot is None:
            return None
        sport = snapshot.sport_key
        inputs = await cadence_inputs(db, deps.redis, settings, [sport], now)
    polled = inputs.last_polled.get(sport, aware(snapshot.last_polled_at))
    age = (aware(now) - polled).total_seconds()
    if age < settings.PRE_MATCH_FORCE_REFRESH_COOLDOWN_SECONDS:
        return RefreshOutcome("FRESH", snapshot, "cache", f"{sport} was fetched {age:.0f}s ago; served from the store, no credit spent")
    if await read_shield_mode(deps.redis, settings) == "FORCE_CACHE_SHIELD":
        raise RefreshRefused("the operator has forced the quota cache shield: the stored board is served")
    if sport not in OddsApiIngestor.coverage(settings):
        raise RefreshRefused(f"{sport} is no longer in the fleet's Odds API sports")
    reason = await refusal_reason(deps, SOURCE_ID)
    if reason is not None:
        raise RefreshRefused(reason)
    summary = await run_source(SOURCE_ID, deps, runner="manual", force=True, scope=[sport])
    if summary.status == "skipped":
        raise RefreshRefused(_SKIPPED.get(summary.reason or "", f"the fleet did not run ({summary.reason})"))
    if summary.status != "ok":
        raise RefreshFailed(summary.reason or summary.status)
    async with deps.session_factory() as db:
        fresh = await get_fixture(db, match_id)
    if fresh is None:
        return RefreshOutcome("NOT_OFFERED", snapshot, "cache", "the fixture has left the store (past retention)")
    if aware(fresh.last_polled_at) > aware(snapshot.last_polled_at):
        return RefreshOutcome("SYNCED", fresh, "the_odds_api", f"{sport} board bought from The Odds API")
    return RefreshOutcome("NOT_OFFERED", fresh, "the_odds_api", "the provider's board no longer lists this fixture; the last stored prices are shown")
