"""ASHOKA, the Oracle, under ``/api/v1/oracle`` (Group 69).

    POST /oracle/suggest                    the core-satellite allocator (unchanged)
    GET  /oracle/slips                      vetted slips from the live market, staked for a bankroll
    POST /oracle/slips/recheck              one slip re-priced against the latest feeds ("Re-check odds")
    POST /oracle/odds-check                 Parimatch vs 1xBet (and the rest) on prices the user typed in
    GET  /oracle/trending                   sharp steam parlays, public traps, AI hybrids
    GET  /oracle/bets                       the user's placed bets (active / settled / all), legs included
    POST /oracle/bets                       "I placed this bet"
    DELETE /oracle/bets/{id}                remove a mistaken entry (pending only)
    POST /oracle/bets/{id}/cashout-advice   HOLD / CASH OUT / HEDGE LEG for a running multiple
    POST /oracle/bets/{id}/cashout          record a cashout taken at the bookmaker
    GET  /oracle/pnl                        today / week / month / all-time scorecard (optional tax estimate)
    GET  /oracle/twin                       the betting twin: strengths and leaks
    POST /oracle/scores                     record a final score (admin); settles what it decides
    POST /oracle/scores/poll                fetch scores for fixtures pending bets wait on (admin)

Everything Ashoka prices comes from the feeds already in Redis or from what the user typed; nothing here
calls a bookmaker. Every user sees and changes only their own bets.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser, DbSession
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import AshokaOracle, OracleContext, OracleResponse
from app.domain.oracle.cashout_advisor import OpenLeg, SettledLeg, advise
from app.domain.oracle.markets import LegResult, parse_market
from app.domain.oracle.slip_formatter import LegPrice, book_view, compare
from app.domain.popular_picks.manager import PopularPicksManager
from app.models.cfo_vault import BankrollAccount
from app.models.user_bets_ledger import PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import CashoutRecord, CashoutRequest, OddsCheckRequest, PlaceBetRequest, ScoreIn, SlipLegsRequest
from app.schemas.popular_picks import PopularParlayRead
from app.services import ashoka_market
from app.services import user_pnl_tracker as tracker
from app.services.oracle_scores import poll_scores

ashoka_log = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["oracle"])

_oracle = AshokaOracle()
_trends = PopularPicksManager()
AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
LIVE_WINDOW = timedelta(minutes=125)


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _need_redis(request: Request) -> Redis:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_MARKET", "message": "The live market (Redis) is unavailable"})
    return redis


async def _bankroll(session: AsyncSession, user_id: uuid.UUID, given: Decimal | None) -> Decimal | None:
    """The bankroll a stake is sized on: the caller's figure, else the user's CFO main account."""
    if given is not None:
        return given
    account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id, BankrollAccount.bot_id.is_(None)))
    if account is None:
        return None
    return account.available_balance + account.exposure_balance


@router.post("/suggest", response_model=OracleResponse)
async def suggest(context: OracleContext, current_user: CurrentUser) -> OracleResponse:
    ashoka_log.info("ASHOKA suggest requested by user=%s with %d value bets", current_user.id, len(context.available_value_bets))
    return _oracle.generate_suggestions(context)


# ================================================================ slips
@router.get("/slips")
async def slips(
    request: Request, user: CurrentUser, db: DbSession, settings: AppSettings,
    bankroll_inr: Annotated[Decimal | None, Query(gt=0)] = None, refresh: bool = False,
) -> dict[str, Any]:
    redis = _need_redis(request)
    bankroll = await _bankroll(db, user.id, bankroll_inr)
    try:
        payload = await ashoka_market.vetted_slips(redis, settings, datetime.now(UTC), bankroll, refresh=refresh)
    except (RedisError, OSError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_MARKET", "message": "The live market (Redis) is unavailable"}) from exc
    payload["bankroll_inr"] = None if bankroll is None else str(bankroll)
    return payload


@router.post("/slips/recheck")
async def recheck(body: SlipLegsRequest, request: Request, user: CurrentUser, db: DbSession, settings: AppSettings) -> dict[str, Any]:
    redis = _need_redis(request)
    bankroll = await _bankroll(db, user.id, body.bankroll_inr)
    slip = await ashoka_market.recheck(redis, settings, datetime.now(UTC), body.leg_ids, body.kind, bankroll)
    if slip is None:
        raise HTTPException(status.HTTP_410_GONE, {"reason": "NO_LONGER_QUOTED", "message": "A leg of this slip is no longer quoted by the feeds"})
    slip["rechecked_at"] = datetime.now(UTC).isoformat()
    return slip


@router.post("/odds-check")
async def odds_check(body: OddsCheckRequest, user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    """The user's own prices from the bookmakers' sites: views, the best book, Parimatch vs 1xBet."""
    from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, canonical_bookmaker  # noqa: PLC0415

    legs: list[LegPrice] = []
    for i, leg in enumerate(body.legs):
        ref = parse_market(leg.market)
        if ref is None or leg.selection.upper() not in ref.selections:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "BAD_LEG", "message": f"leg {i + 1}: {leg.market} {leg.selection} is not a market Ashoka knows"})
        prices = {}
        for book, price in leg.prices.items():
            key = canonical_bookmaker(book)
            if key is None or price <= 1.0:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "BAD_PRICE", "message": f"leg {i + 1}: {book} {price}"})
            prices[key] = price
        legs.append(LegPrice(f"check-{i}", leg.home, leg.away, ref.key, leg.selection.upper(), prices))
    views = [book_view(book, legs, body.stake_inr) for book in ASHOKA_BOOKMAKERS]
    comparison = compare(views, body.stake_inr)
    return {
        "stake_inr": str(body.stake_inr),
        "books": [{"bookmaker": v.bookmaker, "label": v.label, "available": v.available, "odds": None if v.odds is None else str(v.odds),
                   "payout_inr": None if v.payout is None else str(v.payout), "missing": list(v.missing), "note": v.note, "legs": list(v.legs)} for v in views],
        "best": comparison.best, "best_odds": None if comparison.best_odds is None else str(comparison.best_odds),
        "parimatch_odds": None if comparison.pair.get("parimatch") is None else str(comparison.pair["parimatch"]),
        "onexbet_odds": None if comparison.pair.get("1xbet") is None else str(comparison.pair["1xbet"]),
        "difference_inr": None if comparison.difference_inr is None else str(comparison.difference_inr),
        "difference_pct": None if comparison.difference_pct is None else str(comparison.difference_pct),
        "recommended": comparison.recommended, "recommendation": comparison.recommendation,
    }


@router.get("/trending", response_model=list[PopularParlayRead])
async def trending(request: Request, user: CurrentUser, db: DbSession, settings: AppSettings, refresh: bool = False) -> list[PopularParlayRead]:  # noqa: ARG001
    redis = _redis(request)
    picks = [p for p in await _trends.get_active_picks(db) if p.category is not None]
    if (refresh or not picks) and redis is not None:
        try:
            await _trends.scan_trending(db, redis, settings)
        except (RedisError, OSError, TimeoutError):
            ashoka_log.warning("ASHOKA trend scan skipped: the live market is unreachable")
        picks = [p for p in await _trends.get_active_picks(db) if p.category is not None]
    order = {"SHARP_STEAM": 0, "AI_HYBRID": 1, "PUBLIC_TRAP": 2}
    return [PopularParlayRead.model_validate(p) for p in sorted(picks, key=lambda p: order.get(p.category or "", 9))]


# ================================================================ the user's bets
def _bet_payload(bet: UserPlacedBet, legs: list[UserPlacedLeg], now: datetime) -> dict[str, Any]:
    def live(leg: UserPlacedLeg) -> str:
        if leg.result != PlacedStatus.PENDING.value:
            return "FT"
        kickoff = leg.kickoff if leg.kickoff is None or leg.kickoff.tzinfo else leg.kickoff.replace(tzinfo=UTC)
        if kickoff is None:
            return "UNKNOWN"
        if now < kickoff:
            return "UPCOMING"
        return "LIVE" if now - kickoff <= LIVE_WINDOW else "AWAITING_RESULT"

    return {
        "id": str(bet.id), "slip_id": bet.slip_id, "source": bet.source, "bookmaker": bet.bookmaker, "bookmaker_name": bet.bookmaker_name,
        "structure": bet.structure, "stake_inr": str(bet.stake_inr), "unit_stake_inr": None if bet.unit_stake_inr is None else str(bet.unit_stake_inr),
        "placed_odds": None if bet.placed_odds is None else str(bet.placed_odds), "placed_at": bet.placed_at.isoformat() if bet.placed_at else None,
        "status": bet.status, "return_inr": None if bet.return_inr is None else str(bet.return_inr), "pnl_inr": None if bet.pnl_inr is None else str(bet.pnl_inr),
        "settled_at": bet.settled_at.isoformat() if bet.settled_at else None, "notes": bet.notes,
        "legs": [
            {"id": str(leg.id), "position": leg.position, "fixture_id": leg.fixture_id, "home": leg.home, "away": leg.away, "league": leg.league,
             "kickoff": leg.kickoff.isoformat() if leg.kickoff else None, "market": leg.market, "selection": leg.selection, "odds": str(leg.odds),
             "fair_probability": leg.fair_probability, "result": leg.result, "score": None if leg.home_goals is None else f"{leg.home_goals}-{leg.away_goals}",
             "match_status": live(leg)}
            for leg in legs
        ],
    }


@router.get("/bets")
async def list_bets(user: CurrentUser, sessions: Sessions, which: Literal["active", "settled", "all"] = "all", limit: Annotated[int, Query(ge=1, le=500)] = 200) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    await tracker.settle_pending(sessions, now, user_id=user.id)
    async with sessions() as session:
        bets, legs = await tracker.user_bets(session, user.id)
    if which == "active":
        bets = [b for b in bets if b.status == PlacedStatus.PENDING.value]
    elif which == "settled":
        bets = [b for b in bets if b.status != PlacedStatus.PENDING.value]
    return [_bet_payload(b, legs.get(b.id, []), now) for b in bets[:limit]]


@router.post("/bets", status_code=status.HTTP_201_CREATED)
async def place_bet(body: PlaceBetRequest, request: Request, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        bet = await tracker.record_bet(session, user.id, body, now)
        await session.commit()
        legs = list((await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet.id).order_by(UserPlacedLeg.position))).scalars())
        payload = _bet_payload(bet, legs, now)
    await tracker.settle_pending(sessions, now, user_id=user.id)  # a leg already decided settles at once
    await tracker.bump(_redis(request), [user.id])
    ashoka_log.info("ASHOKA: user %s recorded a %s bet of %s at %s", user.id, body.structure, body.stake_inr, body.bookmaker)
    return payload


async def _own_bet(session: AsyncSession, user_id: uuid.UUID, bet_id: uuid.UUID) -> tuple[UserPlacedBet, list[UserPlacedLeg]]:
    bet = await session.get(UserPlacedBet, bet_id)
    if bet is None or bet.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_FOUND", "message": "No such bet"})
    legs = list((await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet_id).order_by(UserPlacedLeg.position))).scalars())
    return bet, legs


@router.delete("/bets/{bet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bet(bet_id: uuid.UUID, request: Request, user: CurrentUser, sessions: Sessions) -> Response:
    async with sessions() as session:
        bet, _ = await _own_bet(session, user.id, bet_id)
        if bet.status != PlacedStatus.PENDING.value:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "SETTLED", "message": "A settled bet stays in the record"})
        await session.execute(delete(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet_id))
        await session.delete(bet)
        await session.commit()
    await tracker.bump(_redis(request), [user.id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bets/{bet_id}/cashout-advice")
async def cashout_advice(bet_id: uuid.UUID, body: CashoutRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        bet, legs = await _own_bet(session, user.id, bet_id)
        bankroll = await _bankroll(session, user.id, body.bankroll_inr)
    if bet.status != PlacedStatus.PENDING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "SETTLED", "message": "This bet is settled"})
    if bet.structure not in ("SINGLE", "DOUBLE", "TREBLE", "ACCUMULATOR"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "SYSTEM_BET", "message": "Cashout advice covers straight multiples; a system's lines settle separately"})
    open_rows = [leg for leg in legs if leg.result == PlacedStatus.PENDING.value]
    if not open_rows:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOTHING_OPEN", "message": "Every leg is decided: the bet settles"})
    redis = _redis(request)
    live: dict[str, Any] = {}
    if redis is not None:
        try:
            candidates, _ = await ashoka_market.load_candidates(redis, settings, now - timedelta(hours=3), fixtures={leg.fixture_id for leg in open_rows})
            live = {c.leg_id: c for c in candidates}
        except (RedisError, OSError, TimeoutError):
            live = {}
    open_legs: list[OpenLeg] = []
    for leg in open_rows:
        leg_id = f"{leg.fixture_id}|{leg.market}|{leg.selection}"
        candidate = live.get(leg_id)
        probability = body.probabilities.get(leg.position) or ashoka_market.fair_value_inputs(candidate)
        if probability is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"reason": "NO_LIVE_PRICE", "message": f"No live price for {leg.home} vs {leg.away} ({leg.market} {leg.selection}): give its current probability", "position": leg.position},
            )
        hedge_back: list[tuple[str, float]] = []
        hedge_book = None
        ref = parse_market(leg.market)
        if ref is not None:
            others = [live.get(f"{leg.fixture_id}|{ref.key}|{s}") for s in ref.selections if s != leg.selection]
            quotes = [(c, c.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, ("pinnacle", "betfair", "1xbet", "parimatch", "stake"))) for c in others if c is not None]
            if quotes and len(quotes) == len(ref.selections) - 1 and all(q is not None for _, q in quotes):
                hedge_back = [(c.selection, q.net_odds) for c, q in quotes]  # type: ignore[union-attr]
                hedge_book = ", ".join(sorted({q.bookmaker for _, q in quotes}))  # type: ignore[union-attr]
        open_legs.append(OpenLeg(f"{leg.home} vs {leg.away}: {leg.selection}", float(leg.odds), float(probability), tuple(hedge_back), hedge_book, body.lay_odds.get(leg.position)))
    settled = [SettledLeg(f"{leg.home} vs {leg.away}", float(leg.odds), LegResult(leg.result)) for leg in legs if leg.result != PlacedStatus.PENDING.value]
    try:
        advice = advise(
            float(bet.stake_inr), settled, open_legs, offer=None if body.offer_inr is None else float(body.offer_inr),
            bankroll=None if bankroll is None else float(bankroll), hold_ratio=settings.ASHOKA_CASHOUT_HOLD_RATIO,
            total_odds=None if bet.placed_odds is None else float(bet.placed_odds),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_ADVISABLE", "message": str(exc)}) from exc
    hedge = advice.hedge
    return {
        "bet_id": str(bet.id), "advice": advice.advice, "fair_value_inr": str(advice.fair_value), "potential_payout_inr": str(advice.potential_payout),
        "win_probability": round(advice.win_probability, 4), "offer_inr": None if advice.offer is None else str(advice.offer),
        "offer_ratio": None if advice.offer_ratio is None else round(advice.offer_ratio, 4), "implied_margin": None if advice.implied_margin is None else round(advice.implied_margin, 4),
        "certainty_equivalent_inr": None if advice.certainty_equivalent is None else str(advice.certainty_equivalent),
        "hedge": None if hedge is None else {"kind": hedge.kind, "book": hedge.book, "stakes": [{"outcome": o, "stake_inr": str(s)} for o, s in hedge.stakes],
                                             "total_outlay_inr": str(hedge.total_outlay), "locked_profit_inr": str(hedge.locked_profit), "instruction": hedge.instruction},
        "reasons": list(advice.reasons), "open_legs": len(open_legs), "settled_legs": len(settled),
    }


@router.post("/bets/{bet_id}/cashout")
async def take_cashout(bet_id: uuid.UUID, body: CashoutRecord, request: Request, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        bet, legs = await _own_bet(session, user.id, bet_id)
        try:
            await tracker.record_cashout(session, bet, body.cashout_inr, now)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "SETTLED", "message": str(exc)}) from exc
        await session.commit()
        payload = _bet_payload(bet, legs, now)
    await tracker.bump(_redis(request), [user.id])
    return payload


# ================================================================ P&L
@router.get("/pnl")
async def pnl(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings, tax: bool = False, tax_rate: Annotated[float | None, Query(ge=0, lt=1)] = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    report = await tracker.settle_pending(sessions, now, user_id=user.id)
    redis = _redis(request)
    if report.bets:
        await tracker.bump(redis, [user.id])
    rate = tracker.settings_tax(settings, tax, tax_rate)
    key, hit = await tracker.cached(redis, user.id, f"pnl:{rate}:{now.astimezone().date()}")
    if hit is not None:
        return {**hit, "cached": True}
    async with sessions() as session:
        bets, _ = await tracker.user_bets(session, user.id)
    card = tracker.scorecard(bets, now, settings.ORACLE_TIMEZONE, rate)
    await tracker.store(redis, key, card, ttl=120)
    return {**card, "cached": False}


@router.get("/twin")
async def twin(request: Request, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    await tracker.settle_pending(sessions, now, user_id=user.id)
    redis = _redis(request)
    key, hit = await tracker.cached(redis, user.id, "twin")
    if hit is not None:
        return hit
    async with sessions() as session:
        bets, legs = await tracker.user_bets(session, user.id)
    profile = tracker.twin_profile(bets, legs)
    await tracker.store(redis, key, profile, ttl=600)
    return profile


@router.post("/scores", status_code=status.HTTP_201_CREATED)
async def record_score(body: ScoreIn, request: Request, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        row = await tracker.record_score(session, body, source="admin", by=admin.id, now=now)
        await session.commit()
        fixture_id = row.fixture_id
    report = await tracker.settle_pending(sessions, now)
    await tracker.bump(_redis(request), report.users)
    return {"fixture_id": fixture_id, "settled_legs": report.legs, "settled_bets": report.bets}


@router.post("/scores/poll")
async def poll(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _need_redis(request)
    return await poll_scores(sessions, redis, settings, getattr(request.app.state, "vault", None))
