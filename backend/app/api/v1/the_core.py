"""The Core HTTP + WebSocket surface. Mounted under /engine by the main router."""



import asyncio

import copy

import logging

from datetime import UTC, datetime

from typing import Annotated, Any

from uuid import UUID



from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker



from app.api.deps import WsUser, get_db

from app.core.database import AsyncSessionLocal

from app.domain.the_core.errors import (

    CoreDomainError,

    EngineConcurrencyError,

    InvalidSmallcaseStateError,

    SmallcaseNotFoundError,

)

from app.domain.the_core.orchestrator import CoreOrchestrator

from app.models.the_core import BacktestJobModel, SmallcaseStatus

from app.schemas.the_core import (

    BacktestJobRead,

    BacktestJobRequest,

    EngineTelemetryRead,

    SmallcaseDetailRead,

    SmallcaseRead,

    SmallcaseToggleRequest,

    TestBenchRequest,

    TestBenchRunRead,

    TestCompareRequest,

)



logger = logging.getLogger(__name__)



router = APIRouter(tags=["Engine Room"])



WS_SEND_TIMEOUT_S = 5.0



STRESS_MATCH_CONTEXT: dict[str, Any] = {

    "home_team": "PRATAP Stress XI",

    "away_team": "PANINI Chaos FC",

    "league": "The Core Crucible",

    "home_expected_goals": 0.0,

    "away_expected_goals": 25.0,

    "odds": {"home": 1_000_000.0, "draw": 1.0, "away": 0.0},

    "stress_profile": ["zero_probability", "insane_odds", "non_positive_odds", "extreme_expected_goals"],

}





def _envelope(event: str, payload: dict[str, Any]) -> dict[str, Any]:

    return {"event": event, "source": "PRATAP", "timestamp": datetime.now(UTC).isoformat(), "payload": payload}





class ConnectionManager:

    """In-memory fan-out hub for the Engine Room live stream."""



    def __init__(self) -> None:

        self._connections: set[WebSocket] = set()



    @property

    def active_connections(self) -> int:

        return len(self._connections)



    async def connect(self, websocket: WebSocket) -> None:

        await websocket.accept()

        self._connections.add(websocket)



    def disconnect(self, websocket: WebSocket) -> None:

        self._connections.discard(websocket)



    async def _safe_send(self, websocket: WebSocket, message: dict[str, Any]) -> bool:

        try:

            await asyncio.wait_for(websocket.send_json(message), timeout=WS_SEND_TIMEOUT_S)

            return True

        except Exception:  # noqa: BLE001 - dead or slow sockets are pruned

            return False



    async def broadcast(self, message: dict[str, Any]) -> None:

        targets = list(self._connections)

        if not targets:

            return

        results = await asyncio.gather(*(self._safe_send(ws, message) for ws in targets))

        for websocket, delivered in zip(targets, results, strict=True):

            if not delivered:

                self.disconnect(websocket)





engine_room_manager = ConnectionManager()

_orchestrator = CoreOrchestrator()





def get_ws_manager() -> ConnectionManager:

    return engine_room_manager





def get_orchestrator() -> CoreOrchestrator:

    return _orchestrator





def get_session_factory() -> async_sessionmaker[AsyncSession]:

    return AsyncSessionLocal





DbSession = Annotated[AsyncSession, Depends(get_db)]

Orchestrator = Annotated[CoreOrchestrator, Depends(get_orchestrator)]

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]

WsManager = Annotated[ConnectionManager, Depends(get_ws_manager)]





def _http_error(exc: CoreDomainError) -> HTTPException:

    if isinstance(exc, SmallcaseNotFoundError):

        code = status.HTTP_404_NOT_FOUND

    elif isinstance(exc, (InvalidSmallcaseStateError, EngineConcurrencyError)):

        code = status.HTTP_409_CONFLICT

    else:

        code = status.HTTP_400_BAD_REQUEST

    return HTTPException(status_code=code, detail=exc.to_dict())





async def _safe_broadcast(manager: ConnectionManager, event: str, payload: dict[str, Any]) -> None:

    try:

        await manager.broadcast(_envelope(event, payload))

    except Exception:  # noqa: BLE001 - live stream must never break an HTTP request

        logger.warning("Engine Room broadcast failed for %s", event, exc_info=True)





@router.get("/status", response_model=EngineTelemetryRead)

async def get_engine_status(db: DbSession, orch: Orchestrator) -> EngineTelemetryRead:

    return await orch.get_engine_status(db)





@router.post("/bootstrap", response_model=list[SmallcaseRead])

async def bootstrap_smallcases(db: DbSession, orch: Orchestrator) -> list[SmallcaseRead]:

    smallcases = await orch.bootstrap_smallcases(db)

    return [SmallcaseRead.model_validate(s) for s in smallcases]





@router.get("/smallcases", response_model=list[SmallcaseRead])

async def list_smallcases(

    db: DbSession,

    orch: Orchestrator,

    status_filter: Annotated[SmallcaseStatus | None, Query(alias="status")] = None,

) -> list[SmallcaseRead]:

    smallcases = await orch.list_smallcases(db, status=status_filter)

    return [SmallcaseRead.model_validate(s) for s in smallcases]





@router.get("/smallcases/{smallcase_id}", response_model=SmallcaseDetailRead)

