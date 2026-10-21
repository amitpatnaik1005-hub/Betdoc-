"""TODAR MAL's exchange rates and the Section 115BBJ estimator (Group 97: moved out of the retired Paper Trading Sandbox).

Developed for Amit Ashok Kumar Patnaik. Every route needs a signed-in user; forcing a provider refresh is admin-only.

* ``GET  /currency/rate``, ``/currency/convert``, ``/currency/board``; ``POST /currency/refresh`` (admin): live FX,
  never a guessed constant (503 when no fresh rate exists)
* ``GET  /tax/estimate``: 30% TDS on one bet's net winnings (Section 115BBJ / 194BA)

The real-money tax certificate stays at ``/real-money/tax-certificate``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, CurrentUser, get_db
from app.core.config import Settings, get_settings
from app.schemas.currency_tax import ConversionRead, CurrencyBoardRead, CurrencyRateRead, TaxEstimateResponse
from app.services.currency_service import CurrencyService, CurrencyUnavailableError
from app.services.twin.vetting import developer_credit

router = APIRouter(tags=["Currency & Tax (TODAR MAL)"])

Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _fx_down(exc: CurrencyUnavailableError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


# ================================================================ currency
@router.get("/currency/rate", response_model=CurrencyRateRead, summary="Get Live FX Rate")
async def get_fx_rate(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser,  # noqa: ARG001
                      base: Annotated[str, Query(min_length=3, max_length=8)] = "USD",
                      quote: Annotated[str, Query(min_length=3, max_length=8)] = "INR") -> CurrencyRateRead:
    """Quote units per one base unit, from a fresh rate (never a guessed constant: 503 when none is fresh)."""
    try:
        rate = await CurrencyService(settings, _redis(request)).get_exchange_rate(db, base, quote, datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CurrencyUnavailableError as exc:
        raise _fx_down(exc) from exc
    return CurrencyRateRead(base_currency=rate.base, quote_currency=rate.quote, rate=float(rate.rate), last_updated_at=rate.as_of,
                            provider=rate.provider, developer_credit=await developer_credit(db))


@router.get("/currency/convert", response_model=ConversionRead, summary="Convert an Amount")
async def convert(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser,  # noqa: ARG001
                  amount: Annotated[float, Query(ge=0, le=1e12)],
                  from_currency: Annotated[str, Query(alias="from", min_length=3, max_length=8)] = "INR",
                  to_currency: Annotated[str, Query(alias="to", min_length=3, max_length=8)] = "USD") -> ConversionRead:
    try:
        converted, rate = await CurrencyService(settings, _redis(request)).convert(db, Decimal(str(amount)), from_currency, to_currency, datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CurrencyUnavailableError as exc:
        raise _fx_down(exc) from exc
    return ConversionRead(amount=amount, from_currency=rate.base, to_currency=rate.quote, converted=float(converted), rate=float(rate.rate),
                          last_updated_at=rate.as_of, provider=rate.provider, developer_credit=await developer_credit(db))


@router.get("/currency/board", response_model=CurrencyBoardRead, summary="INR per Unit of Every Supported Currency")
async def board(request: Request, db: Db, settings: AppSettings, current_user: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    return {**await CurrencyService(settings, _redis(request)).board(db, datetime.now(UTC)), "developer_credit": await developer_credit(db)}


@router.post("/currency/refresh", response_model=CurrencyBoardRead, summary="Refresh the Rates from the Providers (admin)")
async def refresh(request: Request, db: Db, settings: AppSettings, admin: CurrentAdmin) -> dict[str, Any]:  # noqa: ARG001
    service = CurrencyService(settings, _redis(request))
    now = datetime.now(UTC)
    await service.refresh(db, now)
    return {**await service.board(db, now), "developer_credit": await developer_credit(db)}


# ================================================================ tax (Section 115BBJ)
@router.get("/tax/estimate", response_model=TaxEstimateResponse, summary="Section 115BBJ 30% TDS Estimator")
async def estimate_indian_tds(db: Db, settings: AppSettings, current_user: CurrentUser,  # noqa: ARG001
                              gross_win: Annotated[float, Query(ge=0, le=1e12)], stake: Annotated[float, Query(ge=0, le=1e12)]) -> TaxEstimateResponse:
    """30% TDS on the net winnings (``max(0, payout - stake)``) of one bet."""
    calc = CurrencyService(settings).calculate_indian_tds(gross_win, stake)
    return TaxEstimateResponse(gross_win=calc["gross_win"], stake=calc["stake"], gross_profit=calc["net_profit"], tds_rate_pct=calc["tds_rate_pct"],
                               tds_deducted=calc["tds_deducted"], net_take_home=calc["net_take_home"], cess=calc["cess"],
                               effective_rate_pct=calc["effective_rate_pct"], developer_credit=await developer_credit(db))
