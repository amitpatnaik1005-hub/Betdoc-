"""Venue weather, every ``WIRE_WEATHER_SCAN_MINUTES``: each tracked fixture's match window forecast, its friction
factor (``app/domain/the_wire/weather_impact.py``), a ``wire_weather_snapshots`` row and the fortress's weather
section.

An arena sport or a fixed roof is written as indoor (the weather pillar passes on it). An open or retractable
venue needs Open-Meteo's hours for the window; without them (the API down, kickoff beyond its 16 days) nothing is
written and the pillar stays UNVERIFIED. A fixture with no known venue is counted and skipped, never given a
default climate. Venues sharing a grid point share one forecast request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.wire import open_meteo
from app.core.config import Settings
from app.domain.the_wire import weather_impact as wi
from app.models.the_wire import WeatherSnapshot
from app.schemas.the_wire import WeatherReport
from app.schemas.twin import FixtureIntel, WeatherIntel
from app.services.the_wire import live
from app.services.the_wire.espn_sync import read_links
from app.services.the_wire.fixtures import Fixture, match_hours, tracked
from app.services.the_wire.venues import Placement, place
from app.services.twin.intel import write_intel

logger = logging.getLogger("betdoc.vidur.weather")


def snapshot_row(fixture: Fixture, placement: Placement, conditions: wi.Conditions | None, impact: wi.Impact, hours: float, elevation: float | None,
                 now: datetime, source: str) -> WeatherSnapshot:
    c = conditions
    indoor = placement.roof in (wi.Roof.FIXED_DOME, wi.Roof.INDOOR)
    return WeatherSnapshot(
        fixture_id=fixture.fixture_id, sport_key=fixture.sport_key, home_team=fixture.home, away_team=fixture.away, kickoff_at=fixture.kickoff,
        venue_name=placement.venue_name, venue_source=placement.source, roof_type=placement.roof.value, latitude=placement.latitude, longitude=placement.longitude,
        elevation_m=elevation, window_hours=hours, temp_c=None if c is None else c.temperature_c, humidity_pct=None if c is None else c.humidity_pct,
        wind_speed_kmh=None if c is None else c.wind_kmh, wind_direction_deg=None if c is None else c.wind_direction_deg,
        wind_cardinal=None if c is None else wi.cardinal(c.wind_direction_deg), precipitation_mmh=None if c is None else c.precipitation_mmh,
        snow=False if c is None else c.snow, weather_code=None if c is None else c.weather_code,
        condition=("Indoor" if indoor and c is None else wi.condition_text(c.weather_code, c.snow) if c is not None else "Unknown"),
        pitch_impact_score=impact.factor, factors=impact.factors, is_indoor_dome=indoor, roof_may_close=impact.roof_may_close, dew_expected=impact.dew_expected,
        tactical_advisory=impact.advisory, source=source, fetched_at=now,
    )


def intel_of(row: WeatherSnapshot, now: datetime) -> WeatherIntel:
    if row.is_indoor_dome:
        return WeatherIntel(source=row.source[:64], observed_at=now, indoor=True, altitude_m=row.elevation_m)
    return WeatherIntel(source=row.source[:64], observed_at=now, indoor=False, wind_kmh=min(400.0, row.wind_speed_kmh or 0.0),
                        precipitation_mmh=min(500.0, row.precipitation_mmh or 0.0), temperature_c=None if row.temp_c is None else max(-60.0, min(60.0, row.temp_c)),
                        altitude_m=None if row.elevation_m is None else max(-500.0, min(6000.0, row.elevation_m)), dew_expected=row.dew_expected)


def report_of(row: WeatherSnapshot) -> WeatherReport:
    return WeatherReport(match_id=row.fixture_id, temperature_c=row.temp_c, condition=row.condition, wind_speed_kmh=row.wind_speed_kmh, wind_cardinal=row.wind_cardinal,
                         humidity_pct=row.humidity_pct, precipitation_mmh=row.precipitation_mmh, pitch_impact_score=row.pitch_impact_score,
                         is_indoor_dome=row.is_indoor_dome, roof_may_close=row.roof_may_close, tactical_advisory=row.tactical_advisory, venue_name=row.venue_name,
                         kickoff_at=row.kickoff_at, fetched_at=row.fetched_at)


def snapshot_dict(row: WeatherSnapshot) -> dict[str, Any]:
    return {**report_of(row).model_dump(mode="json"), "home_team": row.home_team, "away_team": row.away_team, "sport_key": row.sport_key, "roof_type": row.roof_type,
            "venue_source": row.venue_source, "latitude": row.latitude, "longitude": row.longitude, "elevation_m": row.elevation_m, "window_hours": row.window_hours,
            "wind_direction_deg": row.wind_direction_deg, "snow": row.snow, "weather_code": row.weather_code, "factors": row.factors, "dew_expected": row.dew_expected,
            "source": row.source}


async def latest(session: AsyncSession, fixture_ids: list[str]) -> dict[str, WeatherSnapshot]:
    if not fixture_ids:
        return {}
    rows = (await session.execute(select(WeatherSnapshot).where(WeatherSnapshot.fixture_id.in_(fixture_ids)).order_by(WeatherSnapshot.fetched_at))).scalars()
    return {r.fixture_id: r for r in rows}  # the newest wins


async def scan(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *,
               http: httpx.AsyncClient | None = None, fixtures: list[Fixture] | None = None) -> dict[str, Any]:
    fixtures = fixtures if fixtures is not None else await tracked(redis, settings, now)
    policy = wi.WeatherPolicy.from_settings(settings)
    links = await read_links(redis, settings, [f.fixture_id for f in fixtures])
    report = {"fixtures": len(fixtures), "indoor": 0, "forecast": 0, "no_venue": 0, "no_forecast": 0}
    forecasts: dict[tuple[float, float], open_meteo.Forecast | None] = {}
    frames = []
    own = http is None
    http = http or open_meteo.client(settings)
    try:
        async with sessions() as session:
            for fixture in fixtures:
                espn_venue = (links.get(fixture.fixture_id) or {}).get("venue")
                try:
                    placement = await place(session, settings, fixture, espn_venue, http, now)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("VIDUR: geocoding %s failed (%s)", fixture.home, type(exc).__name__)
                    placement = None
                if placement is None:
                    report["no_venue"] += 1
                    continue
                hours = match_hours(settings, fixture.sport_key)
                if not placement.needs_forecast:
                    impact = wi.impact(None, placement.roof, placement.elevation_m, fixture.sport_key, policy)
                    row = snapshot_row(fixture, placement, None, impact, hours, placement.elevation_m, now, f"wire:{placement.source}")
                    report["indoor"] += 1
                else:
                    point = (round(placement.latitude or 0.0, 2), round(placement.longitude or 0.0, 2))
                    if point not in forecasts:
                        try:
                            forecasts[point] = await open_meteo.forecast(http, settings, point[0], point[1], fixture.kickoff + timedelta(hours=hours), now)
                        except (httpx.HTTPError, ValueError, KeyError) as exc:
                            logger.warning("VIDUR: forecast for %s unavailable (%s)", placement.venue_name, type(exc).__name__)
                            forecasts[point] = None
                    fc = forecasts[point]
                    window = [] if fc is None else open_meteo.window(fc, fixture.kickoff, fixture.kickoff + timedelta(hours=hours))
                    conditions = wi.summarize(window, policy)
                    if fc is None or conditions is None:
                        report["no_forecast"] += 1
                        continue
                    elevation = fc.elevation_m if fc.elevation_m is not None else placement.elevation_m
                    impact = wi.impact(conditions, placement.roof, elevation, fixture.sport_key, policy)
                    row = snapshot_row(fixture, placement, conditions, impact, hours, elevation, now, "open-meteo")
                    report["forecast"] += 1
                session.add(row)
                if redis is not None:
                    await write_intel(redis, settings, fixture.fixture_id, FixtureIntel(weather=intel_of(row, now)))
                frames.append({"type": "weather", **report_of(row).model_dump(mode="json")})
            await session.commit()
    finally:
        if own:
            await http.aclose()
    await live.publish(redis, settings, frames)
    return report
