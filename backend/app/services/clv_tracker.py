"""Closing Line Value (CLV): did the price you took beat where the market closed? (Group 116)
Developed for Amit Ashok Kumar Patnaik.

    CLV Edge = (Placed Odds / Closing Odds) - 1.0

The closing line is the one BetDoc already records for every Oracle bet (``user_placed_bets.closing_odds``): the sharp
books' last complete price before kickoff, from Nalanda's ticks (Group 73). A real-money ticket placed from the Bet-Slip
or the system splitter is tracked by such a bet, so its CLV shows on its placement (``ticket_clv``).

Held over many bets, a positive CLV is the best-known sign of positive expectancy; a single bet's CLV says little on
its own, and nothing here promises a result.
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


def ticket_clv(placed_odds: Decimal | float | None, closing_odds: Decimal | float | None) -> dict[str, Any] | None:
    """A placed bet's CLV once its closing line is recorded (None before kickoff, or when no sharp close was seen)."""
    if placed_odds is None or closing_odds is None or Decimal(str(placed_odds)) <= 1 or Decimal(str(closing_odds)) <= 1:
        return None
    return calculate_clv_edge(placed_odds, closing_odds)
