from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import BetLedger, ExchangeAccount
from app.schemas.bet import BetRead

router = APIRouter(tags=["ledger"])

MAX_PAGE_SIZE = 500


def _as_utc(value: datetime | None) -> datetime | None:
    # Treat naive datetimes as UTC so filters don't shift by the DB session timezone
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.get("", response_model=list[BetRead])
async def list_ledger(
    current_user: CurrentUser,
    db: DbSession,
    # Pagination guard: without an upper bound, ?limit=10000000 would load the whole ledger
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[BetRead]:
    start = _as_utc(start_date)
    end = _as_utc(end_date)

    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be on or before end_date",
        )

    stmt = (
        select(BetLedger)
        .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
        .where(ExchangeAccount.user_id == current_user.id)
    )

    if start is not None:
        stmt = stmt.where(BetLedger.placed_at >= start)
    if end is not None:
        stmt = stmt.where(BetLedger.placed_at <= end)

    # BetLedger.id breaks ties: bets sharing a placed_at would otherwise shuffle
    # between pages, causing duplicates and skipped rows
    stmt = (
        stmt.order_by(BetLedger.placed_at.desc(), BetLedger.id.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    return [BetRead.model_validate(bet) for bet in result.scalars().all()]
