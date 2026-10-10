"""Open-Meteo (free, no key): the hourly forecast at a venue and the geocoder for a venue's city (Group 78)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.domain.the_wire.weather_impact import Hour

HOURLY = "temperature_2m,relative_humidity_2m,precipitation,snowfall,wind_speed_10m,wind_direction_10m,weather_code"
MAX_FORECAST_DAYS = 16
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta", "MB": "Manitoba",
}
COUNTRY_ALIASES = {"usa": "us", "united states": "us", "england": "gb", "scotland": "gb", "wales": "gb", "northern ireland": "gb", "united kingdom": "gb"}


@dataclass(frozen=True, slots=True)
class Forecast:
    latitude: float
    longitude: float
    elevation_m: float | None
    timezone: str | None
    utc_offset_seconds: int
    hours: list[tuple[datetime, Hour]]  # (UTC start of the hour, the reading at the venue's local time)


@dataclass(frozen=True, slots=True)
class Place:
    latitude: float
    longitude: float
    elevation_m: float | None
    timezone: str | None
    name: str
    country: str | None


def client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.WIRE_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": settings.WIRE_USER_AGENT}, follow_redirects=True)


def parse_forecast(payload: dict[str, Any]) -> Forecast:
    hourly = payload.get("hourly") or {}
    offset = int(payload.get("utc_offset_seconds") or 0)
    times = hourly.get("time") or []
    rows: list[tuple[datetime, Hour]] = []
    def value(name: str, i: int) -> float | None:
        values = hourly.get(name) or []
        raw = values[i] if i < len(values) else None
        return None if raw is None else float(raw)

    for i, stamp in enumerate(times):
        temp, wind, rain = value("temperature_2m", i), value("wind_speed_10m", i), value("precipitation", i)
        if temp is None or wind is None or rain is None:
            continue  # an hour the model has not filled is skipped, never zeroed
        local = datetime.fromisoformat(stamp)
        rows.append((local.replace(tzinfo=UTC) - timedelta(seconds=offset), Hour(
            local=local, temperature_c=temp, humidity_pct=value("relative_humidity_2m", i) or 0.0, precipitation_mm=rain,
            snowfall_cm=value("snowfall", i) or 0.0, wind_kmh=wind, wind_direction_deg=value("wind_direction_10m", i) or 0.0,
            weather_code=int(value("weather_code", i) or 0),
        )))
    elevation = payload.get("elevation")
    return Forecast(float(payload["latitude"]), float(payload["longitude"]), None if elevation is None or (isinstance(elevation, float) and math.isnan(elevation)) else float(elevation),
                    payload.get("timezone"), offset, rows)


async def forecast(http: httpx.AsyncClient, settings: Settings, latitude: float, longitude: float, until: datetime, now: datetime) -> Forecast:
    days = min(MAX_FORECAST_DAYS, max(1, math.ceil((until - now).total_seconds() / 86400) + 1))
    response = await http.get(settings.WIRE_OPEN_METEO_FORECAST_URL, params={
        "latitude": latitude, "longitude": longitude, "hourly": HOURLY, "timezone": "auto", "forecast_days": days, "wind_speed_unit": "kmh",
    })
    response.raise_for_status()
    return parse_forecast(response.json())


def window(fc: Forecast, start: datetime, end: datetime) -> list[Hour]:
    """The hours that overlap [start, end): the hour kickoff falls in through the last hour of play."""
    return [h for at, h in fc.hours if at + timedelta(hours=1) > start and at < end]


def pick_place(results: list[dict[str, Any]], country: str | None, region: str | None) -> Place | None:
    """The geocoder's result in the right country (and region when ESPN gave one); with neither, nothing is guessed."""
    want_country = (country or "").casefold()
    want_code = COUNTRY_ALIASES.get(want_country, want_country if len(want_country) == 2 else "")
    want_region = US_STATES.get((region or "").upper(), region or "").casefold()
    for row in results:
        names = {str(row.get(k) or "").casefold() for k in ("country", "admin1", "admin2")}
        code = str(row.get("country_code") or "").casefold()
        country_ok = bool(want_country) and (want_country in names or (want_code and code == want_code))
        region_ok = not want_region or want_region in names
        if (country_ok and region_ok) or (not want_country and want_region and want_region in names):
            elevation = row.get("elevation")
            return Place(float(row["latitude"]), float(row["longitude"]), None if elevation is None else float(elevation), row.get("timezone"),
                         str(row.get("name") or ""), row.get("country"))
    return None


async def geocode(http: httpx.AsyncClient, settings: Settings, city: str, country: str | None, region: str | None) -> Place | None:
    response = await http.get(settings.WIRE_OPEN_METEO_GEOCODING_URL, params={"name": city, "count": 10, "language": "en", "format": "json"})
    response.raise_for_status()
    return pick_place(response.json().get("results") or [], country, region)
