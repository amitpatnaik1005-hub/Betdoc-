"""Isolated fixtures for THE VAULT - CFO Advisory. Never imports app.main."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.cfo import router as cfo_router
from app.domain.cfo.manager import CfoManager
from app.models import Base
from app.models.cfo import CfoAdvisoryModel, CfoAlertModel, StressTestResultModel, TaxRecordModel

CFO_TABLES = [
    CfoAlertModel.__table__,
    TaxRecordModel.__table__,
    StressTestResultModel.__table__,
    CfoAdvisoryModel.__table__,
]

app = FastAPI(title="BetDoc CFO Advisory Test App")
app.include_router(cfo_router, prefix="/api/v1/the-vault/cfo")


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
        await conn.run_sync(Base.metadata.drop_all, tables=CFO_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=CFO_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=CFO_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=True on purpose: proves every write path refreshes before returning.
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> CfoManager:
    return CfoManager()


@pytest.fixture
def insert_alert(session_factory: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[uuid.UUID]]:
    """Test-only helper: inserts an alert row directly (the application never seeds alerts)."""

    async def _insert(
        *,
        user_id: uuid.UUID | None = None,
        level: str = "WARNING",
        message: str = "Test alert",
        is_read: bool = False,
        created_at: datetime | None = None,
    ) -> uuid.UUID:
        alert_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                insert(CfoAlertModel).values(
                    id=alert_id,
                    user_id=user_id,
                    level=level,
                    message=message,
                    is_read=is_read,
                    created_at=created_at or datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
                )
            )
            await session.commit()
        return alert_id

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
