"""The True Digital Betting Twin under ``/api/v1/twin`` (Group 72).

    POST   /twin/vet                         run the 14-pillar fortress over one of Ashoka's slips (an audit row)
    GET    /twin/audits?vetted=true          the user's audits, newest first
    GET    /twin/audits/{id}                 one audit, every pillar with its reason and numbers
    POST   /twin/audits/{id}/confirm         pillar 14 again on freshly read prices: run it just before placing
    POST   /twin/audits/{id}/ledger          Pathway A: "I placed it" (book, stake, booking code) into Ashoka's ledger, watched in play
    POST   /twin/audits/{id}/route           Pathway B (admin): a vetted single to the Smart Order Router, after the re-check
    GET    /twin/ledger                      the twin-placed bets' own scorecard (the full P&L stays at /oracle/pnl)
    GET    /twin/intel/{fixture_id}          the evidence the context pillars read for a fixture
    PUT    /twin/intel/{fixture_id}          write evidence sections (admin, or a feed through the same body)
    DELETE /twin/intel/{fixture_id}          drop sections (admin)
    GET    /twin/monitors                    the user's in-play watches
    POST   /twin/monitors/{bet_id}           watch a placed bet (re-arms a fired watch)
    PUT    /twin/monitors/{id}/offer         the book's current cashout offer, as the user reads it
    DELETE /twin/monitors/{id}               stop watching
    POST   /twin/monitors/tick               re-price the user's watches now (Celery does it every TWIN_INPLAY_POLL_SECONDS)

Nothing here calls a bookmaker except ``route``, which goes through the router's own guards (kill switch,
slippage, Vault reservations). Booking codes are the bookmaker's own: the user copies them from the book.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.digital_twin import TwinInPlayMonitor, TwinVettingAudit
from app.models.user_bets_ledger import UserPlacedBet
from app.schemas.twin import INTEL_SECTIONS, FixtureIntel, LedgerFromAudit, MonitorOffer, MonitorStart, VetRequest
from app.services import user_pnl_tracker as tracker
from app.services.twin import inplay, vetting
from app.services.twin.intel import clear_intel, read_intel, write_intel

router = APIRouter(prefix="/twin", tags=["True Digital Betting Twin"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _need_redis(request: Request) -> Redis:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_MARKET", "message": "The live market (Redis) is unavailable"})
    return redis


def _refused(exc: vetting.TwinRefusal) -> HTTPException:
    return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, **exc.detail})


async def _own_audit(session: AsyncSession, user_id: uuid.UUID, audit_id: uuid.UUID) -> TwinVettingAudit:
    audit = await session.get(TwinVettingAudit, audit_id)
    if audit is None or audit.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such audit")
    return audit


# ================================================================ the fortress
@router.post("/vet", status_code=status.HTTP_201_CREATED)
async def vet(body: VetRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    redis = _need_redis(request)
    now = datetime.now(UTC)
    try:
        audit = await vetting.vet(sessions, redis, settings, user_id=user.id, leg_ids=body.leg_ids, kind=body.kind, bankroll=body.bankroll_inr, now=now)
    except vetting.TwinRefusal as exc:
        raise _refused(exc) from exc
    except (RedisError, OSError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_MARKET", "message": "The live market (Redis) is unavailable"}) from exc
    async with sessions() as session:
        credit = await vetting.developer_credit(session)
    return vetting.audit_view(audit, credit)


@router.get("/audits")
async def audits(user: CurrentUser, sessions: Sessions, vetted: bool = False, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    async with sessions() as session:
        query = select(TwinVettingAudit).where(TwinVettingAudit.user_id == user.id)
        if vetted:
            query = query.where(TwinVettingAudit.is_vetted.is_(True))
        rows = list((await session.execute(query.order_by(TwinVettingAudit.created_at.desc()).limit(limit))).scalars())
        credit = await vetting.developer_credit(session)
    return {"developer_credit": credit, "audits": [vetting.audit_view(a) for a in rows]}


@router.get("/audits/{audit_id}")
async def audit(audit_id: uuid.UUID, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    async with sessions() as session:
        row = await _own_audit(session, user.id, audit_id)
        credit = await vetting.developer_credit(session)
    return vetting.audit_view(row, credit)


@router.post("/audits/{audit_id}/confirm")
async def confirm(audit_id: uuid.UUID, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    redis = _need_redis(request)
    async with sessions() as session:
        row = await _own_audit(session, user.id, audit_id)
    try:
        return await vetting.confirm(sessions, redis, settings, row, datetime.now(UTC))
    except vetting.TwinRefusal as exc:
        raise _refused(exc) from exc
    except (RedisError, OSError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_MARKET", "message": "The live market (Redis) is unavailable"}) from exc


@router.post("/audits/{audit_id}/ledger", status_code=status.HTTP_201_CREATED)
async def placed(audit_id: uuid.UUID, body: LedgerFromAudit, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    now = datetime.now(UTC)
    redis = _redis(request)
    async with sessions() as session:
        row = await _own_audit(session, user.id, audit_id)
        try:
            bet = await vetting.record_placed(session, user.id, row, body, now)
        except vetting.TwinRefusal as exc:
            raise _refused(exc) from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "INVALID_SLIP", "message": str(exc)}) from exc
        watch: dict[str, Any] = {"started": False}
        if body.watch:
            try:
                monitor = await inplay.start(session, redis, settings, bet, now, audit_id=row.id, target_profit_pct=body.target_profit_pct, stop_loss_pct=body.stop_loss_pct)
                watch = {"started": True, "monitor": inplay.monitor_view(monitor)}
            except inplay.NoLivePrice as exc:
                watch = {"started": False, "reason": "NO_LIVE_PRICE", "message": str(exc)}
            except ValueError as exc:
                watch = {"started": False, "reason": "NOT_WATCHABLE", "message": str(exc)}
        await session.commit()
    await tracker.bump(redis, [user.id])
    return {"bet_id": str(bet.id), "status": bet.status, "bookmaker": bet.bookmaker, "booking_code": bet.booking_code, "stake_inr": str(bet.stake_inr),
            "placed_odds": None if bet.placed_odds is None else str(bet.placed_odds), "vetting_audit_id": str(row.id), "watch": watch}


@router.post("/audits/{audit_id}/route", status_code=status.HTTP_201_CREATED)
async def route(audit_id: uuid.UUID, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    """Pathway B: the Smart Order Router places a vetted single at its audited book, after pillar 14 passes again."""
    from app.api.v1.execution_router import build_router  # noqa: PLC0415 - the router stack is heavy
    from app.services.execution.smart_router import ExecutionOrder, RouterRefusal  # noqa: PLC0415

    redis = _need_redis(request)
    async with sessions() as session:
        row = await _own_audit(session, admin.id, audit_id)
    if not row.is_vetted:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_VETTED", "message": "Only a slip that cleared every enforced pillar is routed"})
    if len(row.leg_ids) != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_A_SINGLE", "message": "The router places one selection per order: place a multiple by hand (Pathway A)"})
    if row.stake_inr <= 0:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NO_STAKE", "message": "The audit sized no stake"})
    check = await vetting.confirm(sessions, redis, settings, row, datetime.now(UTC))
    if check["status"] != "PASS":
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "RECHECK_FAILED", "message": check["reason"], "check": check})
    leg = row.slip["legs"][0]
    live = check["legs"][0]
    q4 = Decimal("0.0001")
    try:
        order = ExecutionOrder(
            order_id=f"twin-{row.id.hex}", match_id=leg["fixture_id"], market=leg["market"], selection=leg["selection"],
            odds=Decimal(str(live["current_odds"])).quantize(q4, rounding=ROUND_DOWN),
            min_acceptable_odds=min(Decimal(str(live["floor"])), Decimal(str(live["current_odds"]))).quantize(q4, rounding=ROUND_DOWN),
            desired_total_stake=row.stake_inr, target_bookmakers=(row.bookmaker,), currency="INR",
            true_prob=Decimal(str(leg["fair_probability"])).quantize(Decimal("0.000001")) if leg.get("fair_probability") else None,
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False, include_context=False)) from exc
    smart = build_router(request, sessions, settings)
    try:
        return {"audit_id": str(row.id), "check": check, "order": await smart.route(order, user_id=admin.id)}
    except RouterRefusal as exc:
        raise HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, "report": exc.detail, "order": exc.view}) from exc


@router.get("/ledger")
async def ledger(user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    """The bets placed from twin audits: scorecard, and each with its booking code and audit."""
    now = datetime.now(UTC)
    async with sessions() as session:
        bets = list((await session.execute(
            select(UserPlacedBet).where(UserPlacedBet.user_id == user.id, UserPlacedBet.vetting_audit_id.is_not(None)).order_by(UserPlacedBet.placed_at.desc())
        )).scalars())
        credit = await vetting.developer_credit(session)
    card = tracker.scorecard(bets, now, settings.ORACLE_TIMEZONE)
    return {
        **card, "developer_credit": credit,
        "bets": [{"id": str(b.id), "status": b.status, "bookmaker": b.bookmaker, "booking_code": b.booking_code, "stake_inr": str(b.stake_inr),
                  "placed_odds": None if b.placed_odds is None else str(b.placed_odds), "pnl_inr": None if b.pnl_inr is None else str(b.pnl_inr),
                  "vetting_audit_id": str(b.vetting_audit_id), "placed_at": b.placed_at.isoformat()} for b in bets[:200]],
    }


# ================================================================ evidence
@router.get("/intel/{fixture_id}")
async def get_intel(fixture_id: str, request: Request, user: CurrentUser, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _need_redis(request)
    found = (await read_intel(redis, settings, [fixture_id])).get(fixture_id)
    return {"fixture_id": fixture_id, "sections": {} if found is None else found.model_dump(mode="json", exclude_none=True),
            "max_age_minutes": settings.TWIN_INTEL_MAX_AGE_MINUTES}


@router.put("/intel/{fixture_id}")
async def put_intel(fixture_id: str, body: FixtureIntel, request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _need_redis(request)
    if len(fixture_id) > 128:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "fixture id too long")
    written = await write_intel(redis, settings, fixture_id, body)
    if not written:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "EMPTY", "message": f"send at least one of {', '.join(INTEL_SECTIONS)}"})
    return {"fixture_id": fixture_id, "written": written}


@router.delete("/intel/{fixture_id}")
async def delete_intel(fixture_id: str, request: Request, admin: CurrentAdmin, settings: AppSettings,  # noqa: ARG001
                       sections: Annotated[list[str] | None, Query()] = None) -> dict[str, Any]:
    redis = _need_redis(request)
    return {"fixture_id": fixture_id, "removed": await clear_intel(redis, settings, fixture_id, sections or INTEL_SECTIONS)}


# ================================================================ the in-play watch
async def _own_monitor(session: AsyncSession, user_id: uuid.UUID, monitor_id: uuid.UUID) -> TwinInPlayMonitor:
    row = await session.get(TwinInPlayMonitor, monitor_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such watch")
    return row


@router.get("/monitors")
async def monitors(user: CurrentUser, sessions: Sessions, active: bool = False) -> list[dict[str, Any]]:
    async with sessions() as session:
        query = select(TwinInPlayMonitor).where(TwinInPlayMonitor.user_id == user.id)
        if active:
            query = query.where(TwinInPlayMonitor.is_active.is_(True))
        rows = list((await session.execute(query.order_by(TwinInPlayMonitor.created_at.desc()).limit(200))).scalars())
        bets = {b.id: b for b in (await session.execute(select(UserPlacedBet).where(UserPlacedBet.id.in_([r.bet_id for r in rows])))).scalars()} if rows else {}
    return [inplay.monitor_view(r, bets.get(r.bet_id)) for r in rows]


@router.post("/monitors/tick")
async def tick(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    report = await inplay.tick(sessions, _redis(request), settings, datetime.now(UTC), user_id=user.id)
    return {"ran": report.ran, "watched": report.watched, "priced": report.priced, "closed": report.closed, "alerts": report.alerts}


@router.post("/monitors/{bet_id}", status_code=status.HTTP_201_CREATED)
async def watch(bet_id: uuid.UUID, body: MonitorStart, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    async with sessions() as session:
        bet = await session.get(UserPlacedBet, bet_id)
        if bet is None or bet.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such bet")
        try:
            monitor = await inplay.start(session, _redis(request), settings, bet, datetime.now(UTC), target_profit_pct=body.target_profit_pct, stop_loss_pct=body.stop_loss_pct)
        except inplay.NoLivePrice as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "NO_LIVE_PRICE", "message": str(exc), "positions": exc.positions}) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_WATCHABLE", "message": str(exc)}) from exc
        await session.commit()
        return inplay.monitor_view(monitor, bet)


@router.put("/monitors/{monitor_id}/offer")
async def offer(monitor_id: uuid.UUID, body: MonitorOffer, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    async with sessions() as session:
        row = await _own_monitor(session, user.id, monitor_id)
        row.cashout_offer_inr = None if body.cashout_offer_inr is None else body.cashout_offer_inr.quantize(Decimal("0.01"))
        row.updated_at = datetime.now(UTC)
        await session.commit()
        return inplay.monitor_view(row)


@router.delete("/monitors/{monitor_id}")
async def unwatch(monitor_id: uuid.UUID, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:
    async with sessions() as session:
        row = await _own_monitor(session, user.id, monitor_id)
        row.is_active = False
        row.updated_at = datetime.now(UTC)
        row.detail = {**(row.detail or {}), "closed": "stopped by the user"}
        await session.commit()
        return inplay.monitor_view(row)

