from __future__ import annotations

import random
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.bookmakers import (
    get_omniroute_client,
    nan_safe_validation_exception_handler,
)
from app.api.v1.bookmakers import router as bookmakers_router
from app.domain.bookmakers.omniroute import OmniRouteClient
from app.models import Base
from app.models.bookmakers import BookmakerConfigModel

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
API_PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [BookmakerConfigModel.__table__]
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=tables)
        await test_engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def omniroute_client() -> OmniRouteClient:
    return OmniRouteClient(
        base_latency_ms=5.0, jitter_ms=10.0, simulate_delay=False, rng=random.Random(1337)
    )


@pytest.fixture
def app(
    session_factory: async_sessionmaker[AsyncSession], omniroute_client: OmniRouteClient
) -> FastAPI:
    application = FastAPI()
    application.include_router(bookmakers_router, prefix=API_PREFIX)
    application.add_exception_handler(RequestValidationError, nan_safe_validation_exception_handler)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_omniroute_client] = lambda: omniroute_client
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
