"""Isolated fixtures for Competitive Intelligence. Never imports app.main."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.competitive_intel import router as competitive_intel_router
from app.domain.competitive_intel.manager import CompetitiveIntelManager
from app.models import Base
from app.models.competitive_intel import CompetitorBotModel, DevSuggestionModel, FeatureGapAlertModel

COMPETITIVE_INTEL_TABLES = [
    CompetitorBotModel.__table__,
    FeatureGapAlertModel.__table__,
    DevSuggestionModel.__table__,
]

app = FastAPI(title="BetDoc Competitive Intel Test App")
app.include_router(competitive_intel_router, prefix="/api/v1/rnd/competitive-intel")


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
        await conn.run_sync(Base.metadata.drop_all, tables=COMPETITIVE_INTEL_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=COMPETITIVE_INTEL_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=COMPETITIVE_INTEL_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=True on purpose: surfaces any post-commit lazy load (MissingGreenlet).
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> CompetitiveIntelManager:
    return CompetitiveIntelManager()


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
