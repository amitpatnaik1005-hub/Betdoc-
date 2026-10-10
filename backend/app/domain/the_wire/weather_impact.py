"""The weather friction factor (Group 78): how much the conditions at kickoff suppress or lift scoring.

    Pi = f_temp(T) x f_wind(W) x f_precip(P) x f_alt(H) x f_dew

* ``f_temp``: under 0 C 0.92, under 10 C 0.96, over 32 C 0.95, otherwise 1 (the brief left 25-32 C undefined;
  it is treated as optimal). Bands from ``WIRE_TEMP_*``.
* ``f_wind = max(floor, 1 - slope x max(0, W - free))``, W the strongest hourly wind (km/h) in the match window.
* ``f_precip``: dry 1, up to ``WIRE_RAIN_HEAVY_MMH`` 0.90, heavier rain or any snow 0.78 (the wettest hour).
* ``f_alt = 1 + slope x min(cap, H)``: thin air carries the ball (Denver, Mexico City).
* ``f_dew``: a cricket match with an hour at or after 19:30 local time over 75% humidity: 1.15 (a wet ball, the
  chasing side favoured).

A fixed dome or an arena sport scores 1 exactly: no weather reaches the field. A retractable roof is scored as
if open and flagged, because whether it closes is decided on the day.

The forecast is Open-Meteo's hourly forecast for the venue over the match window (kickoff plus
``WIRE_MATCH_HOURS`` for the sport). The fortress's weather pillar receives the window's worst hour: the
strongest wind and the heaviest rain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time

from app.core.config import Settings
from app.domain.the_wire.venue_geocoder import Roof

CARDINALS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# WMO weather interpretation codes (Open-Meteo's ``weather_code``)
WMO_TEXT: dict[int, str] = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})


@dataclass(frozen=True, slots=True)
class WeatherPolicy:
    cold_bands: tuple[tuple[float, float], ...]
    hot_above_c: float
    hot_factor: float
    wind_free_kmh: float
    wind_slope: float
    wind_floor: float
    rain_light_factor: float
    rain_heavy_mmh: float
    rain_heavy_factor: float
    altitude_slope: float
    altitude_cap_m: float
    dew_sports: tuple[str, ...]
    dew_factor: float
    dew_humidity_pct: float
    dew_from: time

    @classmethod
    def from_settings(cls, s: Settings) -> WeatherPolicy:
        hh, mm = (int(x) for x in s.WIRE_DEW_LOCAL_FROM.split(":"))
        return cls(
            cold_bands=tuple(sorted((float(b), float(f)) for b, f in s.WIRE_TEMP_COLD_BANDS)), hot_above_c=s.WIRE_TEMP_HOT_ABOVE_C, hot_factor=s.WIRE_TEMP_HOT_FACTOR,
            wind_free_kmh=s.WIRE_WIND_FREE_KMH, wind_slope=s.WIRE_WIND_SLOPE, wind_floor=s.WIRE_WIND_FLOOR,
            rain_light_factor=s.WIRE_RAIN_LIGHT_FACTOR, rain_heavy_mmh=s.WIRE_RAIN_HEAVY_MMH, rain_heavy_factor=s.WIRE_RAIN_HEAVY_FACTOR,
            altitude_slope=s.WIRE_ALTITUDE_SLOPE, altitude_cap_m=s.WIRE_ALTITUDE_CAP_M,
            dew_sports=tuple(p.casefold() for p in s.WIRE_DEW_SPORT_PREFIXES), dew_factor=s.WIRE_DEW_FACTOR, dew_humidity_pct=s.WIRE_DEW_HUMIDITY_PCT,
            dew_from=time(hh, mm),
        )


@dataclass(frozen=True, slots=True)
class Hour:
    """One forecast hour at the venue; ``local`` is the venue's wall-clock time."""

    local: datetime
    temperature_c: float
    humidity_pct: float
    precipitation_mm: float  # in the hour: mm/h
    snowfall_cm: float
    wind_kmh: float
    wind_direction_deg: float
    weather_code: int


@dataclass(frozen=True, slots=True)
class Conditions:
    """The match window summarised: mean temperature and humidity, the worst wind and rain."""

    temperature_c: float
    humidity_pct: float
    precipitation_mmh: float
    snow: bool
    wind_kmh: float
    wind_direction_deg: float
    weather_code: int
    hours: int
    dew_hours: int  # hours at or after the dew time over the humidity threshold


@dataclass(frozen=True, slots=True)
class Impact:
    factor: float
    factors: dict[str, float]
    advisory: str
    roof_may_close: bool
    dew_expected: bool


