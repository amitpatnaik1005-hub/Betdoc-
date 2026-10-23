"""Tests for Group 116 — Closing Line Value (CLV) Tracker and Monte Carlo Stress-Test Simulator.
Developed for Amit Ashok Kumar Patnaik.
"""

from decimal import Decimal
import pytest

from app.services.clv_tracker import calculate_clv_edge, DEVELOPER_CREDIT as CLV_CREDIT
from app.services.bankroll_stress_test import simulate_bankroll_stress_test, DEVELOPER_CREDIT as STRESS_CREDIT


def test_developer_credits():
    assert CLV_CREDIT == "Amit Ashok Kumar Patnaik"
    assert STRESS_CREDIT == "Amit Ashok Kumar Patnaik"


def test_clv_positive_edge():
    # Placed at 2.10, closed at 1.90 -> +10.53% CLV edge
    res = calculate_clv_edge(placed_odds=2.10, closing_odds=1.90)
    assert res["beat_the_market"] is True
    assert res["clv_edge_pct"] > 10.0
    assert "Beat the market" in res["rationale"]


def test_clv_negative_edge():
    # Placed at 1.80, closed at 2.00 -> -10% CLV edge
    res = calculate_clv_edge(placed_odds=1.80, closing_odds=2.00)
    assert res["beat_the_market"] is False
    assert res["clv_edge_pct"] < 0
    assert "Negative CLV" in res["rationale"]


def test_clv_neutral_edge():
    res = calculate_clv_edge(placed_odds=2.00, closing_odds=2.00)
    assert res["beat_the_market"] is False
    assert res["clv_edge_pct"] == 0.0
    assert "Fair value lock" in res["rationale"]


def test_clv_invalid_odds():
    with pytest.raises(ValueError):
        calculate_clv_edge(placed_odds=0.95, closing_odds=2.00)
    with pytest.raises(ValueError):
        calculate_clv_edge(placed_odds=2.00, closing_odds=1.00)


def test_monte_carlo_bankroll_stress_test():
    res = simulate_bankroll_stress_test(
        starting_bankroll=10000.0,
        win_probability=0.58,
        average_odds=1.90,
        kelly_fraction=0.25,
        num_iterations=1000,  # fast test run
        bets_per_iteration=50,
        seed=42,
    )
    assert res["ruin_probability"] < 0.01
    assert res["ruin_shield_passed"] is True
    assert res["median_final_bankroll"] > 10000.0
    assert res["p95_max_drawdown_pct"] < 50.0
    assert res["architect"] == "Amit Ashok Kumar Patnaik"
