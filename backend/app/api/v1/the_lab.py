import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser, get_db
from app.core.database import AsyncSessionLocal
from app.domain.phantom.errors import PhantomDomainError
from app.domain.phantom.manager import PhantomManager
from app.domain.the_lab.data_agent import DataResearchAgent
from app.domain.the_lab import (
    ApiHealthMonitor,
    ExperimentManager,
    ExperimentNotFoundError,
    ExperimentStateConflictError,
    InvalidExperimentWinnerError,
    ResearchExecutor,
    ResearchReportManager,
)
from app.schemas.phantom import (
    CointegrationRequest,
    CointegrationResponse,
    DutchingRequest,
    DutchingResponse,
    MarketMakerRequest,
    MarketMakerResponse,
    MatchedBettingRequest,
    MatchedBettingResponse,
    PhantomCalculationLogRead,
)
from app.schemas.the_lab import (
    ApiHealthStatus,
    ExperimentConclude,
    ExperimentCreate,
    ExperimentRead,
    ResearchCreate,
    ResearchRead,
)

router = APIRouter(tags=["the_lab"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
Limit = Annotated[int, Query(ge=1, le=100, description="Page size")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip")]

health_monitor = ApiHealthMonitor()
research_manager = ResearchReportManager()
research_executor = ResearchExecutor()
research_agent = DataResearchAgent(AsyncSessionLocal)
experiment_manager = ExperimentManager()


# ------------------------------------------------------------------ health
@router.get("/health", response_model=list[ApiHealthStatus])
async def get_api_health(current_user: CurrentUser) -> list[ApiHealthStatus]:  # noqa: ARG001
    return await health_monitor.check_all()


# ---------------------------------------------------------------- research
@router.post("/research", response_model=ResearchRead, status_code=status.HTTP_202_ACCEPTED)
async def submit_research(
    payload: ResearchCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> ResearchRead:
    bind = db.bind
    if bind is None:  # verify before persisting so no orphaned PENDING row is created
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database session is not bound to an engine",
        )

    report = await research_manager.create_report(db, category=payload.category, topic=payload.topic)
    response = ResearchRead.model_validate(report)

    # Fresh factory: the request-scoped session is closed before background tasks run.
    session_factory = async_sessionmaker(bind, expire_on_commit=False)
    background_tasks.add_task(
        research_executor.execute_task,
        response.id,
        response.topic,
        research_agent,
        session_factory,
    )
    return response


@router.get("/research", response_model=list[ResearchRead])
async def list_research(
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[ResearchRead]:
    reports = await research_manager.list_reports(db, skip=offset, limit=limit)
    return [ResearchRead.model_validate(r) for r in reports]


@router.get("/research/{report_id}", response_model=ResearchRead)
async def get_research(
    report_id: uuid.UUID,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> ResearchRead:
    report = await research_manager.get_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research report not found")
    return ResearchRead.model_validate(report)


# ------------------------------------------------------------- experiments
@router.get("/experiments", response_model=list[ExperimentRead])
async def list_experiments(
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[ExperimentRead]:
    experiments = await experiment_manager.list_experiments(db, skip=offset, limit=limit)
    return [ExperimentRead.model_validate(e) for e in experiments]


@router.post("/experiments", response_model=ExperimentRead, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    payload: ExperimentCreate,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> ExperimentRead:
    experiment = await experiment_manager.create_experiment(db, payload.model_dump())
    return ExperimentRead.model_validate(experiment)


@router.patch("/experiments/{experiment_id}/conclude", response_model=ExperimentRead)
async def conclude_experiment(
    experiment_id: uuid.UUID,
    payload: ExperimentConclude,
    current_user: CurrentUser,  # noqa: ARG001
    db: DbSession,
) -> ExperimentRead:
    try:
        experiment = await experiment_manager.conclude_experiment(
            db, experiment_id, winner=payload.winner, metrics=payload.metrics
        )
    except ExperimentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExperimentStateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidExperimentWinnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ExperimentRead.model_validate(experiment)


# ================================================================ Group 96: the quant toolkit (absorbed from Phantom)
quant_manager = PhantomManager()


def _quant_error(exc: PhantomDomainError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)


@router.post("/quant-toolkit/dutching", response_model=DutchingResponse)
async def calculate_dutching(payload: DutchingRequest, db: DbSession, current_user: CurrentUser) -> DutchingResponse:  # noqa: ARG001
    """Stakes that return the same amount whichever selection wins."""
    try:
        outcome = await quant_manager.calculate_dutching(db, target_total_stake=payload.target_total_stake, odds=payload.odds)
    except PhantomDomainError as exc:
        raise _quant_error(exc) from exc
    return DutchingResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/quant-toolkit/matched-betting", response_model=MatchedBettingResponse)
async def calculate_matched_bet(payload: MatchedBettingRequest, db: DbSession, current_user: CurrentUser) -> MatchedBettingResponse:  # noqa: ARG001
    """The lay stake that balances a back bet (standard, underlay or overlay)."""
    try:
        outcome = await quant_manager.calculate_matched_bet(
            db, back_stake=payload.back_stake, back_odds=payload.back_odds, lay_odds=payload.lay_odds,
            lay_commission_pct=payload.lay_commission_pct, mode=payload.mode,
        )
    except PhantomDomainError as exc:
        raise _quant_error(exc) from exc
    return MatchedBettingResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/quant-toolkit/market-maker", response_model=MarketMakerResponse)
async def calculate_market_maker(payload: MarketMakerRequest, db: DbSession, current_user: CurrentUser) -> MarketMakerResponse:  # noqa: ARG001
    """Avellaneda-Stoikov reservation price and optimal bid/ask."""
    try:
        outcome = await quant_manager.calculate_market_maker_quotes(
            db, mid_price=payload.mid_price, inventory=payload.inventory, gamma=payload.gamma,
            volatility_sigma=payload.volatility_sigma, time_horizon_t=payload.time_horizon_t,
            current_time_t=payload.current_time_t, liquidity_k=payload.liquidity_k,
        )
    except PhantomDomainError as exc:
        raise _quant_error(exc) from exc
    return MarketMakerResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.post("/quant-toolkit/cointegration", response_model=CointegrationResponse)
async def evaluate_cointegration(payload: CointegrationRequest, db: DbSession, current_user: CurrentUser) -> CointegrationResponse:  # noqa: ARG001
    """The pairs-trade signal for a spread z-score against its entry, exit and stop thresholds."""
    try:
        outcome = await quant_manager.evaluate_cointegration(
            db, current_z_score=payload.current_z_score, entry_threshold=payload.entry_threshold,
            exit_threshold=payload.exit_threshold, stop_loss_threshold=payload.stop_loss_threshold,
        )
    except PhantomDomainError as exc:
        raise _quant_error(exc) from exc
    return CointegrationResponse(**outcome["result"], log=PhantomCalculationLogRead.model_validate(outcome["log"]))


@router.get("/quant-toolkit/calculations", response_model=list[PhantomCalculationLogRead])
async def list_quant_calculations(
    db: DbSession,
    current_user: CurrentUser,  # noqa: ARG001
    calc_type: Annotated[str | None, Query(max_length=32, description="DUTCHING, MATCHED_BETTING, AVELLANEDA or COINTEGRATION")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[PhantomCalculationLogRead]:
    """The newest quant toolkit runs."""
    try:
        logs = await quant_manager.list_calculations(db, calc_type=calc_type, limit=limit)
    except PhantomDomainError as exc:
        raise _quant_error(exc) from exc
    return [PhantomCalculationLogRead.model_validate(log) for log in logs]
