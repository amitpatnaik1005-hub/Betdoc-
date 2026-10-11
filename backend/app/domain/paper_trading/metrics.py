"""Forward-test performance of a paper account (Group 80). Developed for Amit Ashok Kumar Patnaik.

* Virtual balance: ``Balance_t = Balance_{t-1} - Stake + Payout_settled``.
* ROI: total net P&L over the initial balance, in percent.
* Win rate: ``W / (W + L)`` over decided bets only (a push or void dilutes nothing; a half win counts as a win and
  a half loss as a loss, a cash-out is neither).
* Maximum drawdown: the deepest fall of *equity* (balance plus open stakes at cost) from its running peak, in
  percent. Equity rather than the bare balance: placing a stake moves money into a bet, it is not a loss.
* Profit factor: gross profits over gross losses (None while nothing has lost).
* Sharpe ratio: daily equity returns in excess of the daily risk-free rate (6.5% a year, Indian sovereign bonds),
  over their standard deviation, annualised by sqrt(365).
* Significance: a one-sided t-test of the mean per-bet return (P&L / stake) against zero. The sandbox calls a
  forward-test proven only past ``min_sample`` decided bets with p under ``alpha``: KUMBHA's firewall reads it
  before real money is risked.
"""

from __future__ import annotations

import math
import statistics
from itertools import pairwise
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from scipy import stats as sps

TRADING_DAYS = 365
DEFAULT_RISK_FREE = 0.065


def win_rate_pct(won: int, lost: int) -> float:
    decided = won + lost
    return round(won / decided * 100.0, 2) if decided else 0.0


def roi_pct(total_pnl: float, initial_balance: float) -> float:
    return round(total_pnl / initial_balance * 100.0, 4) if initial_balance > 0 else 0.0


def max_drawdown_pct(equity: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return round(worst * 100.0, 4)


def profit_factor(gross_profits: float, gross_losses: float) -> float | None:
    return None if gross_losses <= 0 else round(gross_profits / gross_losses, 4)


def daily_closes(points: Sequence[tuple[date, float]]) -> list[float]:
    """The last equity of each day, in day order."""
    closes: dict[date, float] = {}
    for day, value in sorted(points, key=lambda p: p[0]):
        closes[day] = value
    return [closes[d] for d in sorted(closes)]


def sharpe_ratio(daily_equity: Sequence[float], risk_free_annual: float = DEFAULT_RISK_FREE) -> float | None:
    """Annualised Sharpe of the daily equity curve; None with fewer than two returns or no variation."""
    returns = [b / a - 1.0 for a, b in pairwise(daily_equity) if a > 0]
    if len(returns) < 2:
        return None
    rf_daily = (1.0 + risk_free_annual) ** (1.0 / TRADING_DAYS) - 1.0
    excess = [r - rf_daily for r in returns]
    sd = statistics.stdev(excess)
    if sd <= 1e-12:
        return None
    return round(statistics.fmean(excess) / sd * math.sqrt(TRADING_DAYS), 4)


@dataclass(frozen=True, slots=True)
class Significance:
    sample: int
    mean_return: float
    std_return: float
    t_stat: float | None
    p_value: float | None
    proven: bool
    verdict: str

    def as_dict(self) -> dict[str, object]:
        return {"sample": self.sample, "mean_return": round(self.mean_return, 6), "std_return": round(self.std_return, 6),
                "t_stat": None if self.t_stat is None else round(self.t_stat, 4), "p_value": None if self.p_value is None else round(self.p_value, 6),
                "proven": self.proven, "verdict": self.verdict}


def forward_test_significance(per_bet_returns: Sequence[float], *, min_sample: int = 30, alpha: float = 0.05) -> Significance:
    n = len(per_bet_returns)
    if n < 2:
        return Significance(n, per_bet_returns[0] if n else 0.0, 0.0, None, None, False, f"{n} decided bet{'s' if n != 1 else ''}: no sample yet")
    mean = statistics.fmean(per_bet_returns)
    sd = statistics.stdev(per_bet_returns)
    if sd <= 1e-12:
        t, p = None, None
    else:
        t = mean / (sd / math.sqrt(n))
        p = float(sps.t.sf(t, df=n - 1))  # one-sided: is the mean return above zero?
    proven = n >= min_sample and p is not None and p < alpha and mean > 0
    if n < min_sample:
        verdict = f"{n} of {min_sample} decided bets: building the sample"
    elif proven:
        verdict = f"mean return {mean:+.2%} per bet, p = {p:.4f}: the edge is statistically significant"
    elif mean > 0:
        verdict = f"mean return {mean:+.2%} per bet, p = {p if p is not None else float('nan'):.4f}: positive, not yet significant"
    else:
        verdict = f"mean return {mean:+.2%} per bet: no edge shown"
    return Significance(n, mean, sd, t, p, proven, verdict)


def status_summary(total_pnl: float, decided: int, significance: Significance, *, min_sample: int = 30) -> str:
    if decided < min_sample:
        return "BUILDING STATISTICAL SAMPLE"
    if significance.proven:
        return "PROFITABLE FORWARD-TEST"
    if total_pnl > 0:
        return "PROFITABLE, NOT YET SIGNIFICANT"
    return "CALIBRATING ADVICE"


def brier_score(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Mean squared error of (forecast probability, outcome 0/1) pairs: Drona's calibration read on paper outcomes."""
    if not pairs:
        return None
    return round(statistics.fmean((p - o) ** 2 for p, o in pairs), 6)
