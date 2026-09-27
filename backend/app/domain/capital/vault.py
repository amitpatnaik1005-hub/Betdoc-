import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import DateTime, Numeric, Select, case, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BetLedger, ExchangeAccount, utc_now
from app.schemas.vault import (
    BookmakerPnL,
    CapitalOverview,
    GrowthNode,
    MarketPnL,
    PnLTimeframe,
    TimeframePnLNode,
    VaultFilterParams,
    WaterfallNode,
)

porus = logging.getLogger("betdoc.porus")

# ---------- Status sets ----------
ACTIVE_STATUSES: tuple[str, ...] = ("PENDING", "PENDING_NETWORK", "ACCEPTED", "UNKNOWN")
DECIDED_STATUSES: tuple[str, ...] = ("WON", "LOST", "HALF_WON", "HALF_LOST", "CASH_OUT")  # volume excludes VOID / REJECTED
SETTLED_STATUSES: tuple[str, ...] = DECIDED_STATUSES + ("VOID",)

# ---------- Limits ----------
MAX_TIMELINE_PERIODS = 3700   # ~10 years of daily nodes
CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,10}$")

# ---------- Decimal-safe SQL building blocks ----------
MONEY_SQL_TYPE = Numeric(20, 6)
SQL_ZERO = literal(Decimal("0"), MONEY_SQL_TYPE)
SQL_ONE = literal(Decimal("1"), MONEY_SQL_TYPE)

# Payout NULL Guard (matches KUMBHA): profit = coalesce(payout, 0) - stake for WON/LOST, else 0.
PROFIT_EXPR = case(
    (BetLedger.status.in_(DECIDED_STATUSES), func.coalesce(BetLedger.payout, SQL_ZERO) - BetLedger.stake),
    else_=SQL_ZERO,
)
POSITIVE_PROFIT_EXPR = case((PROFIT_EXPR > SQL_ZERO, PROFIT_EXPR), else_=SQL_ZERO)
NEGATIVE_PROFIT_EXPR = case((PROFIT_EXPR < SQL_ZERO, -PROFIT_EXPR), else_=SQL_ZERO)
VOLUME_EXPR = case((BetLedger.status.in_(DECIDED_STATUSES), BetLedger.stake), else_=SQL_ZERO)
WON_COUNT_EXPR = case((BetLedger.status == "WON", 1), else_=0)
LOST_COUNT_EXPR = case((BetLedger.status == "LOST", 1), else_=0)
EXPECTED_PROFIT_EXPR = BetLedger.stake * ((BetLedger.true_probability * BetLedger.odds) - SQL_ONE)

# Fixed internal mapping; values are rendered as SQL constants (never user input).
_TRUNC_UNITS: dict[PnLTimeframe, str] = {
    PnLTimeframe.DAILY: "day",
    PnLTimeframe.WEEKLY: "week",
    PnLTimeframe.MONTHLY: "month",
    PnLTimeframe.YEARLY: "year",
}

Q4 = Decimal("0.0001")


class VaultQueryError(ValueError):
    """Invalid filter/range input. Mapped to HTTP 400 by the router."""


# ---------- Decimal helpers ----------

def to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        return Decimal(int(value))
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return Decimal(0)
        d = Decimal(repr(value))
    else:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def money_out(value: Decimal) -> float:
    result = float(to_decimal(value).quantize(Q4, rounding=ROUND_HALF_EVEN))
    return result + 0.0  # normalizes IEEE -0.0 to 0.0


def pct_out(numerator: Decimal, denominator: Decimal) -> float:
    den = to_decimal(denominator)
    if den == 0:
        return 0.0
    return money_out(to_decimal(numerator) / den * Decimal(100))


# ---------- Time helpers ----------

def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_period(dt: datetime, timeframe: PnLTimeframe) -> datetime:
    """Python mirror of PostgreSQL date_trunc (ISO weeks start Monday)."""
    day = as_utc(dt).replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == PnLTimeframe.DAILY:
        return day
    if timeframe == PnLTimeframe.WEEKLY:
        return day - timedelta(days=day.weekday())
    if timeframe == PnLTimeframe.MONTHLY:
        return day.replace(day=1)
    return day.replace(month=1, day=1)


