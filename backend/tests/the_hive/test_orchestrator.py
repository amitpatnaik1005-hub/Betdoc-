import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.domain.the_hive import (
    DependencyCycleDetectedError,
    InvalidTaskStateError,
    TaskAlreadyClaimedError,
    TaskExpiredError,
)
from app.models.the_hive import (
    BotProfileModel,
    HiveTaskModel,
    ImmutableLedgerError,
    LegendaryBot,
    SelfLearningLogModel,
    TaskStatus,
)


async def _create(sf, orch, title: str, **kwargs: object) -> uuid.UUID:
    async with sf() as session:
        task = await orch.create_task(session, title=title, **kwargs)
        return task.id


async def _load(sf, task_id: uuid.UUID) -> HiveTaskModel:
    async with sf() as session:
        task = await session.get(HiveTaskModel, task_id)
        assert task is not None
        return task


async def _link(sf, orch, parent: uuid.UUID, child: uuid.UUID) -> None:
    async with sf() as session:
        await orch.add_dependency(session, parent, child)


@pytest.mark.asyncio
async def test_concurrent_claims_prevented(session_factory, orch) -> None:
    task_id = await _create(session_factory, orch, "Contested arb window", priority=99)
    bots = list(LegendaryBot)[:10]

    async def attempt(bot: LegendaryBot):
        async with session_factory() as session:
            return await orch.claim_task(session, task_id, bot)

    results = await asyncio.gather(*(attempt(bot) for bot in bots), return_exceptions=True)
    winners = [r for r in results if isinstance(r, HiveTaskModel)]
    losers = [r for r in results if isinstance(r, TaskAlreadyClaimedError)]

    assert len(winners) == 1
    assert len(losers) == 9
    stored = await _load(session_factory, task_id)
    assert stored.status == TaskStatus.IN_PROGRESS
    assert stored.assignee_name == winners[0].assignee_name


@pytest.mark.asyncio
async def test_ghost_sweeper_and_expiration(session_factory, orch) -> None:
    now = datetime.now(UTC)
    ghost = await _create(session_factory, orch, "ghost")
    alive = await _create(session_factory, orch, "alive")
    doomed = await _create(session_factory, orch, "doomed", expires_at=now - timedelta(minutes=1))
    orphan = await _create(session_factory, orch, "orphan")
    healthy = await _create(session_factory, orch, "healthy", expires_at=now + timedelta(hours=1))
    await _link(session_factory, orch, doomed, orphan)

    async with session_factory() as session:
        await orch.claim_task(session, ghost, LegendaryBot.GARUDA)
    async with session_factory() as session:
        await orch.claim_task(session, alive, LegendaryBot.PANINI)
    async with session_factory() as session:
        with pytest.raises(TaskExpiredError):
            await orch.claim_task(session, doomed, LegendaryBot.SHIVAJI)

    async with session_factory.begin() as session:
        await session.execute(
            update(BotProfileModel)
            .where(BotProfileModel.bot_name == LegendaryBot.GARUDA)
            .values(last_ping_at=now - timedelta(minutes=20))
        )

    async with session_factory() as session:
        report = await orch.sweep_ghosts_and_expired(session)

    assert report.reverted_task_ids == (ghost,)
    assert report.expired_task_ids == (doomed,)
    assert report.blocked_task_ids == (orphan,)
    assert LegendaryBot.GARUDA in report.offline_bots

    ghost_task = await _load(session_factory, ghost)
    assert ghost_task.status == TaskStatus.BACKLOG and ghost_task.assignee_name is None
    assert (await _load(session_factory, alive)).status == TaskStatus.IN_PROGRESS
    assert (await _load(session_factory, doomed)).status == TaskStatus.EXPIRED
    assert (await _load(session_factory, orphan)).status == TaskStatus.BLOCKED
    assert (await _load(session_factory, healthy)).status == TaskStatus.BACKLOG


