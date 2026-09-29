"""Isolated fixtures for Oracle Popular Picks. Never imports app.main."""

import math
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
from app.api.v1.popular_picks import router as popular_picks_router
from app.domain.popular_picks.manager import PopularPicksManager
from app.models import Base
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel

POPULAR_PICKS_TABLES = [PopularParlayModel.__table__, ParlayReviewGateModel.__table__]

app = FastAPI(title="BetDoc Popular Picks Test App")
app.include_router(popular_picks_router, prefix="/api/v1/popular-picks")


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
        await conn.run_sync(Base.metadata.drop_all, tables=POPULAR_PICKS_TABLES)
        await conn.run_sync(Base.metadata.create_all, tables=POPULAR_PICKS_TABLES)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=POPULAR_PICKS_TABLES)
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
def manager() -> PopularPicksManager:
    return PopularPicksManager()


@pytest.fixture
def make_parlay(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[uuid.UUID]]:
    async def _make(
        *,
        title: str = "Custom Parlay",
        pick_type: str = "TRENDING",
        legs: list[dict[str, Any]] | None = None,
        total_odds: float | None = None,
        historical_success_rate: float = 0.3,
        is_active: bool = True,
        expires_in: timedelta = timedelta(hours=6),
    ) -> uuid.UUID:
        resolved_legs = legs or [
            {"match_id": "TEST-HOME-AWAY", "selection": "HOME", "odds": 2.0},
            {"match_id": "TEST-FOO-BAR", "selection": "DRAW", "odds": 3.0},
        ]
        parlay_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                insert(PopularParlayModel).values(
                    id=parlay_id,
                    title=title,
                    pick_type=pick_type,
                    legs=resolved_legs,
                    total_odds=total_odds if total_odds is not None else math.prod(l["odds"] for l in resolved_legs),
                    historical_success_rate=historical_success_rate,
                    is_active=is_active,
                    expires_at=datetime.now(UTC) + expires_in,
                )
            )
            await session.commit()
        return parlay_id

    return _make


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
