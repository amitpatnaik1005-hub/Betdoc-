"""Group 93: TODAR MAL's closing-line-value warehouse arithmetic. Pure: no I/O.

Inputs are the user's real recorded slips (``UserPlacedBet``) once settled. CLV is only ever computed from a
recorded closing price; a slip without one is simply not in the CLV sample, and with no sample the tier is
UNMEASURED. USD figures exist only when a measured INR-per-USD rate is passed in.

* ``clv_pct``       = (placed odds / closing odds - 1) x 100
* ``clv_sharp_pct`` = (placed odds x de-vigged closing probability - 1) x 100
* turnover, return, P&L and ROI exclude VOID slips (the stake came back; they say nothing about the edge)

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from app.domain.archive.backup_vault_math import convert_inr_to_usd

ClvEdgeTier = Literal["SHARP_ALPHA", "POSITIVE_CLV", "NEUTRAL_LINE", "ADVERSE_DRIFT", "UNMEASURED"]

CENT: Final = Decimal("0.01")
WINNING: Final = frozenset({"WON", "HALF_WON"})
LOSING: Final = frozenset({"LOST", "HALF_LOST"})
VOID: Final = "VOID"


@dataclass(frozen=True, slots=True)
class TierThresholds:
    sharp_alpha_min_pct: float
    sharp_beat_rate_min: float  # a fraction
    positive_min_pct: float
    adverse_below_pct: float
    min_sample: int


@dataclass(frozen=True, slots=True)
class SettledBetInput:
    bet_id: str
    bookmaker: str
    sport_key: str
    status: str
    stake_inr: Decimal
    return_inr: Decimal
    pnl_inr: Decimal
    clv_pct: float | None
    clv_sharp_pct: float | None


@dataclass(frozen=True, slots=True)
class ClvAggregateMetrics:
    settled_bets_count: int
    won_bets_count: int
    lost_bets_count: int
    void_bets_count: int
    win_rate_pct: float | None
    total_staked_inr: Decimal
    total_return_inr: Decimal
    net_pnl_inr: Decimal
    total_staked_usd: Decimal | None
    total_return_usd: Decimal | None
    net_pnl_usd: Decimal | None
    roi_pct: float | None
    clv_sample_count: int
    avg_clv_pct: float | None
    avg_clv_sharp_pct: float | None
    beat_close_rate_pct: float | None
    edge_tier: ClvEdgeTier
    edge_tier_provisional: bool  # measured on fewer than min_sample slips


def compute_leg_clv_pct(placed_odds: Decimal | float | None, closing_odds: Decimal | float | None) -> float | None:
    """(placed / closing - 1) x 100 to 3 places; None unless both prices are real decimal odds."""
    if placed_odds is None or closing_odds is None:
        return None
    placed, closing = float(placed_odds), float(closing_odds)
    if placed <= 1.0 or closing <= 1.0:
        return None
    return round((placed / closing - 1.0) * 100.0, 3)


def compute_sharp_clv_pct(placed_odds: Decimal | float | None, closing_fair_prob: float | None) -> float | None:
    """(placed x de-vigged closing probability - 1) x 100 to 3 places; None unless both are measured."""
    if placed_odds is None or closing_fair_prob is None:
        return None
    placed, fair = float(placed_odds), float(closing_fair_prob)
    if placed <= 1.0 or not 0.0 < fair < 1.0:
        return None
    return round((placed * fair - 1.0) * 100.0, 3)


def classify_clv_edge_tier(*, avg_clv_pct: float | None, beat_close_rate_fraction: float | None, thresholds: TierThresholds) -> ClvEdgeTier:
    if avg_clv_pct is None or beat_close_rate_fraction is None:
        return "UNMEASURED"
    if avg_clv_pct >= thresholds.sharp_alpha_min_pct and beat_close_rate_fraction >= thresholds.sharp_beat_rate_min:
        return "SHARP_ALPHA"
    if avg_clv_pct > thresholds.positive_min_pct:
        return "POSITIVE_CLV"
    if avg_clv_pct >= thresholds.adverse_below_pct:
        return "NEUTRAL_LINE"
    return "ADVERSE_DRIFT"


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def evaluate_clv_warehouse_aggregates(
    bets: Sequence[SettledBetInput], *, inr_per_usd: Decimal | None, thresholds: TierThresholds
) -> ClvAggregateMetrics:
    staked = returned = pnl = Decimal("0")
    won = lost = void = 0
    clv: list[float] = []
    sharp: list[float] = []
    for bet in bets:
        if bet.status in WINNING:
            won += 1
        elif bet.status in LOSING:
            lost += 1
        if bet.status == VOID:
            void += 1
        else:
            staked += bet.stake_inr
            returned += bet.return_inr
            pnl += bet.pnl_inr
        if bet.clv_pct is not None:
            clv.append(bet.clv_pct)
        if bet.clv_sharp_pct is not None:
            sharp.append(bet.clv_sharp_pct)

    avg_clv = round(sum(clv) / len(clv), 3) if clv else None
    beat = sum(1 for v in clv if v > 0.0) / len(clv) if clv else None
    staked, returned, pnl = _cents(staked), _cents(returned), _cents(pnl)
    return ClvAggregateMetrics(
        settled_bets_count=len(bets),
        won_bets_count=won,
        lost_bets_count=lost,
        void_bets_count=void,
        win_rate_pct=round(won / (won + lost) * 100.0, 2) if won + lost else None,
        total_staked_inr=staked,
        total_return_inr=returned,
        net_pnl_inr=pnl,
        total_staked_usd=convert_inr_to_usd(staked, inr_per_usd),
        total_return_usd=convert_inr_to_usd(returned, inr_per_usd),
        net_pnl_usd=convert_inr_to_usd(pnl, inr_per_usd),
        roi_pct=float(_cents(pnl / staked * 100)) if staked > 0 else None,
        clv_sample_count=len(clv),
        avg_clv_pct=avg_clv,
        avg_clv_sharp_pct=round(sum(sharp) / len(sharp), 3) if sharp else None,
        beat_close_rate_pct=round(beat * 100.0, 2) if beat is not None else None,
        edge_tier=classify_clv_edge_tier(avg_clv_pct=avg_clv, beat_close_rate_fraction=beat, thresholds=thresholds),
        edge_tier_provisional=0 < len(clv) < thresholds.min_sample,
    )
