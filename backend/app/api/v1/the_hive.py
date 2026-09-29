import asyncio
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, get_db
from app.domain.the_hive import (
    DependencyCycleDetectedError,
    HiveDomainError,
    HiveOrchestrator,
    InvalidTaskStateError,
    SweepReport,
    TaskAlreadyClaimedError,
    TaskExpiredError,
    TaskNotFoundError,
)
from app.models.the_hive import HiveTaskModel, LegendaryBot, TaskStatus
from app.schemas.the_hive import (
    BotProfileRead,
    ClaimRequest,
    CompleteRequest,
    DependencyCreate,
    FailRequest,
    FailureRead,
    HeartbeatRequest,
    LearningLogCreate,
    LearningLogRead,
    TaskCreate,
    TaskEvent,
    TaskEventType,
    TaskRead,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hive", tags=["The Hive"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]

MAX_WS_CONNECTIONS: Final[int] = 500
WS_SEND_TIMEOUT_SECONDS: Final[float] = 2.0
SWEEP_INTERVAL_SECONDS: Final[float] = 60.0

_ERROR_STATUS: Final[tuple[tuple[type[HiveDomainError], int], ...]] = (
    (TaskNotFoundError, status.HTTP_404_NOT_FOUND),
    (TaskExpiredError, status.HTTP_410_GONE),
    (TaskAlreadyClaimedError, status.HTTP_409_CONFLICT),
    (DependencyCycleDetectedError, status.HTTP_409_CONFLICT),
    (InvalidTaskStateError, status.HTTP_409_CONFLICT),
)


def _http_error(exc: HiveDomainError) -> HTTPException:
    for error_type, code in _ERROR_STATUS:
        if isinstance(exc, error_type):
            return HTTPException(status_code=code, detail=exc.message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)


class ConnectionManager:
    """In-process WebSocket fan-out with bounded connections and slow-consumer eviction."""

    def __init__(
        self, *, max_connections: int = MAX_WS_CONNECTIONS, send_timeout: float = WS_SEND_TIMEOUT_SECONDS
    ) -> None:
        self._connections: set[WebSocket] = set()
        self._max_connections = max_connections
        self._send_timeout = send_timeout

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> bool:
        if len(self._connections) >= self._max_connections:
            await websocket.close(code=1013)  # Try Again Later
            return False
        await websocket.accept()
        self._connections.add(websocket)
        return True

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: str) -> None:
        targets = tuple(self._connections)
        if not targets:
            return
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(message), timeout=self._send_timeout) for ws in targets),
            return_exceptions=True,
        )
        for websocket, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                self.disconnect(websocket)

    async def publish(self, events: Sequence[TaskEvent]) -> None:
        for task_event in events:
            await self.broadcast(task_event.model_dump_json())


orchestrator = HiveOrchestrator()
manager = ConnectionManager()


def _now() -> datetime:
    return datetime.now(UTC)


def _task_event(event: TaskEventType, task: HiveTaskModel) -> TaskEvent:
    return TaskEvent(
        event=event, task_id=task.id, status=task.status, assignee_name=task.assignee_name, occurred_at=_now()
    )


def _bulk_events(event: TaskEventType, task_ids: Iterable[uuid.UUID], task_status: TaskStatus) -> list[TaskEvent]:
    occurred_at = _now()
    return [TaskEvent(event=event, task_id=tid, status=task_status, occurred_at=occurred_at) for tid in task_ids]


def sweep_events(report: SweepReport) -> list[TaskEvent]:
    return [
        *_bulk_events(TaskEventType.TASK_REVERTED, report.reverted_task_ids, TaskStatus.BACKLOG),
        *_bulk_events(TaskEventType.TASK_EXPIRED, report.expired_task_ids, TaskStatus.EXPIRED),
        *_bulk_events(TaskEventType.TASK_BLOCKED, report.blocked_task_ids, TaskStatus.BLOCKED),
    ]


async def run_hive_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, interval_seconds: float = SWEEP_INTERVAL_SECONDS
) -> None:
    """Long-running sweeper loop; start from the app lifespan and cancel on shutdown."""
    while True:
        try:
            async with session_factory() as session:
                report = await orchestrator.sweep_ghosts_and_expired(session)
            await manager.publish(sweep_events(report))
        except Exception:
            logger.exception("Hive sweeper iteration failed")
        await asyncio.sleep(interval_seconds)


