from __future__ import annotations

import math
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.dashboard._common import align_to_column, ensure_aware_utc, to_float, utc_now
from app.domain.risk.common import ACTIVE_STATUSES
from app.models import BetLedger, ExchangeAccount, RiskMandate
from app.schemas.dashboard import DashboardSummary

MIN_TZ_OFFSET_HOURS = -12.0
MAX_TZ_OFFSET_HOURS = 14.0
STOP_LOSS_WARNING_RATIO = 0.80
STOP_LOSS_BREACH_RATIO = 1.00

SETTLED_STATUSES = ("WON", "LOST", "HALF_WON", "HALF_LOST", "VOID", "CASH_OUT")


def utc_start_of_local_day(tz_offset_hours: float, now: datetime | None = None) -> datetime:
    """Return the UTC instant corresponding to the user's local midnight."""
    if not math.isfinite(tz_offset_hours) or not (
        MIN_TZ_OFFSET_HOURS <= tz_offset_hours <= MAX_TZ_OFFSET_HOURS
    ):
        raise ValueError(
            f"tz_offset_hours must be between {MIN_TZ_OFFSET_HOURS} and {MAX_TZ_OFFSET_HOURS}"
        )
    now_utc = ensure_aware_utc(now or utc_now())
    offset = timedelta(hours=tz_offset_hours)
    local_wall_clock = now_utc + offset
    local_midnight = local_wall_clock.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight - offset


def asian_win_rate_pct(won: float, lost: float, half_won: float, half_lost: float) -> float:
    """(WON + 0.5 * HALF_WON) / (WON + LOST + HALF_WON + HALF_LOST), 0.0 on empty denominator."""
    try:
        rate = (won + 0.5 * half_won) / (won + lost + half_won + half_lost)
    except ZeroDivisionError:
        return 0.0
    return round(rate * 100.0, 2)


def stop_loss_status(exposure: float, max_daily_exposure: float | None) -> str:
    if max_daily_exposure is None:
        return "NO_MANDATE"
    if max_daily_exposure <= 0:
        return "BREACHED" if exposure > 0 else "OK"
    ratio = exposure / max_daily_exposure
    if ratio >= STOP_LOSS_BREACH_RATIO:
        return "BREACHED"
    if ratio >= STOP_LOSS_WARNING_RATIO:
        return "WARNING"
    return "OK"


def _status_count(*statuses: str):
    return func.coalesce(func.sum(case((BetLedger.status.in_(statuses), 1), else_=0)), 0)


async def build_dashboard_summary(
    user_id: UUID, db: AsyncSession, tz_offset_hours: float = 0.0
) -> DashboardSummary:
    day_start = align_to_column(utc_start_of_local_day(tz_offset_hours), BetLedger.resolved_at)
    pnl_expr = func.coalesce(BetLedger.payout, 0.0) - BetLedger.stake

    aggregates = (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                BetLedger.status.in_(SETTLED_STATUSES),
                                BetLedger.resolved_at.is_not(None),
                                BetLedger.resolved_at >= day_start,
                            ),
                            pnl_expr,
                        ),
                        else_=0.0,
                    )
                ),
                0.0,
            ).label("daily_pnl"),
            _status_count("WON").label("won"),
            _status_count("LOST").label("lost"),
            _status_count("HALF_WON").label("half_won"),
            _status_count("HALF_LOST").label("half_lost"),
            _status_count(*ACTIVE_STATUSES).label("active_bets"),
            func.coalesce(
                func.sum(case((BetLedger.status.in_(ACTIVE_STATUSES), BetLedger.stake), else_=0.0)),
                0.0,
            ).label("exposure"),
            func.coalesce(
                func.sum(case((BetLedger.status.in_(SETTLED_STATUSES), pnl_expr), else_=0.0)),
                0.0,
            ).label("realized_pnl"),
        )
        .select_from(BetLedger)
        .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
        .where(ExchangeAccount.user_id == user_id)
    )
    row = (await db.execute(aggregates)).one()

    mandate_limit = (
        await db.execute(
            select(RiskMandate.max_daily_exposure).where(RiskMandate.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()

    exposure = to_float(row.exposure)
    max_exposure = None if mandate_limit is None else to_float(mandate_limit)

    return DashboardSummary(
        # Configured starting capital plus everything realised since.
        total_bankroll=round(get_settings().starting_bankroll + to_float(row.realized_pnl), 2),
        daily_pnl=round(to_float(row.daily_pnl), 2),
        active_bets_count=int(row.active_bets or 0),
        win_rate_pct=asian_win_rate_pct(
            to_float(row.won), to_float(row.lost), to_float(row.half_won), to_float(row.half_lost)
        ),
        current_exposure=round(exposure, 2),
        stop_loss_status=stop_loss_status(exposure, max_exposure),
    )
