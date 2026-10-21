"""/prediction: BETDOC SCORE PREDICTION (Group 102, point b.16). Developed for Amit Ashok Kumar Patnaik.

* ``POST /prediction/football``: the scoreline heatmap, the likeliest scores and every Oracle market priced from them,
  from the market's de-vigged prices, your own xG, or the soccer adapter on team stats;
* ``GET  /prediction/football/pre-match/{match_id}``: the same from a fixture the pre-match store holds (Group 101):
  its h2h consensus and de-vigged totals, no provider call;
* ``POST /prediction/cricket``: limited-overs innings projections (median, interval, powerplay, runs per phase) and
  the chase, from the ball-by-ball Markov engine;
* ``POST /prediction/tennis`` | ``/basketball`` | ``/mma``: set scores / games / tiebreaks, projected points and
  lines, method and rounds, from the sport adapters.

Stats and context are the adapters' own (``/sports/calculate-odds``); a ``fixture_id`` merges the twin's intel (The
Wire's weather, injuries, officials) under them, and a sport deactivated on the desk is refused (409). The engines are
CPU-bound, so they run in a worker thread. Mounted for signed-in users (``router.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.domain.oracle.match_model import DEFAULT_RHO, MarketTargets
from app.domain.prediction import score_predictor as sp
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.errors import SportNotFoundError
from app.domain.sports.registry import SportAdapterRegistry
from app.schemas.score_prediction import (
    BasketballPredictionRequest,
    CricketPredictionRequest,
    CricketPredictionResponse,
    FixtureRef,
    FootballPredictionRequest,
    FootballPredictionResponse,
    SportPredictionResponse,
    SportStatsRequest,
)
from app.services import pre_match_odds_service, sports_catalog, sports_twin

router = APIRouter(tags=["BetDoc Score Prediction"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
T = TypeVar("T")


def _adapter(sport: str) -> BaseSportAdapter:
    try:
        return SportAdapterRegistry.get_adapter(sport)
    except SportNotFoundError as exc:  # pragma: no cover - the five sports here are always registered
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc


async def _context(request: Request, db: AsyncSession, settings: Settings, adapter: BaseSportAdapter,
                   payload: SportStatsRequest) -> tuple[dict[str, Any], list[str]]:
    """The desk's stored model parameters, the request's context over them, the twin's intel under both."""
    try:
        config = await sports_catalog.get_config(db, adapter.sport_key)
    except SportNotFoundError:
        config = None
    if config is not None and not config.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Sport {adapter.sport_key!r} is currently deactivated.")
    ctx = sports_catalog.model_context(config, payload.context)
    sections: list[str] = []
    redis = getattr(request.app.state, "redis", None)
    if payload.fixture_id and redis is not None:
        intel, sections = await sports_twin.intel_context(redis, settings, payload.fixture_id, adapter.sport_key)
        ctx = sports_twin.merge_context(ctx, intel)
    return ctx, sections


