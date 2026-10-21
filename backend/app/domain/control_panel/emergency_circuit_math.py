"""Group 86: the mathematics of the emergency fortress: the auto-drawdown and loss-streak circuit breaker, INR/USD
display, and the SHA-256 chain of the emergency audit trail.

Commanders: KAUTILYA (macro control), CHANAKYA (drawdown fortress) and SHIVAJI (cryptographic audit).
Developer Credit: Amit Ashok Kumar Patnaik

* No guessed constants: USD figures only with a fresh INR/USD rate from TODAR MAL's CurrencyService, else ``None``.
* An unmeasured bankroll (``None`` or ``<= 0``) never produces a drawdown percentage; the streak rule still applies.
* Each audit entry hashes the previous entry's hash with its own canonical fields, so editing or deleting any
  entry breaks every later link.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
GENESIS_AUDIT_HASH: str = "0" * 64


@dataclass(frozen=True, slots=True)
class DrawdownEvaluationResult:
    reference_bankroll_inr: Decimal | None
    reference_bankroll_usd: Decimal | None
    rolling_loss_inr: Decimal
    rolling_loss_usd: Decimal | None
    drawdown_pct: float | None
    threshold_drawdown_pct: float
    window_hours: int
    consecutive_losses: int
    max_consecutive_losses: int
    should_trip_breaker: bool
    lockout_mode: str  # ARMED | AUTO_DRAWDOWN_TRIPPED | STREAK_COOLDOWN
    verdict_summary: str
    recommended_cooldown_minutes: int


def convert_inr_to_usd(amount_inr: Decimal | None, usd_inr_rate: Decimal | None) -> Decimal | None:
    """INR to USD at TODAR MAL's measured rate (INR per USD); None when either is unmeasured."""
    if amount_inr is None or usd_inr_rate is None or usd_inr_rate <= 0:
        return None
    return (Decimal(str(amount_inr)) / Decimal(str(usd_inr_rate))).quantize(CENT, rounding=ROUND_HALF_UP)


def compute_emergency_audit_hash(*, prev_hash: str, event_type: str, target_scope: str, triggered_by: str, reason: str,
                                 drawdown_pct: float | None, protected_capital_inr: Decimal | None, timestamp_iso: str) -> str:
    """SHA-256 of the previous hash and this entry's canonical fields, '|'-joined."""
    dd = "NONE" if drawdown_pct is None else f"{float(drawdown_pct):.2f}"
    cap = "NONE" if protected_capital_inr is None else f"{Decimal(str(protected_capital_inr)):.2f}"
    payload = "|".join([prev_hash.strip() or GENESIS_AUDIT_HASH, event_type.strip().upper(), target_scope.strip().upper(), triggered_by.strip(),
                        reason.strip(), dd, cap, timestamp_iso.strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_auto_drawdown_breaker(*, reference_bankroll_inr: Decimal | None, rolling_loss_inr: Decimal, threshold_drawdown_pct: float, window_hours: int,
                                   consecutive_losses: int, max_consecutive_losses: int, drawdown_cooldown_minutes: int, streak_cooldown_minutes: int,
                                   usd_inr_rate: Decimal | None = None) -> DrawdownEvaluationResult:
    """Trip when the window's measured loss reaches the threshold share of the reference bankroll, or the streak its limit."""
    loss = max(Decimal("0.00"), Decimal(str(rolling_loss_inr))).quantize(CENT, rounding=ROUND_HALF_UP)
    threshold = max(1.0, min(95.0, float(threshold_drawdown_pct)))
    window = max(1, int(window_hours))
    streak, limit = max(0, int(consecutive_losses)), max(1, int(max_consecutive_losses))
    bankroll: Decimal | None = None
    drawdown: float | None = None
    if reference_bankroll_inr is not None and Decimal(str(reference_bankroll_inr)) > 0:
        bankroll = Decimal(str(reference_bankroll_inr)).quantize(CENT, rounding=ROUND_HALF_UP)
        drawdown = round(float(loss / bankroll * 100), 2)
    loss_usd = convert_inr_to_usd(loss, usd_inr_rate)

    if drawdown is not None and bankroll is not None and drawdown >= threshold:
        trip, mode, cooldown = True, "AUTO_DRAWDOWN_TRIPPED", drawdown_cooldown_minutes
        usd = f" (${loss_usd:,.2f})" if loss_usd is not None else ""
        verdict = (f"AUTO-KILL ENGAGED: {window}h rolling drawdown reached {drawdown:.2f}% (₹{loss:,.2f}{usd}), breaching the {threshold:.2f}% "
                   f"circuit breaker. All exposure zeroed to protect the remaining ₹{max(Decimal('0.00'), bankroll - loss):,.2f}.")
    elif streak >= limit:
        trip, mode, cooldown = True, "STREAK_COOLDOWN", streak_cooldown_minutes
        verdict = f"STREAK LOCKOUT ENGAGED: {streak} consecutive losing bets reached the limit of {limit}. Locked for a {cooldown}-minute cool-down."
    elif drawdown is None:
        trip, mode, cooldown = False, "ARMED", 0
        verdict = f"FORTRESS ARMED: no funded bankroll measured yet (rolling {window}h loss ₹{loss:,.2f}); loss streak {streak}/{limit}."
    else:
        trip, mode, cooldown = False, "ARMED", 0
        verdict = (f"FORTRESS ARMED & HEALTHY: rolling {window}h drawdown {drawdown:.2f}% ({max(0.0, round(threshold - drawdown, 2)):.2f}% below the "
                   f"{threshold:.2f}% cap); loss streak {streak}/{limit}.")
    return DrawdownEvaluationResult(bankroll, convert_inr_to_usd(bankroll, usd_inr_rate), loss, loss_usd, drawdown, threshold, window, streak, limit,
                                    trip, mode, verdict, cooldown)
