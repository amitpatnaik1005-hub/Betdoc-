"""Async SQLite harness. StaticPool keeps one in-memory database alive."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from betdoc.adapters.persistence.db import build_engine, build_sessionmaker, create_all
from betdoc.adapters.persistence.ledger import ensure_account
from betdoc.adapters.persistence.models import AccountType, LedgerAccount

SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Fresh in-memory database per test, with foreign keys enforced.

    SQLite disables foreign key enforcement by default, so without this pragma
    the ``RESTRICT`` on ``ledger_entries.account_id`` would be decorative and
    the tests would prove less than they appear to.
    """
    created = build_engine(SQLITE_MEMORY_URL, echo=False)

    @event.listens_for(created.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    await create_all(created)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def sessionmaker_fixture(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(engine)


@pytest_asyncio.fixture
async def session(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_fixture() as active:
        yield active
        await active.rollback()


@pytest_asyncio.fixture
async def foreign_keys_enabled(session: AsyncSession) -> bool:
    result = await session.execute(text("PRAGMA foreign_keys"))
    return bool(result.scalar())


@pytest_asyncio.fixture
async def cash_account(session: AsyncSession) -> LedgerAccount:
    account = await ensure_account(session, "CASH", AccountType.ASSET)
    await session.commit()
    return account


@pytest_asyncio.fixture
async def open_bets_account(session: AsyncSession) -> LedgerAccount:
    account = await ensure_account(session, "OPEN_BETS", AccountType.ASSET)
    await session.commit()
    return account


@pytest.fixture
def unknown_account_id() -> uuid.UUID:
    return uuid.uuid4()
