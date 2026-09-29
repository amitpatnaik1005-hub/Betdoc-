import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.domain.the_hive import HiveOrchestrator
from app.models import Base

HIVE_TABLE_NAMES = ("hive_bot_profiles", "hive_tasks", "hive_task_dependencies", "hive_self_learning_logs")


def _hive_tables() -> list[Table]:
    return [Base.metadata.tables[name] for name in HIVE_TABLE_NAMES]


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    # Set HIVE_TEST_DATABASE_URL=postgresql+asyncpg://... to exercise SKIP LOCKED / GIN / advisory locks.
    url = os.environ.get("HIVE_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'hive.db'}")
    connect_args = {"timeout": 30} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.drop_all(sync_conn, tables=_hive_tables()))
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_hive_tables()))
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.drop_all(sync_conn, tables=_hive_tables()))
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def orch() -> HiveOrchestrator:
    return HiveOrchestrator()
