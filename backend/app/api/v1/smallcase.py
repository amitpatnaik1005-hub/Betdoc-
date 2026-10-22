"""Live smallcases under ``/api/v1/smallcase`` (Group 107, point b.8; ASHOKA's market, KUMBHA's sizing). Developed for
Amit Ashok Kumar Patnaik. Every route needs a signed-in user and the live market (Redis: 503 without it); nothing is
placed from here.

    GET  /smallcase/baskets                     the six baskets from the live market, sized for you (?bankroll_inr= overrides the CFO account)
    GET  /smallcase/baskets/{basket_id}         one basket
    POST /smallcase/baskets/{basket_id}/rebalance   the same, from a fresh scan of the market (the cached scan is replaced)
    GET  /smallcase/ticker                      the six baskets in one line each
    POST /smallcase/rebalance-slip              your slip as a smallcase at your book: one holding per fixture, legs under fair left out
    GET  /smallcase/components                  every quantitative component, implemented or not, and how the smallcases use it
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.schemas.smallcase import SlipAsSmallcase
from app.services import smallcase
from app.services.twin import vetting

router = APIRouter(prefix="/smallcase", tags=["Live Smallcases (ASHOKA, KUMBHA)"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Bankroll = Annotated[Decimal | None, Query(gt=0, le=Decimal("1000000000"))]


def _need_redis(request: Request) -> Redis:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The live market lives in Redis, which is unavailable"})
    return redis


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, smallcase.SmallcaseRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message})
    if isinstance(exc, vetting.TwinRefusal):
        return HTTPException(exc.status_code, {"reason": exc.reason, "message": exc.message, **exc.detail})
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "INVALID", "message": str(exc)})


@router.get("/baskets")
async def baskets(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings, bankroll_inr: Bankroll = None) -> dict[str, Any]:
    return await smallcase.baskets(sessions, _need_redis(request), settings, user.id, bankroll=bankroll_inr, now=datetime.now(UTC))


@router.get("/baskets/{basket_id}")
async def basket(basket_id: str, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings, bankroll_inr: Bankroll = None) -> dict[str, Any]:
    board = await smallcase.baskets(sessions, _need_redis(request), settings, user.id, bankroll=bankroll_inr, now=datetime.now(UTC))
    try:
        return smallcase.basket(board, basket_id)
    except smallcase.SmallcaseRefusal as exc:
        raise _refused(exc) from exc


@router.post("/baskets/{basket_id}/rebalance")
async def rebalance(basket_id: str, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings, bankroll_inr: Bankroll = None) -> dict[str, Any]:
    """The basket rebuilt from a fresh read of the live market (and the shared scan replaced with it)."""
    board = await smallcase.baskets(sessions, _need_redis(request), settings, user.id, bankroll=bankroll_inr, now=datetime.now(UTC), fresh=True)
    try:
        return smallcase.basket(board, basket_id)
    except smallcase.SmallcaseRefusal as exc:
        raise _refused(exc) from exc


@router.get("/ticker")
async def ticker(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    board = await smallcase.baskets(sessions, _need_redis(request), settings, user.id, bankroll=None, now=datetime.now(UTC))
    return {"as_of": board["as_of"], "baskets": smallcase.ticker(board)}


@router.post("/rebalance-slip")
async def rebalance_slip(body: SlipAsSmallcase, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        return await smallcase.rebalance_slip(sessions, _need_redis(request), settings, user.id, leg_ids=body.leg_ids, skin=body.skin,
                                              bankroll=body.bankroll_inr, now=datetime.now(UTC))
    except (smallcase.SmallcaseRefusal, vetting.TwinRefusal, ValueError) as exc:
        raise _refused(exc) from exc


@router.get("/components")
async def components(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    return await smallcase.components(sessions, _need_redis(request), settings, user.id, datetime.now(UTC))
