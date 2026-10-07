import asyncio
import logging
import traceback
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast, runtime_checkable

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.the_lab import ResearchReportModel, ResearchStatus

logger = logging.getLogger("betdoc.the_lab.research")

RESEARCH_TIMEOUT_SECONDS: Final[float] = 300.0
STALE_TASK_GRACE: Final[timedelta] = timedelta(seconds=60)


@runtime_checkable
class ResearchAgent(Protocol):
    async def generate_report(self, topic: str) -> str: ...


class ResearchReportManager:
    async def create_report(
        self, db: AsyncSession, *, category: str, topic: str
    ) -> ResearchReportModel:
        report = ResearchReportModel(
            category=category, topic=topic, status=ResearchStatus.PENDING.value
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report

    async def list_reports(
        self, db: AsyncSession, skip: int = 0, limit: int = 50
    ) -> Sequence[ResearchReportModel]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be >= 0 and limit must be >= 1")
        stmt = (
            select(ResearchReportModel)
            .order_by(ResearchReportModel.created_at.desc(), ResearchReportModel.id.desc())
            .offset(skip)
            .limit(limit)
        )
        return (await db.scalars(stmt)).all()

    async def get_report(self, db: AsyncSession, report_id: uuid.UUID) -> ResearchReportModel | None:
        return await db.get(ResearchReportModel, report_id)


class ResearchExecutor:
    def __init__(self, timeout_seconds: float = RESEARCH_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    async def execute_task(
        self,
        report_id: uuid.UUID,
        topic: str,
        agent: ResearchAgent,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        try:
            claimed = await self._claim(report_id, session_factory)
        except Exception:
            logger.exception("Research %s: claim failed; leaving for the stale-task reaper", report_id)
            return
        if not claimed:
            logger.debug("Research %s: already claimed or not PENDING; aborting", report_id)
            return

        try:
            content = await asyncio.wait_for(
                agent.generate_report(topic), timeout=self._timeout_seconds
            )
            if not isinstance(content, str):
                raise TypeError(f"ResearchAgent returned {type(content).__name__}, expected str")
            if not await self._finalize(report_id, session_factory, ResearchStatus.COMPLETED, content):
                logger.warning("Research %s: no longer RUNNING at completion; result discarded", report_id)
        except BaseException as exc:  # includes CancelledError, KeyboardInterrupt, SystemExit
            failure_trace = traceback.format_exc()
            logger.error("Research %s failed: %s", report_id, type(exc).__name__)
            await self._record_failure(report_id, session_factory, failure_trace)
            if not isinstance(exc, Exception):
                raise  # never swallow cancellation/shutdown signals

    async def reap_stale_tasks(self, session_factory: async_sessionmaker[AsyncSession]) -> int:
        """Fail PENDING/RUNNING reports orphaned by a hard crash (e.g. SIGKILL). Run on startup."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self._timeout_seconds) - STALE_TASK_GRACE
        stmt = (
            update(ResearchReportModel)
            .where(
                ResearchReportModel.status.in_(
                    (ResearchStatus.PENDING.value, ResearchStatus.RUNNING.value)
                ),
                ResearchReportModel.created_at < cutoff,
            )
            .values(
                status=ResearchStatus.FAILED.value,
                completed_at=now,
                markdown_content="Task reaped: worker terminated before reporting a result.",
            )
            .execution_options(synchronize_session=False)
        )
        async with session_factory.begin() as session:
            result = cast(CursorResult[Any], await session.execute(stmt))
            reaped = result.rowcount
        if reaped:
            logger.warning("Reaped %d stale research task(s)", reaped)
        return reaped

    # ------------------------------------------------------------- internals
    @staticmethod
    async def _claim(
        report_id: uuid.UUID, session_factory: async_sessionmaker[AsyncSession]
    ) -> bool:
        stmt = (
            update(ResearchReportModel)
            .where(
                ResearchReportModel.id == report_id,
                ResearchReportModel.status == ResearchStatus.PENDING.value,
            )
            .values(status=ResearchStatus.RUNNING.value)
            .execution_options(synchronize_session=False)
        )
        async with session_factory.begin() as session:
            result = cast(CursorResult[Any], await session.execute(stmt))
            return result.rowcount == 1

    @staticmethod
    async def _finalize(
        report_id: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
        status: ResearchStatus,
        content: str,
    ) -> bool:
        stmt = (
            update(ResearchReportModel)
            .where(
                ResearchReportModel.id == report_id,
                ResearchReportModel.status == ResearchStatus.RUNNING.value,
            )
            .values(status=status.value, markdown_content=content, completed_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        async with session_factory.begin() as session:
            result = cast(CursorResult[Any], await session.execute(stmt))
            return result.rowcount == 1

    async def _record_failure(
        self,
        report_id: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
        failure_trace: str,
    ) -> None:
        try:
            # shield: the FAILED write completes even if a second cancel arrives mid-shutdown
            await asyncio.shield(
                self._finalize(report_id, session_factory, ResearchStatus.FAILED, failure_trace)
            )
        except Exception:
            logger.exception("Research %s: could not persist FAILED status", report_id)
