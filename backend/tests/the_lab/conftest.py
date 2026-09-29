import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.models.the_lab import ExperimentModel, ResearchReportModel

LAB_TABLES = [ResearchReportModel.__table__, ExperimentModel.__table__]


class ScriptedAgent:
    """Research agent with controllable behaviour and call counting."""

    def __init__(
        self,
        *,
        result: object = "# Report",
        delay: float = 0.0,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self.result, self.delay, self.error, self.block = result, delay, error, block
        self.calls = 0
        self.started = asyncio.Event()

    async def generate_report(self, topic: str) -> str:
        self.calls += 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=LAB_TABLES))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
