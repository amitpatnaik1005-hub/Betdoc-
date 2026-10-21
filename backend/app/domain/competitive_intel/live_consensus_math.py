"""Group 85: the mathematics of live competitive market intelligence (Commanders KARNA & PANINI).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Analytics Fleet.

* **Bookmaker margin.** One book's overround on a market is ``sum(1 / odds) - 1`` over every selection; its payout
  efficiency ``1 / sum(1 / odds)``; the multiplicatively de-vigged fair probabilities ``(1 / o_i) / sum(1 / o)``.
* **Divergence.** Money share minus ticket share on a selection (points): money heavier than tickets is the
  classic footprint of large, sharp stakes.
* **Reverse line movement (RLM).** The crowd heavy on a side (tickets >= ``rlm_public_heavy``) whose price drifts
  out by at least ``rlm_min_odds_move``; or the crowd light on a side (<= ``rlm_public_light``) that the money backs
  (>= ``rlm_money_heavy``) and whose price is cut by at least that much.
* **EV.** ``p x best_odds - 1`` with ``p`` BetDoc's ensemble probability and the better of Parimatch and 1xBet.
* **Public trap.** Tickets >= ``trap_public`` and either EV under ``trap_max_ev`` or the money avoiding the side
  (money share <= ``trap_money_max``).
* **Contrarian edge score** (0-100): ``base + clamp(divergence x w_d) + clamp(EV x w_e) + RLM bonus - trap penalty
  + a bonus when the cheaper book's margin is tight``.
* **Verdict.** PUBLIC_TRAP; SHARP_CONTRARIAN (EV over the contrarian bar and divergence over its bar or RLM);
  SHARP_CONSENSUS (EV over the consensus bar and money over its share); else NEUTRAL.

Shares and the opening price are measurements: when one is missing (``None``) every rule that needs it simply
does not fire, and the score takes no term for it. Nothing here guesses a share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

PCT = Decimal("0.01")


def _q(value: float, places: str = "0.01") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ConsensusPolicy:
    rlm_public_heavy: float
    rlm_public_light: float
    rlm_money_heavy: float
    rlm_min_odds_move: float
    trap_public: float
    trap_max_ev: float
    trap_money_max: float
    contrarian_min_ev: float
    contrarian_min_divergence: float
    consensus_min_ev: float
    consensus_min_money: float
    weights: dict[str, float]

    @classmethod
    def from_settings(cls, s: Any) -> ConsensusPolicy:
        return cls(s.COMPETITIVE_RLM_PUBLIC_HEAVY_PCT, s.COMPETITIVE_RLM_PUBLIC_LIGHT_PCT, s.COMPETITIVE_RLM_MONEY_HEAVY_PCT, s.COMPETITIVE_RLM_MIN_ODDS_MOVE,
                   s.COMPETITIVE_TRAP_PUBLIC_PCT, s.COMPETITIVE_TRAP_MAX_EV_PCT, s.COMPETITIVE_TRAP_MONEY_MAX_PCT, s.COMPETITIVE_CONTRARIAN_MIN_EV_PCT,
                   s.COMPETITIVE_CONTRARIAN_MIN_DIVERGENCE_PCT, s.COMPETITIVE_CONSENSUS_MIN_EV_PCT, s.COMPETITIVE_CONSENSUS_MIN_MONEY_PCT,
                   dict(s.COMPETITIVE_EDGE_WEIGHTS))


@dataclass(frozen=True, slots=True)
class MarginMetrics:
    overround_pct: Decimal
    payout_efficiency_pct: Decimal
    fair_probabilities: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class ConsensusEvaluation:
    divergence_pct: Decimal | None
    rlm_detected: bool
    betdoc_ev_pct: Decimal
    public_trap_flag: bool
    contrarian_edge_score: Decimal
    verdict: str


def compute_bookmaker_margin(odds_list: Sequence[float | Decimal]) -> MarginMetrics:
    """Overround %, payout efficiency % and de-vigged fair probabilities of one book's prices on one market."""
    prices = [float(o) for o in odds_list]
    if not prices or any(o <= 1.0 for o in prices):
        raise ValueError("a market's prices are decimal odds above 1, one per selection")
    book = sum(1.0 / o for o in prices)
    return MarginMetrics(overround_pct=_q((book - 1.0) * 100.0), payout_efficiency_pct=_q(100.0 / book),
                         fair_probabilities=tuple(_q((1.0 / o) / book, "0.0001") for o in prices))


def detect_reverse_line_movement(public_ticket_pct: float | None, money_pct: float | None, opening_odds: float | None, current_odds: float,
                                 policy: ConsensusPolicy) -> bool:
    if public_ticket_pct is None or opening_odds is None or opening_odds <= 1.0:
        return False
    move = float(current_odds) - float(opening_odds)
    if public_ticket_pct >= policy.rlm_public_heavy and move >= policy.rlm_min_odds_move:
        return True
    return bool(money_pct is not None and public_ticket_pct <= policy.rlm_public_light and money_pct >= policy.rlm_money_heavy
                and move <= -policy.rlm_min_odds_move)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def evaluate_consensus_signal(*, public_ticket_pct: float | None, money_pct: float | None, opening_odds: float | None, best_odds: float,
                              cheapest_margin_pct: float | None, betdoc_true_prob: float, policy: ConsensusPolicy) -> ConsensusEvaluation:
    ev = (betdoc_true_prob * best_odds - 1.0) * 100.0
    divergence = None if public_ticket_pct is None or money_pct is None else money_pct - public_ticket_pct
    rlm = detect_reverse_line_movement(public_ticket_pct, money_pct, opening_odds, best_odds, policy)
    trap = bool(public_ticket_pct is not None and public_ticket_pct >= policy.trap_public
                and (ev < policy.trap_max_ev or (money_pct is not None and money_pct <= policy.trap_money_max)))
    w = policy.weights
    score = w["base"] + _clamp(ev * w["ev_weight"], w["ev_min"], w["ev_max"])
    if divergence is not None:
        score += _clamp(divergence * w["divergence_weight"], w["divergence_min"], w["divergence_max"])
    if rlm and not trap:
        score += w["rlm_bonus"]
    if trap:
        score -= w["trap_penalty"]
    if cheapest_margin_pct is not None and cheapest_margin_pct <= w["tight_margin_pct"]:
        score += w["tight_margin_bonus"]
    if trap:
        verdict = "PUBLIC_TRAP"
    elif ev >= policy.contrarian_min_ev and ((divergence is not None and divergence >= policy.contrarian_min_divergence) or rlm):
        verdict = "SHARP_CONTRARIAN"
    elif ev >= policy.consensus_min_ev and money_pct is not None and money_pct >= policy.consensus_min_money:
        verdict = "SHARP_CONSENSUS"
    else:
        verdict = "NEUTRAL"
    return ConsensusEvaluation(divergence_pct=None if divergence is None else _q(divergence), rlm_detected=rlm, betdoc_ev_pct=_q(ev),
                               public_trap_flag=trap, contrarian_edge_score=_q(_clamp(score, 0.0, 100.0)), verdict=verdict)
