"""The 3-tier refresh cadence of the pre-match board (Group 101, point b.13a). Developed for Amit Ashok Kumar Patnaik.

Pre-match prices drift slowly far from kickoff and sharply close to it, so how old a stored board may get depends on how
far its fixture is from kickoff *now* (a board fetched when the match was 30h away is due within the hour once the match
is 20h away):

* tier 1, kickoff more than ``PRE_MATCH_TIER1_KICKOFF_SECONDS`` away: ``PRE_MATCH_TIER1_MAX_AGE_SECONDS`` (6h);
* tier 2, more than ``PRE_MATCH_TIER2_KICKOFF_SECONDS`` away: ``PRE_MATCH_TIER2_MAX_AGE_SECONDS`` (60 min);
* tier 3, closer or already in play: ``PRE_MATCH_TIER3_MAX_AGE_SECONDS`` (60 s).

A ``stretch`` factor (the Group 92 quota cache shield's interval multiplier, 1.0 when the shield is off) lengthens every
tier alike. Pure functions: no I/O, every threshold from Settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.core.config import Settings

Tier = Literal[1, 2, 3]


def aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored time is UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def tier_for(seconds_to_kickoff: float, settings: Settings) -> Tier:
    if seconds_to_kickoff > settings.PRE_MATCH_TIER1_KICKOFF_SECONDS:
        return 1
    if seconds_to_kickoff > settings.PRE_MATCH_TIER2_KICKOFF_SECONDS:
        return 2
    return 3


def max_age_seconds(tier: Tier, settings: Settings, stretch: float = 1.0) -> float:
    base = {1: settings.PRE_MATCH_TIER1_MAX_AGE_SECONDS, 2: settings.PRE_MATCH_TIER2_MAX_AGE_SECONDS, 3: settings.PRE_MATCH_TIER3_MAX_AGE_SECONDS}[tier]
    return base * max(1.0, stretch)


@dataclass(frozen=True, slots=True)
class Freshness:
    """Where one stored board stands against its tier."""

    tier: Tier
    age_seconds: float
    max_age_seconds: float
    next_refresh_at: datetime

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > self.max_age_seconds


def freshness(commence_time: datetime, last_polled_at: datetime, now: datetime, settings: Settings, stretch: float = 1.0) -> Freshness:
    now, commence, polled = aware(now), aware(commence_time), aware(last_polled_at)
    tier = tier_for((commence - now).total_seconds(), settings)
    limit = max_age_seconds(tier, settings, stretch)
    return Freshness(tier, max(0.0, (now - polled).total_seconds()), limit, polled + timedelta(seconds=limit))


def sport_is_due(nearest_kickoff: datetime | None, last_polled_at: datetime | None, now: datetime, settings: Settings, stretch: float = 1.0) -> bool:
    """Whether one sport's board should be bought again. Never fetched: due. No fixture stored (off season, or a board
    that came back empty): tier 1, so new fixtures are still found within its max age."""
    if last_polled_at is None:
        return True
    if nearest_kickoff is None:
        limit = max_age_seconds(1, settings, stretch)
        return (aware(now) - aware(last_polled_at)).total_seconds() >= limit - 1.0  # one second of scheduler slack, as the fleet's is_due
    fresh = freshness(nearest_kickoff, last_polled_at, now, settings, stretch)
    return fresh.age_seconds >= fresh.max_age_seconds - 1.0


def due_sports(
    sports: list[str],
    nearest_kickoff: Mapping[str, datetime],
    last_polled: Mapping[str, datetime],
    now: datetime,
    settings: Settings,
    stretch: float = 1.0,
) -> list[str]:
    """The sports (in the caller's order) whose board is due."""
    return [s for s in sports if sport_is_due(nearest_kickoff.get(s), last_polled.get(s), now, settings, stretch)]
