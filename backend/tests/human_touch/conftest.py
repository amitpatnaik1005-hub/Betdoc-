"""Isolated fixtures for Human Touch Mode (FA-8). Never imports app.main."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.v1.human_touch import router as human_touch_router
from app.domain.human_touch.manager import HumanTouchManager
from app.models import Base
from app.models.human_touch import HumanOverrideLogModel, HumanTouchConfigModel, MatchNarrativeModel

HUMAN_TOUCH_TABLES = [
    HumanTouchConfigModel.__table__,
    MatchNarrativeModel.__table__,
    HumanOverrideLogModel.__table__,
]

app = FastAPI(title="BetDoc Human Touch Test App")
app.include_router(human_touch_router)  # router already carries /api/v1/human-touch


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
        await conn.run_sync(Base.metadata.drop_all, tables=HUMAN_TOUCH_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=HUMAN_TOUCH_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=HUMAN_TOUCH_TABLES)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager() -> HumanTouchManager:
    return HumanTouchManager()


@pytest.fixture
def make_config() -> Callable[..., SimpleNamespace]:
    """In-memory config objects for pure-math tests (every value supplied by the test)."""

    def _make(
        *,
        active: bool = True,
        max_pct: float = 25.0,
        min_pct: float = 0.0,
        sentiment_weight: float = 1.0,
        momentum_weight: float = 0.0,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            is_blended_mode_active=active,
            max_adjustment_limit_pct=max_pct,
            min_adjustment_threshold_pct=min_pct,
            sentiment_weight=sentiment_weight,
            momentum_weight=momentum_weight,
        )

    return _make


@pytest.fixture
def insert_log(session_factory: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[uuid.UUID]]:
    async def _insert(*, pure: float, blended: float, match_id: str = "EPL-ARS-CHE") -> uuid.UUID:
        log_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                insert(HumanOverrideLogModel).values(
                    id=log_id, match_id=match_id, pure_math_prob=pure, blended_prob=blended
                )
            )
            await session.commit()
        return log_id

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
