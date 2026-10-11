"""
Multi-Sport API Router & Quantitative Dispatch Endpoints (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Mounted under ``/sports`` next to the Groups 54-58 routes (``/sports/{sport}/config``, ``/sports/cricket/...``),
whose paths it does not overlap. Reads and pricing need a signed-in user; changing a sport's configuration,
registering a league and reading the provider catalogue are administrator-only.

* ``GET  /sports/supported``, ``/sports/bet-types``: the nine adapters and the 27 universal bet types
* ``GET  /sports/config``, ``/sports/config/{sport}``; ``PATCH /sports/config/{sport}`` (admin)
* ``GET  /sports/leagues/{sport}``; ``POST /sports/leagues``, ``PATCH /sports/leagues/{league}`` (admin)
* ``POST /sports/calculate-odds``: fair probabilities and line markets from the sport's sub-model consensus
* ``POST /sports/resolve-bet``, ``/sports/settle-structure``: leg and structure settlement
* ``POST /sports/evaluate``: the twin pipeline (pillar 1 consensus, pillar 9 sharp benchmark, OmniRoute,
  the 0-100 confidence index, sport-calibrated Kelly sizing)
* ``POST /sports/edges/screen``; ``GET /sports/edges``: model line shopping, the beat's stored edge list
* ``POST /sports/in-play/tick``: FA-1 live probability + stop-loss shield, published live
* ``POST /sports/discipline/assess``: FA-2 tilt / revenge read, XP for a disciplined bet
* ``GET  /sports/providers`` (admin): the omni-provider catalogue (never a key)
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, CurrentUser, get_db
from app.core.config import Settings, get_settings
from app.domain.cfo import growth_math as gm
from app.domain.sports import discipline as dsc
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.bet_catalog import CATALOG, STRUCTURE_TYPES, markets_for
from app.domain.sports.errors import InvalidSportConfigError, SportNotFoundError, SportsDomainError
from app.domain.sports.registry import SportAdapterRegistry, sport_for_odds_key
from app.domain.sports.settlement import SettlementInputError, is_win
from app.domain.sports.structures import (
    LegSpec,
    StructureError,
    settle_future,
    settle_if_bet,
    settle_parlay,
    settle_pleaser,
    settle_round_robin,
    settle_teaser,
)
from app.domain.sports.twin_bridge import (
    confidence_index,
    consensus_verdict,
    route_best_price,
    sharp_benchmark,
    sport_kelly_multiplier,
    sport_kelly_sizing,
)
from app.models.never_forget import XPActionType
from app.models.sports_config import SportConfiguration
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.schemas.bet_types import LegStatus
from app.schemas.sports_config import (
    BetTypeOut,
    DisciplineRequest,
    DisciplineResponse,
    EdgeScreenRequest,
    EvaluateRequest,
    EvaluateResponse,
    InPlayRequest,
    InPlayResponse,
    LeagueCreate,
    LeagueResponse,
    LeagueUpdate,
    MarketResolutionRequest,
    MarketResolutionResponse,
    MatchProbabilitiesRequest,
    MatchProbabilitiesResponse,
    SportConfigResponse,
    SportConfigUpdate,
    StructureSettleRequest,
    StructureSettleResponse,
    SubModelOut,
    SupportedSport,
)
from app.services import sports_catalog as catalog
from app.services import sports_twin
from app.services.multi_sport_odds_sync import MultiSportOddsSyncService
from app.services.twin.xp_engine import Award, award

logger = logging.getLogger("betdoc.sports")
router = APIRouter(prefix="/sports", tags=["Multi-Sport Quantitative Engine"])

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
HISTORY_LIMIT = 200


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, SportNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.message)
    if isinstance(exc, SportsDomainError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _adapter(sport_key: str) -> BaseSportAdapter:
    try:
        return SportAdapterRegistry.get_adapter(sport_key)
    except SportNotFoundError as exc:
        raise _fail(exc) from exc


async def _config(db: AsyncSession, sport_key: str) -> SportConfiguration | None:
    try:
        return await catalog.get_config(db, sport_key)
    except SportNotFoundError:
        return None


async def _context(request: Request, db: AsyncSession, settings: Settings, adapter: BaseSportAdapter, context: dict[str, Any] | None,
                   fixture_id: str | None) -> tuple[dict[str, Any], list[str], SportConfiguration | None]:
    config = await _config(db, adapter.sport_key)
    if config is not None and not config.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Sport {adapter.sport_key!r} is currently deactivated.")
    ctx = catalog.model_context(config, context)
    sections: list[str] = []
    redis = _redis(request)
    if fixture_id and redis is not None:
        intel, sections = await sports_twin.intel_context(redis, settings, fixture_id, adapter.sport_key)
        ctx = sports_twin.merge_context(ctx, intel)
    return ctx, sections, config


# ================================================================ catalogue
@router.get("/supported", response_model=list[SupportedSport])
async def get_supported_sports(current_user: CurrentUser) -> list[dict[str, Any]]:  # noqa: ARG001 - authentication
    """Lists all 9 registered quantitative sport adapters and their standard markets."""
    return SportAdapterRegistry.list_supported_sports()


@router.get("/bet-types", response_model=list[BetTypeOut])
async def get_bet_types(current_user: CurrentUser) -> list[BetTypeOut]:  # noqa: ARG001 - authentication
    """The 27 universal bet types, which sports offer each and the market keys implementing it there."""
    adapters = SportAdapterRegistry.all_adapters()
    out = []
    for key, info in CATALOG.items():
        sports = [a.sport_key for a in adapters if key.value in a.BET_TYPES]
        markets = {} if key in STRUCTURE_TYPES else {s: list(markets_for(key, s)) for s in sports}
        out.append(BetTypeOut(key=key.value, label=info.label, kind=info.kind, description=info.description, sports=sports, markets=markets))
    return out


@router.get("/config", response_model=list[SportConfigResponse])
async def list_sport_configs(db: Db, current_user: CurrentUser) -> list[SportConfiguration]:  # noqa: ARG001
    """Retrieves all sport engine configurations and parameters."""
    return await catalog.list_configs(db)


@router.get("/config/{sport_key}", response_model=SportConfigResponse)
async def read_sport_config(sport_key: str, db: Db, current_user: CurrentUser) -> SportConfiguration:  # noqa: ARG001
    try:
        return await catalog.get_config(db, sport_key)
    except SportsDomainError as exc:
        raise _fail(exc) from exc


@router.patch("/config/{sport_key}", response_model=SportConfigResponse)
async def update_sport_config(sport_key: str, payload: SportConfigUpdate, db: Db, admin: CurrentAdmin) -> SportConfiguration:
    """Updates risk threshold, Kelly fraction, markets or mathematical parameters for a sport (administrators)."""
    try:
        row = await catalog.update_config(db, sport_key, payload)
    except SportsDomainError as exc:
        raise _fail(exc) from exc
    logger.info("SPORTS: %s configuration updated by %s", row.sport_key, admin.username)
    return row


@router.get("/leagues/{sport_key}", response_model=list[LeagueResponse])
async def list_sport_leagues(sport_key: str, db: Db, current_user: CurrentUser,  # noqa: ARG001
                             active_only: Annotated[bool, Query()] = False) -> list[Any]:
    """Retrieves registered leagues and competitions for a given sport."""
    adapter = _adapter(sport_key)
    return await catalog.list_leagues(db, adapter.sport_key, active_only=active_only)


@router.post("/leagues", response_model=LeagueResponse, status_code=status.HTTP_201_CREATED)
async def create_league(payload: LeagueCreate, db: Db, admin: CurrentAdmin) -> Any:  # noqa: ARG001 - administrators only
    _adapter(payload.sport_key)
    try:
        return await catalog.create_league(db, payload)
    except SportsDomainError as exc:
        if isinstance(exc, InvalidSportConfigError) and "already registered" in exc.message:
            raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from exc
        raise _fail(exc) from exc


@router.patch("/leagues/{league_key}", response_model=LeagueResponse)
async def update_league(league_key: str, payload: LeagueUpdate, db: Db, admin: CurrentAdmin) -> Any:  # noqa: ARG001
    try:
        return await catalog.update_league(db, league_key, payload)
    except SportsDomainError as exc:
        raise _fail(exc) from exc


# ================================================================ pricing & settlement
@router.post("/calculate-odds", response_model=MatchProbabilitiesResponse)
async def calculate_match_odds(payload: MatchProbabilitiesRequest, request: Request, db: Db, settings: AppSettings,
                               current_user: CurrentUser) -> MatchProbabilitiesResponse:  # noqa: ARG001
    """
    Computes fair win/draw/totals probabilities and unmargined odds
    for any match across all 9 sports using the corresponding quantitative adapter.
    """
    adapter = _adapter(payload.sport_key)
    ctx, _, _ = await _context(request, db, settings, adapter, payload.context, payload.fixture_id)
    started = time.perf_counter()
    try:
        res = adapter.calculate_fair_odds(home_stats=payload.home_stats, away_stats=payload.away_stats, context=ctx)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise _fail(exc) from exc
    return MatchProbabilitiesResponse(
        sport_key=adapter.sport_key, home_win_prob=res.home_win_prob, away_win_prob=res.away_win_prob, draw_prob=res.draw_prob,
        fair_home_odds=res.fair_home_odds, fair_away_odds=res.fair_away_odds, fair_draw_odds=res.fair_draw_odds,
        expected_total_score=res.expected_total_score, sport_specific_metrics=res.sport_specific_metrics, market_probabilities=res.market_probabilities,
        sub_models=[SubModelOut(**m.as_dict()) for m in res.sub_models], consensus=res.consensus.as_dict() if res.consensus else {},
        evidence=res.evidence, kelly_multiplier=sport_kelly_multiplier(adapter.sport_key, settings.SPORTS_KELLY_MULTIPLIERS),
        model_family=res.model_family, latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


@router.post("/resolve-bet", response_model=MarketResolutionResponse)
async def resolve_bet(payload: MarketResolutionRequest, current_user: CurrentUser) -> MarketResolutionResponse:  # noqa: ARG001
    """Resolves a selection from the match result and line: WON, LOST, VOID (push), HALF_WON or HALF_LOST."""
    adapter = _adapter(payload.sport_key)
    try:
        canonical, _ = adapter.canonical_market(payload.market_type)
        status_ = adapter.settle_market(payload.market_type, payload.match_result, payload.selection, payload.line, payload.params)
    except SettlementInputError as exc:
        raise _fail(exc) from exc
    return MarketResolutionResponse(is_win=is_win(status_), status=status_.value, sport_key=adapter.sport_key, market_type=payload.market_type,
                                    canonical_market=canonical, selection=payload.selection, line=payload.line)


@router.post("/settle-structure", response_model=StructureSettleResponse)
async def settle_structure(payload: StructureSettleRequest, current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    """Parlay, teaser, pleaser, round robin, if-bet, action reverse, double action and futures settlement."""
    legs = [LegSpec(leg_id=leg.leg_id, odds=leg.odds, sport_key=leg.sport_key, market_type=leg.market_type, selection=leg.selection, line=leg.line,
                    params=leg.params, match_result=leg.match_result, status=LegStatus(leg.status) if leg.status else None) for leg in payload.legs]
    try:
        kind = payload.structure
        if kind == "futures":
            if payload.selection is None or payload.odds is None or payload.stake is None:
                raise StructureError("futures need selection, odds and stake")
            result = settle_future(payload.selection, payload.odds, payload.stake, payload.winners, cancelled=payload.cancelled)
        elif kind in ("if_bet", "action_reverse", "double_action"):
            stakes = payload.stakes or ([payload.stake] * len(legs) if payload.stake else None)
            if not stakes:
                raise StructureError("if-bets need stakes")
            result = settle_if_bet(legs, stakes, double_action=kind == "double_action", reverse=kind == "action_reverse")
        else:
            if payload.stake is None:
                raise StructureError(f"a {kind} needs a stake")
            if kind == "parlay":
                result = settle_parlay(legs, payload.stake)
            elif kind == "round_robin":
                result = settle_round_robin(legs, payload.stake, payload.sizes or [2])
            elif kind in ("teaser", "pleaser"):
                if payload.points is None:
                    raise StructureError(f"a {kind} needs points")
                result = (settle_teaser if kind == "teaser" else settle_pleaser)(legs, payload.stake, payload.points)
            else:
                raise StructureError(f"unknown structure {kind!r}")
    except (StructureError, SportsDomainError) as exc:
        raise _fail(exc) from exc
    return result.as_dict()


# ================================================================ the twin pipeline
@router.post("/evaluate", response_model=EvaluateResponse)
async def evaluate_selection(payload: EvaluateRequest, request: Request, db: Db, settings: AppSettings,
                             current_user: CurrentUser) -> EvaluateResponse:  # noqa: ARG001
    """Pillar 1 consensus, pillar 9 sharp benchmark, OmniRoute, the 0-100 confidence index and the sport's Kelly stake,
    in one read, before anything reaches the CFO."""
    adapter = _adapter(payload.sport_key)
    ctx, sections, config = await _context(request, db, settings, adapter, payload.context, payload.fixture_id)
    try:
        fair = adapter.calculate_fair_odds(payload.home_stats, payload.away_stats, ctx)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise _fail(exc) from exc
    side = payload.selection
    if side == "draw" and fair.draw_prob <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{adapter.display_name} prices no draw")
    p = fair.probability_of(side)
    outcomes = ["home", "draw", "away"] if fair.draw_prob > 0.02 else ["home", "away"]
    bench = sharp_benchmark(payload.venue_odds, outcomes) if payload.venue_odds else None
    route = route_best_price({v: q[side] for v, q in payload.venue_odds.items() if side in q}, bet_kind=payload.bet_kind) if payload.venue_odds else None
    odds = route.net_odds if route is not None else 1.0 / p
    verdict = consensus_verdict(fair.sub_models, side, odds, ctx.get("model_weights"), min_models=settings.TWIN_MIN_MODELS,
                                min_ev=settings.TWIN_MIN_CONSENSUS_EV)
    edge = p * route.net_odds - 1.0 if route is not None else None
    sharp_p = bench.fair_probabilities.get(side) if bench is not None else None
    confidence = confidence_index(adapter.sport_key, agreement=fair.consensus.agreement if fair.consensus else 0.0, model_count=len(fair.sub_models),
                                  model_probability=p, sharp_probability=sharp_p, edge=edge if edge is not None else 0.0, evidence=fair.evidence,
                                  consensus_status=verdict.status)
    sizing = None
    if route is not None and edge is not None and edge > 0:
        overrides = dict(settings.SPORTS_KELLY_MULTIPLIERS)
        if config is not None:
            overrides[adapter.sport_key] = config.default_kelly_fraction
        policy = gm.SizingPolicy.from_settings(settings)
        result = sport_kelly_sizing(adapter.sport_key, p, route.net_odds, Decimal(str(payload.bankroll_inr)), payload.drawdown, policy, overrides=overrides)
        sizing = {**result.as_dict(), "kelly_multiplier": sport_kelly_multiplier(adapter.sport_key, overrides), "venue": route.venue,
                  "submit": confidence.recommendation != "NO_BET" and not result.halted}
    return EvaluateResponse(sport_key=adapter.sport_key, selection=side, fair_probability=round(p, 5), fair_odds=round(1.0 / p, 4),
                            pillar_1_consensus=verdict.as_dict(), pillar_9_sharp_benchmark=bench.as_dict() if bench else None,
                            route=route.as_dict() if route else None, edge_vs_model=None if edge is None else round(edge, 5),
                            confidence=confidence.as_dict(), sizing=sizing, evidence=fair.evidence, intel_sections=sections)


@router.post("/edges/screen")
async def screen_edges(payload: EdgeScreenRequest, request: Request, db: Db, settings: AppSettings,
                       current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    adapter = _adapter(payload.sport_key)
    ctx, _, _ = await _context(request, db, settings, adapter, payload.context, None)
    service = MultiSportOddsSyncService(settings=settings)
    try:
        edges = await service.screen_multi_provider_edges(adapter.sport_key, payload.match_id, payload.home_team, payload.away_team,
                                                          payload.venue_odds, payload.home_stats, payload.away_stats, ctx)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise _fail(exc) from exc
    return {"match_id": payload.match_id, "sport_key": adapter.sport_key, "edges": edges, "developer_credit": "Amit Ashok Kumar Patnaik"}


@router.get("/edges")
async def latest_edges(request: Request, settings: AppSettings, current_user: CurrentUser,  # noqa: ARG001
                       sport_key: Annotated[str | None, Query(max_length=64)] = None) -> dict[str, Any]:
    redis = _redis(request)
    if redis is None:
        return {"screened_at": None, "edges": [], "available": False}
    try:
        data = await MultiSportOddsSyncService(settings=settings).latest_edges(redis)
    except RedisError:
        return {"screened_at": None, "edges": [], "available": False}
    if sport_key:
        wanted = _adapter(sport_key).sport_key
        data["edges"] = [e for e in data["edges"] if e.get("sport_key") == wanted]
    return {**data, "available": True}


@router.get("/providers")
async def provider_catalog(db: Db, settings: AppSettings, admin: CurrentAdmin) -> list[dict[str, Any]]:  # noqa: ARG001 - administrators only
    return await MultiSportOddsSyncService(settings=settings).provider_catalog(db)


# ================================================================ FA-1: in-play
@router.post("/in-play/tick", response_model=InPlayResponse)
async def in_play_tick(payload: InPlayRequest, request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> InPlayResponse:
    adapter = _adapter(payload.sport_key)
    ctx, _, _ = await _context(request, db, settings, adapter, payload.context, payload.fixture_id)
    try:
        live = adapter.in_play_win_probability(payload.home_stats, payload.away_stats, payload.live_state, ctx)
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
        raise _fail(exc) from exc
    shield = None
    if payload.selection is not None and payload.stake_inr is not None and payload.entry_probability is not None:
        if payload.selection == "draw" and live.draw_prob <= 0 and not adapter.has_draw:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{adapter.display_name} has no draw to back")
        shield = sports_twin.shield_call(settings, live, selection=payload.selection, entry_probability=payload.entry_probability,
                                         stake_inr=payload.stake_inr, entry_odds=payload.entry_odds, stop_loss_pct=payload.stop_loss_pct,
                                         cashout_offer_inr=payload.cashout_offer_inr)
    frame = sports_twin.tick_frame(adapter.sport_key, live, fixture_id=payload.fixture_id, shield=shield, at=datetime.now(UTC))
    redis = _redis(request)
    published = await sports_twin.publish_tick(redis, settings, current_user.id, frame) if redis is not None else False
    return InPlayResponse(sport_key=adapter.sport_key, home_win_prob=round(live.home_win_prob, 5), away_win_prob=round(live.away_win_prob, 5),
                          draw_prob=round(live.draw_prob, 5), detail=live.detail, shield=shield, published=published)


# ================================================================ FA-2: discipline
async def _ledger_history(db: AsyncSession, user_id: Any) -> list[dsc.BetRecord]:
    bets = (await db.execute(select(UserPlacedBet).where(UserPlacedBet.user_id == user_id).order_by(UserPlacedBet.placed_at.desc())
                             .limit(HISTORY_LIMIT))).scalars().all()
    if not bets:
        return []
    legs = (await db.execute(select(UserPlacedLeg.bet_id, UserPlacedLeg.sport_key, UserPlacedLeg.position)
                             .where(UserPlacedLeg.bet_id.in_([b.id for b in bets])).order_by(UserPlacedLeg.position))).all()
    sport_of: dict[Any, str] = {}
    for bet_id, sport_key, _ in legs:
        if bet_id not in sport_of:
            mapped = sport_for_odds_key(sport_key)
            if mapped:
                sport_of[bet_id] = mapped
    out = []
    for b in bets:
        if b.id not in sport_of:
            continue
        out.append(dsc.BetRecord(placed_at=b.placed_at if b.placed_at.tzinfo else b.placed_at.replace(tzinfo=UTC), sport_key=sport_of[b.id],
                                 stake=float(b.stake_inr), odds=float(b.placed_odds or 1), result=str(b.status),
                                 pnl=None if b.pnl_inr is None else float(b.pnl_inr)))
    return out


@router.post("/discipline/assess", response_model=DisciplineResponse)
async def assess_discipline(payload: DisciplineRequest, db: Db, settings: AppSettings, current_user: CurrentUser) -> DisciplineResponse:
    cand = payload.candidate
    adapter = _adapter(cand.sport_key)
    now = datetime.now(UTC)
    placed_at = cand.placed_at or now
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=UTC)
    kelly_stake = None
    if cand.model_probability is not None and cand.bankroll_inr is not None:
        policy = gm.SizingPolicy.from_settings(settings)
        sizing = sport_kelly_sizing(adapter.sport_key, cand.model_probability, cand.odds, Decimal(str(cand.bankroll_inr)), 0.0, policy,
                                    Decimal("0.01"), overrides=settings.SPORTS_KELLY_MULTIPLIERS)
        kelly_stake = float(sizing.stake)
    if payload.history is not None:
        history = [dsc.BetRecord(placed_at=h.placed_at if h.placed_at.tzinfo else h.placed_at.replace(tzinfo=UTC), sport_key=_adapter(h.sport_key).sport_key,
                                 stake=h.stake, odds=h.odds, result=h.result, pnl=h.pnl) for h in payload.history]
    else:
        history = await _ledger_history(db, current_user.id)
    report = dsc.assess(dsc.CandidateBet(placed_at, adapter.sport_key, cand.stake, cand.odds, cand.model_probability, kelly_stake), history)
    paid = 0
    if report.disciplined:
        paid = await award(db, settings, Award(current_user.id, XPActionType.DISCIPLINED_BET, f"sport-bet:{cand.bet_ref}",
                                               f"Disciplined {adapter.display_name} bet: edge {report.edge:.1%} within its Kelly stake",
                                               {"sport_key": adapter.sport_key, "edge": report.edge, "kelly_stake": kelly_stake}), now)
        await db.commit()
    return DisciplineResponse(score=report.score, flags=list(report.flags), reasons=report.reasons, core_sports=list(report.core_sports),
                              disciplined=report.disciplined, edge=report.edge, detail=report.detail, kelly_stake=kelly_stake, xp_awarded=paid)

