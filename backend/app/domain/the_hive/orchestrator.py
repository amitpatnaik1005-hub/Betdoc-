import logging
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NoReturn, cast

from sqlalchemy import CTE, ColumnElement, CursorResult, Exists, Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.domain.the_hive.errors import (
    DependencyCycleDetectedError,
    InvalidTaskStateError,
    TaskAlreadyClaimedError,
    TaskExpiredError,
    TaskNotFoundError,
)
from app.models.the_hive import (
    BotProfileModel,
    BotStatus,
    HiveTaskDependencyModel,
    HiveTaskModel,
    LegendaryBot,
    SelfLearningLogModel,
    TaskStatus,
)

logger = logging.getLogger(__name__)

GHOST_TIMEOUT: Final[timedelta] = timedelta(minutes=15)
CASCADE_ROOT_CHUNK: Final[int] = 500
HIVE_DAG_LOCK_KEY: Final[int] = 0x48495645  # "HIVE": pg advisory lock serializing DAG writes
DEPENDABLE_CHILD_STATUSES: Final[tuple[TaskStatus, ...]] = (TaskStatus.BACKLOG, TaskStatus.BLOCKED)
DEAD_PARENT_STATUSES: Final[tuple[TaskStatus, ...]] = (TaskStatus.FAILED, TaskStatus.EXPIRED)


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone=True columns."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    task: HiveTaskModel
    retried: bool
    cascaded_task_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class SweepReport:
    reverted_task_ids: tuple[uuid.UUID, ...]
    expired_task_ids: tuple[uuid.UUID, ...]
    blocked_task_ids: tuple[uuid.UUID, ...]
    offline_bots: tuple[LegendaryBot, ...]