def advance_period(dt: datetime, timeframe: PnLTimeframe) -> datetime:
    if timeframe == PnLTimeframe.DAILY:
        return dt + timedelta(days=1)
    if timeframe == PnLTimeframe.WEEKLY:
        return dt + timedelta(days=7)
    if timeframe == PnLTimeframe.MONTHLY:
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1)
        return dt.replace(month=dt.month + 1)
    return dt.replace(year=dt.year + 1)


def build_timeline(start: datetime, end: datetime, timeframe: PnLTimeframe) -> list[datetime]:
    cursor = truncate_period(start, timeframe)
    last = truncate_period(end, timeframe)
    periods: list[datetime] = []
    while cursor <= last:
        periods.append(cursor)
        if len(periods) > MAX_TIMELINE_PERIODS:
            raise VaultQueryError(
                f"Requested range exceeds {MAX_TIMELINE_PERIODS} {timeframe.value.lower()} periods; narrow the dates"
            )
        cursor = advance_period(cursor, timeframe)
    return periods


def _period_expr(unit: str):
    return func.date_trunc(
        literal_column(f"'{unit}'"),
        func.timezone(literal_column("'UTC'"), BetLedger.resolved_at),
        type_=DateTime(),
    )


# ---------- Normalized filters ----------

@dataclass(frozen=True, slots=True)
class _Window:
    currency: str
    start: datetime | None
    end: datetime | None


def _normalize(params: VaultFilterParams) -> _Window:
    currency = (params.currency or "").strip().upper()
    if not CURRENCY_PATTERN.match(currency):
        raise VaultQueryError("currency must be 3-10 alphanumeric characters")
    start = as_utc(params.start_date) if params.start_date else None
    end = as_utc(params.end_date) if params.end_date else None
    if start and end and start > end:
        raise VaultQueryError("start_date must be on or before end_date")
    return _Window(currency=currency, start=start, end=end)


