"""Hedging, arbitrage and FX under ``/api/v1/omni`` (Group 64).

    GET  /omni/portfolio             every open book marked to the live books, with its hedges
    POST /omni/portfolio/hedge       fire a hedge (free bet ... balanced) leg by leg
    GET  /omni/arbitrage             the latest scan (commission- and FX-adjusted)
    POST /omni/arbitrage/execute     fire an arbitrage: Leg A first, the rest sized from its fill
    GET  /omni/fx-rates              the live rates and their age
    PUT  /omni/fx-rates              set one (admin)

The live view is ``/api/v1/ws/portfolio`` (5 updates a second); these are the request/response forms.
Refusals carry ``{"reason", "message", ...}`` in ``detail``, like every CFO endpoint.
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.schemas.portfolio import ArbitrageExecuteRequest, FxRateUpdate, HedgeExecuteRequest, MultiLegReceipt
from app.services.bookmaker_gateway import BookmakerGateway
from app.services.cfo_execution import TradeExecutor
from app.services.cfo_ledger import CfoError
from app.services.fx_rates import FxRates, FxUnavailableError
from app.services.leg_executor import LegExecutor
from app.services.portfolio_manager import PortfolioManager
from app.services.portfolio_stream import PortfolioKeys

router = APIRouter(prefix="/omni", tags=["portfolio"])

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _http(exc: CfoError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.message, **exc.detail})


def _manager(request: Request, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> PortfolioManager:
    sniper = getattr(request.app.state, "sniper", None)
    return PortfolioManager(_redis(request), sessions, settings, sniper.gateway.venues if sniper is not None else None)


def _legs(request: Request, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> LegExecutor:
    gateway: BookmakerGateway | None = getattr(request.app.state, "bookmaker", None)
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "BOOKMAKER_UNCONFIGURED", "message": "No bookmaker gateway is configured"})
    redis = _redis(request)
    return LegExecutor(TradeExecutor(sessions, redis, settings, gateway), _manager(request, sessions, settings), redis, settings)


# ---------------------------------------------------------------- portfolio + hedging
@router.get("/portfolio")
async def portfolio(request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> dict[str, Any]:
    return await _manager(request, sessions, settings).snapshot(user.id, cached=False)


@router.post("/portfolio/hedge", response_model=MultiLegReceipt)
async def hedge(payload: HedgeExecuteRequest, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> MultiLegReceipt:
    try:
        return await _legs(request, sessions, settings).execute_hedge(user.id, payload)
    except CfoError as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- arbitrage
@router.get("/arbitrage")
async def arbitrage(request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    if redis is not None:
        try:
            raw = await redis.get(PortfolioKeys(settings).arbitrage_last)
        except (RedisError, OSError):
            raw = None
        if raw:
            return json.loads(raw)
    arbs = await _manager(request, sessions, settings).scan()  # no publisher running: scan on demand
    return {"type": "arbitrage", "ts": None, "arbs": arbs}


@router.post("/arbitrage/execute", response_model=MultiLegReceipt)
async def execute_arbitrage(
    payload: ArbitrageExecuteRequest, request: Request, user: CurrentUser, sessions: SessionFactory, settings: AppSettings
) -> MultiLegReceipt:
    try:
        return await _legs(request, sessions, settings).execute_arbitrage(user.id, payload)
    except CfoError as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- FX
@router.get("/fx-rates")
async def fx_rates(request: Request, user: CurrentUser, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    fx = FxRates(_redis(request), settings)
    now = time.time()
    rates = await fx.snapshot()
    return {
        "max_age_seconds": settings.FX_MAX_AGE_SECONDS,
        "haircut_pct": settings.FX_HAIRCUT_PCT,
        "rates": [{**rate.as_dict(now), "usable": now - rate.as_of <= settings.FX_MAX_AGE_SECONDS} for rate in sorted(rates.values(), key=lambda r: r.currency)],
    }


@router.put("/fx-rates")
async def set_fx_rate(payload: FxRateUpdate, request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    try:
        rate = await FxRates(_redis(request), settings).publish(payload.currency, payload.inr_per_unit, payload.source)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "INVALID_RATE", "message": str(exc)}) from exc
    except (FxUnavailableError, RedisError, OSError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "FX_UNAVAILABLE", "message": "Rates are stored in Redis, which is unavailable"}) from exc
    return rate.as_dict(time.time())
