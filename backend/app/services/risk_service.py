import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.risk import (
    ACTIVE_STATUSES,
    SETTLED_STATUSES,
    bet_profit,
    calculate_current_drawdown,
    calculate_cvar,
    calculate_exposure,
    calculate_historical_var,
    calculate_max_drawdown,
    calculate_parametric_var,
    calmar_ratio,
    evt_tail_risk,
    omega_ratio,
    portfolio_kelly,
    safe_float,
    sharpe_ratio,
    sortino_ratio,
)
from app.models import BetLedger, ExchangeAccount
from app.schemas.risk import RiskMetrics

kumbha = logging.getLogger("betdoc.kumbha")

MIN_HISTORICAL_VAR_SAMPLE = 30

SettledRow = tuple[str, float, datetime | None]   # (status, profit, timestamp)
ActiveRow = tuple[float, float, float]            # (stake, odds, true_probability)


def _annualized_return(profits: list[float], timestamps: list[datetime], bankroll: float) -> float:
    """Simple (linear) annualization of total return on bankroll. Span floored at 1 day."""
    if not profits or bankroll <= 0.0:
        return 0.0
    stamps = [t for t in timestamps if t is not None]
    if len(stamps) < 2:
        span_days = 1.0
    else:
        span_days = max((max(stamps) - min(stamps)).total_seconds() / 86400.0, 1.0)
    total_return = sum(profits) / bankroll
    return safe_float(total_return * (365.0 / span_days))


def _compute_metrics(settled: list[SettledRow], active: list[ActiveRow], bankroll: float) -> RiskMetrics:
    profits = [p for _, p, _ in settled]
    timestamps = [t for _, _, t in settled if t is not None]

    wins = sum(1 for s, _, _ in settled if s == "WON")
    losses = sum(1 for s, _, _ in settled if s == "LOST")
    decided = wins + losses
    win_rate = wins / decided if decided > 0 else 0.0

    equity_curve = [bankroll]
    for p in profits:
        equity_curve.append(equity_curve[-1] + p)

    if len(profits) >= MIN_HISTORICAL_VAR_SAMPLE:
        var_95 = calculate_historical_var(profits, 0.95)
        var_99 = calculate_historical_var(profits, 0.99)
    else:
        var_95 = calculate_parametric_var(profits, 0.95)
        var_99 = calculate_parametric_var(profits, 0.99)

    cvar_95 = calculate_cvar(profits, 0.95)
    cvar_99 = calculate_cvar(profits, 0.99)

    max_dd = calculate_max_drawdown(equity_curve)
    current_dd = calculate_current_drawdown(equity_curve)

    sharpe = sharpe_ratio(profits)
    sortino = sortino_ratio(profits)
    omega = omega_ratio(profits)
    calmar = calmar_ratio(_annualized_return(profits, timestamps, bankroll), max_dd)

    total_exposure, exposure_pct = calculate_exposure([stake for stake, _, _ in active], bankroll)

    edges = [prob * odds - 1.0 for _, odds, prob in active]
    kelly_fractions = portfolio_kelly(edges, [odds for _, odds, _ in active])
    kelly_total = sum(kelly_fractions)

    tail = evt_tail_risk([-p for p in profits if p < 0.0])
    kumbha.debug("EVT tail diagnostics: %s", tail)

    return RiskMetrics(
        var_95=safe_float(var_95),
        var_99=safe_float(var_99),
        cvar_95=safe_float(cvar_95),
        cvar_99=safe_float(cvar_99),
        max_drawdown=safe_float(max_dd),
        current_drawdown=safe_float(current_dd),
        sharpe_ratio=safe_float(sharpe),
        sortino_ratio=safe_float(sortino),
        calmar_ratio=safe_float(calmar),
        omega_ratio=safe_float(omega),
        total_exposure=safe_float(total_exposure),
        exposure_pct=safe_float(exposure_pct),
        kelly_portfolio_fraction=safe_float(kelly_total),
        total_bets=int(len(settled)),
        win_rate=safe_float(win_rate),
    )


class RiskService:
    async def compute_metrics(self, db: AsyncSession, user_id: uuid.UUID, bankroll: float) -> RiskMetrics:
        ts_col = func.coalesce(BetLedger.resolved_at, BetLedger.placed_at).label("ts")

        settled_stmt = (
            select(BetLedger.status, BetLedger.stake, BetLedger.payout, ts_col)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(
                ExchangeAccount.user_id == user_id,
                BetLedger.status.in_(SETTLED_STATUSES),
            )
            .order_by(ts_col.asc(), BetLedger.id.asc())
        )
        active_stmt = (
            select(BetLedger.stake, BetLedger.odds, BetLedger.true_probability)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(
                ExchangeAccount.user_id == user_id,
                BetLedger.status.in_(ACTIVE_STATUSES),
            )
        )

        # One AsyncSession cannot run concurrent queries: execute sequentially.
        settled_rows = (await db.execute(settled_stmt)).all()
        active_rows = (await db.execute(active_stmt)).all()

        settled: list[SettledRow] = [
            (str(r.status).upper(), bet_profit(r.status, r.stake, r.payout), r.ts)
            for r in settled_rows
        ]
        active: list[ActiveRow] = [
            (safe_float(r.stake), safe_float(r.odds), safe_float(r.true_probability))
            for r in active_rows
        ]

        metrics = await asyncio.to_thread(_compute_metrics, settled, active, safe_float(bankroll))
        kumbha.info(
            "Risk metrics for user=%s: bets=%d exposure=%.2f var95=%.2f max_dd=%.4f",
            user_id, metrics.total_bets, metrics.total_exposure, metrics.var_95, metrics.max_drawdown,
        )
        return metrics
