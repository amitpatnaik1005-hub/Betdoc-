"""ESPN, every ``WIRE_ESPN_SYNC_MINUTES``: scores with the clock, venues, team sheets, injuries, officials, and the
officiating records of finished matches.

1. Each league the tracked fixtures play in (``WIRE_ESPN_LEAGUES`` maps the feed's sport keys) is read for the
   days that matter: the last ``WIRE_ESPN_DAYS_BACK`` days and each kickoff's date (and the day before: ESPN files
   evening games in US time).
2. An ESPN event is paired with a fixture by both team names and kickoff (``match_fixture``); the pairing is kept in
   Redis (``<WIRE_PREFIX>:espn:<fixture>``) with the score, clock and venue.
3. A finished match in a refereed league becomes an officiating record (cards, penalties, goals).
4. A paired fixture still to finish has its summary read at most every ``WIRE_ESPN_SUMMARY_REFRESH_MINUTES``
   (``WIRE_ESPN_SUMMARY_NEAR_MINUTES`` inside the critical window): the team sheets become the lineups section,
   the injury list the absences (and the injuries section once rated), named officials the fixture's referee.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.wire import espn
from app.core.config import Settings
from app.domain.the_wire.espn_parse import EspnEvent, match_fixture, parse_scoreboard, parse_summary
from app.models.the_wire import OfficiatingRecord
from app.schemas.twin import FixtureIntel, LineupIntel
from app.services.the_wire import lineups, live, referees
from app.services.the_wire.fixtures import Fixture, match_hours, tracked
from app.services.twin.intel import write_intel

logger = logging.getLogger("betdoc.vidur.espn")
_CONCURRENCY = 4
ESPN_ET_LAG = timedelta(hours=6)  # ESPN files a 01:00 UTC kickoff under the previous US date


def link_key(settings: Settings, fixture_id: str) -> str:
    return f"{settings.WIRE_PREFIX}:espn:{fixture_id}"


def summary_gate_key(settings: Settings, fixture_id: str) -> str:
    return f"{settings.WIRE_PREFIX}:espn:summary:{fixture_id}"


async def read_links(redis: Redis | None, settings: Settings, fixture_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if redis is None or not fixture_ids:
        return {}
    try:
        raw = await redis.mget([link_key(settings, f) for f in fixture_ids])
    except (RedisError, OSError):
        return {}
    out = {}
    for fixture_id, value in zip(fixture_ids, raw, strict=True):
        if value:
            try:
                out[fixture_id] = json.loads(value)
            except ValueError:
                continue
    return out


def link_payload(event: EspnEvent, now: datetime) -> dict[str, Any]:
    return {"event_id": event.event_id, "league": event.league, "home": event.home, "away": event.away, "home_id": event.home_id, "away_id": event.away_id,
            "start": event.start.isoformat(), "status": event.status, "home_score": event.home_score, "away_score": event.away_score, "clock": event.clock,
            "detail": event.detail, "period": event.period, "venue": None if event.venue is None else asdict(event.venue) | {"id": event.venue.espn_id},
            "synced_at": now.isoformat()}


def _days(fixtures: Sequence[Fixture], now: datetime, back: int) -> set[date]:
    days = {(now - timedelta(days=i)).date() for i in range(back + 1)}
    for f in fixtures:
        days |= {f.kickoff.date(), (f.kickoff - ESPN_ET_LAG).date()}
    return days


async def _scoreboards(http: httpx.AsyncClient, settings: Settings, wanted: dict[str, set[date]]) -> dict[str, list[EspnEvent]]:
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(league: str, day: date) -> tuple[str, list[EspnEvent]]:
        async with gate:
            try:
                return league, parse_scoreboard(await espn.scoreboard(http, settings, league, day), league)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("VIDUR: ESPN %s %s unavailable (%s)", league, day, type(exc).__name__)
                return league, []

    out: dict[str, dict[str, EspnEvent]] = defaultdict(dict)
    for league, events in await asyncio.gather(*(one(lg, d) for lg, days in wanted.items() for d in sorted(days))):
        for e in events:
            out[league][e.event_id] = e
    return {lg: list(evs.values()) for lg, evs in out.items()}


async def _summary_due(redis: Redis, settings: Settings, fixture: Fixture, now: datetime) -> bool:
    near = timedelta(0) <= fixture.kickoff - now <= timedelta(hours=settings.WIRE_CRITICAL_WINDOW_HOURS)
    minutes = settings.WIRE_ESPN_SUMMARY_NEAR_MINUTES if near else settings.WIRE_ESPN_SUMMARY_REFRESH_MINUTES
    return bool(await redis.set(summary_gate_key(settings, fixture.fixture_id), now.isoformat(), nx=True, ex=max(60, int(minutes * 60))))


async def _apply_summary(session: AsyncSession, redis: Redis, settings: Settings, fixture: Fixture, event: EspnEvent, payload: dict[str, Any], now: datetime) -> dict[str, int]:
    summary = parse_summary(payload, event.event_id, event.home_id, event.away_id)
    done = {"lineups": 0, "injuries": 0, "referees": 0}
    if summary.sheets_listed:
        both = summary.sheets["HOME"] and summary.sheets["AWAY"]
        await write_intel(redis, settings, fixture.fixture_id, FixtureIntel(lineups=LineupIntel(
            source="espn", observed_at=now, home_confirmed=summary.sheets["HOME"], away_confirmed=summary.sheets["AWAY"], published_at=now if both else None)))
        done["lineups"] = 1
    if summary.injuries_listed:
        await lineups.upsert_espn(session, fixture, summary.absences, now)
        result = await lineups.publish_intel(session, redis, settings, fixture, now, source="espn+wire-ratings")
        done["injuries"] = int(bool(result.get("written")))
    if summary.officials and referees.refereed(settings, event.league):
        row = (await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.fixture_id == fixture.fixture_id))).scalars().first()
        if row is None:
            session.add(OfficiatingRecord(fixture_id=fixture.fixture_id, espn_event_id=event.event_id, league=event.league, referee_name=summary.officials[0][:96],
                                          home_team=fixture.home, away_team=fixture.away, played_at=fixture.kickoff, status="SCHEDULED", source="espn", created_at=now))
            done["referees"] = 1
        elif row.referee_name is None:
            row.referee_name, row.updated_at = summary.officials[0][:96], now
            done["referees"] = 1
    return done


async def sync(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    if not settings.WIRE_ESPN_ENABLED:
        return {"enabled": False}
    fixtures = [f for f in await tracked(redis, settings, now) if f.sport_key in settings.WIRE_ESPN_LEAGUES]
    by_league: dict[str, list[Fixture]] = defaultdict(list)
    for f in fixtures:
        by_league[settings.WIRE_ESPN_LEAGUES[f.sport_key or ""]].append(f)
    async with sessions() as session:
        waiting = list((await session.execute(select(OfficiatingRecord.league, OfficiatingRecord.played_at).where(
            OfficiatingRecord.status == "SCHEDULED", OfficiatingRecord.played_at <= now, OfficiatingRecord.played_at >= now - timedelta(days=settings.WIRE_ESPN_DAYS_BACK + 1)))).all())
    wanted = {lg: _days(fx, now, settings.WIRE_ESPN_DAYS_BACK) for lg, fx in by_league.items()}
    for league, played in waiting:
        if "/" in league:
            wanted.setdefault(league, set()).update({played.date(), (played - ESPN_ET_LAG).date()})
    report: dict[str, Any] = {"leagues": len(wanted), "events": 0, "paired": 0, "records": 0, "lineups": 0, "injuries": 0, "referees": 0, "referee_intel": 0}
    if not wanted:
        return report
    own = http is None
    http = http or espn.client(settings)
    try:
        events = await _scoreboards(http, settings, wanted)
        previous = await read_links(redis, settings, [f.fixture_id for f in fixtures])
        frames: list[dict[str, Any]] = []
        touched: set[str] = set()
        async with sessions() as session:
            for league, evs in events.items():
                rows = [f.row for f in by_league.get(league, [])]
                by_id = {f.fixture_id: f for f in by_league.get(league, [])}
                for event in evs:
                    report["events"] += 1
                    fixture_id = match_fixture(event, rows, cutoff=settings.WIRE_TEAM_MATCH_CUTOFF, tolerance=timedelta(minutes=settings.WIRE_KICKOFF_TOLERANCE_MINUTES))
                    if event.completed and referees.refereed(settings, league):
                        if await referees.record_espn(session, event, fixture_id, None, now) is not None:
                            report["records"] += 1
                            touched.add(league)
                    if fixture_id is None:
                        continue
                    report["paired"] += 1
                    fixture = by_id[fixture_id]
                    link = link_payload(event, now)
                    old = previous.get(fixture_id) or {}
                    if redis is not None:
                        ttl = int((fixture.kickoff - now).total_seconds() + (match_hours(settings, fixture.sport_key) + 6) * 3600)
                        await redis.set(link_key(settings, fixture_id), json.dumps(link, default=str), ex=max(3600, ttl))
                    if (old.get("home_score"), old.get("away_score"), old.get("status"), old.get("clock")) != (event.home_score, event.away_score, event.status, event.clock):
                        frames.append({"type": "scores", "match_id": fixture_id, "home_team": fixture.home, "away_team": fixture.away, "home_score": event.home_score,
                                       "away_score": event.away_score, "status": event.status, "clock": event.clock, "detail": event.detail})
                    if redis is None or event.completed or not await _summary_due(redis, settings, fixture, now):
                        continue
                    try:
                        payload = await espn.summary(http, settings, league, event.event_id)
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.warning("VIDUR: ESPN summary %s unavailable (%s)", event.event_id, type(exc).__name__)
                        continue
                    done = await _apply_summary(session, redis, settings, fixture, event, payload, now)
                    for k, v in done.items():
                        report[k] += v
                    if done["referees"]:
                        touched.add(league)
            for league in touched:
                await referees.refresh(session, settings, league, now)
            report["referee_intel"] = await referees.write_intel_for(session, redis, settings, fixtures, now)
            await session.commit()
        await live.publish(redis, settings, frames)
        return report
    finally:
        if own:
            await http.aclose()
