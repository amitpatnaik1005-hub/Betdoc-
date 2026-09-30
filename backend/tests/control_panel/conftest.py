"""Isolated fixtures for the Control Panel. Never imports app.main."""

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
from app.api.v1.control_panel import router as control_panel_router
from app.domain.control_panel.manager import ControlPanelManager
from app.models import Base
from app.models.control_panel import SystemSettingsModel

CONTROL_PANEL_TABLES = [SystemSettingsModel.__table__]

app = FastAPI(title="BetDoc Control Panel Test App")
app.include_router(control_panel_router, prefix="/api/v1/control-panel")


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
        await conn.run_sync(Base.metadata.drop_all, tables=CONTROL_PANEL_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=CONTROL_PANEL_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=CONTROL_PANEL_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=True on purpose: proves every path refreshes before returning.
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> ControlPanelManager:
    return ControlPanelManager()


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
