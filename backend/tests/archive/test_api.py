"""HTTP tests for The Archive using httpx.AsyncClient + ASGITransport."""

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.domain.archive.manager import REDACTED
from app.models.archive import ArchiveAccessLogModel

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/archive"


async def test_overview_returns_200(client):
    response = await client.get(f"{BASE}/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["encryption_status"] == "E2E Active"
    assert body["algorithm"] == "AES-256-GCM"
    assert body["database_status"] == "ONLINE"
    assert body["total_tables"] >= 1
    assert body["last_backup_at"]


async def test_tables_returns_200_with_archive_logs(client, seed_logs):
    await seed_logs(4)
    response = await client.get(f"{BASE}/tables")
    assert response.status_code == 200
    by_name = {entry["table_name"]: entry["row_count"] for entry in response.json()}
    assert "archive_access_logs" in by_name
    assert by_name["archive_access_logs"] == 4


async def test_table_data_returns_serialized_rows(client, probe_table):
    response = await client.get(f"{BASE}/tables/{probe_table['name']}/data", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["table_name"] == probe_table["name"]
    assert body["limit"] == 3
    assert body["offset"] == 0
    assert [row["id"] for row in body["data"]] == [1, 2, 3]
    UUID(body["data"][0]["reference"])
    assert body["data"][0]["happened_on"] == "2026-09-01"
    assert all(row["api_token"] == REDACTED for row in body["data"])


async def test_table_data_pagination_offset(client, probe_table):
    response = await client.get(f"{BASE}/tables/{probe_table['name']}/data", params={"limit": 2, "offset": 3})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [4, 5]


async def test_table_data_records_access_log(client, session_factory, probe_table):
    await client.get(f"{BASE}/tables/{probe_table['name']}/data")
    await client.get(f"{BASE}/tables/{probe_table['name']}/data")
    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ArchiveAccessLogModel)
                .where(
                    ArchiveAccessLogModel.target_resource == probe_table["name"],
                    ArchiveAccessLogModel.action == "VIEW_TABLE",
                )
            )
        ).scalar_one()
    assert count == 2


async def test_archive_logs_browse_returns_string_uuids(client):
    response = await client.get(f"{BASE}/tables/archive_access_logs/data")
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 1  # the access entry recorded by this very request
    UUID(rows[0]["id"])
    assert rows[0]["action"] == "VIEW_TABLE"
    assert isinstance(rows[0]["created_at"], str)


async def test_unknown_table_returns_404(client):
    response = await client.get(f"{BASE}/tables/definitely_not_a_table/data")
    assert response.status_code == 404
    assert "definitely_not_a_table" in response.json()["detail"]


async def test_unmaterialized_table_returns_404(client, phantom_table):
    response = await client.get(f"{BASE}/tables/{phantom_table}/data")
    assert response.status_code == 404


@pytest.mark.parametrize("params", [{"limit": 1001}, {"limit": 0}, {"offset": -1}, {"limit": "many"}])
async def test_table_data_validation_errors_return_422(client, params):
    response = await client.get(f"{BASE}/tables/archive_access_logs/data", params=params)
    assert response.status_code == 422