def cardinal(degrees: float) -> str:
    return CARDINALS[round((degrees % 360) / 45.0) % 8]


def condition_text(code: int, snow: bool = False) -> str:
    text = WMO_TEXT.get(code, f"WMO {code}")
    return text if not snow or code in SNOW_CODES else f"{text}, snow"


def summarize(hours: Sequence[Hour], policy: WeatherPolicy) -> Conditions | None:
    if not hours:
        return None
    windiest = max(hours, key=lambda h: h.wind_kmh)
    n = len(hours)
    return Conditions(
        temperature_c=round(sum(h.temperature_c for h in hours) / n, 1),
        humidity_pct=round(sum(h.humidity_pct for h in hours) / n, 1),
        precipitation_mmh=round(max(h.precipitation_mm for h in hours), 2),
        snow=any(h.snowfall_cm > 0 or h.weather_code in SNOW_CODES for h in hours),
        wind_kmh=round(windiest.wind_kmh, 1),
        wind_direction_deg=round(windiest.wind_direction_deg, 0),
        weather_code=max(h.weather_code for h in hours),
        hours=n,
        dew_hours=sum(1 for h in hours if h.local.time() >= policy.dew_from and h.humidity_pct > policy.dew_humidity_pct),
    )


def f_temp(t: float, p: WeatherPolicy) -> float:
    if t > p.hot_above_c:
        return p.hot_factor
    for bound, factor in p.cold_bands:
        if t < bound:
            return factor
    return 1.0


def f_wind(w: float, p: WeatherPolicy) -> float:
    return max(p.wind_floor, 1.0 - p.wind_slope * max(0.0, w - p.wind_free_kmh))


def f_precip(mmh: float, snow: bool, p: WeatherPolicy) -> float:
    if snow or mmh > p.rain_heavy_mmh:
        return p.rain_heavy_factor
    return p.rain_light_factor if mmh > 0 else 1.0


def f_alt(elevation_m: float | None, p: WeatherPolicy) -> float:
    return 1.0 + p.altitude_slope * min(p.altitude_cap_m, max(0.0, elevation_m or 0.0))


def impact(conditions: Conditions | None, roof: Roof, elevation_m: float | None, sport_key: str | None, p: WeatherPolicy) -> Impact:
    """Pi for the match. Indoors (a dome, an arena sport) is 1 exactly, whatever the sky does."""
    if roof in (Roof.FIXED_DOME, Roof.INDOOR) or conditions is None:
        why = "Indoor arena: the weather cannot reach the match." if roof is Roof.INDOOR else "Fixed roof: a controlled climate, no weather friction."
        if conditions is None and roof not in (Roof.FIXED_DOME, Roof.INDOOR):
            why = "No forecast for the match window."
        return Impact(1.0, {"temp": 1.0, "wind": 1.0, "precip": 1.0, "alt": 1.0, "dew": 1.0}, why, False, False)
    c = conditions
    ft, fw, fp, fa = f_temp(c.temperature_c, p), f_wind(c.wind_kmh, p), f_precip(c.precipitation_mmh, c.snow, p), f_alt(elevation_m, p)
    family = (sport_key or "").split("_", 1)[0].casefold()
    dew = any(family.startswith(s) for s in p.dew_sports) and c.dew_hours > 0
    fd = p.dew_factor if dew else 1.0
    notes = []
    if ft < 1.0:
        notes.append(f"{'heat' if c.temperature_c > p.hot_above_c else 'cold'} {c.temperature_c:g}°C (x{ft:g})")
    if fw < 1.0:
        notes.append(f"wind {c.wind_kmh:g} km/h {cardinal(c.wind_direction_deg)}: long balls and kicks penalised (x{fw:.3f})")
    if fp < 1.0:
        notes.append(("snow" if c.snow else f"rain up to {c.precipitation_mmh:g} mm/h") + f": a slick surface (x{fp:g})")
    if fa > 1.0:
        notes.append(f"altitude {elevation_m:.0f} m: thin air adds carry (x{fa:.3f})")
    if dew:
        notes.append(f"evening dew ({c.dew_hours}h over {p.dew_humidity_pct:g}% humidity): spin loses grip, the chase is favoured (x{fd:g})")
    roof_may_close = roof is Roof.RETRACTABLE
    if roof_may_close:
        notes.append("retractable roof: these apply only if it stays open")
    factor = round(ft * fw * fp * fa * fd, 4)
    advisory = "; ".join(notes) if notes else "Neutral conditions: no weather friction."
    return Impact(factor, {"temp": ft, "wind": round(fw, 4), "precip": fp, "alt": round(fa, 4), "dew": fd}, advisory, roof_may_close, dew)