class VaultEngine:
    # ---------- Query scaffolding (RELATIONAL SECURITY) ----------

    @staticmethod
    def _scoped(stmt: Select, user_id: uuid.UUID, window: _Window) -> Select:
        """EVERY vault query goes through here: join ExchangeAccount + user filter + currency."""
        return (
            stmt.select_from(BetLedger)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(ExchangeAccount.user_id == user_id, BetLedger.currency == window.currency)
        )

    @staticmethod
    def _settled_in_window(stmt: Select, window: _Window, statuses: tuple[str, ...]) -> Select:
        stmt = stmt.where(BetLedger.status.in_(statuses), BetLedger.resolved_at.is_not(None))
        if window.start is not None:
            stmt = stmt.where(BetLedger.resolved_at >= window.start)
        if window.end is not None:
            stmt = stmt.where(BetLedger.resolved_at <= window.end)
        return stmt

    async def _settled_totals(
        self, db: AsyncSession, user_id: uuid.UUID, window: _Window
    ) -> tuple[Decimal, Decimal, Decimal, int]:
        stmt = select(
            func.coalesce(func.sum(POSITIVE_PROFIT_EXPR), SQL_ZERO).label("gross_profit"),
            func.coalesce(func.sum(NEGATIVE_PROFIT_EXPR), SQL_ZERO).label("gross_loss"),
            func.coalesce(func.sum(VOLUME_EXPR), SQL_ZERO).label("volume"),
            func.count(BetLedger.id).label("settled_count"),
        )
        stmt = self._settled_in_window(self._scoped(stmt, user_id, window), window, SETTLED_STATUSES)
        row = (await db.execute(stmt)).one()
        return (
            to_decimal(row.gross_profit),
            to_decimal(row.gross_loss),
            to_decimal(row.volume),
            int(row.settled_count or 0),
        )

    async def _active_totals(
        self, db: AsyncSession, user_id: uuid.UUID, window: _Window
    ) -> tuple[Decimal, int]:
        """Live exposure is point-in-time: date filters do not apply."""
        stmt = select(
            func.coalesce(func.sum(BetLedger.stake), SQL_ZERO).label("exposure"),
            func.count(BetLedger.id).label("active_count"),
        )
        stmt = self._scoped(stmt, user_id, window).where(BetLedger.status.in_(ACTIVE_STATUSES))
        row = (await db.execute(stmt)).one()
        return to_decimal(row.exposure), int(row.active_count or 0)

    # ---------- 1. Overview ----------

    async def get_overview(
        self, db: AsyncSession, user_id: uuid.UUID, params: VaultFilterParams, bankroll: float
    ) -> CapitalOverview:
        window = _normalize(params)
        starting = to_decimal(bankroll)

        gross_profit, gross_loss, volume, settled_count = await self._settled_totals(db, user_id, window)
        exposure, active_count = await self._active_totals(db, user_id, window)

        net = gross_profit - gross_loss
        available = starting + net - exposure

        porus.info(
            "Overview user=%s ccy=%s net=%s exposure=%s volume=%s",
            user_id, window.currency, net, exposure, volume,
        )
        return CapitalOverview(
            base_currency=window.currency,
            starting_bankroll=money_out(starting),
            total_exposure=money_out(exposure),
            available_capital=money_out(available),
            gross_profit=money_out(gross_profit),
            gross_loss=money_out(gross_loss),
            net_pnl=money_out(net),
            total_volume=money_out(volume),
            roi_pct=pct_out(net, volume),
            active_bets_count=active_count,
            settled_bets_count=settled_count,
        )

    # ---------- 2. P&L by timeframe ----------

    async def get_pnl_by_timeframe(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        params: VaultFilterParams,
        timeframe: PnLTimeframe,
    ) -> list[TimeframePnLNode]:
        window = _normalize(params)
        period_col = _period_expr(_TRUNC_UNITS[timeframe]).label("period")

        stmt = select(
            period_col,
            func.coalesce(func.sum(PROFIT_EXPR), SQL_ZERO).label("profit"),
            func.coalesce(func.sum(VOLUME_EXPR), SQL_ZERO).label("volume"),
            func.coalesce(func.sum(WON_COUNT_EXPR), 0).label("won"),
            func.coalesce(func.sum(LOST_COUNT_EXPR), 0).label("lost"),
        )
        stmt = self._settled_in_window(self._scoped(stmt, user_id, window), window, DECIDED_STATUSES)
        stmt = stmt.group_by(period_col).order_by(period_col)
        rows = (await db.execute(stmt)).all()

        by_period: dict[datetime, tuple[Decimal, Decimal, int, int]] = {}
        for row in rows:
            if row.period is None:
                continue
            key = truncate_period(row.period, timeframe)
            by_period[key] = (to_decimal(row.profit), to_decimal(row.volume), int(row.won or 0), int(row.lost or 0))

        if window.start is None and not by_period:
            return []
        start = window.start or min(by_period)
        end = window.end or (max(by_period) if by_period else utc_now())

        # PYTHON GAP-FILL
        nodes: list[TimeframePnLNode] = []
        for period in build_timeline(start, end, timeframe):
            profit, volume, won, lost = by_period.get(period, (Decimal(0), Decimal(0), 0, 0))
            nodes.append(
                TimeframePnLNode(
                    period=period,
                    profit=money_out(profit),
                    volume=money_out(volume),
                    yield_pct=pct_out(profit, volume),
                    bets_won=won,
                    bets_lost=lost,
                )
            )
        return nodes

    # ---------- 3. Waterfall ----------

    async def get_waterfall(
        self, db: AsyncSession, user_id: uuid.UUID, params: VaultFilterParams, bankroll: float
    ) -> list[WaterfallNode]:
        window = _normalize(params)
        starting = to_decimal(bankroll)
        gross_profit, gross_loss, _volume, _count = await self._settled_totals(db, user_id, window)
        current = starting + gross_profit - gross_loss

        return [
            WaterfallNode(category="Starting Capital", value=money_out(starting)),
            WaterfallNode(category="Gross Wins", value=money_out(gross_profit)),
            WaterfallNode(category="Gross Losses", value=money_out(-gross_loss)),
            WaterfallNode(category="Current Capital", value=money_out(current)),
        ]

    # ---------- 4. P&L by market ----------

    async def get_pnl_by_market(
        self, db: AsyncSession, user_id: uuid.UUID, params: VaultFilterParams
    ) -> list[MarketPnL]:
        window = _normalize(params)
        stmt = select(
            BetLedger.market_type.label("market_type"),
            func.coalesce(func.sum(PROFIT_EXPR), SQL_ZERO).label("net_profit"),
            func.coalesce(func.sum(VOLUME_EXPR), SQL_ZERO).label("volume"),
        )
        stmt = self._settled_in_window(self._scoped(stmt, user_id, window), window, DECIDED_STATUSES)
        stmt = stmt.group_by(BetLedger.market_type)
        rows = (await db.execute(stmt)).all()

        results = [
            MarketPnL(
                market_type=str(row.market_type),
                net_profit=money_out(to_decimal(row.net_profit)),
                volume=money_out(to_decimal(row.volume)),
                roi_pct=pct_out(to_decimal(row.net_profit), to_decimal(row.volume)),
            )
            for row in rows
        ]
        return sorted(results, key=lambda m: m.net_profit, reverse=True)

    # ---------- 5. P&L by bookmaker ----------

    async def get_pnl_by_bookmaker(
        self, db: AsyncSession, user_id: uuid.UUID, params: VaultFilterParams
    ) -> list[BookmakerPnL]:
        window = _normalize(params)
        stmt = select(
            ExchangeAccount.exchange_name.label("exchange"),
            func.coalesce(func.sum(PROFIT_EXPR), SQL_ZERO).label("net_profit"),
            func.coalesce(func.sum(VOLUME_EXPR), SQL_ZERO).label("volume"),
        )
        stmt = self._settled_in_window(self._scoped(stmt, user_id, window), window, DECIDED_STATUSES)
        stmt = stmt.group_by(ExchangeAccount.exchange_name)
        rows = (await db.execute(stmt)).all()

        results = [
            BookmakerPnL(
                exchange=str(row.exchange),
                net_profit=money_out(to_decimal(row.net_profit)),
                volume=money_out(to_decimal(row.volume)),
                roi_pct=pct_out(to_decimal(row.net_profit), to_decimal(row.volume)),
            )
            for row in rows
        ]
        return sorted(results, key=lambda b: b.net_profit, reverse=True)

    # ---------- 6. Growth trajectory ----------

    async def get_growth_trajectory(
        self, db: AsyncSession, user_id: uuid.UUID, params: VaultFilterParams, bankroll: float
    ) -> list[GrowthNode]:
        window = _normalize(params)
        day_col = _period_expr("day").label("day")

        stmt = select(
            day_col,
            func.coalesce(func.sum(PROFIT_EXPR), SQL_ZERO).label("actual_profit"),
            func.coalesce(func.sum(EXPECTED_PROFIT_EXPR), SQL_ZERO).label("expected_profit"),
        )
        stmt = self._settled_in_window(self._scoped(stmt, user_id, window), window, DECIDED_STATUSES)
        stmt = stmt.group_by(day_col).order_by(day_col)
        rows = (await db.execute(stmt)).all()

        by_day: dict[datetime, tuple[Decimal, Decimal]] = {}
        for row in rows:
            if row.day is None:
                continue
            key = truncate_period(row.day, PnLTimeframe.DAILY)
            by_day[key] = (to_decimal(row.actual_profit), to_decimal(row.expected_profit))

        if window.start is None and not by_day:
            return []
        start = window.start or min(by_day)
        end = window.end or utc_now()

        # PYTHON GAP-FILL with Decimal carry-over
        starting = to_decimal(bankroll)
        actual_bankroll = starting
        cumulative_ev = Decimal(0)
        nodes: list[GrowthNode] = []
        for day in build_timeline(start, end, PnLTimeframe.DAILY):
            actual_profit, expected_profit = by_day.get(day, (Decimal(0), Decimal(0)))
            actual_bankroll += actual_profit
            cumulative_ev += expected_profit
            nodes.append(
                GrowthNode(
                    timestamp=day,
                    actual_bankroll=money_out(actual_bankroll),
                    projected_bankroll=money_out(starting + cumulative_ev),
                    cumulative_ev=money_out(cumulative_ev),
                )
            )
        return nodes
