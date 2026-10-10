"""Officiating records, referee profiles and the fortress's referee section (``app/domain/the_wire/referee_bias.py``).

A record per officiated match (``wire_officiating_records``): ESPN fills the cards, penalties and goals when the
match ends; the referee is ESPN's (leagues that name officials) or an administrator's (``POST
/the-wire/referees/assign``). Administrators may also import past records with their source named.

The referee section is written for a tracked fixture once its referee is known and has at least
``WIRE_REFEREE_MIN_MATCHES`` records in the league; the fortress applies it to ``TWIN_REFEREE_SPORTS``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.the_wire import referee_bias as rb
from app.domain.the_wire.espn_parse import EspnEvent
from app.models.the_wire import OfficiatingRecord, RefereeProfile
from app.schemas.the_wire import OfficiatingImport
from app.schemas.twin import FixtureIntel, RefereeIntel
from app.services.the_wire.fixtures import Fixture
from app.services.twin.intel import write_intel

INTEL_SOURCE = "wire:officiating-records"


def league_of(settings: Settings, sport_key: str | None) -> str:
    return settings.WIRE_ESPN_LEAGUES.get(sport_key or "", sport_key or "unknown")


def refereed(settings: Settings, league: str) -> bool:
    """A league whose referee the fortress weighs: its family ('soccer/eng.1' or 'soccer_epl' -> soccer) is in TWIN_REFEREE_SPORTS."""
    family = league.replace("/", "_").split("_", 1)[0].casefold()
    return any(family.startswith(s.strip().casefold()) for s in settings.TWIN_REFEREE_SPORTS.split(",") if s.strip())


def to_domain(rows: Iterable[OfficiatingRecord]) -> list[rb.Record]:
    return [rb.Record(r.referee_name, r.league, r.home_yellow or 0, r.away_yellow or 0, r.home_red or 0, r.away_red or 0, r.home_penalties or 0, r.away_penalties or 0,
                      r.home_goals or 0, r.away_goals or 0) for r in rows if r.status == "FINAL" and r.home_yellow is not None]


def record_dict(r: OfficiatingRecord) -> dict[str, Any]:
    return {"id": str(r.id), "fixture_id": r.fixture_id, "espn_event_id": r.espn_event_id, "league": r.league, "referee": r.referee_name, "home": r.home_team,
            "away": r.away_team, "played_at": r.played_at.isoformat(), "status": r.status, "cards": None if r.home_yellow is None else {
                "home_yellow": r.home_yellow, "away_yellow": r.away_yellow, "home_red": r.home_red, "away_red": r.away_red},
            "penalties": None if r.home_penalties is None else [r.home_penalties, r.away_penalties],
            "goals": None if r.home_goals is None else [r.home_goals, r.away_goals], "source": r.source}


async def assign(session: AsyncSession, settings: Settings, fixture: Fixture, referee: str, by: str, user_id: uuid.UUID, now: datetime) -> OfficiatingRecord:
    row = (await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.fixture_id == fixture.fixture_id))).scalars().first()
    if row is None:
        row = OfficiatingRecord(id=uuid.uuid4(), fixture_id=fixture.fixture_id, league=league_of(settings, fixture.sport_key), home_team=fixture.home,
                                away_team=fixture.away, played_at=fixture.kickoff, status="SCHEDULED", source=f"admin:{by}"[:64], created_at=now)
        session.add(row)
    elif not row.source.endswith(f"admin:{by}"):
        row.source = f"{row.source}+admin:{by}"[:64]
    row.referee_name, row.assigned_by, row.updated_at = " ".join(referee.split())[:96], user_id, now
    await session.flush()
    return row


async def record_espn(session: AsyncSession, event: EspnEvent, fixture_id: str | None, referee: str | None, now: datetime) -> OfficiatingRecord | None:
    """A finished ESPN match into the records (once: keyed by ESPN's event, or the fixture an administrator assigned)."""
    if not event.completed or not event.discipline:
        return None
    row = (await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.espn_event_id == event.event_id))).scalars().first()
    if row is None and fixture_id is not None:
        row = (await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.fixture_id == fixture_id))).scalars().first()
    if row is not None and row.status == "FINAL":
        return row
    if row is None:
        row = OfficiatingRecord(id=uuid.uuid4(), fixture_id=fixture_id, league=event.league, home_team=event.home, away_team=event.away, played_at=event.start,
                                status="SCHEDULED", source="espn", created_at=now)
        session.add(row)
    elif row.source != "espn" and not row.source.startswith("espn+"):
        row.source = f"espn+{row.source}"[:64]
    home, away = event.discipline.get("HOME"), event.discipline.get("AWAY")
    row.espn_event_id, row.league, row.status, row.played_at = event.event_id, event.league, "FINAL", event.start
    row.referee_name = row.referee_name or referee
    row.home_yellow, row.away_yellow = home.yellow if home else 0, away.yellow if away else 0
    row.home_red, row.away_red = home.red if home else 0, away.red if away else 0
    row.home_penalties, row.away_penalties = home.penalties if home else 0, away.penalties if away else 0
    row.home_goals, row.away_goals, row.updated_at = event.home_score, event.away_score, now
    await session.flush()
    return row