async def _run(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (sp.PredictionInputError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def _football_out(p: sp.FootballPrediction, settings: Settings, fixture: FixtureRef | None = None) -> FootballPredictionResponse:
    return FootballPredictionResponse(
        source=p.source, expected_goals=list(p.expected_goals), heatmap=p.heatmap, heatmap_goals=settings.PREDICTION_HEATMAP_GOALS,
        heatmap_mass=p.heatmap_mass,
        top_scorelines=[{"score": s.label, "home": s.home, "away": s.away, "probability": s.probability} for s in p.top_scorelines],
        home_win=p.home_win, draw=p.draw, away_win=p.away_win, btts_yes=p.btts_yes, btts_no=round(1.0 - p.btts_yes, 6),
        over={f"{k:g}": v for k, v in p.over.items()}, under={f"{k:g}": round(1.0 - v, 6) for k, v in p.over.items()},
        markets=[{**{f: getattr(m, f) for f in ("market", "kind", "line", "selection", "win", "half_win", "push", "half_loss", "loss", "fair_odds")},
                  "win_probability": round(m.win_probability, 6)} for m in p.markets],
        rho=p.rho, fit_error=p.fit_error, detail=p.detail, fixture=fixture,
    )


# ================================================================ football
@router.post("/football", response_model=FootballPredictionResponse)
async def predict_football(payload: FootballPredictionRequest, request: Request, db: DbSession, settings: AppSettings) -> FootballPredictionResponse:
    shape = {"heatmap_goals": settings.PREDICTION_HEATMAP_GOALS, "top": settings.PREDICTION_TOP_SCORELINES}
    rho = DEFAULT_RHO if payload.rho is None else payload.rho
    if payload.source == "market" and payload.market is not None:
        m = payload.market
        prediction = await _run(sp.football_from_market, MarketTargets(m.home, m.draw, m.away, dict(m.totals_over)), rho=rho, **shape)
    elif payload.source == "xg" and payload.xg is not None:
        prediction = await _run(sp.football_from_xg, payload.xg.home, payload.xg.away, rho=rho, **shape)
    else:
        adapter = _adapter("soccer")
        ctx, _ = await _context(request, db, settings, adapter, payload)
        prediction = await _run(sp.football_from_stats, adapter, payload.home_stats, payload.away_stats, ctx, **shape)
    return _football_out(prediction, settings)


@router.get("/football/pre-match/{match_id}", response_model=FootballPredictionResponse)
async def predict_stored_fixture(match_id: Annotated[str, Path(min_length=1, max_length=128)], db: DbSession, settings: AppSettings) -> FootballPredictionResponse:
    row = await pre_match_odds_service.get_fixture(db, match_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fixture not found in the pre-match store")
    if not row.sport_key.startswith("soccer"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{row.sport_key} is not football: scorelines are a football view")
    try:
        targets = sp.market_targets_from_store(row.consensus_fair_odds, row.markets_json)
    except sp.PredictionInputError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    prediction = await _run(sp.football_from_market, targets, heatmap_goals=settings.PREDICTION_HEATMAP_GOALS, top=settings.PREDICTION_TOP_SCORELINES)
    fixture = FixtureRef(match_id=row.match_id, sport_key=row.sport_key, home_team=row.home_team, away_team=row.away_team,
                         commence_time=row.commence_time, last_polled_at=row.last_polled_at, books=int(row.consensus_fair_odds.get("books", 0)))
    return _football_out(prediction, settings, fixture)


# ================================================================ cricket
@router.post("/cricket", response_model=CricketPredictionResponse)
async def predict_cricket(payload: CricketPredictionRequest, request: Request, db: DbSession, settings: AppSettings) -> CricketPredictionResponse:
    adapter = _adapter("cricket")
    ctx, sections = await _context(request, db, settings, adapter, payload)
    p = await _run(sp.predict_cricket, adapter, payload.home_stats, payload.away_stats, ctx,
                   coverage=settings.PREDICTION_INTERVAL_COVERAGE, chase_target=payload.chase_target)

    def innings(i: sp.InningsProjection) -> dict[str, Any]:
        return {"side": i.side, "median": i.median, "interval": list(i.interval), "mean": i.mean, "powerplay_median": i.powerplay_median,
                "powerplay_interval": None if i.powerplay_interval is None else list(i.powerplay_interval),
                "phase_expected_runs": i.phase_expected_runs, "expected_sixes": i.expected_sixes}

    c = p.chase
    return CricketPredictionResponse(
        format=p.format, overs=p.overs, batting_first=p.batting_first, first_innings=innings(p.first_innings), second_innings=innings(p.second_innings),
        chase={"target": c.target, "source": c.source, "win": c.win, "tie": c.tie, "lose": c.lose, "win_with_super_over": c.win_with_super_over},
        match_winner=p.match_winner, coverage=p.coverage, conditions=p.conditions, intel_sections=sections,
    )


# ================================================================ tennis, basketball, MMA
@router.post("/tennis", response_model=SportPredictionResponse)
async def predict_tennis(payload: SportStatsRequest, request: Request, db: DbSession, settings: AppSettings) -> SportPredictionResponse:
    adapter = _adapter("tennis")
    ctx, sections = await _context(request, db, settings, adapter, payload)
    return SportPredictionResponse(sport_key=adapter.sport_key, prediction=await _run(sp.predict_tennis, adapter, payload.home_stats, payload.away_stats, ctx),
                                   intel_sections=sections)


@router.post("/basketball", response_model=SportPredictionResponse)
async def predict_basketball(payload: BasketballPredictionRequest, request: Request, db: DbSession, settings: AppSettings) -> SportPredictionResponse:
    adapter = _adapter("basketball")
    ctx, sections = await _context(request, db, settings, adapter, payload)
    prediction = await _run(sp.predict_basketball, adapter, payload.home_stats, payload.away_stats, ctx, coverage=settings.PREDICTION_INTERVAL_COVERAGE,
                            spread_line=payload.spread_line, total_line=payload.total_line)
    return SportPredictionResponse(sport_key=adapter.sport_key, prediction=prediction, intel_sections=sections)


@router.post("/mma", response_model=SportPredictionResponse)
async def predict_mma(payload: SportStatsRequest, request: Request, db: DbSession, settings: AppSettings) -> SportPredictionResponse:
    adapter = _adapter("mma")
    ctx, sections = await _context(request, db, settings, adapter, payload)
    return SportPredictionResponse(sport_key=adapter.sport_key, prediction=await _run(sp.predict_mma, adapter, payload.home_stats, payload.away_stats, ctx),
                                   intel_sections=sections)
