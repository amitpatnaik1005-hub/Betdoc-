"""Where a Lab backtest runs: in the API process (a worker thread) or on a Celery worker.

``LAB_BACKTEST_EXECUTOR = "inline"`` (the default) suits a single machine: the run starts at once
and its CPU-bound part leaves the event loop for a thread. ``"celery"`` hands it to the
``lab.run_backtest`` task, so a long sweep never shares a process with the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.services.backtesting.runner import run_backtest

_INLINE: set[asyncio.Task[None]] = set()  # strong references: a running backtest is never garbage-collected


def dispatch(run_id: uuid.UUID, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> str:
    if settings.LAB_BACKTEST_EXECUTOR == "celery":
        run_backtest_task.apply_async(args=[str(run_id)])
        return "celery"
    task = asyncio.get_running_loop().create_task(run_backtest(session_factory, settings, run_id), name=f"lab-backtest-{run_id}")
    _INLINE.add(task)
    task.add_done_callback(_INLINE.discard)
    return "inline"


async def _run(run_id: uuid.UUID) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    try:
        await run_backtest(async_sessionmaker(engine, expire_on_commit=False), settings, run_id)
    finally:
        with contextlib.suppress(Exception):
            await engine.dispose()


@celery_app.task(name="lab.run_backtest", acks_late=True)
def run_backtest_task(run_id: str) -> dict[str, Any]:
    asyncio.run(_run(uuid.UUID(run_id)))
    return {"run_id": run_id}
