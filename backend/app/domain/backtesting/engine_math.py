"""Backtest mathematics added in Group 77 to the Lab's engine (Group 66): rolling walk-forward folds, the
square-root impact model, tail risk, and the models' Brier skill.

* **Rolling walk-forward.** ``K`` folds over ``[start, end]``, each an in-sample window of ``r W`` followed by an
  out-of-sample window of ``(1 - r) W``; fold ``k`` starts ``k (1 - r) W`` after the first, so the out-of-sample
  windows tile the tail of the span without overlapping. ``W = (end - start) / (r + K (1 - r))``. Every fold is
  tuned on its own in-sample window only and judged on the window after it: no fold sees its own future.
* **Square-root impact.** Taking a share ``x`` of the money at a price leaves ``1 - k sqrt(x)`` of its net-of-one
  price (the winnings), never below 0. The largest share an order may take and keep its price above ``floor``
  is ``((1 - (floor - 1) / (price - 1)) / k)^2``.
* **Tail risk.** Value-at-Risk at ``q`` is the loss the worst ``1 - q`` of outcomes reach (the ``1 - q``
  quantile of P&L, negated); CVaR is their mean loss.
* **Brier skill.** ``BS = mean (p - y)^2`` of the model's probability at each fill, against the closing line's
  de-vigged probability on the same fills: ``BSS = 1 - BS_model / BS_close``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

import numpy as np

ONE = Decimal(1)
ZERO = Decimal(0)


# ================================================================ rolling folds
@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    start: datetime
    split: datetime  # the in-sample window ends, the out-of-sample one starts
    end: datetime

    def as_dict(self) -> dict[str, object]:
        return {"fold": self.index, "in_sample_window": [self.start.isoformat(), self.split.isoformat()], "out_of_sample_window": [self.split.isoformat(), self.end.isoformat()]}


def rolling_folds(start: datetime, end: datetime, folds: int, train_ratio: float) -> list[Fold]:
    if folds < 1:
        raise ValueError("at least one fold")
    if not 0.0 < train_ratio < 1.0:
        raise ValueError("the in-sample share is in (0, 1)")
    if end <= start:
        raise ValueError("the window must end after it starts")
    width = (end - start) / (train_ratio + folds * (1.0 - train_ratio))
    step = width * (1.0 - train_ratio)
    out = []
    for k in range(folds):
        fold_start = start + step * k
        split = fold_start + width * train_ratio
        fold_end = end if k == folds - 1 else split + step
        out.append(Fold(k + 1, fold_start.replace(microsecond=0), split.replace(microsecond=0), fold_end.replace(microsecond=0)))
    return out


def walk_forward_summary(rows: Sequence[dict[str, object]], *, robust_label: str = "ROBUST") -> dict[str, object]:
    """Across folds: how many held up, the out-of-sample Sharpe spread, and walk-forward efficiency (the mean
    out-of-sample return over the mean in-sample return: near 1, the edge survives; near 0 or below, it was fitted)."""
    oos = [r for r in rows if isinstance(r.get("out_of_sample"), dict)]
    sharpes = [float(r["out_of_sample"]["sharpe"]) for r in oos if r["out_of_sample"].get("sharpe") is not None]  # type: ignore[index]
    is_returns = [float(r["in_sample"]["return_pct"]) for r in oos if isinstance(r.get("in_sample"), dict) and r["in_sample"].get("return_pct") is not None]  # type: ignore[index]
    oos_returns = [float(r["out_of_sample"]["return_pct"]) for r in oos if r["out_of_sample"].get("return_pct") is not None]  # type: ignore[index]
    mean_is = sum(is_returns) / len(is_returns) if is_returns else None
    mean_oos = sum(oos_returns) / len(oos_returns) if oos_returns else None
    return {
        "folds": len(rows),
        "robust_folds": sum(1 for r in rows if (r.get("verdict") or {}).get("verdict") == robust_label),  # type: ignore[union-attr]
        "oos_sharpe_mean": None if not sharpes else round(sum(sharpes) / len(sharpes), 4),
        "oos_sharpe_min": None if not sharpes else round(min(sharpes), 4),
        "oos_return_pct_mean": None if mean_oos is None else round(mean_oos, 4),
        "walk_forward_efficiency": None if mean_is is None or mean_oos is None or mean_is <= 0 else round(mean_oos / mean_is, 4),
    }


# ================================================================ square-root impact
def sqrt_impact_multiplier(participation: Decimal, coefficient: Decimal) -> Decimal:
    """What is left of the net-of-one price after taking ``participation`` of the money at it: 1 - k sqrt(x)."""
    if participation <= 0:
        return ONE
    return max(ZERO, ONE - coefficient * participation.sqrt())


def sqrt_max_participation(price: Decimal, floor: Decimal, coefficient: Decimal) -> Decimal:
    """The largest share of the money an order may take and keep its impacted price at or above ``floor``."""
    if price <= floor:
        return ZERO
    if coefficient == 0:
        return ONE
    room = ONE - (floor - ONE) / (price - ONE)
    return min(ONE, ((room / coefficient) ** 2).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))


def executed_price(quote: Decimal, stake: Decimal, liquidity: Decimal, rate: Decimal) -> Decimal:
    """The brief's form on the net-of-one price: 1 + (quote - 1)(1 - rate sqrt(stake / liquidity))."""
    if liquidity <= 0:
        raise ValueError("liquidity must be positive")
    return ONE + (quote - ONE) * sqrt_impact_multiplier(stake / liquidity, rate)


# ================================================================ tail risk
def var_cvar(pnl: Sequence[float] | np.ndarray, levels: Sequence[float] = (0.95, 0.99)) -> dict[str, dict[str, float]]:
    """``{"95": {"var": .., "cvar": ..}, ...}``: losses as positive numbers (0 when even the tail makes money)."""
    values = np.asarray(pnl, dtype=np.float64)
    if values.size == 0:
        return {f"{round(q * 100):d}": {"var": 0.0, "cvar": 0.0} for q in levels}
    out = {}
    for q in levels:
        cut = float(np.quantile(values, 1.0 - q))
        tail = values[values <= cut]
        out[f"{round(q * 100):d}"] = {"var": round(max(0.0, -cut), 2), "cvar": round(max(0.0, -float(tail.mean())), 2)}
    return out


# ================================================================ Brier skill
def brier(pairs: Sequence[tuple[float, float]]) -> float | None:
    return None if not pairs else sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def brier_skill(model: Sequence[tuple[float, float]], reference: Sequence[tuple[float, float]]) -> float | None:
    """1 - BS_model / BS_reference over the same outcomes; None without both, or with a perfect reference."""
    bm, br = brier(model), brier(reference)
    if bm is None or br is None or br <= 0:
        return None
    return 1.0 - bm / br


def implied_probability(odds: Decimal, commission: Decimal, ev: Decimal) -> float:
    """The probability a decision priced: EV = p x net - 1 with net = 1 + (odds - 1)(1 - commission)."""
    net = ONE + (odds - ONE) * (ONE - commission)
    return min(1.0, max(0.0, float((ONE + ev) / net)))