@pytest.mark.asyncio
async def test_retry_logic(session_factory, orch) -> None:
    # Rule: retry while retry_count < max_retries. max_retries=3 => 3 re-queues, 4th failure is terminal.
    parent = await _create(session_factory, orch, "flaky scrape", max_retries=3)
    child = await _create(session_factory, orch, "model refit")
    grandchild = await _create(session_factory, orch, "publish tip")
    await _link(session_factory, orch, parent, child)
    await _link(session_factory, orch, child, grandchild)
    bot = LegendaryBot.ARYABHATA

    for attempt in range(1, 4):
        async with session_factory() as session:
            await orch.claim_task(session, parent, bot)
        async with session_factory() as session:
            outcome = await orch.fail_task(session, parent, bot, {"attempt": attempt})
        assert outcome.retried is True
        assert outcome.task.status == TaskStatus.BACKLOG
        assert outcome.task.retry_count == attempt
        assert outcome.task.assignee_name is None
        assert outcome.cascaded_task_ids == ()

    async with session_factory() as session:
        await orch.claim_task(session, parent, bot)
    async with session_factory() as session:
        final = await orch.fail_task(session, parent, bot, {"attempt": 4})

    assert final.retried is False
    assert final.task.status == TaskStatus.FAILED
    assert set(final.cascaded_task_ids) == {child, grandchild}
    assert (await _load(session_factory, child)).status == TaskStatus.FAILED
    assert (await _load(session_factory, grandchild)).status == TaskStatus.FAILED

    async with session_factory() as session:
        profile = await session.scalar(select(BotProfileModel).where(BotProfileModel.bot_name == bot))
        assert profile is not None and profile.error_count == 4


@pytest.mark.asyncio
async def test_dag_cycle_prevention(session_factory, orch) -> None:
    a = await _create(session_factory, orch, "A")
    b = await _create(session_factory, orch, "B")
    c = await _create(session_factory, orch, "C")
    await _link(session_factory, orch, a, b)
    await _link(session_factory, orch, b, c)

    async with session_factory() as session:
        with pytest.raises(DependencyCycleDetectedError):
            await orch.add_dependency(session, c, a)
    async with session_factory() as session:
        with pytest.raises(DependencyCycleDetectedError):
            await orch.add_dependency(session, a, a)

    async with session_factory() as session:
        task_a = await orch.get_task(session, a)
        assert [p.id for p in task_a.parents] == []
        assert [ch.id for ch in task_a.children] == [b]


@pytest.mark.asyncio
async def test_claim_respects_priority_and_dependencies(session_factory, orch) -> None:
    low = await _create(session_factory, orch, "low", priority=10)
    blocker = await _create(session_factory, orch, "blocker", priority=50)
    high = await _create(session_factory, orch, "high", priority=90)
    await _link(session_factory, orch, blocker, high)
    bot = LegendaryBot.KAUTILYA

    async with session_factory() as session:
        first = await orch.claim_highest_priority_task(session, bot)
    assert first is not None and first.id == blocker  # "high" gated by unmet dependency

    async with session_factory() as session:
        await orch.complete_task(session, blocker, bot, {"ok": True})
    async with session_factory() as session:
        second = await orch.claim_highest_priority_task(session, bot)
    assert second is not None and second.id == high

    async with session_factory() as session:
        third = await orch.claim_highest_priority_task(session, bot)
    assert third is not None and third.id == low

    async with session_factory() as session:
        assert await orch.claim_highest_priority_task(session, bot) is None


@pytest.mark.asyncio
async def test_completion_requires_owner(session_factory, orch) -> None:
    task_id = await _create(session_factory, orch, "owned")
    async with session_factory() as session:
        await orch.claim_task(session, task_id, LegendaryBot.BAJIRAO)
    async with session_factory() as session:
        with pytest.raises(TaskAlreadyClaimedError):
            await orch.complete_task(session, task_id, LegendaryBot.VIDUR, {})
    async with session_factory() as session:
        done = await orch.complete_task(session, task_id, LegendaryBot.BAJIRAO, {"pnl": 12.5})
    assert done.status == TaskStatus.DONE
    async with session_factory() as session:
        with pytest.raises(InvalidTaskStateError):
            await orch.complete_task(session, task_id, LegendaryBot.BAJIRAO, {})


@pytest.mark.asyncio
async def test_learning_ledger_is_immutable(session_factory, orch) -> None:
    async with session_factory() as session:
        entry = await orch.record_learning(
            session,
            bot_name=LegendaryBot.DEVRAYA,
            parameter_name="kelly_multiplier",
            old_value=0.25,
            new_value=0.2,
            reasoning="Drawdown exceeded backtest tolerance",
            confidence_score=0.87,
        )
        entry_id = entry.id
    async with session_factory() as session:
        stored = await session.get(SelfLearningLogModel, entry_id)
        assert stored is not None and stored.new_value == 0.2
        stored.reasoning = "tampered"
        with pytest.raises(ImmutableLedgerError):
            await session.commit()
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await orch.record_learning(
                session,
                bot_name=LegendaryBot.PANINI,
                parameter_name="x",
                old_value=None,
                new_value=1,
                reasoning="r",
                confidence_score=1.5,
            )
