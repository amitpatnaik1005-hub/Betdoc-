"""Async engine and session management. No synchronous DB path exists here."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from betdoc.adapters.persistence.models import Base

__all__ = [
    "DEFAULT_DATABASE_URL",
    "build_engine",
    "build_sessionmaker",
    "create_all",
    "dispose_engine",
    "drop_all",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "session_scope",
]

DEFAULT_DATABASE_URL: Final[str] = (
    "postgresql+asyncpg://quant:quant@localhost:5432/betting_quant"
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_lock: Final[asyncio.Lock] = asyncio.Lock()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _database_url() -> str:
    return (
        os.getenv("BETDOC_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def build_engine(url: str | None = None, *, echo: bool | None = None) -> AsyncEngine:
    """Construct an ``AsyncEngine`` with dialect-appropriate pooling.

    SQLite in-memory needs ``StaticPool``: the default pool opens a fresh
    connection per checkout, and each fresh connection to ``:memory:`` is a
    brand new empty database, so schema created on one connection is invisible
    to the next.

    PostgreSQL gets a real pool with ``pool_pre_ping`` (so a connection killed
    by a failover is discarded rather than raising mid-transaction) and
    ``pool_recycle`` below any proxy idle timeout.
    """
    resolved = url or _database_url()
    verbose = os.getenv("BETDOC_DB_ECHO", "").lower() in {"1", "true", "yes"}
    kwargs: dict[str, Any] = {"echo": echo if echo is not None else verbose}

    if resolved.startswith("sqlite"):
        kwargs.update(
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        kwargs.update(
            pool_pre_ping=True,
            pool_size=_env_int("BETDOC_DB_POOL_SIZE", 10),
            max_overflow=_env_int("BETDOC_DB_MAX_OVERFLOW", 20),
            pool_timeout=_env_int("BETDOC_DB_POOL_TIMEOUT", 30),
            pool_recycle=_env_int("BETDOC_DB_POOL_RECYCLE", 1_800),
            connect_args={
                "timeout": _env_int("BETDOC_DB_CONNECT_TIMEOUT", 10),
                "server_settings": {
                    "application_name": os.getenv("BETDOC_APP_NAME", "betdoc"),
                    "jit": "off",
                },
            },
        )

    return create_async_engine(resolved, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory tuned for explicit transaction control.

    ``expire_on_commit=False`` keeps loaded attributes usable after commit,
    which matters because an expired attribute triggers lazy IO that is illegal
    on an async session. ``autoflush=False`` makes write ordering explicit
    rather than incidental.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


async def get_engine() -> AsyncEngine:
    """Process-wide engine, initialised exactly once under a lock."""
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    async with _lock:
        if _engine is None:
            _engine = build_engine()
            _sessionmaker = build_sessionmaker(_engine)
    return _engine


async def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    await get_engine()
    if _sessionmaker is None:  # pragma: no cover - set by get_engine
        msg = "sessionmaker was not initialised"
        raise RuntimeError(msg)
    return _sessionmaker


async def dispose_engine() -> None:
    """Release every pooled connection. Call from the shutdown handler."""
    global _engine, _sessionmaker
    async with _lock:
        if _engine is not None:
            await _engine.dispose()
        _engine = None
        _sessionmaker = None


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on any exception.

    Rollback is unconditional on failure. A half-applied ledger transaction is
    worse than no transaction, so there is no partial-commit path.
    """
    maker = factory or await get_sessionmaker()
    session = maker()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency-injection entry point (FastAPI ``Depends``)."""
    async with session_scope() as session:
        yield session


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema. Tests only. Production uses Alembic migrations."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    """Drop the schema. Tests only."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
