import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.the_lab import ResearchExecutor, ResearchReportManager, ResearchStatus
from app.models.the_lab import ResearchReportModel
from tests.the_lab.conftest import ScriptedAgent

pytestmark = pytest.mark.asyncio


async def _create(session_factory, topic: str = "Arsenal vs Chelsea") -> uuid.UUID:
    async with session_factory() as session:
        report = await ResearchReportManager().create_report(session, category="PRE_MATCH", topic=topic)
        return report.id


async def _load(session_factory, report_id: uuid.UUID) -> ResearchReportModel:
    async with session_factory() as session:
        report = await session.get(ResearchReportModel, report_id)
        assert report is not None
        return report


async def test_successful_run_completes_report(session_factory) -> None:
    report_id = await _create(session_factory)
    agent = ScriptedAgent(result="# Done")
    await ResearchExecutor().execute_task(report_id, "t", agent, session_factory)

    report = await _load(session_factory, report_id)
    assert report.status == ResearchStatus.COMPLETED
    assert report.markdown_content == "# Done"
    assert report.completed_at is not None


async def test_cas_claim_prevents_double_execution(session_factory) -> None:
    report_id = await _create(session_factory)
    agent = ScriptedAgent(delay=0.05)
    executor = ResearchExecutor()
    await asyncio.gather(
        executor.execute_task(report_id, "t", agent, session_factory),
        executor.execute_task(report_id, "t", agent, session_factory),
    )
    assert agent.calls == 1
    assert (await _load(session_factory, report_id)).status == ResearchStatus.COMPLETED


async def test_already_finished_report_is_silently_skipped(session_factory) -> None:
    report_id = await _create(session_factory)
    executor = ResearchExecutor()
    await executor.execute_task(report_id, "t", ScriptedAgent(), session_factory)
    rerun = ScriptedAgent(result="# Overwrite")
    await executor.execute_task(report_id, "t", rerun, session_factory)
    assert rerun.calls == 0
    assert (await _load(session_factory, report_id)).markdown_content == "# Report"


async def test_unknown_report_is_silently_skipped(session_factory) -> None:
    agent = ScriptedAgent()
    await ResearchExecutor().execute_task(uuid.uuid4(), "t", agent, session_factory)
    assert agent.calls == 0


async def test_exception_saves_traceback_with_line_numbers(session_factory) -> None:
    report_id = await _create(session_factory)
    agent = ScriptedAgent(error=RuntimeError("scraper exploded"))
    await ResearchExecutor().execute_task(report_id, "t", agent, session_factory)

    report = await _load(session_factory, report_id)
    assert report.status == ResearchStatus.FAILED
    assert report.completed_at is not None
    assert "Traceback (most recent call last)" in report.markdown_content
    assert "RuntimeError: scraper exploded" in report.markdown_content
    assert ", line " in report.markdown_content


async def test_timeout_marks_failed(session_factory) -> None:
    report_id = await _create(session_factory)
    agent = ScriptedAgent(delay=1.0)
    await ResearchExecutor(timeout_seconds=0.05).execute_task(report_id, "t", agent, session_factory)

    report = await _load(session_factory, report_id)
    assert report.status == ResearchStatus.FAILED
    assert "TimeoutError" in report.markdown_content


async def test_non_string_result_marks_failed(session_factory) -> None:
    report_id = await _create(session_factory)
    await ResearchExecutor().execute_task(report_id, "t", ScriptedAgent(result={"raw": 1}), session_factory)

    report = await _load(session_factory, report_id)
    assert report.status == ResearchStatus.FAILED
    assert "TypeError" in report.markdown_content


async def test_cancellation_marks_failed_and_is_reraised(session_factory) -> None:
    report_id = await _create(session_factory)
    agent = ScriptedAgent(block=True)
    task = asyncio.create_task(ResearchExecutor().execute_task(report_id, "t", agent, session_factory))
    await asyncio.wait_for(agent.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    report = await _load(session_factory, report_id)
    assert report.status == ResearchStatus.FAILED
    assert "CancelledError" in report.markdown_content


async def test_reaper_fails_only_stale_tasks(session_factory) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory.begin() as session:
        stale_pending = ResearchReportModel(category="MARKET", topic="a", status="PENDING", created_at=old)
        stale_running = ResearchReportModel(category="MARKET", topic="b", status="RUNNING", created_at=old)
        stale_done = ResearchReportModel(category="MARKET", topic="c", status="COMPLETED", created_at=old)
        fresh = ResearchReportModel(category="MARKET", topic="d", status="PENDING")
        session.add_all([stale_pending, stale_running, stale_done, fresh])

    reaped = await ResearchExecutor().reap_stale_tasks(session_factory)

    assert reaped == 2
    assert (await _load(session_factory, stale_pending.id)).status == ResearchStatus.FAILED
    assert (await _load(session_factory, stale_running.id)).status == ResearchStatus.FAILED
    assert (await _load(session_factory, stale_done.id)).status == ResearchStatus.COMPLETED
    assert (await _load(session_factory, fresh.id)).status == ResearchStatus.PENDING


async def test_list_reports_orders_by_created_at_desc_with_pagination(db, session_factory) -> None:
    base = datetime.now(UTC)
    async with session_factory.begin() as session:
        for i in range(5):
            session.add(ResearchReportModel(
                category="STRATEGY", topic=f"t{i}", status="PENDING", created_at=base + timedelta(minutes=i)
            ))
    manager = ResearchReportManager()
    page1 = await manager.list_reports(db, skip=0, limit=2)
    page2 = await manager.list_reports(db, skip=2, limit=2)
    assert [r.topic for r in page1] == ["t4", "t3"]
    assert [r.topic for r in page2] == ["t2", "t1"]
