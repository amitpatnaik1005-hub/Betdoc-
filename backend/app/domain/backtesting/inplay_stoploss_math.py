"""The in-play stop-loss shield's rules (Group 77), checked on every tick of the twin's watch (Group 72).

A placed bet's money cannot be guaranteed: a book suspends cashout exactly when the game swings (a goal, a red
card), and a straight multiple can lose outright between two ticks. What the shield does is fire the moment a
rule is met, so the user can take what is still on offer:

1. **Stop-loss floor.** The cashout value is at or under ``(1 - stop_loss_pct) x stake``. The value is the book's
   offer when the user has read one in, else the slip's fair value from the live market (the book's offer is
   typically somewhat under fair value, so a fair-value trigger fires no later than an offer-based one would).
2. **Relative probability collapse.** The live win probability is under ``collapse_ratio x`` the entry
   probability (the twin's absolute rule, a drop of ``TWIN_PULLOUT_PROB_DROP`` points, still applies too).
3. **Severe adverse events** move the market before any feed reports them: a red card or a wicket collapse
   shows up here as the probability collapse or the cashout floor, ticks before an event feed would say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StopLossPolicy:
    default_pct: float
    min_pct: float
    max_pct: float
    collapse_ratio: float

    @classmethod
    def from_settings(cls, settings: object) -> StopLossPolicy:
        policy = cls(float(settings.TWIN_STOP_LOSS_PCT), float(settings.TWIN_STOP_LOSS_MIN_PCT), float(settings.TWIN_STOP_LOSS_MAX_PCT),  # type: ignore[attr-defined]
                     float(settings.TWIN_PULLOUT_PROB_RATIO))  # type: ignore[attr-defined]
        if not 0 < policy.min_pct <= policy.default_pct <= policy.max_pct < 1:
            raise ValueError("TWIN_STOP_LOSS_*: 0 < min <= default <= max < 1")
        return policy

    def clamp(self, pct: float | None) -> float:
        return self.default_pct if pct is None else min(self.max_pct, max(self.min_pct, pct))


@dataclass(frozen=True, slots=True)
class StopLossCall:
    rule: str  # STOP_LOSS_FLOOR | PROBABILITY_COLLAPSE
    reason: str
    floor_inr: Decimal
    value_inr: Decimal
    value_source: str  # "offer" | "fair_value"


def floor(stake: Decimal, stop_loss_pct: float) -> Decimal:
    return (stake * (Decimal(1) - Decimal(str(stop_loss_pct)))).quantize(Decimal("0.01"))


def evaluate(stake: Decimal, stop_loss_pct: float, *, offer: Decimal | None, fair_value: Decimal, entry_probability: float, live_probability: float,
             policy: StopLossPolicy) -> StopLossCall | None:
    """The first rule met, or None."""
    line = floor(stake, stop_loss_pct)
    value, source = (offer, "offer") if offer is not None else (fair_value, "fair_value")
    if value <= line:
        what = f"the ₹{value:,} cashout offer" if source == "offer" else f"fair value ₹{value:,}"
        return StopLossCall("STOP_LOSS_FLOOR", f"{what} is at or under the {stop_loss_pct:.0%} stop-loss floor ₹{line:,}: cash out now", line, value, source)
    if entry_probability > 0 and live_probability < policy.collapse_ratio * entry_probability:
        return StopLossCall("PROBABILITY_COLLAPSE", f"win probability {entry_probability:.1%} -> {live_probability:.1%}, under {policy.collapse_ratio:.0%} of the entry: cash out now",
                            line, value, source)
    return None


def recommended_pct(joint_probability: float, policy: StopLossPolicy) -> float:
    """A wider stop for a long shot (its value swings more on every event, a tight stop would fire on noise), a
    tighter one for a near-certainty: linear in the slip's win probability between the policy's bounds."""
    p = min(1.0, max(0.0, joint_probability))
    return round(policy.max_pct - (policy.max_pct - policy.min_pct) * p, 4)
