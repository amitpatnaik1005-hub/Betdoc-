"""ArchiveManager tests: overview, inventory, dynamic browsing, serialization."""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.domain.archive.errors import ArchiveDomainError, TableNotFoundError
from app.domain.archive.manager import MAX_PAGE_LIMIT, REDACTED, ArchiveManager, _serialize_row
from app.models import Base
from app.models.archive import ArchiveAccessLogModel
from app.schemas.archive import ArchiveLogRead, ArchiveOverviewResponse

pytestmark = pytest.mark.asyncio


class _Colour(Enum):
    RED = "red"


class _Tier(IntEnum):
    GOLD = 1


class _Mode(StrEnum):
    LIVE = "LIVE"


async def _log_count(session, target: str | None = None) -> int:
    statement = select(func.count()).select_from(ArchiveAccessLogModel)
    if target is not None:
        statement = statement.where(ArchiveAccessLogModel.target_resource == target)
    return (await session.execute(statement)).scalar_one()


# ---------------------------------------------------------------- overview


async def test_get_overview_returns_mock_security_payload(manager, db_session, seed_logs):
    await seed_logs(2)
    overview = await manager.get_overview(db_session)

    assert overview["encryption_status"] == "E2E Active"
    assert overview["algorithm"] == "AES-256-GCM"
    assert overview["total_tables"] == len(Base.metadata.tables)
    assert overview["database_status"] == "ONLINE"
    assert overview["probe_latency_ms"] is not None and overview["probe_latency_ms"] >= 0
    assert overview["total_records"] >= 2

    expected_backup = datetime.now(UTC) - timedelta(hours=2)
    assert abs((overview["last_backup_at"] - expected_backup).total_seconds()) < 60

    ArchiveOverviewResponse.model_validate(overview)


async def test_overview_total_records_matches_inventory(manager, db_session, seed_logs):
    await seed_logs(3)
    overview = await manager.get_overview(db_session)
    tables = await manager.get_tables(db_session)
    assert overview["total_records"] == sum(t["row_count"] for t in tables)


# ---------------------------------------------------------------- inventory


async def test_get_tables_counts_archive_logs(manager, db_session, seed_logs):
    await seed_logs(3)
    tables = await manager.get_tables(db_session)
    by_name = {entry["table_name"]: entry["row_count"] for entry in tables}

    # Base.metadata is global: assert presence, never absolute length.
    assert "archive_access_logs" in by_name
    assert by_name["archive_access_logs"] == 3


async def test_get_tables_skips_registered_but_missing_tables(manager, db_session, phantom_table):
    tables = await manager.get_tables(db_session)
    names = {entry["table_name"] for entry in tables}
    assert phantom_table not in names
    assert "archive_access_logs" in names


async def test_get_tables_includes_dynamic_probe_table(manager, db_session, probe_table):
    tables = {e["table_name"]: e["row_count"] for e in await manager.get_tables(db_session)}
    assert tables[probe_table["name"]] == 5


# ---------------------------------------------------------------- data browser


async def test_query_table_serializes_dynamic_rows(manager, db_session, probe_table):
    result = await manager.query_table(db_session, probe_table["name"], limit=10)

    assert result["table_name"] == probe_table["name"]
    assert result["limit"] == 10
    assert result["offset"] == 0
    assert len(result["data"]) == 5

    first = result["data"][0]
    expected = probe_table["rows"][0]
    assert first["id"] == 1
    assert isinstance(first["reference"], str)
    assert UUID(first["reference"]) == expected["reference"]
    assert first["happened_on"] == "2026-09-01"
    assert isinstance(first["recorded_at"], str)
    assert first["recorded_at"].startswith("2026-09-29T12:01")
    json.dumps(result["data"], allow_nan=False)


async def test_query_table_redacts_sensitive_columns(manager, db_session, probe_table):
    result = await manager.query_table(db_session, probe_table["name"])
    assert all(row["api_token"] == REDACTED for row in result["data"])
    assert all(row["label"].startswith("item-") for row in result["data"])


