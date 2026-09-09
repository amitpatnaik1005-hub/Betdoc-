from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from betdoc.application.explanation_formatter import RiskExplanationFormatter
from betdoc.domain.intelligence.session_oracle import (
    DRY_MARKET_PAYLOAD,
    VARIANCE_WARNING_PAYLOAD,
    SessionIntelligenceOracle,
)
from betdoc.domain.intelligence.advisor_models import MarketOpportunity, RejectionReason
from betdoc.domain.risk.models import ConstraintBreakdown, RiskDecision, RiskVerdict
from tests.support.fakes import FailingNotifier, FrozenClock, RecordingNotifier

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_FORMATTER = RiskExplanationFormatter()

@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "\u20b90.00"),
        (1, "\u20b90.01"),
        (99, "\u20b90.99"),
        (100, "\u20b91.00"),
        (99_999, "\u20b9999.99"),
        (100_000, "\u20b91,000.00"),
        (1_234_567, "\u20b912,345.67"),
        (10_000_000, "\u20b91,00,000.00"),          
        (100_000_000, "\u20b910,00,000.00"),        
        (1_000_000_000, "\u20b91,00,00,000.00"),    
        (10_000_000_000, "\u20b910,00,00,000.00"),  
        (-10_000_000, "-\u20b91,00,000.00"),
    ],
)
def test_format_inr_groups_by_the_indian_numbering_system(
    paise: int, expected: str
) -> None:
    assert RiskExplanationFormatter._format_inr(paise) == expected


def test_format_inr_is_exact_beyond_float_precision() -> None:
    huge_paise = 9_007_199_254_740_993 
    rendered = RiskExplanationFormatter._format_inr(huge_paise)
    assert rendered.endswith(".93")
    assert rendered.replace("\u20b9", "").replace(",", "").replace(".", "") == str(
        huge_paise
    )

def _decision(
    *,
    verdict: RiskVerdict,
    approved_stake_paise: int,
    requested_stake_paise: int = 900_000,
    binding_constraint: RejectionReason | None,
    rejection_reason: RejectionReason | None = None,
    expected_value_bps: int = 1_550,
    detail: str = "test detail",
    single_stake_cap_paise: int = 200_000,
    sport_exposure_headroom_paise: int = 5_000_000,
    daily_loss_headroom_paise: int = 250_000,
    settled_liquidity_paise: int = 10_000_000,
) -> RiskDecision:
    return RiskDecision(
        verdict=verdict,
        approved_stake_paise=approved_stake_paise,
        requested_stake_paise=requested_stake_paise,
        rejection_reason=rejection_reason,
        binding_constraint=binding_constraint,
        detail=detail,
        expected_value_bps=expected_value_bps,
        constraints=ConstraintBreakdown(
            single_stake_cap_paise=single_stake_cap_paise,
            sport_exposure_headroom_paise=sport_exposure_headroom_paise,
            daily_loss_headroom_paise=daily_loss_headroom_paise,
            settled_liquidity_paise=settled_liquidity_paise,
        ),
        decided_at=_NOW,
    )


def test_full_approval_renders_the_fail_safe_line() -> None:
    text = _FORMATTER.format_decision(
        _decision(
            verdict=RiskVerdict.APPROVED,
            approved_stake_paise=50_000,
            binding_constraint=None,
        )
    )
    assert text == "Stake of \u20b9500.00 approved in full. Favourable EV detected."

def _opportunity(
    *,
    index: int = 0,
    offered_odds: float = 2.10,
    fair_probability: float = 0.55,
    quoted_at: datetime = _NOW,
) -> MarketOpportunity:
    return MarketOpportunity(
        opportunity_id=f"opp-{index}",
        sport_type="cricket",
        selection_label="MI",
        offered_odds=offered_odds,
        fair_probability=fair_probability,
        quoted_at=quoted_at,
    )


def _oracle(
    *,
    notifier: RecordingNotifier | FailingNotifier,
    clock: FrozenClock,
    max_tracked_opportunities: int = 8_192,
) -> SessionIntelligenceOracle:
    return SessionIntelligenceOracle(
        notifier=notifier,
        clock=clock,
        max_tracked_opportunities=max_tracked_opportunities,
    )

def test_empty_window_scores_zero() -> None:
    oracle = _oracle(notifier=RecordingNotifier(), clock=FrozenClock(_NOW))
    health = oracle.market_health_score()
    assert health.score == 0.0

