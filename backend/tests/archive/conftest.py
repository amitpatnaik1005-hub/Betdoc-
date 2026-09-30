"""Isolated fixtures for The Archive. Never imports app.main."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Date, DateTime, Integer, String, Table, Uuid, event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.archive import router as archive_router
from app.domain.archive.manager import ArchiveManager
from app.models import Base
from app.models.archive import ArchiveAccessLogModel

ARCHIVE_TABLES = [ArchiveAccessLogModel.__table__]
PROBE_TABLE_NAME = "archive_probe_items"
PHANTOM_TABLE_NAME = "archive_phantom_probe"

app = FastAPI(title="BetDoc Archive Test App")
app.include_router(archive_router, prefix="/api/v1/archive")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(test_engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=ARCHIVE_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=ARCHIVE_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=ARCHIVE_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=True on purpose: proves refresh() is performed after commit.
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> ArchiveManager:
    return ArchiveManager(probe_latency_s=0.0)


@pytest.fixture
def seed_logs(session_factory: async_sessionmaker[AsyncSession]) -> Callable[[int], Awaitable[list[uuid.UUID]]]:
    async def _seed(count: int) -> list[uuid.UUID]:
        ids = [uuid.uuid4() for _ in range(count)]
        async with session_factory() as session:
            for index, log_id in enumerate(ids):
                await session.execute(
                    insert(ArchiveAccessLogModel).values(
                        id=log_id,
                        user_id=None,
                        action="INTEGRITY_CHECK",
                        target_resource=f"seed_resource_{index}",
                    )
                )
            await session.commit()
        return ids

    return _seed


@pytest_asyncio.fixture
async def probe_table(engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """A temporary table with UUID/date/datetime/sensitive columns, registered on Base.metadata."""
    table = Table(
        PROBE_TABLE_NAME,
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("label", String(32), nullable=False),
        Column("reference", Uuid(as_uuid=True), nullable=False),
        Column("happened_on", Date, nullable=False),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
        Column("api_token", String(64), nullable=True),
    )
    rows = [
        {
            "id": index,
            "label": f"item-{index}",
            "reference": uuid.uuid4(),
            "happened_on": date(2026, 9, index),
            "recorded_at": datetime(2026, 9, 29, 12, 0, tzinfo=UTC) + timedelta(minutes=index),
            "api_token": f"tok_live_{index:04d}",
        }
        for index in range(1, 6)
    ]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(table.create)
            await conn.execute(insert(table), rows)
        yield {"name": PROBE_TABLE_NAME, "rows": rows}
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(table.drop, checkfirst=True)
        Base.metadata.remove(table)


@pytest.fixture
def phantom_table() -> Iterator[str]:
    """Registered on Base.metadata but never created in the database."""
    table = Table(PHANTOM_TABLE_NAME, Base.metadata, Column("id", Integer, primary_key=True))
    try:
        yield PHANTOM_TABLE_NAME
    finally:
        Base.metadata.remove(table)


@pytest_asyncio.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()