# ================================================================ websocket
@router.websocket("/board/live")
async def board_live(websocket: WebSocket) -> None:
    # SECURITY: add token verification here before production exposure.
    if not await manager.connect(websocket):
        return
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text('{"event":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


# ===================================================================== bots
@router.post("/bots/{name}/heartbeat", response_model=BotProfileRead)
async def bot_heartbeat(
    name: LegendaryBot, payload: HeartbeatRequest, current_user: CurrentUser, db: DbSession  # noqa: ARG001
) -> BotProfileRead:
    profile = await orchestrator.record_heartbeat(
        db,
        name,
        status=payload.status,
        uptime_seconds=payload.uptime_seconds,
        resource_metrics=payload.resource_metrics,
    )
    return BotProfileRead.model_validate(profile)


# ==================================================================== tasks
@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[TaskRead]:
    tasks = await orchestrator.list_tasks(db, status=task_status, limit=limit, offset=offset)
    return [TaskRead.from_model(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> TaskRead:  # noqa: ARG001
    try:
        return TaskRead.from_model(await orchestrator.get_task(db, task_id))
    except HiveDomainError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate, background_tasks: BackgroundTasks, current_user: CurrentUser, db: DbSession  # noqa: ARG001
) -> TaskRead:
    task = await orchestrator.create_task(
        db,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        max_retries=payload.max_retries,
        payload=payload.payload,
        expires_at=payload.expires_at,
        assignee_name=payload.assignee_name,
    )
    background_tasks.add_task(manager.publish, [_task_event(TaskEventType.TASK_CREATED, task)])
    return TaskRead.from_model(task)


@router.post("/tasks/{task_id}/dependencies", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def add_dependency(
    task_id: uuid.UUID,
    payload: DependencyCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> TaskRead:
    try:
        child = await orchestrator.add_dependency(db, payload.parent_task_id, task_id)
    except HiveDomainError as exc:
        raise _http_error(exc) from exc
    background_tasks.add_task(manager.publish, [_task_event(TaskEventType.DEPENDENCY_ADDED, child)])
    return TaskRead.from_model(child)


@router.post(
    "/tasks/claim",
    response_model=TaskRead,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No claimable task"}},
)
async def claim_task(
    payload: ClaimRequest, background_tasks: BackgroundTasks, current_user: CurrentUser, db: DbSession  # noqa: ARG001
) -> TaskRead | Response:
    try:
        task = await orchestrator.claim_highest_priority_task(db, payload.bot_name)
    except HiveDomainError as exc:
        raise _http_error(exc) from exc
    if task is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    background_tasks.add_task(manager.publish, [_task_event(TaskEventType.TASK_CLAIMED, task)])
    return TaskRead.from_model(task)


@router.patch("/tasks/{task_id}/complete", response_model=TaskRead)
async def complete_task(
    task_id: uuid.UUID,
    payload: CompleteRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> TaskRead:
    try:
        task = await orchestrator.complete_task(db, task_id, payload.bot_name, payload.result_payload)
    except HiveDomainError as exc:
        raise _http_error(exc) from exc
    background_tasks.add_task(manager.publish, [_task_event(TaskEventType.TASK_COMPLETED, task)])
    return TaskRead.from_model(task)


@router.patch("/tasks/{task_id}/fail", response_model=FailureRead)
async def fail_task(
    task_id: uuid.UUID,
    payload: FailRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> FailureRead:
    try:
        outcome = await orchestrator.fail_task(db, task_id, payload.bot_name, payload.error)
    except HiveDomainError as exc:
        raise _http_error(exc) from exc
    primary = TaskEventType.TASK_RETRY_SCHEDULED if outcome.retried else TaskEventType.TASK_FAILED
    events = [
        _task_event(primary, outcome.task),
        *_bulk_events(TaskEventType.TASK_CASCADE_FAILED, outcome.cascaded_task_ids, TaskStatus.FAILED),
    ]
    background_tasks.add_task(manager.publish, events)
    return FailureRead(
        task=TaskRead.from_model(outcome.task),
        retried=outcome.retried,
        cascaded_task_ids=list(outcome.cascaded_task_ids),
    )


# ================================================================= learning
@router.post("/learning-logs", response_model=LearningLogRead, status_code=status.HTTP_201_CREATED)
async def create_learning_log(
    payload: LearningLogCreate, current_user: CurrentUser, db: DbSession  # noqa: ARG001
) -> LearningLogRead:
    entry = await orchestrator.record_learning(
        db,
        bot_name=payload.bot_name,
        parameter_name=payload.parameter_name,
        old_value=payload.old_value,
        new_value=payload.new_value,
        reasoning=payload.reasoning,
        confidence_score=payload.confidence_score,
    )
    return LearningLogRead.model_validate(entry)
