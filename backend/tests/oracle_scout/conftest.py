"""Isolated fixtures for the Scout Oracle (ASHOKA). Never imports app.main."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.oracle_scout import router as oracle_scout_router
from app.domain.oracle_scout.manager import OracleScoutManager
from app.models import Base
from app.models.oracle_scout import OracleScoutHistoryModel

ORACLE_SCOUT_TABLES = [OracleScoutHistoryModel.__table__]

app = FastAPI(title="BetDoc ASHOKA Scout Oracle Test App")
app.include_router(oracle_scout_router, prefix="/api/v1/oracle-scout")


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
        await conn.run_sync(Base.metadata.drop_all, tables=ORACLE_SCOUT_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=ORACLE_SCOUT_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=ORACLE_SCOUT_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=True on purpose: proves the manager refreshes before returning.
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> OracleScoutManager:
    return OracleScoutManager()


@pytest.fixture
def insert_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[list[uuid.UUID]]]:
    """Insert rows with explicit, strictly increasing timestamps. Returns ids oldest-first."""

    async def _insert(
        count: int,
        *,
        user_id: uuid.UUID | None = None,
        start: datetime | None = None,
        step: timedelta = timedelta(minutes=1),
    ) -> list[uuid.UUID]:
        base = start or datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        ids = [uuid.uuid4() for _ in range(count)]
        async with session_factory() as session:
            for index, row_id in enumerate(ids):
                await session.execute(
                    insert(OracleScoutHistoryModel).values(
                        id=row_id,
                        user_id=user_id,
                        page_context="/dashboard",
                        user_message=f"message {index}",
                        oracle_response=f"response {index}",
                        created_at=base + step * index,
                    )
                )
            await session.commit()
        return ids

    return _insert


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
