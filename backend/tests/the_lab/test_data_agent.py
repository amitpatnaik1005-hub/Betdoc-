"""DataResearchAgent maths: vig-free consensus and drift come from stored prices, not templates."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.the_lab.data_agent import DataResearchAgent

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _snap(book: str, sel: str, odds: float, ts: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        match_id="m1", home_team="Arsenal", away_team="Chelsea", commence_time=T0 + timedelta(days=1),
        bookmaker=book, selection=sel, odds=odds, timestamp=ts,
    )


def test_consensus_removes_each_books_overround() -> None:
    rows = [_snap("A", "Arsenal", 2.0, T0), _snap("A", "Chelsea", 2.0, T0), _snap("B", "Arsenal", 1.8, T0), _snap("B", "Chelsea", 2.2, T0)]
    fair = DataResearchAgent._consensus(rows)  # type: ignore[arg-type]
    assert fair["Arsenal"] == pytest.approx((0.5 + (1 / 1.8) / (1 / 1.8 + 1 / 2.2)) / 2)
    assert sum(fair.values()) == pytest.approx(1.0)


def test_fixture_section_reports_drift_best_price_and_spread() -> None:
    later = T0 + timedelta(hours=6)
    rows = [
        _snap("A", "Arsenal", 2.0, T0), _snap("A", "Chelsea", 2.0, T0),
        _snap("A", "Arsenal", 1.7, later), _snap("A", "Chelsea", 2.4, later),
        _snap("B", "Arsenal", 1.8, later), _snap("B", "Chelsea", 2.2, later),
    ]
    text = "\n".join(DataResearchAgent(None)._fixture_sections(rows))  # type: ignore[arg-type]
    assert "## Arsenal v Chelsea" in text
    assert "| Arsenal | 50.0% |" in text and "▲" in text  # Arsenal shortened: backed into kick-off
    assert "1.80 (B)" in text and "| 0.20 |" in text  # best price and book spread for Arsenal
