"""Venue circuit breakers (Group 71): a venue that keeps refusing is paused before it burns more orders.

``ROUTER_BREAKER_FAILURES`` (2) consecutive rejected or timed-out slices at one venue, the first of them
no more than ``ROUTER_BREAKER_WINDOW_SECONDS`` (60 s) before the last, trip its breaker: the venue is
paused for ``ROUTER_BREAKER_PAUSE_SECONDS`` (5 min). The router plans around a paused venue, the slices
already reserved on it are released (never dispatched), and the Sentinel pages CRITICAL. A fill closes
the streak; the pause simply runs out (the sweep then announces the venue live again).

One row per venue, locked ``FOR UPDATE`` for every change: two workers recording failures at once count
both, and exactly one of them sees the trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.execution.venue import CircuitPolicy
from app.models.execution_router import VenueCircuitBreaker

LIVE = "LIVE"
PAUSED = "PAUSED"


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class BreakerState:
    venue_id: str
    state: str  # LIVE | PAUSED
    consecutive_failures: int
    paused_until: datetime | None
    trips: int
    last_failure_reason: str | None
    last_failure_at: datetime | None
    last_success_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        iso = lambda m: m.isoformat() if m else None  # noqa: E731
        return {"venue_id": self.venue_id, "state": self.state, "consecutive_failures": self.consecutive_failures, "paused_until": iso(self.paused_until),
                "trips": self.trips, "last_failure_reason": self.last_failure_reason, "last_failure_at": iso(self.last_failure_at),
                "last_success_at": iso(self.last_success_at)}


def _state(row: VenueCircuitBreaker, now: datetime) -> BreakerState:
    until = _aware(row.paused_until)
    paused = until is not None and until > now
    return BreakerState(row.venue_id, PAUSED if paused else LIVE, int(row.consecutive_failures or 0), until if paused else None, int(row.trips or 0),
                        row.last_failure_reason, _aware(row.last_failure_at), _aware(row.last_success_at))


async def _locked(session: AsyncSession, venue_id: str) -> VenueCircuitBreaker:
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(
        insert(VenueCircuitBreaker).values(venue_id=venue_id, consecutive_failures=0, trips=0, updated_at=datetime.now(UTC)).on_conflict_do_nothing()
    )
    return (await session.execute(
        select(VenueCircuitBreaker).where(VenueCircuitBreaker.venue_id == venue_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one()


async def record_failure(session: AsyncSession, venue_id: str, reason: str, policy: CircuitPolicy, *, now: datetime) -> bool:
    """Count a rejected or timed-out slice (the caller commits). True: this failure tripped the breaker."""
    row = await _locked(session, venue_id)
    started = _aware(row.streak_started_at)
    if row.consecutive_failures <= 0 or started is None or now - started > policy.window:
        row.consecutive_failures, row.streak_started_at = 1, now  # a new streak: the last one is too old to count
    else:
        row.consecutive_failures += 1
    row.last_failure_reason, row.last_failure_at = reason[:500], now
    until = _aware(row.paused_until)
    already_paused = until is not None and until > now
    if row.consecutive_failures >= policy.failures and not already_paused:
        row.paused_until, row.trips = now + policy.pause, int(row.trips or 0) + 1
        row.consecutive_failures, row.streak_started_at = 0, None
        return True
    return False


async def record_success(session: AsyncSession, venue_id: str, *, now: datetime) -> None:
    """A fill: the streak is over (an earlier pause still runs its course)."""
    row = await _locked(session, venue_id)
    row.consecutive_failures, row.streak_started_at, row.last_success_at = 0, None, now


async def reset(session: AsyncSession, venue_id: str, *, now: datetime) -> None:
    """The operator closes the breaker by hand."""
    row = await _locked(session, venue_id)
    row.consecutive_failures, row.streak_started_at, row.paused_until = 0, None, None
    row.updated_at = now


async def paused_venues(session: AsyncSession, venue_ids: Iterable[str], *, now: datetime) -> dict[str, datetime]:
    """The venues among these that are paused right now, with when each pause ends."""
    ids = sorted(set(venue_ids))
    if not ids:
        return {}
    rows = (await session.execute(select(VenueCircuitBreaker).where(VenueCircuitBreaker.venue_id.in_(ids)))).scalars()
    out: dict[str, datetime] = {}
    for row in rows:
        until = _aware(row.paused_until)
        if until is not None and until > now:
            out[row.venue_id] = until
    return out


async def states(session: AsyncSession, *, now: datetime, venue_ids: Iterable[str] = ()) -> list[BreakerState]:
    """Every venue's breaker (plus a LIVE row for each named venue that has never failed)."""
    rows = {r.venue_id: _state(r, now) for r in (await session.execute(select(VenueCircuitBreaker).order_by(VenueCircuitBreaker.venue_id))).scalars()}
    for venue in venue_ids:
        rows.setdefault(venue, BreakerState(venue, LIVE, 0, None, 0, None, None, None))
    return [rows[k] for k in sorted(rows)]


async def expired_pauses(session: AsyncSession, *, now: datetime) -> list[str]:
    """Pauses that ran out: cleared here (the caller commits) so each venue is announced live once."""
    rows = list((await session.execute(
        select(VenueCircuitBreaker).where(VenueCircuitBreaker.paused_until.is_not(None)).order_by(VenueCircuitBreaker.venue_id).with_for_update()
    )).scalars())
    done: list[str] = []
    for row in rows:
        until = _aware(row.paused_until)
        if until is not None and until <= now:
            row.paused_until = None
            done.append(row.venue_id)
    return done
