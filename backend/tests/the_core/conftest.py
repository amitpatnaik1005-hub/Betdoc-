"""The Crucible: shared fixtures for The Core test suite."""



from collections.abc import AsyncIterator, Callable

from contextlib import asynccontextmanager

from typing import Any



import pytest

import pytest_asyncio

from pydantic import BaseModel, ConfigDict

from sqlalchemy import event

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from sqlalchemy.pool import StaticPool



from app.domain.the_core.orchestrator import CoreOrchestrator

from app.models import Base

from app.models.the_core import (

    BacktestJobModel,

    CoreEngineMetricsModel,

    SmallcaseRegistryModel,

    TestBenchRunModel,

)



# Only The Core's tables: other models in Base.metadata may use Postgres-only types.

CORE_TABLES = [

    SmallcaseRegistryModel.__table__,

    CoreEngineMetricsModel.__table__,

    TestBenchRunModel.__table__,

    BacktestJobModel.__table__,

]





class StubMatchContext(BaseModel):

    model_config = ConfigDict(extra="allow")



    home_team: str

    away_team: str





class FakePoissonModel:

    def predict(self, context: StubMatchContext) -> dict[str, float]:

        assert isinstance(context, StubMatchContext)

        return {"home_win_prob": 0.52, "draw_prob": 0.26, "away_win_prob": 0.22}





class FakeAsyncDixonColesModel:

    async def predict(self, context: StubMatchContext) -> dict[str, Any]:

        assert isinstance(context, StubMatchContext)

        return {"probabilities": {"home": 0.48, "draw": 0.28, "away": 0.24}}





class ExplodingModel:

    def predict(self, context: StubMatchContext) -> dict[str, float]:

        raise RuntimeError("PANINI overheated")





class RecordingBroadcaster:

    def __init__(self) -> None:

        self.messages: list[dict[str, Any]] = []



    async def broadcast(self, message: dict[str, Any]) -> None:

        self.messages.append(message)



    def events(self, name: str) -> list[dict[str, Any]]:

        return [m for m in self.messages if m["event"] == name]





def _create_sqlite_engine() -> AsyncEngine:

    engine = create_async_engine(

        "sqlite+aiosqlite:///:memory:",

        connect_args={"check_same_thread": False},

        poolclass=StaticPool,

    )



    @event.listens_for(engine.sync_engine, "connect")

    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:

        cursor = dbapi_connection.cursor()

        cursor.execute("PRAGMA foreign_keys=ON")

        cursor.close()



    return engine





@asynccontextmanager

async def _sqlite_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:

    engine = _create_sqlite_engine()

    async with engine.begin() as conn:

        await conn.run_sync(Base.metadata.drop_all, tables=CORE_TABLES)

        await conn.run_sync(Base.metadata.create_all, tables=CORE_TABLES)

    try:

        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    finally:

        async with engine.begin() as conn:

            await conn.run_sync(Base.metadata.drop_all, tables=CORE_TABLES)

        await engine.dispose()





@pytest.fixture

def sqlite_database() -> Callable[[], Any]:

    return _sqlite_database





@pytest_asyncio.fixture

async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:

    async with _sqlite_database() as factory:

        yield factory





@pytest_asyncio.fixture

async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:

    async with session_factory() as session:

        yield session





@pytest.fixture

def model_registry() -> dict[str, type[Any]]:

    return {"PoissonModel": FakePoissonModel, "DixonColesModel": FakeAsyncDixonColesModel}





@pytest.fixture

def orch(model_registry: dict[str, type[Any]]) -> CoreOrchestrator:

    return CoreOrchestrator(model_registry=model_registry, context_schema=StubMatchContext)





@pytest.fixture

def exploding_orch() -> CoreOrchestrator:

    return CoreOrchestrator(model_registry={"PoissonModel": ExplodingModel}, context_schema=StubMatchContext)





@pytest.fixture

def broadcaster() -> RecordingBroadcaster:

    return RecordingBroadcaster()





@pytest.fixture

def match_context() -> dict[str, Any]:

    return {

        "home_team": "Mumbai Meteors",

        "away_team": "Delhi Dynamos",

        "league": "BetDoc Premier",

        "kickoff": "2026-09-29T18:30:00+00:00",

        "home_expected_goals": 1.65,

        "away_expected_goals": 1.05,

        "odds": {"home": 2.10, "draw": 3.40, "away": 3.60},

    }





@pytest.fixture

def stress_context() -> dict[str, Any]:

    return {

        "home_team": "PRATAP Stress XI",

        "away_team": "PANINI Chaos FC",

        "home_expected_goals": 0.0,

        "away_expected_goals": 25.0,

        "odds": {"home": 1_000_000.0, "draw": 1.0, "away": 0.0},

    }





@pytest_asyncio.fixture

async def smallcases(orch: CoreOrchestrator, db_session: AsyncSession) -> list[SmallcaseRegistryModel]:

    return await orch.bootstrap_smallcases(db_session)





@pytest.fixture

def reload(session_factory: async_sessionmaker[AsyncSession]) -> Callable[..., Any]:

    async def _reload(model: type[Any], pk: Any) -> Any:

        async with session_factory() as session:

            return await session.get(model, pk, populate_existing=True)



    return _reload