"""Closing Line Value (CLV): the difference between the odds you got and the
odds available right before the market closed. Beating the closing line
consistently is the standard proxy for "does this bettor/model actually have
an edge", because the closing line is generally the most efficient (sharpest)
price available.

The mistake most bettors and even semi-serious tools make: looking at raw
average CLV and concluding "positive average = I have an edge." At normal bet
volumes, variance in CLV is large enough that a positive average can easily
be noise. This module runs a one-sample t-test against the null hypothesis
"true mean CLV = 0" so you get a confidence level, not just a point estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class ClvSignificanceResult:
    n_bets: int
    mean_clv_pct: float
    std_clv_pct: float
    t_statistic: float
    p_value: float
    is_significant_at_95: bool
    interpretation: str


def calculate_clv_pct(bet_decimal_odds: float, closing_decimal_odds: float) -> float:
    """CLV as a percentage, comparing IMPLIED PROBABILITY (not raw decimal
    odds) so the number is comparable across odds levels. Positive means you
    got a better number than the closing line (good); negative means the
    line moved against you."""
    bet_implied = 1.0 / bet_decimal_odds
    close_implied = 1.0 / closing_decimal_odds
    # If the closing implied probability is LOWER than your bet's implied
    # probability, you got worse odds than the close -> negative CLV.
    return (bet_implied - close_implied) / close_implied * -1.0


def test_clv_significance(clv_pct_values: list[float]) -> ClvSignificanceResult:
    """One-sample t-test: is mean CLV significantly different from zero?

    NOTE: this assumes bets are roughly independent, which breaks down if
    many bets are on correlated events (e.g. every leg of the same game) —
    filter to one representative bet per correlated cluster before running
    this, or the effective sample size is smaller than n_bets suggests.
    """
    values = np.array(clv_pct_values)
    n = len(values)

    if n < 2:
        return ClvSignificanceResult(
            n_bets=n,
            mean_clv_pct=float(values.mean()) if n else 0.0,
            std_clv_pct=0.0,
            t_statistic=0.0,
            p_value=1.0,
            is_significant_at_95=False,
            interpretation="Not enough bets to test significance (need at least 2, "
            "want 100+ for a meaningful read).",
        )

    t_stat, p_value = stats.ttest_1samp(values, popmean=0.0)
    mean_clv = float(values.mean())
    is_sig = bool(p_value < 0.05 and n >= 100)  # require a real sample size too

    if n < 100:
        interpretation = (
            f"n={n} is too small to draw conclusions regardless of p-value "
            f"({p_value:.3f}). Wait for at least 100 bets."
        )
    elif is_sig and mean_clv > 0:
        interpretation = (
            f"Statistically significant positive CLV (p={p_value:.4f}) — evidence of genuine edge."
        )
    elif is_sig and mean_clv < 0:
        interpretation = f"Statistically significant negative CLV (p={p_value:.4f}) — the model or process is losing to the market."
    else:
        interpretation = f"No statistically significant edge detected yet (p={p_value:.4f})."

    return ClvSignificanceResult(
        n_bets=n,
        mean_clv_pct=round(mean_clv, 4),
        std_clv_pct=round(float(values.std(ddof=1)), 4),
        t_statistic=round(float(t_stat), 4),
        p_value=round(float(p_value), 4),
        is_significant_at_95=is_sig,
        interpretation=interpretation,
    )
