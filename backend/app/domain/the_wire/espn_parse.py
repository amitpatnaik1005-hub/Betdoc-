"""ESPN's public scoreboard and match summary, parsed (Group 78). Pure: ``app/adapters/wire/espn.py`` fetches.

    scoreboard  /<sport>/<league>/scoreboard?dates=YYYYMMDD    events: teams, score, clock, venue, card and penalty events
    summary     /<sport>/<league>/summary?event=<id>           team sheets (rosters with starters), injuries, officials

What each league carries differs, and nothing missing is filled in: soccer summaries name no referee and list no
injuries (team sheets appear once published); the NFL, NBA, MLB and NHL list injuries; the NFL and NHL name
officials. ``match_fixture`` pairs an ESPN event with the odds feed's fixture by both team names and kickoff.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.the_wire.venue_geocoder import normalize

STATUS_MAP = {"pre": "SCHEDULED", "in": "LIVE", "post": "FT"}
INJURY_STATUS = {"out": "OUT", "injured reserve": "OUT", "suspension": "SUSPENDED", "suspended": "SUSPENDED", "doubtful": "DOUBTFUL", "questionable": "QUESTIONABLE",
                 "day-to-day": "QUESTIONABLE"}


@dataclass(frozen=True, slots=True)
class EspnVenue:
    espn_id: str | None
    name: str
    city: str | None
    state: str | None
    country: str | None
    indoor: bool | None
    grass: bool | None


@dataclass(frozen=True, slots=True)
class TeamDiscipline:
    yellow: int = 0
    red: int = 0
    penalties: int = 0  # events ESPN flags as penalty kicks


@dataclass(frozen=True, slots=True)
class EspnEvent:
    event_id: str
    league: str  # the ESPN path, "soccer/eng.1"
    start: datetime
    home: str
    away: str
    home_id: str | None
    away_id: str | None
    home_score: int
    away_score: int
    status: str  # SCHEDULED / LIVE / FT
    detail: str | None  # "FT", "45'+2'", "Q3 4:12"
    clock: str | None
    period: int | None
    venue: EspnVenue | None
    discipline: dict[str, TeamDiscipline] = field(default_factory=dict)  # "HOME" / "AWAY"

    @property
    def completed(self) -> bool:
        return self.status == "FT"


@dataclass(frozen=True, slots=True)
class EspnAbsence:
    side: str  # HOME / AWAY
    player: str
    position: str | None
    status: str  # OUT / SUSPENDED / DOUBTFUL / QUESTIONABLE
    nature: str | None
    return_date: str | None


@dataclass(frozen=True, slots=True)
class EspnSummary:
    event_id: str
    sheets_listed: bool  # the league carries team sheets for this event (else a missing sheet says nothing)
    sheets: dict[str, bool]  # side -> a published team sheet (a roster with starters)
    injuries_listed: bool  # the league carries injury lists for this event (an empty list then means none)
    absences: tuple[EspnAbsence, ...]
    officials: tuple[str, ...]  # the referee first when ESPN orders them
    venue: EspnVenue | None


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_venue(raw: Any) -> EspnVenue | None:
    if not isinstance(raw, Mapping) or not raw.get("fullName"):
        return None
    address = raw.get("address") or {}
    return EspnVenue(espn_id=str(raw["id"]) if raw.get("id") is not None else None, name=str(raw["fullName"]), city=address.get("city"), state=address.get("state"),
                     country=address.get("country"), indoor=raw.get("indoor") if isinstance(raw.get("indoor"), bool) else None,
                     grass=raw.get("grass") if isinstance(raw.get("grass"), bool) else None)


def _discipline(details: Iterable[Any], side_of: Mapping[str, str]) -> dict[str, TeamDiscipline]:
    counts: dict[str, list[int]] = {"HOME": [0, 0, 0], "AWAY": [0, 0, 0]}
    for d in details:
        if not isinstance(d, Mapping):
            continue
        side = side_of.get(str((d.get("team") or {}).get("id")))
        if side is None:
            continue
        if d.get("yellowCard"):
            counts[side][0] += 1
        if d.get("redCard"):
            counts[side][1] += 1
        if d.get("penaltyKick") and not d.get("shootout"):
            counts[side][2] += 1
    return {side: TeamDiscipline(*c) for side, c in counts.items()}


def parse_scoreboard(payload: Mapping[str, Any], league: str) -> list[EspnEvent]:
    out: list[EspnEvent] = []
    for ev in payload.get("events") or []:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            home, away = sides.get("home"), sides.get("away")
            start = _time(ev.get("date") or comp.get("date"))
            if home is None or away is None or start is None:
                continue
            status = ev.get("status") or comp.get("status") or {}
            state = (status.get("type") or {}).get("state")
            side_of = {str((home.get("team") or {}).get("id")): "HOME", str((away.get("team") or {}).get("id")): "AWAY"}
            out.append(EspnEvent(
                event_id=str(ev["id"]), league=league, start=start,
                home=str((home.get("team") or {}).get("displayName") or ""), away=str((away.get("team") or {}).get("displayName") or ""),
                home_id=str((home.get("team") or {}).get("id")) if (home.get("team") or {}).get("id") is not None else None,
                away_id=str((away.get("team") or {}).get("id")) if (away.get("team") or {}).get("id") is not None else None,
                home_score=_int(home.get("score")), away_score=_int(away.get("score")),
                status=STATUS_MAP.get(state, "SCHEDULED"), detail=(status.get("type") or {}).get("shortDetail"),
                clock=status.get("displayClock") if state == "in" else None, period=status.get("period") if state == "in" else None,
                venue=parse_venue(comp.get("venue") or ev.get("venue")),
                discipline=_discipline(comp.get("details") or [], side_of) if state in ("in", "post") else {},
            ))
        except (KeyError, TypeError, AttributeError, IndexError):
            continue
    return out


def _injury_status(raw: Mapping[str, Any]) -> str | None:
    for text in ((raw.get("type") or {}).get("description"), raw.get("status")):
        if isinstance(text, str) and text.casefold() in INJURY_STATUS:
            return INJURY_STATUS[text.casefold()]
    return None


def parse_summary(payload: Mapping[str, Any], event_id: str, home_id: str | None, away_id: str | None) -> EspnSummary:
    side_of = {k: v for k, v in ((home_id, "HOME"), (away_id, "AWAY")) if k}
    sheets = {"HOME": False, "AWAY": False}
    for roster in payload.get("rosters") or []:
        side = side_of.get(str((roster.get("team") or {}).get("id"))) or {"home": "HOME", "away": "AWAY"}.get(roster.get("homeAway"))
        if side and any(p.get("starter") for p in roster.get("roster") or [] if isinstance(p, Mapping)):
            sheets[side] = True
    absences: list[EspnAbsence] = []
    injuries = payload.get("injuries")
    for team in injuries or []:
        side = side_of.get(str((team.get("team") or {}).get("id")))
        if side is None:
            continue
        for raw in team.get("injuries") or []:
            status = _injury_status(raw)
            athlete = raw.get("athlete") or {}
            name = athlete.get("displayName") or athlete.get("fullName")
            if status is None or not name:
                continue
            details = raw.get("details") or {}
            absences.append(EspnAbsence(side=side, player=str(name), position=(athlete.get("position") or {}).get("abbreviation"), status=status,
                                        nature=details.get("type"), return_date=details.get("returnDate")))
    info = payload.get("gameInfo") or {}
    officials = tuple(str(o.get("displayName") or o.get("fullName")) for o in sorted(info.get("officials") or [], key=lambda o: (o.get("order") or 0))
                      if o.get("displayName") or o.get("fullName"))
    return EspnSummary(event_id=event_id, sheets_listed=isinstance(payload.get("rosters"), list), sheets=sheets, injuries_listed=isinstance(injuries, list), absences=tuple(absences), officials=officials,
                       venue=parse_venue(info.get("venue")))


GENERIC_WORDS = frozenset({"united", "city", "town", "athletic", "rovers", "wanderers", "county", "hotspur", "albion", "sporting"})


def name_similarity(a: str, b: str) -> float:
    """1 for the same club however written, 0 for two clubs, else the character ratio. 'Leeds' and 'Leeds United' are
    one club (the extra words are generic); 'Milan' and 'Inter Milan' are not; 'Manchester City' and 'Manchester
    United' are two (each has a word the other lacks). Precision over recall: an unpaired fixture only misses ESPN."""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    wx, wy = set(x.split()) - {"and"}, set(y.split()) - {"and"}
    if wx - wy and wy - wx:
        return 0.0  # each name has a word the other lacks: 'Manchester City' and 'Manchester United' are two clubs
    short, long_ = (wx, wy) if len(wx) <= len(wy) else (wy, wx)
    if short <= long_ and (len(short) >= 2 or (long_ - short) <= GENERIC_WORDS):
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def match_fixture(event: EspnEvent, fixtures: Sequence[tuple[str, str, str, datetime]], *, cutoff: float, tolerance: timedelta) -> str | None:
    """The fixture id (of ``(id, home, away, kickoff)``) this ESPN event is: both sides at or over ``cutoff``, kickoffs within ``tolerance``."""
    best: tuple[float, str] | None = None
    for fixture_id, home, away, kickoff in fixtures:
        if abs(kickoff - event.start) > tolerance:
            continue
        score = min(name_similarity(event.home, home), name_similarity(event.away, away))
        if score >= cutoff and (best is None or score > best[0]):
            best = (score, fixture_id)
    return None if best is None else best[1]
