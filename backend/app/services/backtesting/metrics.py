"""Quant metrics for one backtest run.

* ROI: net P&L over the money staked on graded bets (a void returns its stake and is not turnover).
* Return: net P&L over the starting capital.
* Maximum drawdown: the deepest peak-to-trough fall of the equity curve (marked at every settlement),
  in rupees and as a share of the peak; with how long the fall lasted.
* Sharpe and Sortino: on daily returns of end-of-day equity across the whole window (flat days
  included: an idle bankroll earns nothing), annualised by sqrt(365) (football is played every day
  of the week); Sortino's downside deviation is against a 0% target.
* Calmar: the annualised (compound) return over the maximum drawdown.
* MAE (maximum adverse excursion): for each fill, the worst mark-to-market before kick-off, at its own
  book: a back bet struck at ``O`` with the price now at ``P`` is worth ``O / P - 1`` of its stake
  if hedged out; the MAE is the lowest such value (0 if it never went against the bet).
* CLV beat %: the share of fills struck at better odds than the same book's closing price (its last
  quote before kick-off), the standard test of whether a strategy is ahead of the market.

Metrics read the tape after the run is over (the clock is past every row they use).
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import fmean
from typing import Any

from app.services.backtesting.replay_engine import HistoricalStore
from app.services.backtesting.simulator import Position, RunResult

ANNUALISE = math.sqrt(365.0)
CURVE_POINTS = 400


def _f(value: Decimal | float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def daily_equity(curve: Sequence[tuple[datetime, Decimal]], start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """End-of-day equity for every calendar day of the window, carried forward between settlements."""
    if not curve:
        return []
    times = [at for at, _ in curve]
    days: list[tuple[datetime, float]] = []
    day = datetime.combine(start.date(), datetime.min.time(), tzinfo=start.tzinfo) + timedelta(days=1)
    last = max(end, times[-1])
    while day <= last + timedelta(days=1):
        i = bisect.bisect_right(times, day) - 1
        days.append((day, float(curve[max(i, 0)][1])))
        day += timedelta(days=1)
    return days


def drawdown(curve: Sequence[tuple[datetime, Decimal]]) -> dict[str, Any]:
    peak, peak_at = None, None
    worst_abs, worst_pct, worst_from, worst_to = 0.0, 0.0, None, None
    for at, equity in curve:
        value = float(equity)
        if peak is None or value > peak:
            peak, peak_at = value, at
        fall = peak - value
        pct = fall / peak if peak > 0 else 0.0
        if pct > worst_pct:
            worst_abs, worst_pct, worst_from, worst_to = fall, pct, peak_at, at
    duration = (worst_to - worst_from).total_seconds() / 86_400 if worst_from and worst_to else 0.0
    return {"max_drawdown_inr": round(worst_abs, 2), "max_drawdown_pct": round(worst_pct * 100, 4), "drawdown_from": worst_from.isoformat() if worst_from else None,
            "drawdown_to": worst_to.isoformat() if worst_to else None, "drawdown_days": round(duration, 2)}


def ratios(daily: Sequence[tuple[datetime, float]], capital: float) -> dict[str, float | None]:
    values = [capital, *(v for _, v in daily)]
    returns = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(returns) < 2:
        return {"sharpe": None, "sortino": None, "volatility_pct": None}
    mean = fmean(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    return {
        "sharpe": round(mean / sd * ANNUALISE, 4) if sd > 0 else None,
        "sortino": round(mean / downside * ANNUALISE, 4) if downside > 0 else None,
        "volatility_pct": round(sd * ANNUALISE * 100, 4),
    }


def closing_odds(store: HistoricalStore, pos: Position) -> Decimal | None:
    fixture = store.fixtures.get(pos.fixture_id)
    if fixture is None:
        return None
    times, rows = store.series(pos.fixture_id, pos.market, pos.bookmaker_id, pos.selection)
    i = bisect.bisect_left(times, fixture.commence_time) - 1
    while i >= 0 and rows[i].suspended:
        i -= 1
    return rows[i].odds if i >= 0 else None


def adverse_excursion(store: HistoricalStore, pos: Position) -> float:
    """The worst hedge-out value of the fill before kick-off, as a % of its stake (<= 0)."""
    fixture = store.fixtures.get(pos.fixture_id)
    if fixture is None:
        return 0.0
    times, rows = store.series(pos.fixture_id, pos.market, pos.bookmaker_id, pos.selection)
    lo, hi = bisect.bisect_right(times, pos.filled_at), bisect.bisect_left(times, fixture.commence_time)
    worst = 0.0
    for row in rows[lo:hi]:
        if not row.suspended:
            worst = min(worst, (float(pos.fill_odds) / float(row.odds) - 1) * 100)
    return worst


def _downsample(points: Sequence[dict[str, Any]], limit: int = CURVE_POINTS) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    picked = {round(i * step) for i in range(limit)}
    return [p for i, p in enumerate(points) if i in picked]


def curves(run: RunResult) -> dict[str, list[dict[str, Any]]]:
    equity, underwater, peak = [], [], None
    for at, value in run.curve:
        v = float(value)
        peak = v if peak is None else max(peak, v)
        equity.append({"t": at.isoformat(), "equity": round(v, 2)})
        underwater.append({"t": at.isoformat(), "drawdown_pct": round((v - peak) / peak * 100 if peak else 0.0, 4)})
    return {"equity": _downsample(equity), "underwater": _downsample(underwater)}


def compute_metrics(run: RunResult, store: HistoricalStore) -> dict[str, Any]:
    settled = run.settled
    graded = [p for p in settled if p.status in ("WON", "LOST")]
    voids = [p for p in settled if p.status == "VOID"]
    staked = sum((p.cost_inr for p in graded), Decimal(0))
    pnl = sum((p.pnl_inr or Decimal(0) for p in settled), Decimal(0))
    capital = float(run.capital)
    final = float(run.curve[-1][1]) if run.curve else capital
    span_end = max(run.end, run.curve[-1][0]) if run.curve else run.end
    years = max((span_end - run.start).total_seconds() / (365 * 86_400), 1 / 365)
    cagr = (final / capital) ** (1 / years) - 1 if capital > 0 and final > 0 else -1.0
    dd = drawdown(run.curve)
    daily = daily_equity(run.curve, run.start, run.end)
    ratio = ratios(daily, capital)
    mdd = dd["max_drawdown_pct"] / 100
    clv_beats, clvs, maes = 0, [], []
    for pos in graded + voids:
        close = closing_odds(store, pos)
        if close is not None and close > 0:
            clvs.append(float(pos.fill_odds / close - 1) * 100)
            clv_beats += pos.fill_odds > close
        maes.append(adverse_excursion(store, pos))
    wins = [p for p in graded if p.status == "WON"]
    return {
        "trades": len(graded),
        "voids": len(voids),
        "voids_injected": sum(1 for p in voids if p.void_reason == "INJECTED"),
        "open_at_end": sum(1 for p in run.positions if p.status == "OPEN_AT_END"),
        "wins": len(wins),
        "win_rate_pct": _f(len(wins) / len(graded) * 100 if graded else None, 2),
        "staked_inr": _f(staked, 2),
        "pnl_inr": _f(pnl, 2),
        "roi_pct": _f(pnl / staked * 100 if staked else None, 4),
        "return_pct": _f((final - capital) / capital * 100 if capital else None, 4),
        "cagr_pct": _f(cagr * 100, 4),
        "starting_capital_inr": _f(capital, 2),
        "final_equity_inr": _f(final, 2),
        **dd,
        **ratio,
        "calmar": _f(cagr / mdd if mdd > 0 else None, 4),
        "mae_avg_pct": _f(fmean(maes) if maes else None, 4),
        "mae_worst_pct": _f(min(maes) if maes else None, 4),
        "clv_beat_pct": _f(clv_beats / len(clvs) * 100 if clvs else None, 2),
        "clv_avg_pct": _f(fmean(clvs) if clvs else None, 4),
        "avg_odds": _f(fmean(float(p.fill_odds) for p in graded) if graded else None, 4),
        "avg_stake_inr": _f(staked / len(graded) if graded else None, 2),
        "commission_paid_inr": _f(sum((p.commission_inr for p in graded), Decimal(0)), 2),
    }


def penalties(run: RunResult) -> dict[str, Any]:
    fills = [p for p in run.positions]
    latencies = [p.latency_ms for p in fills]
    decisions: Counter[str] = run.decisions
    return {
        "signals": run.signals,
        "orders_filled": len(fills),
        "orders_rejected": sum(run.rejections.values()),
        "rejections": dict(run.rejections.most_common()),
        "latency_ms_avg": _f(fmean(latencies) if latencies else None, 1),
        "latency_ms_max": max(latencies) if latencies else None,
        "filled_worse_after_latency": decisions.get("FILLED_WORSE_AFTER_LATENCY", 0),
        "latency_edge_decay_rejections": run.rejections.get("LATENCY_EDGE_DECAY", 0),
        "throttled": run.rejections.get("OUTBOUND_THROTTLED", 0),
        "queued_behind_rate_limit": run.queued_orders,
        "queue_seconds_total": round(run.queue_seconds, 3),
        "partial_fills": sum(v for k, v in decisions.items() if k.startswith("PARTIAL:")),
        "impact_avg_pct": _f(fmean(float(p.impact_pct) for p in fills) if fills else None, 4),
        "impact_max_pct": _f(max((float(p.impact_pct) for p in fills), default=None), 4),
        "participation_max_pct": _f(max((float(p.participation) * 100 for p in fills), default=None), 4),
        "fx": run.fx,
        "fx_fallback_conversions": sum(n for per in run.fx.values() for source, n in per.items() if source == "static"),
        "decisions": dict(decisions.most_common()),
    }


def trade_rows(run: RunResult, store: HistoricalStore, names: dict[str, str], limit: int = 500) -> list[dict[str, Any]]:
    rows = []
    for pos in sorted(run.positions, key=lambda p: p.decided_at)[-limit:]:
        close = closing_odds(store, pos)
        fixture = store.fixtures.get(pos.fixture_id)
        rows.append({
            "bot": names.get(str(pos.bot_id), str(pos.bot_id)), "fixture": f"{fixture.home_team} v {fixture.away_team}" if fixture else pos.fixture_id,
            "market": pos.market, "selection": pos.selection, "bookmaker": pos.bookmaker_id, "currency": pos.currency,
            "decided_at": pos.decided_at.isoformat(), "filled_at": pos.filled_at.isoformat(), "latency_ms": pos.latency_ms, "queue_ms": pos.queue_ms,
            "requested_odds": str(pos.requested_odds), "arrival_odds": str(pos.arrival_odds), "fill_odds": str(pos.fill_odds), "closing_odds": str(close) if close else None,
            "stake_inr": str(pos.cost_inr), "stake_ccy": str(pos.stake_ccy), "commission": str(pos.commission), "commission_inr": str(pos.commission_inr),
            "impact_pct": str(pos.impact_pct), "fill": pos.fill_reason, "status": pos.status, "void_reason": pos.void_reason,
            "pnl_inr": str(pos.pnl_inr) if pos.pnl_inr is not None else None, "fx_bet": str(pos.fx_bet), "fx_settle": str(pos.fx_settle) if pos.fx_settle else None,
            "fx_source": pos.fx_bet_source, "ev_decision": str(pos.ev_decision.quantize(Decimal("0.0001"))), "mae_pct": round(adverse_excursion(store, pos), 4),
        })
    return rows
