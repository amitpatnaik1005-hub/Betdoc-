"""The Smart Order Router under ``/api/v1/router`` (Group 71). Admin only: it spends the Vault fleet's balances.

    POST /router/orders                         route an order across the fleet (201; 409 with the deficit report)
    GET  /router/orders?active=true             routed orders with their slices (the Control Panel monitor)
    GET  /router/orders/{order_id}              one order, its slices, its receipt hash and Nalanda position
    GET  /router/venues                         every venue's circuit breaker (LIVE / PAUSED)
    POST /router/venues/{venue_id}/reset        close a venue's breaker by hand
    POST /router/slices/{slice_id}/release      give back an orphaned reservation (never-dispatched, past ROUTER_ORPHAN_SECONDS)
    GET  /router/legged                         legged positions handed to the Active Portfolio (hedge eligible)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin
from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models.execution_router import RoutedOrder
from app.models.omni_vault import VaultBookmakerAccount
from app.services.bookmaker_gateway import BookmakerGateway
from app.services.cfo_execution import TradeExecutor
from app.services.execution import circuit_breaker
from app.services.execution.slippage_guard import GarudaQuoteSource
from app.services.execution.smart_router import (
    HEDGE_ELIGIBLE,
    ExecutionOrder,
    RedisRouterEvents,
    RouterRefusal,
    SmartOrderRouter,
    list_orders,
    order_view,
)
from app.services.portfolio_positions import legged_positions
from app.workers.execution_dispatcher import CfoSliceExecutor, ExecutionDispatcher

router = APIRouter(prefix="/router", tags=["Smart Order Router"])


def get_router_sessions() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal


Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_router_sessions)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def build_router(request: Request, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> SmartOrderRouter:
    gateway: BookmakerGateway | None = getattr(request.app.state, "bookmaker", None)
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "BOOKMAKER_UNCONFIGURED", "message": "No bookmaker gateway is configured"})
    redis = _redis(request)
    dispatcher = ExecutionDispatcher(CfoSliceExecutor(TradeExecutor(sessions, redis, settings, gateway)), settings)
    return SmartOrderRouter(sessions, settings, quotes=GarudaQuoteSource(redis, settings), dispatcher=dispatcher, events=RedisRouterEvents(redis, settings))


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def route_order(payload: dict[str, Any], request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        order = ExecutionOrder.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False, include_context=False)) from exc
    smart = build_router(request, sessions, settings)
    try:
        return await smart.route(order, user_id=admin.id)
    except RouterRefusal as exc:
        raise HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, "report": exc.detail, "order": exc.view}) from exc


@router.get("/orders")
async def orders(admin: CurrentAdmin, sessions: Sessions, settings: AppSettings, active: bool = False, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        return await list_orders(session, settings, active=active, limit=limit)


@router.get("/orders/{order_id}")
async def order(order_id: str, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        row = (await session.execute(select(RoutedOrder).where(RoutedOrder.order_id == order_id))).scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such routed order")
    smart = SmartOrderRouter(sessions, settings, quotes=GarudaQuoteSource(None, settings), dispatcher=ExecutionDispatcher(_Idle(), settings),
                             events=RedisRouterEvents(_redis(request), settings))
    return await smart.view(order_id)


@router.get("/venues")
async def venues(admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    now = datetime.now(UTC)
    async with sessions() as session:
        books = sorted(set((await session.execute(select(VaultBookmakerAccount.bookmaker_id).where(VaultBookmakerAccount.is_active.is_(True)))).scalars()))
        rows = await circuit_breaker.states(session, now=now, venue_ids=books)
    return {"generated_at": now.isoformat(), "policy": {"failures": settings.ROUTER_BREAKER_FAILURES, "window_seconds": settings.ROUTER_BREAKER_WINDOW_SECONDS,
            "pause_seconds": settings.ROUTER_BREAKER_PAUSE_SECONDS, "orphan_seconds": settings.ROUTER_ORPHAN_SECONDS},
            "venues": [r.as_dict() for r in rows]}


@router.post("/venues/{venue_id}/reset")
async def reset_venue(venue_id: str, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:  # noqa: ARG001
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        await circuit_breaker.reset(session, venue_id[:64], now=now)
    async with sessions() as session:
        state = next(s for s in await circuit_breaker.states(session, now=now, venue_ids=[venue_id[:64]]) if s.venue_id == venue_id[:64])
    return state.as_dict()


@router.post("/slices/{slice_id}/release")
async def release_slice(slice_id: uuid.UUID, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    smart = SmartOrderRouter(sessions, settings, quotes=GarudaQuoteSource(None, settings), dispatcher=ExecutionDispatcher(_Idle(), settings),
                             events=RedisRouterEvents(_redis(request), settings))
    try:
        return await smart.release_orphan(slice_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such slice") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/legged")
async def legged(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    async with sessions() as session:
        rows = list((await session.execute(select(RoutedOrder).where(RoutedOrder.hedge_state == HEDGE_ELIGIBLE).order_by(RoutedOrder.created_at.desc()).limit(100))).scalars())
    return {"orders": [order_view(r, []) for r in rows], "portfolio": await legged_positions(_redis(request), settings, admin.id)}


class _Idle:
    """Reads and operator actions never dispatch."""

    async def execute(self, ticket: Any) -> Any:  # noqa: ARG002
        raise RuntimeError("this router instance does not dispatch")
