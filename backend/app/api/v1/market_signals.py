import logging
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.database import AsyncSessionLocal
from app.domain.market_signals import (
    SQLOddsTickRepository,
    SteamDetectorEngine,
    find_back_lay_arbitrage,
    find_best_price,
    find_market_surebets,
)
from app.models import utc_now
from app.schemas.market_signals import (
    MAX_FUTURE_SKEW,
    ArbitrageOpportunity,
    LineShopResult,
    MarketSurebet,
    OddsTick,
    OddsType,
    SteamAlert,
    TickIngestResult,
)

signals_log = logging.getLogger("betdoc.signals")

router = APIRouter(tags=["signals"])

_steam = SteamDetectorEngine()

MAX_INGEST_BATCH = 5000
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_MIN_PROB_DELTA = 2.0        # percentage points
DEFAULT_MIN_LINE_SHIFT = 0.25
DEFAULT_MIN_TIME_DELTA_SECONDS = 60


async def _market_ticks(
    db: DbSession, match_id: str, market_type: str, line: float | None, staleness_minutes: int
) -> list[OddsTick]:
    now = utc_now()
    repo = SQLOddsTickRepository(db)
    ticks = await repo.fetch_ticks(
        now - timedelta(minutes=staleness_minutes),
        now + MAX_FUTURE_SKEW,
        match_ids=[match_id],
        market_type=market_type
    )
    target = None if line is None else round(line, 4)
    return [t for t in ticks if t.line == target]


async def _run_steam_detection(ticks: list[OddsTick]) -> None:
    """Background task: own session, never raises into the event loop."""
    try:
        async with AsyncSessionLocal() as session:
            alerts = await _steam.process_tick_batch(
                ticks,
                SQLOddsTickRepository(session),
                DEFAULT_WINDOW_MINUTES,
                DEFAULT_MIN_PROB_DELTA,
                DEFAULT_MIN_LINE_SHIFT,
                DEFAULT_MIN_TIME_DELTA_SECONDS,
            )
        for alert in alerts:
            signals_log.warning(
                "STEAM %s/%s/%s %.2f -> %.2f (%+.2fpp) books=%s",
                alert.match_id, alert.market_type, alert.selection_id,
                alert.opening_odds, alert.current_odds, alert.implied_prob_delta_pct,
                ",".join(alert.triggering_bookmakers),
            )
    except Exception:  # noqa: BLE001
        signals_log.exception("Background steam detection failed")


@router.get("/steam-moves", response_model=list[SteamAlert])
async def get_steam_moves(
    db: DbSession,
    current_user: CurrentUser,
    match_id: str | None = Query(default=None, max_length=128),
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=1440),
    min_prob_delta: float = Query(DEFAULT_MIN_PROB_DELTA, gt=0.0, le=100.0),
    min_line_shift: float = Query(DEFAULT_MIN_LINE_SHIFT, ge=0.0, le=100.0),
    min_time_delta_seconds: int = Query(DEFAULT_MIN_TIME_DELTA_SECONDS, ge=1, le=86_400),
) -> list[SteamAlert]:
    now = utc_now()
    history = await SQLOddsTickRepository(db).fetch_ticks(
        now - timedelta(minutes=window_minutes),
        now,
        match_ids=[match_id] if match_id else None,
        odds_type=OddsType.BACK,
    )
    return _steam.detect(history, now, window_minutes, min_prob_delta, min_line_shift, min_time_delta_seconds)


@router.get("/best-price", response_model=LineShopResult)
async def get_best_price(
    db: DbSession,
    current_user: CurrentUser,
    match_id: str = Query(..., min_length=1, max_length=128),
    market_type: str = Query(..., min_length=1, max_length=64),
    selection_id: str = Query(..., min_length=1, max_length=128),
    line: float | None = Query(default=None),
    max_staleness_minutes: int = Query(10, ge=1, le=1440),
) -> LineShopResult:
    ticks = await _market_ticks(db, match_id, market_type, line, max_staleness_minutes)
    result = find_best_price(ticks, selection_id, max_staleness_minutes)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No fresh price or sharp consensus")
    return result


@router.get("/arbitrage", response_model=list[ArbitrageOpportunity])
async def get_arbitrage(
    db: DbSession,
    current_user: CurrentUser,
    match_id: str = Query(..., min_length=1, max_length=128),
    market_type: str = Query(..., min_length=1, max_length=64),
    line: float | None = Query(default=None),
    commission_pct: float = Query(0.02, ge=0.0, lt=1.0),
    max_staleness_minutes: int = Query(5, ge=1, le=1440),
) -> list[ArbitrageOpportunity]:
    ticks = await _market_ticks(db, match_id, market_type, line, max_staleness_minutes)
    opportunities: list[ArbitrageOpportunity] = []
    for selection_id in sorted({t.selection_id for t in ticks}):
        opp = find_back_lay_arbitrage(ticks, selection_id, commission_pct, max_staleness_minutes)
        if opp is not None:
            opportunities.append(opp)
    return sorted(opportunities, key=lambda o: o.net_profit_pct, reverse=True)


@router.get("/surebets", response_model=list[MarketSurebet])
async def get_surebets(
    db: DbSession,
    current_user: CurrentUser,
    match_id: str | None = Query(default=None, max_length=128),
    max_staleness_minutes: int = Query(5, ge=1, le=1440),
    min_profit_pct: float = Query(0.0, ge=0.0, le=100.0),
) -> list[MarketSurebet]:
    now = utc_now()
    ticks = await SQLOddsTickRepository(db).fetch_ticks(
        now - timedelta(minutes=max_staleness_minutes),
        now + MAX_FUTURE_SKEW,
        match_ids=[match_id] if match_id else None,
        odds_type=OddsType.BACK,
    )
    return find_market_surebets(ticks, max_staleness_minutes, now, min_profit_pct=min_profit_pct)


@router.post("/ingest-ticks", response_model=TickIngestResult, status_code=status.HTTP_202_ACCEPTED)
async def ingest_ticks(
    ticks: list[OddsTick],
    background_tasks: BackgroundTasks,
    db: DbSession,
    current_user: CurrentUser,
) -> TickIngestResult:
    if not ticks:
        return TickIngestResult(ingested=0, steam_detection_scheduled=False)
    if len(ticks) > MAX_INGEST_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch exceeds {MAX_INGEST_BATCH} ticks",
        )
    count = await SQLOddsTickRepository(db).save_ticks(ticks)
    await db.commit()
    background_tasks.add_task(_run_steam_detection, ticks)
    signals_log.info("Ingested %d ticks from user=%s", count, current_user.id)
    return TickIngestResult(ingested=count, steam_detection_scheduled=True)
