"""Closing Line Value (CLV) Beat-the-Market Tracker (Group 116).
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Measures whether a bet placed on Parimatch or 1xBet secured superior odds compared to
the sharp closing market (Pinnacle or Betfair Exchange) recorded at kickoff time:

    CLV Edge = (Placed Odds / Closing Odds) - 1.0

A positive CLV Edge (> 0%) mathematically proves long-term positive expectancy and
market-beating edge regardless of individual event variance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


def calculate_clv_edge(
    placed_odds: Decimal | float,
    closing_odds: Decimal | float,
) -> dict[str, Any]:
    """Calculate Closing Line Value edge against the sharp bookmaker closing line.

    Args:
        placed_odds: Decimal odds at the time of user bet placement.
        closing_odds: Decimal closing odds from sharp market at kickoff.

    Returns:
        Structured evaluation containing CLV edge percentage, market beat verdict, and rationale.
    """
    p_odds = Decimal(str(placed_odds))
    c_odds = Decimal(str(closing_odds))

    if c_odds <= Decimal("1.0"):
        raise ValueError("Closing odds must be strictly greater than 1.0")
    if p_odds <= Decimal("1.0"):
        raise ValueError("Placed odds must be strictly greater than 1.0")

    clv_edge = (p_odds / c_odds) - Decimal("1.0")
    clv_edge_pct = float(clv_edge * Decimal("100.0"))
    beat_market = clv_edge > Decimal("0.0")

    if beat_market:
        rationale = (
            f"Beat the market by +{clv_edge_pct:.2f}%! Placed at {p_odds:.2f} "
            f"before sharp closing contraction to {c_odds:.2f}."
        )
    elif clv_edge == Decimal("0.0"):
        rationale = f"Fair value lock: Placed odds {p_odds:.2f} exactly matched closing price {c_odds:.2f}."
    else:
        rationale = (
            f"Negative CLV ({clv_edge_pct:.2f}%): Placed at {p_odds:.2f} while "
            f"sharp market drifted to {c_odds:.2f} at kickoff."
        )

    return {
        "placed_odds": float(p_odds),
        "closing_odds": float(c_odds),
        "clv_edge": float(clv_edge),
        "clv_edge_pct": round(clv_edge_pct, 2),
        "beat_the_market": beat_market,
        "rationale": rationale,
        "architect": DEVELOPER_CREDIT,
    }
