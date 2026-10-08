"""Canonical ids -> each bookmaker's proprietary ids. No mapping, no shot.

* Fixture: our canonical ``fixture_id`` (the Aryabhata / fleet match id) -> the venue's event id.
* Selection: ``<fixture_id>|<market>|<selection>`` -> the venue's outcome id; venues that use the
  same outcome codes for every event (``selection_codes``, e.g. 1 / X / 2) need no per-event rows.

Rows come from catalog sync (the venue's event list, each side resolved through the alias
dictionary and the kick-off date to the same canonical match id the fleet uses) or from an admin.
Lookups are cached in Redis; a miss raises ``UnmappedEntityError`` and the execution rolls back.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.models import utc_now
from app.models.execution import EntityMapping
from app.services.omni_normalizer import AliasDictionary, default_alias_dictionary

logger = logging.getLogger("betdoc.sniper")

CACHE_TTL_SECONDS = 3600
_MISS = "~"  # cached negative answer, so a burst of shots on an unmapped fixture hits the database once


class UnmappedEntityError(LookupError):
    def __init__(self, kind: str, key: str) -> None:
        super().__init__(f"No {kind} mapping for {key}")
        self.kind = kind
        self.key = key


@dataclass(frozen=True, slots=True)
class RemoteIds:
    event_id: str
    selection_id: str


@dataclass(frozen=True, slots=True)
class VenueEvent:
    """One entry of a venue's event catalog."""

    event_id: str
    sport_key: str
    home: str
    away: str
    commence_time: str  # ISO-8601
    outcomes: dict[str, str] = field(default_factory=dict)  # HOME/DRAW/AWAY -> the venue's outcome id


@dataclass(slots=True)
class SyncReport:
    events: int = 0
    mapped: int = 0
    unresolved: list[str] = field(default_factory=list)


def selection_key(fixture_id: str, market: str, selection: str) -> str:
    return f"{fixture_id}|{market}|{selection}"


class IdMapper:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, aliases: AliasDictionary | None = None) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self._aliases = aliases

    @property
    def aliases(self) -> AliasDictionary:
        if self._aliases is None:
            self._aliases = default_alias_dictionary()
        return self._aliases

    def _cache_key(self, venue_id: str) -> str:
        return f"{self.settings.SNIPER_PREFIX}:idmap:{venue_id}"

    # -------------------------------------------------------------- lookup
    async def resolve(self, venue: VenueConfig, fixture_id: str, market: str, selection: str) -> RemoteIds:
        event_id = await self._lookup(venue.id, "fixture", fixture_id)
        if event_id is None:
            raise UnmappedEntityError("fixture", fixture_id)
        selection_id = await self._lookup(venue.id, "selection", selection_key(fixture_id, market, selection))
        if selection_id is None:
            selection_id = venue.selection_codes.get(selection)
        if not selection_id:
            raise UnmappedEntityError("selection", selection_key(fixture_id, market, selection))
        return RemoteIds(event_id, selection_id)

    async def _lookup(self, venue_id: str, kind: str, key: str) -> str | None:
        field_name = f"{kind}|{key}"
        if self.redis is not None:
            try:
                cached = await self.redis.hget(self._cache_key(venue_id), field_name)
            except (RedisError, OSError):
                cached = None
            if cached is not None:
                return None if cached == _MISS else cached
        async with self.session_factory() as session:
            remote = await session.scalar(
                select(EntityMapping.remote_id).where(EntityMapping.venue_id == venue_id, EntityMapping.kind == kind, EntityMapping.canonical_key == key)
            )
        if self.redis is not None:
            with contextlib.suppress(RedisError, OSError):
                await self.redis.hset(self._cache_key(venue_id), field_name, remote or _MISS)
                await self.redis.expire(self._cache_key(venue_id), CACHE_TTL_SECONDS)
        return remote

    async def forget(self, venue_id: str) -> None:
        if self.redis is not None:
            with contextlib.suppress(RedisError, OSError):
                await self.redis.delete(self._cache_key(venue_id))

    # -------------------------------------------------------------- writes
    async def upsert(self, venue_id: str, rows: Sequence[tuple[str, str, str, str]]) -> int:
        """``rows``: (kind, canonical_key, remote_id, source). Returns how many were written."""
        if not rows:
            return 0
        async with self.session_factory() as session:
            dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            now = utc_now()
            for kind, key, remote, source in rows:
                stmt = insert(EntityMapping).values(venue_id=venue_id, kind=kind, canonical_key=key, remote_id=remote, source=source, detail={}, updated_at=now)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["venue_id", "kind", "canonical_key"],
                    set_={"remote_id": stmt.excluded.remote_id, "source": stmt.excluded.source, "updated_at": now},
                )
                await session.execute(stmt)
            await session.commit()
        await self.forget(venue_id)  # cached answers (including cached misses) are now stale
        return len(rows)

    async def sync_catalog(self, venue: VenueConfig, events: Sequence[VenueEvent]) -> SyncReport:
        """Map a venue's event list onto canonical fixtures: both sides through the alias dictionary,
        then the same canonical match id the ingestion fleet builds (sport, teams, UTC kick-off date)."""
        from datetime import datetime  # local: only catalog sync parses venue timestamps

        report = SyncReport(events=len(events))
        rows: list[tuple[str, str, str, str]] = []
        for event in events:
            home = self.aliases.lookup(event.sport_key, event.home)
            away = self.aliases.lookup(event.sport_key, event.away)
            try:
                kickoff = datetime.fromisoformat(event.commence_time.replace("Z", "+00:00"))
            except ValueError:
                kickoff = None
            if home is None or away is None or kickoff is None:
                report.unresolved.append(f"{event.event_id}: {event.home} v {event.away}")
                continue
            kickoff = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=UTC)
            fixture_id = self.aliases.match_id(event.sport_key, home.id, away.id, kickoff)
            rows.append(("fixture", fixture_id, event.event_id, "catalog"))
            for label, outcome_id in event.outcomes.items():
                rows.append(("selection", selection_key(fixture_id, "Match Odds", label), outcome_id, "catalog"))
            report.mapped += 1
        await self.upsert(venue.id, rows)
        logger.info("Catalog sync %s: %d events, %d mapped, %d unresolved", venue.id, report.events, report.mapped, len(report.unresolved))
        return report
