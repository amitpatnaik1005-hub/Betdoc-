"""Point-in-time discipline for a simulation: nothing a simulated bot reads may postdate its clock.

Two locks, one rule (a row stamped after ``clock.now`` is out of reach):

* ``install_time_lock(session, clock)`` guards a database session. Every ORM ``SELECT`` that touches
  a historical table (``lab_hist_*``) must declare the instant it reads as of
  (``.execution_options(lab_as_of=T)``); a read without one, or one as of a moment after the clock,
  raises ``DataLeakageError`` before any SQL is sent. A declared read is also filtered to
  ``created_at <= T`` on every historical entity it touches, whatever its own ``WHERE`` says. Raw SQL
  against those tables, and any write to them, is refused outright.
* ``SimulationClock.check`` guards the in-memory views (``replay_engine.MarketView``, the FX router):
  asking for any instant after ``now`` raises, and so does any row they would return from after it.

The clock only moves forward, and never past its ceiling (a walk-forward in-sample run's ceiling is
the split: its optimiser cannot see a single out-of-sample row).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, with_loader_criteria
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.util import find_tables

from app.models.lab_quant import LabFixture, LabFixtureResult, LabFxRate, LabOddsTick

AS_OF = "lab_as_of"
TIME_LOCKED = (LabOddsTick, LabFixtureResult, LabFxRate, LabFixture)
_TABLES = {model.__tablename__: model for model in TIME_LOCKED}


class DataLeakageError(RuntimeError):
    """A simulation tried to read data from its own future."""

    def __init__(self, message: str, *, requested: datetime | None = None, now: datetime | None = None, table: str | None = None) -> None:
        super().__init__(message)
        self.requested, self.now, self.table = requested, now, table


class SimulationClock:
    """Simulated time. ``advance`` only moves forward and never past ``ceiling``."""

    __slots__ = ("_now", "ceiling")

    def __init__(self, start: datetime, ceiling: datetime | None = None) -> None:
        if ceiling is not None and start > ceiling:
            raise ValueError("a clock cannot start after its ceiling")
        self._now = start
        self.ceiling = ceiling

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, to: datetime) -> None:
        if to < self._now:
            raise ValueError(f"simulated time does not run backwards ({to.isoformat()} < {self._now.isoformat()})")
        if self.ceiling is not None and to > self.ceiling:
            raise DataLeakageError(f"the simulation may not run past {self.ceiling.isoformat()}", requested=to, now=self._now)
        self._now = to

    def check(self, as_of: datetime, what: str) -> None:
        if as_of > self._now:
            raise DataLeakageError(f"{what} as of {as_of.isoformat()} is in the future of the simulation clock ({self._now.isoformat()})", requested=as_of, now=self._now)

    def check_row(self, created_at: datetime, what: str) -> None:
        if created_at > self._now:
            raise DataLeakageError(f"{what} stamped {created_at.isoformat()} reached a bot at {self._now.isoformat()}", requested=created_at, now=self._now)


def _touched(state: ORMExecuteState) -> set[str]:
    names: set[str] = set()
    for found in find_tables(state.statement, check_columns=True, include_aliases=True, include_crud=True):
        base = getattr(found, "element", found)  # an alias reads its table
        name = getattr(base, "name", None)
        if isinstance(name, str):
            names.add(name)
    names |= {name for mapper in state.all_mappers if isinstance(name := getattr(mapper.local_table, "name", None), str)}
    return names & _TABLES.keys()


def install_time_lock(session: AsyncSession, clock: SimulationClock) -> Callable[[], None]:
    """Guard ``session`` with ``clock``; returns the function that removes the guard."""
    target = session.sync_session

    def guard(state: ORMExecuteState) -> None:
        statement = state.statement
        if isinstance(statement, TextClause):
            text = str(statement).lower()
            hit = sorted(name for name in _TABLES if name in text)
            if hit:
                raise DataLeakageError(f"raw SQL against {', '.join(hit)} cannot be time-locked; read through the ORM with lab_as_of", table=hit[0], now=clock.now)
            return
        touched = _touched(state)
        if not touched:
            return
        if not state.is_select:
            raise DataLeakageError(f"a simulation never writes market history ({', '.join(sorted(touched))})", table=min(touched), now=clock.now)
        as_of = state.execution_options.get(AS_OF)
        if as_of is None:
            raise DataLeakageError(f"an unbounded read of {', '.join(sorted(touched))}: declare lab_as_of", table=min(touched), now=clock.now)
        clock.check(as_of, f"a read of {', '.join(sorted(touched))}")
        for name in sorted(touched):
            model = _TABLES[name]
            statement = statement.options(with_loader_criteria(model, lambda cls: cls.created_at <= as_of, include_aliases=True))
        state.statement = statement

    event.listen(target, "do_orm_execute", guard)
    return lambda: event.remove(target, "do_orm_execute", guard)
