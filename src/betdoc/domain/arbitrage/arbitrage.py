"""Arbitrage detection and stake allocation.

Textbook arbitrage math (below) assumes you can place all legs
simultaneously at the quoted odds. In production that's false: there is
latency between detecting the opportunity and placing each leg, odds can
move or the book can limit/void your stake in that window, and different
books have different max stake limits. A "gold-grade" arb system scores
opportunities by expected survival probability, not just raw guaranteed
profit — see `risk_adjusted_score`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ArbOpportunity:
    outcomes: list[str]  # e.g. ["Team A", "Team B"] or ["Home","Draw","Away"]
    books: list[str]  # book offering the best price per outcome, same order
    decimal_odds: list[float]  # best available odds per outcome, same order
    guaranteed_profit_pct: float  # raw textbook profit, before execution-risk adjustment
    stakes_pct: list[float]  # fraction of total arb bankroll to place per outcome
    risk_adjusted_score: float  # 0-1, see risk_adjusted_score()


def detect_arbitrage(decimal_odds: np.ndarray) -> tuple[bool, float]:
    """n-way arbitrage check. An arbitrage exists iff sum(1/odds_i) < 1 —
    i.e. the combined implied probability across the best price for every
    outcome is under 100%, meaning you can stake proportionally and win
    regardless of outcome.

    Returns (is_arbitrage, guaranteed_profit_pct).
    profit_pct = 1/sum(1/odds_i) - 1, expressed as a fraction (0.02 = 2%).
    """
    implied_sum = float(np.sum(1.0 / decimal_odds))
    is_arb = implied_sum < 1.0
    profit_pct = (1.0 / implied_sum - 1.0) if is_arb else 0.0
    return is_arb, profit_pct


def allocate_stakes(decimal_odds: np.ndarray) -> np.ndarray:
    """Proportional stake allocation across arb legs so that the payout is
    IDENTICAL regardless of which outcome hits. stake_i proportional to
    1/odds_i, normalized to sum to 1 (fraction of total arb bankroll)."""
    implied = 1.0 / decimal_odds
    return implied / implied.sum()


def risk_adjusted_score(
    guaranteed_profit_pct: float,
    tick_age_ms: float,
    book_historical_staleness_ms: dict[str, float],
    books: list[str],
    max_acceptable_latency_ms: float = 800.0,
) -> float:
    """Heuristic 0-1 confidence score for whether this arb will actually
    execute as detected, not just whether the math says it should.

    Penalizes:
      - How old the current tick is (tick_age_ms) relative to your
        acceptable execution window.
      - Each leg's book-specific historical staleness (some books are known
        to lag their true line, or limit sharp bettors quickly — feed this
        from your own execution-fill telemetry over time, not a guess).

    This is deliberately simple and interpretable so you can tune it against
    real fill-rate data. Do not auto-execute above your latency budget purely
    because guaranteed_profit_pct looks attractive — a stale arb is a way to
    get one leg filled and the other rejected, which is a straight loss.
    """
    if tick_age_ms > max_acceptable_latency_ms:
        return 0.0

    latency_factor = 1.0 - (tick_age_ms / max_acceptable_latency_ms)

    staleness_penalties = [
        book_historical_staleness_ms.get(book, 0.0) / max_acceptable_latency_ms for book in books
    ]
    staleness_factor = max(0.0, 1.0 - max(staleness_penalties, default=0.0))

    # Profit itself is a weak positive signal only above a noise floor —
    # very large "guaranteed profit" on an arb is more often a data error
    # (misaligned market, wrong-sport odds, stale line) than free money.
    profit_plausibility = 1.0 if guaranteed_profit_pct < 0.08 else 0.3

    return round(latency_factor * staleness_factor * profit_plausibility, 4)


def find_arbitrage(
    outcomes: list[str],
    books: list[str],
    decimal_odds: list[float],
    tick_age_ms: float,
    book_historical_staleness_ms: dict[str, float],
    min_profit_pct: float = 0.005,
) -> ArbOpportunity | None:
    odds = np.array(decimal_odds)
    is_arb, profit_pct = detect_arbitrage(odds)

    if not is_arb or profit_pct < min_profit_pct:
        return None

    stakes = allocate_stakes(odds)
    score = risk_adjusted_score(profit_pct, tick_age_ms, book_historical_staleness_ms, books)

    return ArbOpportunity(
        outcomes=outcomes,
        books=books,
        decimal_odds=decimal_odds,
        guaranteed_profit_pct=round(profit_pct, 4),
        stakes_pct=stakes.tolist(),
        risk_adjusted_score=score,
    )
