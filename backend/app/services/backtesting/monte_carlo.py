"""Monte Carlo resampling of a finished backtest's trades: the risk of ruin.

The run's settled trades, in rupees, are the same trades in every resample; only their order (or
their draw) changes. A backtest shows one sequence; luck decides the sequence.

* Permutation (the primary measure): the exact trades, reshuffled ``iterations`` times. The final
  equity never changes (the sum is the sum), the path does. The risk of ruin is the share of
  orderings in which equity touches the ruin floor (``ruin_floor_pct`` of the starting capital: 0%,
  bankrupt, by default). The drawdown distribution says how deep the same edge could have dug.
* Bootstrap (the second): trades drawn with replacement, so the final equity varies too: how likely
  the same edge, re-dealt, ends below where it started.

Deterministic for a seed; numpy, vectorised over the iterations.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

FAN_POINTS = 80


def _paths(trades: np.ndarray, capital: float) -> np.ndarray:
    return capital + np.cumsum(trades, axis=1)


def _max_drawdowns(paths: np.ndarray, capital: float) -> np.ndarray:
    start = np.full((paths.shape[0], 1), capital)
    full = np.concatenate([start, paths], axis=1)
    peaks = np.maximum.accumulate(full, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        falls = np.where(peaks > 0, (peaks - full) / peaks, 1.0)
    return falls.max(axis=1)


def risk_of_ruin(pnls: Sequence[float], capital: float, *, iterations: int = 1000, ruin_floor_pct: float = 0.0, seed: int = 66) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("at least one iteration")
    if capital <= 0:
        raise ValueError("a positive starting capital")
    if not 0 <= ruin_floor_pct < 100:
        raise ValueError("the ruin floor is a share of the starting capital in [0, 100)")
    floor = capital * ruin_floor_pct / 100
    n = len(pnls)
    base: dict[str, Any] = {"method": "permutation", "iterations": iterations, "trades": n, "starting_capital_inr": round(capital, 2), "ruin_floor_inr": round(floor, 2), "ruin_floor_pct": ruin_floor_pct}
    if n == 0:
        return {**base, "risk_of_ruin_pct": 0.0, "p_drawdown_50_pct": 0.0, "max_drawdown_pct": {"p50": 0.0, "p95": 0.0, "p99": 0.0}, "observed_max_drawdown_pct": 0.0,
                "final_equity_inr": round(capital, 2), "bootstrap": {"risk_of_ruin_pct": 0.0, "p_loss_pct": 0.0, "final_equity_inr": {"p5": capital, "p50": capital, "p95": capital}}, "fan": []}
    rng = np.random.default_rng(seed)
    trades = np.asarray(pnls, dtype=np.float64)
    shuffled = rng.permuted(np.tile(trades, (iterations, 1)), axis=1)
    paths = _paths(shuffled, capital)
    ruined = (np.minimum(paths.min(axis=1), capital) <= floor).mean()
    dds = _max_drawdowns(paths, capital)
    observed = float(_max_drawdowns(_paths(trades[None, :], capital), capital)[0])

    drawn = rng.choice(trades, size=(iterations, n), replace=True)
    boot = _paths(drawn, capital)
    finals = boot[:, -1]

    index = np.unique(np.linspace(0, n - 1, num=min(FAN_POINTS, n)).round().astype(int))
    p5, p50, p95 = np.percentile(paths[:, index], [5, 50, 95], axis=0)
    fan = [{"trade": int(i) + 1, "p5": round(float(a), 2), "p50": round(float(b), 2), "p95": round(float(c), 2)} for i, a, b, c in zip(index, p5, p50, p95, strict=True)]
    return {
        **base,
        "risk_of_ruin_pct": round(float(ruined) * 100, 4),
        "p_drawdown_50_pct": round(float((dds >= 0.5).mean()) * 100, 4),
        "max_drawdown_pct": {q: round(float(np.percentile(dds, p)) * 100, 4) for q, p in (("p50", 50), ("p95", 95), ("p99", 99))},
        "observed_max_drawdown_pct": round(observed * 100, 4),
        "observed_drawdown_percentile": round(float((dds <= observed + 1e-12).mean()) * 100, 2),
        "final_equity_inr": round(float(paths[0, -1]), 2),
        "bootstrap": {
            "risk_of_ruin_pct": round(float((np.minimum(boot.min(axis=1), capital) <= floor).mean()) * 100, 4),
            "p_loss_pct": round(float((finals < capital).mean()) * 100, 4),
            "final_equity_inr": {q: round(float(np.percentile(finals, p)), 2) for q, p in (("p5", 5), ("p50", 50), ("p95", 95))},
        },
        "fan": fan,
    }
