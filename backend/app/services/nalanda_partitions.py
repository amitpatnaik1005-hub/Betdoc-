"""Nalanda's partitions: one per ISO week, named for it, created ahead of time, vacuumed, dropped cold.

* Naming. ``<table>_p<iso-year>w<week>``: ``nalanda_ticks_p2026w41`` holds Monday 2026-10-05 00:00 UTC
  up to (not including) Monday 2026-10-12. ISO years: 2026-12-31 is in ``p2026w53``, 2027-01-04 opens
  ``p2027w01``. ``<table>_pdefault`` catches any row whose week was never created.
* Pre-allocation (``preallocate``): the current week and ``NALANDA_WEEKS_AHEAD`` after it, for every
  partitioned table; idempotent. A week whose rows already landed in the default partition is created
  by moving those rows into it inside one transaction (create, move, attach), never by failing.
* Vacuum (``vacuum_partitions``): ``VACUUM (ANALYZE)`` on every closed (older than the current week)
  partition with dead tuples above ``NALANDA_VACUUM_MIN_DEAD_TUPLES`` or never analysed. VACUUM cannot
  run in a transaction: it gets an autocommit connection.

Identifiers are generated here and checked against a strict pattern before any DDL is built; nothing
from a request ever reaches them. PostgreSQL only: on any other database these are no-ops.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.models.nalanda_lake import PARTITIONED_TABLES

_IDENT = re.compile(r"^nalanda_[a-z0-9_]{1,60}$")


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    table: str
    name: str
    lower: datetime
    upper: datetime

    @property
    def bounds_sql(self) -> str:
        return f"FROM ('{self.lower.isoformat()}') TO ('{self.upper.isoformat()}')"


_COLUMNS = frozenset(PARTITIONED_TABLES.values())


def _column(name: str) -> str:
    if name not in _COLUMNS:
        raise ValueError(f"not a Nalanda partition key: {name!r}")
    return name


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"not a Nalanda identifier: {name!r}")
    return name


def week_start(moment: datetime | date) -> datetime:
    """Monday 00:00 UTC of the ISO week containing ``moment``."""
    if isinstance(moment, datetime):
        moment = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).date()
    monday = moment - timedelta(days=moment.isoweekday() - 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


def partition_name(table: str, moment: datetime | date) -> str:
    iso = week_start(moment).isocalendar()
    return _ident(f"{table}_p{iso.year}w{iso.week:02d}")


def default_partition(table: str) -> str:
    return _ident(f"{table}_pdefault")


def week_spec(table: str, moment: datetime | date) -> PartitionSpec:
    lower = week_start(moment)
    return PartitionSpec(_ident(table), partition_name(table, lower), lower, lower + timedelta(days=7))


def upcoming(table: str, now: datetime, weeks_ahead: int) -> list[PartitionSpec]:
    """The current week and the ``weeks_ahead`` after it."""
    first = week_start(now)
    return [week_spec(table, first + timedelta(days=7 * i)) for i in range(weeks_ahead + 1)]


def create_sql(spec: PartitionSpec) -> str:
    return f"CREATE TABLE IF NOT EXISTS {_ident(spec.name)} PARTITION OF {_ident(spec.table)} FOR VALUES {spec.bounds_sql}"


def parse_partition_week(name: str) -> datetime | None:
    """The Monday a ``<table>_p<year>w<week>`` partition starts on (None for anything else)."""
    match = re.search(r"_p(\d{4})w(\d{2})$", name)
    if match is None:
        return None
    monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


# ---------------------------------------------------------------- PostgreSQL
def _is_postgres(conn: AsyncConnection | AsyncEngine) -> bool:
    return conn.dialect.name == "postgresql"


async def partitions_of(conn: AsyncConnection, table: str) -> list[tuple[str, str]]:
    """``(partition, bound expression)`` for every partition attached to ``table``."""
    rows = await conn.execute(
        text(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :table ORDER BY c.relname"
        ),
        {"table": _ident(table)},
    )
    return [(name, bound) for name, bound in rows.all()]


async def _ensure_default(conn: AsyncConnection, table: str) -> bool:
    exists = await conn.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": default_partition(table)})
    if exists:
        return False
    await conn.execute(text(f"CREATE TABLE {default_partition(table)} PARTITION OF {_ident(table)} DEFAULT"))
    return True


async def _create(conn: AsyncConnection, spec: PartitionSpec, column: str) -> str:
    """Create one week. Rows of that week already in the default partition move into it first."""
    if await conn.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": spec.name}):
        return "exists"
    col, default = _column(column), default_partition(spec.table)
    stray = 0
    if await conn.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": default}):
        stray = await conn.scalar(text(f"SELECT count(*) FROM {default} WHERE {col} >= :lo AND {col} < :hi"), {"lo": spec.lower, "hi": spec.upper}) or 0
    if not stray:
        await conn.execute(text(create_sql(spec)))
        return "created"
    # ATTACH refuses a range the default partition still holds rows of: create, move, then attach, in one transaction
    await conn.execute(text(f"CREATE TABLE {spec.name} (LIKE {spec.table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
    await conn.execute(
        text(f"WITH moved AS (DELETE FROM {default} WHERE {col} >= :lo AND {col} < :hi RETURNING *) INSERT INTO {spec.name} SELECT * FROM moved"),
        {"lo": spec.lower, "hi": spec.upper},
    )
    await conn.execute(text(f"ALTER TABLE {spec.table} ATTACH PARTITION {spec.name} FOR VALUES {spec.bounds_sql}"))
    return f"created (moved {stray} row(s) out of the default partition)"


async def preallocate(engine: AsyncEngine, now: datetime, weeks_ahead: int, tables: Sequence[str] | None = None) -> dict[str, list[str]]:
    """Every partitioned table gets its default partition and its next weeks. Returns what was created."""
    created: dict[str, list[str]] = {}
    if not _is_postgres(engine):
        return created
    for table in tables or PARTITIONED_TABLES:
        column = PARTITIONED_TABLES[table]
        done: list[str] = []
        async with engine.begin() as conn:
            if await _ensure_default(conn, table):
                done.append(default_partition(table))
        for spec in upcoming(table, now, weeks_ahead):
            async with engine.begin() as conn:  # one transaction per week: a failed move never blocks the rest
                outcome = await _create(conn, spec, column)
            if outcome != "exists":
                done.append(f"{spec.name}: {outcome}")
        created[table] = done
    return created


async def drop_partition(conn: AsyncConnection, table: str, name: str) -> None:
    await conn.execute(text(f"ALTER TABLE {_ident(table)} DETACH PARTITION {_ident(name)}"))
    await conn.execute(text(f"DROP TABLE {_ident(name)}"))


async def partition_stats(engine: AsyncEngine) -> list[dict[str, object]]:
    """Every Nalanda partition: rows (estimate), bytes, dead tuples, last (auto)vacuum and analyse."""
    if not _is_postgres(engine):
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname, p.relname AS parent, pg_get_expr(c.relpartbound, c.oid), c.reltuples::bigint, pg_total_relation_size(c.oid), "
                "s.n_live_tup, s.n_dead_tup, greatest(s.last_vacuum, s.last_autovacuum), greatest(s.last_analyze, s.last_autoanalyze) "
                "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
                "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE p.relname LIKE 'nalanda\\_%' ORDER BY p.relname, c.relname"
            )
        )
        out = []
        for name, parent, bound, estimate, size, live, dead, vacuumed, analysed in rows.all():
            out.append({
                "partition": name, "table": parent, "bound": bound, "rows_estimate": max(int(estimate or 0), int(live or 0)), "bytes": int(size or 0),
                "dead_tuples": int(dead or 0), "last_vacuum": vacuumed.isoformat() if vacuumed else None, "last_analyze": analysed.isoformat() if analysed else None,
                "week_start": (w.isoformat() if (w := parse_partition_week(name)) else None), "is_default": name.endswith("_pdefault"),
            })
        return out


async def vacuum_partitions(engine: AsyncEngine, now: datetime, min_dead: int) -> list[dict[str, object]]:
    """VACUUM (ANALYZE) the closed partitions that need it. Returns what was vacuumed, and why."""
    if not _is_postgres(engine):
        return []
    current = week_start(now)
    targets = []
    for row in await partition_stats(engine):
        week = parse_partition_week(str(row["partition"]))
        closed = week is not None and week < current
        if not (closed or row["is_default"]):
            continue
        if int(row["dead_tuples"]) >= min_dead or (row["last_analyze"] is None and int(row["rows_estimate"]) > 0):
            targets.append(row)
    done = []
    async with engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for row in targets:
            name = _ident(str(row["partition"]))
            await auto.execute(text(f"VACUUM (ANALYZE) {name}"))
            done.append({"partition": name, "dead_tuples_before": row["dead_tuples"], "reason": "dead tuples" if int(row["dead_tuples"]) >= min_dead else "never analysed"})
    return done