async def test_query_table_paginates_in_primary_key_order(manager, db_session, probe_table):
    page = await manager.query_table(db_session, probe_table["name"], limit=2, offset=1)
    assert [row["id"] for row in page["data"]] == [2, 3]
    tail = await manager.query_table(db_session, probe_table["name"], limit=2, offset=4)
    assert [row["id"] for row in tail["data"]] == [5]


async def test_query_table_logs_access_with_refreshed_timestamp(manager, session_factory, probe_table):
    async with session_factory() as session:
        await manager.query_table(session, probe_table["name"], limit=1)

    async with session_factory() as session:
        log = (
            await session.execute(
                select(ArchiveAccessLogModel).where(ArchiveAccessLogModel.target_resource == probe_table["name"])
            )
        ).scalar_one()
        read = ArchiveLogRead.model_validate(log)
    assert read.action == "VIEW_TABLE"
    assert read.user_id is None
    assert read.created_at is not None


async def test_query_archive_logs_includes_own_access(manager, db_session, seed_logs):
    await seed_logs(2)
    result = await manager.query_table(db_session, "archive_access_logs", limit=100)
    assert len(result["data"]) == 3  # 2 seeded + the VIEW_TABLE entry just recorded
    assert all(isinstance(row["id"], str) for row in result["data"])
    assert all(isinstance(row["created_at"], str) for row in result["data"])
    assert {row["action"] for row in result["data"]} == {"INTEGRITY_CHECK", "VIEW_TABLE"}


async def test_query_table_unknown_raises_not_found(manager, db_session):
    with pytest.raises(TableNotFoundError) as exc_info:
        await manager.query_table(db_session, "definitely_not_a_table")
    assert exc_info.value.table_name == "definitely_not_a_table"
    assert await _log_count(db_session) == 0


async def test_query_table_unmaterialized_raises_not_found(manager, db_session, phantom_table):
    with pytest.raises(TableNotFoundError):
        await manager.query_table(db_session, phantom_table)
    assert await _log_count(db_session) == 0


async def test_query_table_blocked_raises_not_found(db_session, probe_table):
    guarded = ArchiveManager(probe_latency_s=0.0, blocked_tables={probe_table["name"]})
    with pytest.raises(TableNotFoundError):
        await guarded.query_table(db_session, probe_table["name"])


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (MAX_PAGE_LIMIT + 1, 0), (10, -1)])
async def test_query_table_rejects_invalid_pagination(manager, db_session, limit, offset):
    with pytest.raises(ArchiveDomainError) as exc_info:
        await manager.query_table(db_session, "archive_access_logs", limit=limit, offset=offset)
    assert not isinstance(exc_info.value, TableNotFoundError)


# ---------------------------------------------------------------- serialization


async def test_serialize_row_casts_non_json_types_to_strings():
    reference = uuid4()
    row = {
        "id": reference,
        "created_at": datetime(2026, 9, 29, 12, 30, tzinfo=UTC),
        "day": date(2026, 9, 29),
        "at": time(8, 15),
        "amount": Decimal("12.50"),
        "colour": _Colour.RED,
        "tier": _Tier.GOLD,
        "mode": _Mode.LIVE,
        "blob": b"\x00\x01",
        "ratio": float("nan"),
        "nested": {"when": date(2026, 1, 1), "ids": [reference]},
        "count": 3,
        "flag": True,
        "missing": None,
    }
    serialized = _serialize_row(row)

    assert serialized["id"] == str(reference)
    assert serialized["created_at"] == "2026-09-29T12:30:00+00:00"
    assert serialized["day"] == "2026-09-29"
    assert serialized["at"] == "08:15:00"
    assert serialized["amount"] == "12.50"
    assert serialized["colour"] == "red"
    assert serialized["tier"] == "1"
    assert serialized["mode"] == "LIVE"
    assert serialized["blob"] == "AAE="
    assert serialized["ratio"] == "nan"
    assert serialized["nested"] == {"when": "2026-01-01", "ids": [str(reference)]}
    assert serialized["count"] == 3
    assert serialized["flag"] is True
    assert serialized["missing"] is None
    json.dumps(serialized, allow_nan=False)


async def test_serialize_row_redacts_requested_columns():
    serialized = _serialize_row({"username": "pratap", "hashed_password": "x"}, {"hashed_password"})
    assert serialized == {"username": "pratap", "hashed_password": REDACTED}