async def import_records(session: AsyncSession, body: OfficiatingImport, by: str, user_id: uuid.UUID, now: datetime) -> int:
    for r in body.records:
        session.add(OfficiatingRecord(id=uuid.uuid4(), league=r.league, referee_name=" ".join(r.referee_name.split()), home_team=r.home_team, away_team=r.away_team,
                                      played_at=r.played_at, status="FINAL", home_yellow=r.home_yellow, away_yellow=r.away_yellow, home_red=r.home_red,
                                      away_red=r.away_red, home_penalties=r.home_penalties, away_penalties=r.away_penalties, home_goals=r.home_goals,
                                      away_goals=r.away_goals, source=f"admin:{by}:{body.source}"[:64], assigned_by=user_id, created_at=now, updated_at=now))
    await session.flush()
    return len(body.records)


async def league_records(session: AsyncSession, league: str) -> list[rb.Record]:
    return to_domain((await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.league == league, OfficiatingRecord.status == "FINAL"))).scalars())


def compute(records: Sequence[rb.Record], referee: str, league: str, settings: Settings) -> rb.Profile | None:
    return rb.profile(records, referee, league, prior=settings.WIRE_REFEREE_PRIOR_MATCHES, line=settings.WIRE_REFEREE_TOTALS_LINE, min_matches=settings.WIRE_REFEREE_MIN_MATCHES)


async def refresh(session: AsyncSession, settings: Settings, league: str, now: datetime) -> list[rb.Profile]:
    """Recompute every named referee's profile in ``league`` (the baseline moves with every record)."""
    records = await league_records(session, league)
    names = sorted({r.referee for r in records if r.referee})
    stored = {p.referee_name.casefold(): p for p in (await session.execute(select(RefereeProfile).where(RefereeProfile.league == league))).scalars()}
    out = []
    for name in names:
        prof = compute(records, name, league, settings)
        if prof is None:
            continue
        out.append(prof)
        row = stored.get(name.casefold())
        if row is None:
            row = RefereeProfile(id=uuid.uuid4(), referee_name=name, league=league)
            session.add(row)
        row.sport = league.replace("/", "_").split("_", 1)[0]
        row.matches_officiated, row.avg_yellow_cards, row.avg_red_cards, row.cards_per_game = prof.matches, prof.avg_yellow_cards, prof.avg_red_cards, prof.cards_per_game
        row.penalties_per_90, row.home_bias_ratio, row.over_totals_pct, row.baseline_matches, row.updated_at = (
            prof.penalties_per_90, prof.home_bias_ratio, prof.over_totals_pct, prof.baseline_matches, now)
    await session.flush()
    return out


async def write_intel_for(session: AsyncSession, redis: Redis | None, settings: Settings, fixtures: Sequence[Fixture], now: datetime) -> int:
    """The referee section for every tracked fixture whose referee is known and profiled enough."""
    if redis is None or not fixtures:
        return 0
    by_id = {f.fixture_id: f for f in fixtures}
    assigned = list((await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.fixture_id.in_(list(by_id)), OfficiatingRecord.referee_name.is_not(None)))).scalars())
    written = 0
    cache: dict[str, list[rb.Record]] = {}
    for row in assigned:
        if not refereed(settings, row.league):
            continue
        records = cache.get(row.league)
        if records is None:
            records = cache[row.league] = await league_records(session, row.league)
        prof = compute(records, row.referee_name or "", row.league, settings)
        if prof is None or not prof.fortress_ready:
            continue
        await write_intel(redis, settings, row.fixture_id or "", FixtureIntel(referee=RefereeIntel(
            source=INTEL_SOURCE, observed_at=now, name=prof.referee[:96], cards_per_game=min(20.0, prof.cards_per_game),
            penalties_per_90=min(5.0, prof.penalties_per_90), matches=prof.matches)))
        written += 1
    return written
