"""Where a fixture is played (``app/domain/the_wire/venue_geocoder.py`` sets the order):

    indoor sport  ->  an administrator's venue  ->  the seed registry  ->  a tennis tournament
                  ->  a venue learnt from ESPN before  ->  ESPN's venue for this fixture, placed by Open-Meteo's geocoder

A venue learnt from ESPN is stored (``wire_venue_locations``, source ``espn+open-meteo``) so the geocoder is asked
once per ground. ESPN's ``indoor`` flag sets the roof (indoor: a fixed roof, else open air); a roof the seed knows
to be retractable stays flagged as such.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.wire import open_meteo
from app.core.config import Settings
from app.domain.the_wire import venue_geocoder as geo
from app.models.the_wire import VenueLocation
from app.services.the_wire.fixtures import Fixture

ESPN_SOURCE = "espn+open-meteo"


@dataclass(frozen=True, slots=True)
class Placement:
    venue_name: str | None
    roof: geo.Roof
    latitude: float | None
    longitude: float | None
    elevation_m: float | None
    source: str  # sport / seed / admin:<name> / espn+open-meteo

    @property
    def needs_forecast(self) -> bool:
        return self.roof not in (geo.Roof.FIXED_DOME, geo.Roof.INDOOR) and self.latitude is not None and self.longitude is not None


def _from_row(row: VenueLocation) -> Placement:
    return Placement(row.venue_name, geo.Roof(row.roof_type), row.latitude, row.longitude, row.elevation_m, row.source)


def _from_seed(venue: geo.Venue) -> Placement:
    return Placement(venue.name, venue.roof, venue.latitude, venue.longitude, venue.elevation_m, "seed")


async def stored(session: AsyncSession, family: str, alias: str) -> list[VenueLocation]:
    return list((await session.execute(select(VenueLocation).where(VenueLocation.sport == family, VenueLocation.alias == alias))).scalars())


async def place(session: AsyncSession, settings: Settings, fixture: Fixture, espn_venue: Mapping[str, Any] | None,
                http: httpx.AsyncClient | None, now: datetime) -> Placement | None:
    if geo.is_indoor_sport(fixture.sport_key, settings.WIRE_INDOOR_SPORT_PREFIXES):
        return Placement(None, geo.Roof.INDOOR, None, None, None, "sport")
    family, alias = fixture.family, geo.normalize(fixture.home)
    rows = await stored(session, family, alias)
    admin = next((r for r in rows if r.source.startswith("admin")), None)
    if admin is not None:
        return _from_row(admin)
    seed = geo.resolve_seed(fixture.home, fixture.sport_key, settings.WIRE_GEOCODER_FUZZY_CUTOFF) or geo.resolve_tournament(fixture.sport_key)
    if seed is not None:
        return _from_seed(seed)
    learnt = next((r for r in rows if r.source == ESPN_SOURCE), None)
    if learnt is not None and (espn_venue is None or espn_venue.get("name") == learnt.venue_name):
        return _from_row(learnt)
    if espn_venue is None or not espn_venue.get("city") or http is None:
        return None
    place_ = await open_meteo.geocode(http, settings, str(espn_venue["city"]), espn_venue.get("country"), espn_venue.get("state"))
    if place_ is None:
        return None
    roof = geo.Roof.FIXED_DOME if espn_venue.get("indoor") else geo.Roof.OPEN_AIR
    surface = None if espn_venue.get("grass") is None else (geo.Surface.NATURAL_GRASS if espn_venue["grass"] else geo.Surface.ARTIFICIAL_TURF)
    if learnt is None:
        learnt = VenueLocation(sport=family, alias=alias, team_name=fixture.home, created_at=now)
        session.add(learnt)
    learnt.venue_name, learnt.city, learnt.country = str(espn_venue["name"])[:160], str(espn_venue["city"])[:96], (espn_venue.get("country") or place_.country)
    learnt.latitude, learnt.longitude, learnt.elevation_m = round(place_.latitude, 4), round(place_.longitude, 4), place_.elevation_m
    learnt.roof_type, learnt.surface_type, learnt.source, learnt.espn_venue_id = roof.value, None if surface is None else surface.value, ESPN_SOURCE, espn_venue.get("id")
    learnt.updated_at = now
    await session.flush()
    return _from_row(learnt)
