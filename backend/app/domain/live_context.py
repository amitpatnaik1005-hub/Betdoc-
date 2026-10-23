"""Fixture context from the sports-data feeds (Group 112, point b.13). Developed for Amit Ashok Kumar Patnaik.

The Odds API prices a fixture; Football-Data.org and Sportmonks say when it really kicks off, how it stands (scheduled,
in play, finished, postponed), its score and where both sides sit in their table. This module pairs one feed's fixtures
to the stored Odds API fixtures and turns a pair into the context the match board shows. Pure: no I/O, every threshold
passed in.

Pairing is precision over recall (an unpaired fixture only goes without context; a wrong pair would show another match's
score): within ``window`` of each other, both sides must reach ``cutoff`` under the Wire's ``name_similarity`` (each
provider's full and short name tried); or, at the same kickoff (within ``tolerance``) in the same competition, one side
reaching ``cutoff`` and the other ``loose`` is enough, because a club plays one match at a time ("Bayern Munich" and "FC
Bayern München" share no word). A provider fixture pairs with at most one stored fixture.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.domain.the_wire.espn_parse import name_similarity

FixtureStatus = Literal["SCHEDULED", "IN_PLAY", "PAUSED", "FINISHED", "POSTPONED", "SUSPENDED", "CANCELLED", "UNKNOWN"]
KickoffCheck = Literal["CONFIRMED", "MOVED"]
LOOSE_SIMILARITY = 0.5  # the other side's floor when the kickoffs agree


@dataclass(frozen=True, slots=True)
class ProviderFixture:
    provider: str  # "football-data.org" | "sportmonks"
    provider_id: str
    sport_key: str  # the Odds API sport key its competition is paired against
    competition: str | None
    kickoff: datetime  # UTC
    home: tuple[str, ...]  # the provider's names for the side, full name first
    away: tuple[str, ...]
    status: FixtureStatus
    home_score: int | None = None
    away_score: int | None = None
    home_position: int | None = None  # league position, where the feed gives it with the fixture
    away_position: int | None = None
    updated_at: datetime | None = None
    # Group 113: the score after 90 minutes plus stoppage time, what bets settle on; None when the feed does not give it
    # (a match decided in extra time or on penalties whose regular-time score is missing)
    regulation: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class StoredFixture:
    fixture_id: str  # the canonical id the live market and the match board use
    sport_key: str
    home: str
    away: str
    kickoff: datetime


@dataclass(frozen=True, slots=True)
class TableRow:
    team: tuple[str, ...]
    position: int
    points: int | None = None
    played: int | None = None


@dataclass(slots=True)
class Pairing:
    pairs: dict[str, ProviderFixture] = field(default_factory=dict)  # stored fixture id -> the provider's fixture
    unpaired: int = 0  # stored fixtures of the provider's competitions left without a pair


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def side_similarity(stored: str, names: Sequence[str]) -> float:
    return max((name_similarity(stored, n) for n in names if n), default=0.0)


def pair(stored: Sequence[StoredFixture], provided: Sequence[ProviderFixture], *, cutoff: float, window: timedelta, tolerance: timedelta) -> Pairing:
    """Each stored fixture's provider fixture, best first: the higher weaker-side similarity, then the nearer kickoff."""
    covered = {p.sport_key for p in provided}
    candidates: list[tuple[float, float, str, int]] = []
    for s in stored:
        for i, p in enumerate(provided):
            if p.sport_key != s.sport_key:
                continue
            gap = abs(_aware(p.kickoff) - _aware(s.kickoff))
            if gap > window:
                continue
            home, away = side_similarity(s.home, p.home), side_similarity(s.away, p.away)
            weaker, stronger = min(home, away), max(home, away)
            if weaker >= cutoff or (gap <= tolerance and stronger >= cutoff and weaker >= LOOSE_SIMILARITY):
                candidates.append((weaker, -gap.total_seconds(), s.fixture_id, i))
    result = Pairing()
    taken: set[int] = set()
    for _, _, fixture_id, i in sorted(candidates, reverse=True):
        if fixture_id in result.pairs or i in taken:
            continue
        result.pairs[fixture_id] = provided[i]
        taken.add(i)
    result.unpaired = sum(1 for s in stored if s.sport_key in covered and s.fixture_id not in result.pairs)
    return result


def table_position(table: Sequence[TableRow], team: str, cutoff: float) -> TableRow | None:
    """The team's row, when exactly one row reaches ``cutoff`` at the best similarity (a tie means no answer)."""
    scored = sorted(((side_similarity(team, row.team), row) for row in table), key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < cutoff or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def context(stored: StoredFixture, provided: ProviderFixture, *, tolerance: timedelta, tables: Mapping[str, Sequence[TableRow]],
            cutoff: float, checked_at: datetime) -> dict[str, Any]:
    """The board's context for one pair: the kickoff check, the status and score, and both sides' table positions (the
    fixture's own positions where the feed gives them, else its competition's table)."""
    gap = _aware(provided.kickoff) - _aware(stored.kickoff)
    check: KickoffCheck = "CONFIRMED" if abs(gap) <= tolerance else "MOVED"
    table = tables.get(provided.sport_key) or ()

    def standing(name: str, given: int | None) -> dict[str, Any] | None:
        if given is not None:
            return {"position": given, "points": None, "played": None}
        row = table_position(table, name, cutoff) if table else None
        return None if row is None else {"position": row.position, "points": row.points, "played": row.played}

    scored = provided.home_score is not None and provided.away_score is not None
    return {
        "provider": provided.provider,
        "provider_id": provided.provider_id,
        "competition": provided.competition,
        "kickoff": {"check": check, "provider_kickoff": _aware(provided.kickoff).isoformat(), "difference_minutes": round(gap.total_seconds() / 60)},
        "status": provided.status,
        "score": {"home": provided.home_score, "away": provided.away_score} if scored else None,
        "standings": {"home": standing(stored.home, provided.home_position), "away": standing(stored.away, provided.away_position)},
        "provider_updated_at": None if provided.updated_at is None else _aware(provided.updated_at).isoformat(),
        "checked_at": _aware(checked_at).isoformat(),
    }