class HiveOrchestrator:
    def __init__(self, *, ghost_timeout: timedelta = GHOST_TIMEOUT) -> None:
        if ghost_timeout <= timedelta(0):
            raise ValueError("ghost_timeout must be positive")
        self._ghost_timeout = ghost_timeout

    # ================================================================= bots
    async def record_heartbeat(
        self,
        db: AsyncSession,
        bot_name: LegendaryBot,
        *,
        status: BotStatus,
        uptime_seconds: int,
        resource_metrics: Mapping[str, Any],
    ) -> BotProfileModel:
        if uptime_seconds < 0:
            raise ValueError("uptime_seconds must be non-negative")
        await self._upsert_bot(
            db,
            bot_name,
            now=utc_now(),
            status=status,
            extra={"uptime_seconds": uptime_seconds, "resource_metrics": dict(resource_metrics)},
        )
        await db.commit()
        profile = await db.scalar(
            select(BotProfileModel)
            .where(BotProfileModel.bot_name == bot_name)
            .execution_options(populate_existing=True)
        )
        if profile is None:  # pragma: no cover - upsert guarantees existence
            raise RuntimeError(f"Bot profile {bot_name} missing after upsert")
        return profile

    async def _upsert_bot(
        self,
        db: AsyncSession,
        bot_name: LegendaryBot,
        *,
        now: datetime,
        status: BotStatus | None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        extra_values = dict(extra or {})
        insert_values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "bot_name": bot_name,
            "status": status or BotStatus.ONLINE,
            "last_ping_at": now,
            "uptime_seconds": 0,
            "tasks_completed": 0,
            "error_count": 0,
            "resource_metrics": {},
            **extra_values,
        }
        update_values: dict[str, Any] = {"last_ping_at": now, **extra_values}
        if status is not None:
            update_values["status"] = status

        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(BotProfileModel).values(**insert_values)
            await db.execute(
                pg_stmt.on_conflict_do_update(index_elements=[BotProfileModel.bot_name], set_=update_values)
            )
        elif dialect == "sqlite":
            lite_stmt = sqlite_insert(BotProfileModel).values(**insert_values)
            await db.execute(
                lite_stmt.on_conflict_do_update(index_elements=[BotProfileModel.bot_name], set_=update_values)
            )
        else:
            result = cast(
                CursorResult[Any],
                await db.execute(
                    update(BotProfileModel)
                    .where(BotProfileModel.bot_name == bot_name)
                    .values(**update_values)
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount == 0:
                db.add(BotProfileModel(**insert_values))
                await db.flush()

    async def _bump_bot_counter(
        self, db: AsyncSession, bot_name: LegendaryBot, *, completed: int = 0, errors: int = 0, now: datetime
    ) -> None:
        await db.execute(
            update(BotProfileModel)
            .where(BotProfileModel.bot_name == bot_name)
            .values(
                tasks_completed=BotProfileModel.tasks_completed + completed,
                error_count=BotProfileModel.error_count + errors,
                last_ping_at=now,
            )
            .execution_options(synchronize_session=False)
        )

    # ================================================================ reads
    @staticmethod
    def _task_query() -> Select[tuple[HiveTaskModel]]:
        return (
            select(HiveTaskModel)
            .options(selectinload(HiveTaskModel.parents), selectinload(HiveTaskModel.children))
            .execution_options(populate_existing=True)
        )

    async def get_task(self, db: AsyncSession, task_id: uuid.UUID) -> HiveTaskModel:
        task = await db.scalar(self._task_query().where(HiveTaskModel.id == task_id))
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found", task_id=task_id)
        return task

    async def list_tasks(
        self, db: AsyncSession, *, status: TaskStatus | None = None, limit: int = 50, offset: int = 0
    ) -> Sequence[HiveTaskModel]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be >= 1 and offset must be >= 0")
        stmt = self._task_query()
        if status is not None:
            stmt = stmt.where(HiveTaskModel.status == status)
        stmt = stmt.order_by(HiveTaskModel.created_at.desc(), HiveTaskModel.id.desc()).offset(offset).limit(limit)
        return (await db.scalars(stmt)).all()

    # =============================================================== create
    async def create_task(
        self,
        db: AsyncSession,
        *,
        title: str,
        description: str = "",
        priority: int = 50,
        max_retries: int = 0,
        payload: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        assignee_name: LegendaryBot | None = None,
    ) -> HiveTaskModel:
        if not 0 <= priority <= 100:
            raise ValueError("priority must be within 0..100")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        task_id = uuid.uuid4()  # captured before commit: expired attributes must not be touched
        db.add(
            HiveTaskModel(
                id=task_id,
                title=title,
                description=description,
                priority=priority,
                max_retries=max_retries,
                retry_count=0,
                payload=dict(payload or {}),
                expires_at=expires_at,
                assignee_name=assignee_name,  # optional reservation for a specific bot
                status=TaskStatus.BACKLOG,
            )
        )
        await db.commit()
        return await self.get_task(db, task_id)

    # ================================================================== DAG
    @staticmethod
    def _descendants_cte(root_ids: Sequence[uuid.UUID]) -> CTE:
        """Transitive closure of children via recursive CTE (O(1) app memory; UNION terminates on cycles)."""
        dep = HiveTaskDependencyModel.__table__
        anchor = (
            select(dep.c.child_task_id.label("task_id"))
            .where(dep.c.parent_task_id.in_(list(root_ids)))
            .cte("hive_descendants", recursive=True)
        )
        previous = anchor.alias("hive_prev")
        step = dep.alias("hive_step")
        return anchor.union(select(step.c.child_task_id).where(step.c.parent_task_id == previous.c.task_id))

    async def _check_for_cycles(self, db: AsyncSession, parent_id: uuid.UUID, child_id: uuid.UUID) -> None:
        """Edge parent->child closes a cycle iff parent is already reachable from child."""
        if parent_id == child_id:
            raise DependencyCycleDetectedError("A task cannot depend on itself", task_id=child_id)
        reachable = self._descendants_cte([child_id])
        hit = await db.scalar(select(reachable.c.task_id).where(reachable.c.task_id == parent_id).limit(1))
        if hit is not None:
            raise DependencyCycleDetectedError(
                f"Dependency {parent_id} -> {child_id} would create a cycle", task_id=child_id
            )

    async def _acquire_dag_lock(self, db: AsyncSession) -> None:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(select(func.pg_advisory_xact_lock(HIVE_DAG_LOCK_KEY)))

    async def add_dependency(
        self, db: AsyncSession, parent_task_id: uuid.UUID, child_task_id: uuid.UUID
    ) -> HiveTaskModel:
        if parent_task_id == child_task_id:
            raise DependencyCycleDetectedError("A task cannot depend on itself", task_id=child_task_id)

        await self._acquire_dag_lock(db)
        rows = (
            await db.execute(
                select(HiveTaskModel.id, HiveTaskModel.status).where(
                    HiveTaskModel.id.in_([parent_task_id, child_task_id])
                )
            )
        ).all()
        statuses: dict[uuid.UUID, TaskStatus] = {row.id: row.status for row in rows}
        for task_id in (parent_task_id, child_task_id):
            if task_id not in statuses:
                await db.rollback()
                raise TaskNotFoundError(f"Task {task_id} not found", task_id=task_id)
        if statuses[child_task_id] not in DEPENDABLE_CHILD_STATUSES:
            await db.rollback()
            raise InvalidTaskStateError(
                f"Child task is {statuses[child_task_id]}; dependencies can only gate BACKLOG/BLOCKED tasks",
                task_id=child_task_id,
            )
        if statuses[parent_task_id] in DEAD_PARENT_STATUSES:
            await db.rollback()
            raise InvalidTaskStateError(
                f"Parent task is {statuses[parent_task_id]}; the child could never run", task_id=parent_task_id
            )

        existing = await db.scalar(
            select(HiveTaskDependencyModel.parent_task_id).where(
                HiveTaskDependencyModel.parent_task_id == parent_task_id,
                HiveTaskDependencyModel.child_task_id == child_task_id,
            )
        )
        if existing is not None:
            await db.rollback()  # idempotent: edge already present
            return await self.get_task(db, child_task_id)

        try:
            await self._check_for_cycles(db, parent_task_id, child_task_id)
        except DependencyCycleDetectedError:
            await db.rollback()
            raise

        db.add(HiveTaskDependencyModel(parent_task_id=parent_task_id, child_task_id=child_task_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # concurrent duplicate insert or deleted task: re-read below decides
        return await self.get_task(db, child_task_id)

    async def _cascade(
        self,
        db: AsyncSession,
        root_ids: Iterable[uuid.UUID],
        *,
        new_status: TaskStatus,
        from_statuses: Sequence[TaskStatus],
    ) -> list[uuid.UUID]:
        roots = list(dict.fromkeys(root_ids))
        affected: list[uuid.UUID] = []
        for start in range(0, len(roots), CASCADE_ROOT_CHUNK):
            descendants = self._descendants_cte(roots[start : start + CASCADE_ROOT_CHUNK])
            stmt = (
                update(HiveTaskModel)
                .where(
                    HiveTaskModel.id.in_(select(descendants.c.task_id)),
                    HiveTaskModel.status.in_(list(from_statuses)),
                )
                .values(status=new_status)
                .returning(HiveTaskModel.id)
                .execution_options(synchronize_session=False)
            )
            affected.extend((await db.execute(stmt)).scalars().all())
        return affected

    # ================================================================ claim
    @staticmethod
    def _unmet_dependencies() -> Exists:
        dep = HiveTaskDependencyModel
        parent = aliased(HiveTaskModel, name="hive_parent")
        return (
            select(dep.parent_task_id)
            .join(parent, parent.id == dep.parent_task_id)
            .where(dep.child_task_id == HiveTaskModel.id, parent.status != TaskStatus.DONE)
            .correlate(HiveTaskModel)
            .exists()
        )

    def _claimable_conditions(self, bot_name: LegendaryBot, now: datetime) -> list[ColumnElement[bool]]:
        return [
            HiveTaskModel.status == TaskStatus.BACKLOG,
            or_(HiveTaskModel.expires_at.is_(None), HiveTaskModel.expires_at > now),
            or_(HiveTaskModel.assignee_name.is_(None), HiveTaskModel.assignee_name == bot_name),
            ~self._unmet_dependencies(),
        ]

    async def _cas_claim(
        self, db: AsyncSession, task_id: uuid.UUID, bot_name: LegendaryBot, now: datetime
    ) -> bool:
        stmt = (
            update(HiveTaskModel)
            .where(HiveTaskModel.id == task_id, *self._claimable_conditions(bot_name, now))
            .values(status=TaskStatus.IN_PROGRESS, assignee_name=bot_name)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await db.execute(stmt))
        return result.rowcount == 1

    async def _raise_claim_failure(
        self, db: AsyncSession, task_id: uuid.UUID, bot_name: LegendaryBot, now: datetime
    ) -> NoReturn:
        task = await db.get(HiveTaskModel, task_id, populate_existing=True)
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found", task_id=task_id)
        if task.status == TaskStatus.EXPIRED or (
            task.expires_at is not None and ensure_aware_utc(task.expires_at) <= now
        ):
            raise TaskExpiredError(f"Task {task_id} has expired", task_id=task_id)
        if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEW):
            raise TaskAlreadyClaimedError(
                f"Task {task_id} already claimed by {task.assignee_name}", task_id=task_id
            )
        if task.status == TaskStatus.BACKLOG and task.assignee_name not in (None, bot_name):
            raise TaskAlreadyClaimedError(
                f"Task {task_id} is reserved for {task.assignee_name}", task_id=task_id
            )
        if task.status == TaskStatus.BACKLOG:
            raise InvalidTaskStateError(f"Task {task_id} has unmet dependencies", task_id=task_id)
        raise InvalidTaskStateError(f"Task {task_id} is {task.status} and cannot be claimed", task_id=task_id)

    async def claim_task(self, db: AsyncSession, task_id: uuid.UUID, bot_name: LegendaryBot) -> HiveTaskModel:
        """Targeted atomic claim of a specific task."""
        now = utc_now()
        if not await self._cas_claim(db, task_id, bot_name, now):
            await db.rollback()
            await self._raise_claim_failure(db, task_id, bot_name, now)
        await self._upsert_bot(db, bot_name, now=now, status=BotStatus.WORKING)
        await db.commit()
        return await self.get_task(db, task_id)

    async def claim_highest_priority_task(
        self, db: AsyncSession, bot_name: LegendaryBot
    ) -> HiveTaskModel | None:
        now = utc_now()
        candidate_stmt = (
            select(HiveTaskModel.id)
            .where(*self._claimable_conditions(bot_name, now))
            .order_by(HiveTaskModel.priority.desc(), HiveTaskModel.created_at.asc(), HiveTaskModel.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True, of=HiveTaskModel)  # PostgreSQL; no-op on SQLite
        )
        task_id = await db.scalar(candidate_stmt)
        if task_id is None:
            await db.rollback()
            return None
        if not await self._cas_claim(db, task_id, bot_name, now):
            await db.rollback()
            raise TaskAlreadyClaimedError(f"Task {task_id} was claimed concurrently", task_id=task_id)
        await self._upsert_bot(db, bot_name, now=now, status=BotStatus.WORKING)
        await db.commit()
        return await self.get_task(db, task_id)

    # ============================================================ lifecycle
    async def _raise_ownership_failure(
        self, db: AsyncSession, task_id: uuid.UUID, bot_name: LegendaryBot
    ) -> NoReturn:
        task = await db.get(HiveTaskModel, task_id, populate_existing=True)
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found", task_id=task_id)
        if task.status == TaskStatus.IN_PROGRESS and task.assignee_name != bot_name:
            raise TaskAlreadyClaimedError(f"Task {task_id} is owned by {task.assignee_name}", task_id=task_id)
        raise InvalidTaskStateError(
            f"Task {task_id} is {task.status}; expected IN_PROGRESS owned by {bot_name}", task_id=task_id
        )

    async def complete_task(
        self,
        db: AsyncSession,
        task_id: uuid.UUID,
        bot_name: LegendaryBot,
        result_payload: Mapping[str, Any],
    ) -> HiveTaskModel:
        now = utc_now()
        stmt = (
            update(HiveTaskModel)
            .where(
                HiveTaskModel.id == task_id,
                HiveTaskModel.status == TaskStatus.IN_PROGRESS,
                HiveTaskModel.assignee_name == bot_name,
            )
            .values(status=TaskStatus.DONE, result_payload=dict(result_payload))
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await db.execute(stmt))
        if result.rowcount != 1:
            await db.rollback()
            await self._raise_ownership_failure(db, task_id, bot_name)
        await self._bump_bot_counter(db, bot_name, completed=1, now=now)
        await db.commit()
        return await self.get_task(db, task_id)

    async def fail_task(
        self,
        db: AsyncSession,
        task_id: uuid.UUID,
        bot_name: LegendaryBot,
        error_payload: Mapping[str, Any],
    ) -> FailureOutcome:
        now = utc_now()
        snapshot = (
            await db.execute(
                select(
                    HiveTaskModel.status,
                    HiveTaskModel.assignee_name,
                    HiveTaskModel.retry_count,
                    HiveTaskModel.max_retries,
                )
                .where(HiveTaskModel.id == task_id)
                .with_for_update()
            )
        ).one_or_none()
        if snapshot is None:
            await db.rollback()
            raise TaskNotFoundError(f"Task {task_id} not found", task_id=task_id)
        if snapshot.status != TaskStatus.IN_PROGRESS or snapshot.assignee_name != bot_name:
            await db.rollback()
            await self._raise_ownership_failure(db, task_id, bot_name)

        retried = snapshot.retry_count < snapshot.max_retries
        failure_record = {"error": dict(error_payload), "failed_attempts": snapshot.retry_count + 1}
        values: dict[str, Any] = (
            {
                "status": TaskStatus.BACKLOG,
                "assignee_name": None,
                "retry_count": snapshot.retry_count + 1,
                "result_payload": failure_record,
            }
            if retried
            else {"status": TaskStatus.FAILED, "result_payload": failure_record}
        )
        stmt = (
            update(HiveTaskModel)
            .where(
                HiveTaskModel.id == task_id,
                HiveTaskModel.status == TaskStatus.IN_PROGRESS,
                HiveTaskModel.assignee_name == bot_name,
                HiveTaskModel.retry_count == snapshot.retry_count,  # optimistic CAS guard
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await db.execute(stmt))
        if result.rowcount != 1:
            await db.rollback()
            raise TaskAlreadyClaimedError(f"Task {task_id} was modified concurrently", task_id=task_id)

        cascaded: list[uuid.UUID] = []
        if not retried:
            cascaded = await self._cascade(
                db, [task_id], new_status=TaskStatus.FAILED, from_statuses=DEPENDABLE_CHILD_STATUSES
            )
        await self._bump_bot_counter(db, bot_name, errors=1, now=now)
        await db.commit()
        return FailureOutcome(
            task=await self.get_task(db, task_id), retried=retried, cascaded_task_ids=tuple(cascaded)
        )

    # ============================================================== sweeper
    async def sweep_ghosts_and_expired(self, db: AsyncSession) -> SweepReport:
        now = utc_now()
        cutoff = now - self._ghost_timeout

        assignee_alive = (
            select(BotProfileModel.id)
            .where(
                BotProfileModel.bot_name == HiveTaskModel.assignee_name,
                BotProfileModel.last_ping_at >= cutoff,
            )
            .correlate(HiveTaskModel)
            .exists()
        )
        reverted = (
            await db.execute(
                update(HiveTaskModel)
                .where(HiveTaskModel.status == TaskStatus.IN_PROGRESS, ~assignee_alive)
                .values(status=TaskStatus.BACKLOG, assignee_name=None)
                .returning(HiveTaskModel.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars().all()

        expired = (
            await db.execute(
                update(HiveTaskModel)
                .where(
                    HiveTaskModel.status == TaskStatus.BACKLOG,
                    HiveTaskModel.expires_at.is_not(None),
                    HiveTaskModel.expires_at <= now,
                )
                .values(status=TaskStatus.EXPIRED)
                .returning(HiveTaskModel.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars().all()

        blocked = await self._cascade(
            db, expired, new_status=TaskStatus.BLOCKED, from_statuses=(TaskStatus.BACKLOG,)
        )

        offline = (
            await db.execute(
                update(BotProfileModel)
                .where(
                    BotProfileModel.last_ping_at < cutoff,
                    BotProfileModel.status.not_in((BotStatus.OFFLINE, BotStatus.FATAL)),
                )
                .values(status=BotStatus.OFFLINE)
                .returning(BotProfileModel.bot_name)
                .execution_options(synchronize_session=False)
            )
        ).scalars().all()

        await db.commit()
        report = SweepReport(
            reverted_task_ids=tuple(reverted),
            expired_task_ids=tuple(expired),
            blocked_task_ids=tuple(blocked),
            offline_bots=tuple(offline),
        )
        if reverted or expired or blocked or offline:
            logger.info(
                "Hive sweep: reverted=%d expired=%d blocked=%d offline_bots=%d",
                len(reverted), len(expired), len(blocked), len(offline),
            )
        return report

    # ============================================================= learning
    async def record_learning(
        self,
        db: AsyncSession,
        *,
        bot_name: LegendaryBot,
        parameter_name: str,
        old_value: Any,
        new_value: Any,
        reasoning: str,
        confidence_score: float,
    ) -> SelfLearningLogModel:
        if not math.isfinite(confidence_score) or not 0.0 <= confidence_score <= 1.0:
            raise ValueError("confidence_score must be within 0.0..1.0")
        entry = SelfLearningLogModel(
            id=uuid.uuid4(),
            bot_name=bot_name,
            parameter_name=parameter_name,
            old_value=old_value,
            new_value=new_value,
            reasoning=reasoning,
            confidence_score=confidence_score,
        )
        db.add(entry)
        await db.commit()
        await db.refresh(entry)
        return entry
