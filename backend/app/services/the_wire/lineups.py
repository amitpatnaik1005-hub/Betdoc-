"""Absences, the operator's ratings, and the fortress's injury section (``app/domain/the_wire/lineup_impact.py``).

ESPN's injury lists (the NFL, NBA, MLB, NHL) arrive through the sync; an administrator adds any other absence
(``POST /the-wire/absences``). A rating given to a player carries over to that player's later absences.

The injury section is written only when every OUT, SUSPENDED or DOUBTFUL absence of the fixture is rated: an
unrated star would otherwise read as a clean bill of health. QUESTIONABLE players count in the lineup delta at a
quarter of their weight but are not absences to the fortress. A managerial change already recorded on the section is
kept when the Wire rewrites it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.the_wire import lineup_impact as li
from app.domain.the_wire.espn_parse import EspnAbsence
from app.models.the_wire import InjuryRosterReport
from app.schemas.the_wire import AbsenceIn
from app.schemas.twin import Absence, FixtureIntel, InjuryIntel
from app.services.the_wire.fixtures import Fixture
from app.services.twin.intel import read_intel, write_intel

FORTRESS_STATUS = {"OUT": "OUT", "SUSPENDED": "OUT", "DOUBTFUL": "DOUBTFUL"}


async def rows_for(session: AsyncSession, fixture_id: str, *, active_only: bool = True) -> list[InjuryRosterReport]:
    stmt = select(InjuryRosterReport).where(InjuryRosterReport.fixture_id == fixture_id)
    if active_only:
        stmt = stmt.where(InjuryRosterReport.is_active.is_(True))
    return list((await session.execute(stmt.order_by(InjuryRosterReport.side, InjuryRosterReport.player_name))).scalars())


async def carried_rating(session: AsyncSession, sport: str, team: str, player: str) -> InjuryRosterReport | None:
    """The player's latest rated absence (any fixture): its rating carries over."""
    return (await session.execute(
        select(InjuryRosterReport).where(InjuryRosterReport.sport == sport, InjuryRosterReport.team_name == team, InjuryRosterReport.player_name == player,
                                         InjuryRosterReport.rating.is_not(None)).order_by(InjuryRosterReport.updated_at.desc()).limit(1)
    )).scalars().first()


async def _new_row(session: AsyncSession, fixture: Fixture, side: str, player: str, now: datetime, source: str) -> InjuryRosterReport:
    team = fixture.home if side == "HOME" else fixture.away
    row = InjuryRosterReport(id=uuid.uuid4(), fixture_id=fixture.fixture_id, sport=fixture.family, side=side, team_name=team, player_name=player,
                             source_name=source, is_active=True, reported_at=now, updated_at=now)
    prior = await carried_rating(session, fixture.family, team, player)
    if prior is not None:
        row.rating, row.replacement_quality, row.rated_by = prior.rating, prior.replacement_quality, prior.rated_by
    session.add(row)
    return row


async def upsert_espn(session: AsyncSession, fixture: Fixture, absences: Iterable[EspnAbsence], now: datetime) -> list[InjuryRosterReport]:
    """ESPN's current list for the fixture; ESPN's earlier entries no longer listed are retired (not deleted)."""
    existing = {(r.side, r.player_name.casefold()): r for r in await rows_for(session, fixture.fixture_id, active_only=False)}
    listed = set()
    for a in absences:
        key = (a.side, a.player.casefold())
        listed.add(key)
        row = existing.get(key) or await _new_row(session, fixture, a.side, a.player[:128], now, "espn")
        row.position, row.status, row.injury_nature, row.return_date = (a.position or row.position), a.status, a.nature, a.return_date
        row.is_active, row.updated_at = True, now
    for key, row in existing.items():
        if key not in listed and row.source_name == "espn" and row.is_active:
            row.is_active, row.updated_at = False, now
    await session.flush()
    return await rows_for(session, fixture.fixture_id)