async def get_smallcase(smallcase_id: UUID, db: DbSession, orch: Orchestrator) -> SmallcaseDetailRead:

    try:

        return await orch.get_smallcase_details(db, smallcase_id)

    except CoreDomainError as exc:

        raise _http_error(exc) from exc





@router.post("/smallcases/{smallcase_id}/toggle", response_model=SmallcaseRead)

async def toggle_smallcase(

    smallcase_id: UUID,

    payload: SmallcaseToggleRequest,

    db: DbSession,

    orch: Orchestrator,

    ws_manager: WsManager,

) -> SmallcaseRead:

    try:

        smallcase = await orch.toggle_smallcase(

            db, smallcase_id, expected_status=payload.expected_status, target_status=payload.target_status

        )

    except CoreDomainError as exc:

        raise _http_error(exc) from exc

    response = SmallcaseRead.model_validate(smallcase)

    await _safe_broadcast(

        ws_manager,

        "smallcase.toggled",

        {

            "smallcase_id": str(response.id),

            "previous_status": payload.expected_status.value,

            "status": response.status.value,

        },

    )

    return response





@router.post(

    "/smallcases/{smallcase_id}/stress-test",

    response_model=TestBenchRunRead,

    status_code=status.HTTP_202_ACCEPTED,

)

async def stress_test_smallcase(

    smallcase_id: UUID,

    background_tasks: BackgroundTasks,

    db: DbSession,

    orch: Orchestrator,

    session_factory: SessionFactory,

    ws_manager: WsManager,

) -> TestBenchRunRead:

    try:

        run = await orch.create_test_bench_run(

            db, smallcase_id, copy.deepcopy(STRESS_MATCH_CONTEXT), is_stress_test=True

        )

    except CoreDomainError as exc:

        raise _http_error(exc) from exc

    response = TestBenchRunRead.model_validate(run)

    background_tasks.add_task(orch.execute_test_bench, session_factory, response.id, ws_manager, True)

    return response





@router.post("/test", response_model=TestBenchRunRead, status_code=status.HTTP_202_ACCEPTED)

async def run_test_bench(

    payload: TestBenchRequest,

    background_tasks: BackgroundTasks,

    db: DbSession,

    orch: Orchestrator,

    session_factory: SessionFactory,

    ws_manager: WsManager,

) -> TestBenchRunRead:

    try:

        run = await orch.create_test_bench_run(db, payload.smallcase_id, payload.match_context)

    except CoreDomainError as exc:

        raise _http_error(exc) from exc

    response = TestBenchRunRead.model_validate(run)

    background_tasks.add_task(orch.execute_test_bench, session_factory, response.id, ws_manager, False)

    return response





@router.post("/test-compare", response_model=list[TestBenchRunRead], status_code=status.HTTP_202_ACCEPTED)

async def run_test_compare(

    payload: TestCompareRequest,

    background_tasks: BackgroundTasks,

    db: DbSession,

    orch: Orchestrator,

    session_factory: SessionFactory,

    ws_manager: WsManager,

) -> list[TestBenchRunRead]:

    try:

        runs = await orch.create_test_bench_runs(db, payload.smallcase_ids, payload.match_context)

    except CoreDomainError as exc:

        raise _http_error(exc) from exc

    responses = [TestBenchRunRead.model_validate(run) for run in runs]

    for response in responses:

        background_tasks.add_task(orch.execute_test_bench, session_factory, response.id, ws_manager, False)

    return responses





@router.post("/backtest", response_model=BacktestJobRead, status_code=status.HTTP_202_ACCEPTED)

async def run_backtest(

    payload: BacktestJobRequest,

    background_tasks: BackgroundTasks,

    db: DbSession,

    orch: Orchestrator,

    session_factory: SessionFactory,

    ws_manager: WsManager,

) -> BacktestJobRead:

    try:

        job = await orch.create_backtest_job(db, payload.smallcase_id, payload.start_date, payload.end_date)

    except CoreDomainError as exc:

        raise _http_error(exc) from exc

    response = BacktestJobRead.model_validate(job)

    background_tasks.add_task(orch.execute_backtest_job, session_factory, response.id, ws_manager)

    return response





@router.websocket("/live")

async def engine_room_live(websocket: WebSocket, manager: WsManager, user: WsUser) -> None:  # noqa: ARG001 - auth gate

    await manager.connect(websocket)

    try:

        await websocket.send_json(

            _envelope(

                "engine.connected",

                {

                    "master_bot": "PRATAP",

                    "math_engine": "PANINI",

                    "active_connections": manager.active_connections,

                    "message": "Connected to the Engine Room conveyor belt.",

                },

            )

        )

        while True:

            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":

                break

            text = (message.get("text") or "").strip().lower()

            if text == "ping":

                await websocket.send_json(

                    _envelope("engine.pong", {"active_connections": manager.active_connections})

                )

    except WebSocketDisconnect:

        pass

    finally:

        manager.disconnect(websocket)


@router.get("/backtests", response_model=list[BacktestJobRead])
async def list_backtests(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[BacktestJobRead]:
    """Backtest jobs, newest first: queued, running and completed with their ROI / accuracy / drawdown."""
    stmt = select(BacktestJobModel).order_by(BacktestJobModel.created_at.desc()).limit(limit)
    return [BacktestJobRead.model_validate(row) for row in (await db.execute(stmt)).scalars().all()]
