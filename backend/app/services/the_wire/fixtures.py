"""The fixtures the Wire follows: every fixture on the live board that is in play or kicks off within
``WIRE_HORIZON_HOURS``. In play means kicked off less than the sport's match window ago (``WIRE_MATCH_HOURS``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.live_odds import read_snapshot
from app.domain.the_wire.venue_geocoder import sport_family


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    home: str
    away: str
    sport_key: str | None
    kickoff: datetime

    @property
    def family(self) -> str:
        return sport_family(self.sport_key)

    @property
    def row(self) -> tuple[str, str, str, datetime]:
        return (self.fixture_id, self.home, self.away, self.kickoff)


def match_hours(settings: Settings, sport_key: str | None) -> float:
    family = sport_family(sport_key)
    return next((float(h) for prefix, h in settings.WIRE_MATCH_HOURS.items() if family.startswith(prefix)), settings.WIRE_MATCH_HOURS_DEFAULT)


async def tracked(redis: Redis | None, settings: Settings, now: datetime) -> list[Fixture]:
    board = await read_snapshot(redis) or []
    out: dict[str, Fixture] = {}
    horizon = now + timedelta(hours=settings.WIRE_HORIZON_HOURS)
    for tick in board:
        if tick.match_id in out or tick.commence_time is None:
            continue
        kickoff = tick.commence_time if tick.commence_time.tzinfo else tick.commence_time.replace(tzinfo=UTC)
        if now - timedelta(hours=match_hours(settings, tick.sport_key)) <= kickoff <= horizon:
            out[tick.match_id] = Fixture(tick.match_id, tick.home_team, tick.away_team, tick.sport_key, kickoff)
    return sorted(out.values(), key=lambda f: (f.kickoff, f.fixture_id))