async def add_manual(session: AsyncSession, fixture: Fixture, body: AbsenceIn, by: str, user_id: uuid.UUID, now: datetime) -> InjuryRosterReport:
    rows = {(r.side, r.player_name.casefold()): r for r in await rows_for(session, fixture.fixture_id, active_only=False)}
    row = rows.get((body.side, body.player_name.casefold())) or await _new_row(session, fixture, body.side, body.player_name, now, f"admin:{by}"[:64])
    row.position, row.status, row.injury_nature = body.position or row.position, body.status, body.injury_nature or row.injury_nature
    if body.rating is not None:
        row.rating, row.rated_by = body.rating, user_id
    if body.replacement_quality is not None:
        row.replacement_quality = body.replacement_quality
    row.is_active, row.updated_at = True, now
    await session.flush()
    return row


def to_domain(rows: Iterable[InjuryRosterReport]) -> list[li.Absence]:
    return [li.Absence(r.side, r.player_name, r.position, r.status, r.rating, r.replacement_quality) for r in rows if r.is_active]


def view(rows: list[InjuryRosterReport], fixture: Fixture, settings: Settings, probabilities: Mapping[str, float] | None) -> dict[str, Any]:
    policy = li.LineupPolicy.from_settings(settings)
    absences = to_domain(rows)
    delta, unrated = li.deltas(absences, fixture.family, policy)
    return {
        "fixture_id": fixture.fixture_id, "home": fixture.home, "away": fixture.away, "sport_key": fixture.sport_key, "kickoff": fixture.kickoff.isoformat(),
        "absences": [{**row_dict(r), "cost": li.cost(a, fixture.family, policy), "fortress_impact": li.fortress_impact(a, fixture.family, policy),
                      "position_weight": policy.weight(fixture.family, a.position)} for r, a in zip([r for r in rows if r.is_active], absences, strict=True)],
        "delta": delta, "unrated": [a.player for a in unrated],
        "probabilities": None if not probabilities else dict(probabilities),
        "adjusted": None if not probabilities else li.adjust(probabilities, delta),
        "rating_scale": policy.rating_scale,
    }


def row_dict(r: InjuryRosterReport) -> dict[str, Any]:
    return {"id": str(r.id), "fixture_id": r.fixture_id, "side": r.side, "team": r.team_name, "player": r.player_name, "position": r.position, "status": r.status,
            "nature": r.injury_nature, "return_date": r.return_date, "rating": r.rating, "replacement_quality": r.replacement_quality,
            "impact_delta_pct": r.impact_delta_pct, "source": r.source_name, "active": r.is_active, "updated_at": r.updated_at.isoformat() if r.updated_at else None}


async def publish_intel(session: AsyncSession, redis: Redis | None, settings: Settings, fixture: Fixture, now: datetime, *, source: str) -> dict[str, Any]:
    """Write the fixture's injury section, or say which absences need a rating first."""
    rows = await rows_for(session, fixture.fixture_id)
    policy = li.LineupPolicy.from_settings(settings)
    for row, a in zip(rows, to_domain(rows), strict=True):
        c = li.cost(a, fixture.family, policy)
        row.impact_delta_pct = None if c is None else round(-100.0 * c, 2)
    await session.flush()
    blocking = [r.player_name for r in rows if r.status in FORTRESS_STATUS and r.rating is None]
    if blocking:
        return {"written": False, "reason": "UNRATED", "unrated": blocking}
    if redis is None:
        return {"written": False, "reason": "NO_REDIS"}
    absences = [Absence(side=r.side, player=r.player_name[:96], impact=li.fortress_impact(a, fixture.family, policy) or 0.0, status=FORTRESS_STATUS[r.status])  # type: ignore[arg-type]
                for r, a in zip(rows, to_domain(rows), strict=True) if r.status in FORTRESS_STATUS]
    current = (await read_intel(redis, settings, [fixture.fixture_id])).get(fixture.fixture_id)
    managers = {} if current is None or current.injuries is None else dict(current.injuries.manager_changed_at)
    section = InjuryIntel(source=source[:64], observed_at=now, absences=absences[:60], manager_changed_at=managers)
    await write_intel(redis, settings, fixture.fixture_id, FixtureIntel(injuries=section))
    return {"written": True, "absences": len(absences)}
