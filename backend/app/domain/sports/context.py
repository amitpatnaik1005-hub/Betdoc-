"""Match context: the evidence the twin's pillars hold, read by every sport adapter (Group 79).
Developed for Amit Ashok Kumar Patnaik.

A pricing request carries a free-form ``context`` dict. This module reads the parts the fortress cares about
into typed values, so every adapter adjusts on the same evidence the same way:

* ``weather`` (fortress pillar 2, the brief's "Pillar 4: venue weather & pitch physics"): temperature,
  humidity, wind (and its direction relative to the park), rain, altitude, the local hour and the evening
  dew; Group 78's friction factor ``friction`` is carried through when The Wire supplies it.
* ``absences`` (fortress pillar 4, the brief's "Pillar 5: lineup absences & replacement WAR"): each missing
  player's wins above replacement (season), usage share, position and status. ``war_lost(side)`` weighs
  an OUT player fully, DOUBTFUL by half and QUESTIONABLE by a quarter.
* ``officials`` (fortress pillar 9, the brief's "Pillar 6: official & referee bias"): referee penalty and
  foul rates, an umpire's strike-zone run factor, MMA judges' scoring variance.
* ``model_weights``: pillar 1's calibrated sub-model weights (Group 74); a weight of 0 benches a sub-model.

Nothing is invented: a missing section is ``None`` (or empty) and the adapter applies no adjustment for it,
while ``evidence()`` reports which pillars were actually fed, which the confidence index reads.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _points(share: float) -> float:
    """A share given as a fraction (<= 1) or in percentage points, in percentage points."""
    return share * 100.0 if 0.0 < share <= 1.0 else share


STATUS_WEIGHTS: dict[str, float] = {"out": 1.0, "doubtful": 0.5, "questionable": 0.25, "probable": 0.1, "available": 0.0}


def _f(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


@dataclass(frozen=True, slots=True)
class Weather:
    temperature_c: float | None = None
    humidity_pct: float | None = None
    wind_kph: float | None = None
    wind_direction: str | None = None  # "out" (blowing out to centre field / downwind), "in", "cross"
    precipitation_mm: float | None = None
    indoor: bool = False
    altitude_m: float | None = None
    local_hour: float | None = None
    dew: str | None = None  # none | light | moderate | heavy
    friction: float | None = None  # The Wire's Pi (Group 78): 1 = neutral conditions

    @property
    def wind_mph(self) -> float | None:
        return None if self.wind_kph is None else self.wind_kph / 1.609344

    @property
    def temperature_f(self) -> float | None:
        return None if self.temperature_c is None else self.temperature_c * 9.0 / 5.0 + 32.0

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None, top: Mapping[str, Any]) -> Weather | None:
        data: dict[str, Any] = dict(raw or {})
        for key in ("dew_intensity", "humidity", "local_hour", "altitude_m"):
            if key not in data and key in top:
                data[key] = top[key]
        if not data:
            return None
        temp = _f(data, "temperature_c", "temp_c")
        if temp is None:
            tf = _f(data, "temperature_f", "temp_f")
            temp = None if tf is None else (tf - 32.0) * 5.0 / 9.0
        wind = _f(data, "wind_kph", "wind_kmh")
        if wind is None:
            mph = _f(data, "wind_mph")
            wind = None if mph is None else mph * 1.609344
        dew = data.get("dew") or data.get("dew_intensity")
        if dew is None and data.get("dew_expected") is True:
            dew = "moderate"
        direction = data.get("wind_direction")
        return cls(
            temperature_c=temp,
            humidity_pct=_f(data, "humidity_pct", "humidity"),
            wind_kph=wind,
            wind_direction=str(direction).strip().lower() if direction else None,
            precipitation_mm=_f(data, "precipitation_mm", "precipitation_mmh", "rain_mm"),
            indoor=bool(data.get("indoor", False)),
            altitude_m=_f(data, "altitude_m", "elevation_m"),
            local_hour=_f(data, "local_hour"),
            dew=str(dew).strip().lower() if dew else None,
            friction=_f(data, "friction", "friction_factor", "pi"),
        )


@dataclass(frozen=True, slots=True)
class Absence:
    side: str  # "home" | "away"
    player: str
    war: float = 0.0  # season wins above replacement
    usage_pct: float | None = None
    position: str | None = None
    status: str = "out"
    impact: float | None = None  # The Wire's fortress impact in [0, 1]

    @property
    def weight(self) -> float:
        return STATUS_WEIGHTS.get(self.status, 1.0)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Absence | None:
        side = str(raw.get("side") or raw.get("team") or "").strip().lower()
        if side not in ("home", "away"):
            return None
        return cls(
            side=side,
            player=str(raw.get("player") or raw.get("name") or "unknown")[:96],
            war=max(0.0, _f(raw, "war", "wins_above_replacement") or 0.0),
            usage_pct=_f(raw, "usage_pct", "usage"),
            position=(str(raw["position"]).strip().lower() if raw.get("position") else None),
            status=str(raw.get("status") or "out").strip().lower(),
            impact=_f(raw, "impact"),
        )


@dataclass(frozen=True, slots=True)
class Officials:
    name: str | None = None
    rates: Mapping[str, float] = field(default_factory=dict)

    def rate(self, *keys: str) -> float | None:
        return _f(self.rates, *keys)

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | None) -> Officials | None:
        if not raw:
            return None
        rates = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))}
        name = raw.get("name") or raw.get("referee") or raw.get("umpire")
        return cls(name=str(name)[:96] if name else None, rates=rates)


@dataclass(frozen=True)
class MatchContext:
    raw: Mapping[str, Any]
    weather: Weather | None
    absences: tuple[Absence, ...]
    officials: Officials | None
    model_weights: Mapping[str, float]

    @classmethod
    def parse(cls, context: Mapping[str, Any] | None) -> MatchContext:
        raw: Mapping[str, Any] = context or {}
        absences = tuple(a for a in (Absence.parse(x) for x in raw.get("absences") or [] if isinstance(x, Mapping)) if a is not None)
        officials_raw = raw.get("officials") or raw.get("referee") or raw.get("umpire")
        weights_raw = raw.get("model_weights") or {}
        weights = ({str(k): float(v) for k, v in weights_raw.items() if isinstance(v, (int, float)) and math.isfinite(float(v))}
                   if isinstance(weights_raw, Mapping) else {})
        weather_raw = raw.get("weather")
        return cls(
            raw=raw,
            weather=Weather.parse(weather_raw if isinstance(weather_raw, Mapping) else None, raw),
            absences=absences,
            officials=Officials.parse(officials_raw if isinstance(officials_raw, Mapping) else None),
            model_weights=weights,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def number(self, key: str, default: float) -> float:
        value = _f(self.raw, key)
        return default if value is None else value

    def text(self, key: str, default: str) -> str:
        value = self.raw.get(key)
        return str(value).strip().lower() if value not in (None, "") else default

    def war_lost(self, side: str, positions: frozenset[str] | None = None) -> float:
        """Status-weighted wins above replacement missing for ``side`` (optionally only some positions)."""
        return sum(a.war * a.weight for a in self.absences if a.side == side and (positions is None or (a.position or "") in positions))

    def usage_lost(self, side: str) -> float:
        """Status-weighted usage share missing for ``side``, in percentage points (0.30 and 30 both read as 30)."""
        return sum(_points(a.usage_pct or 0.0) * a.weight for a in self.absences if a.side == side)

    def evidence(self) -> dict[str, bool]:
        """Which pillars this request fed: weather (fortress P2), absences (P4), officials (P9), calibrated weights (P1)."""
        return {"weather": self.weather is not None, "absences": bool(self.absences), "officials": self.officials is not None,
                "model_weights": bool(self.model_weights)}
