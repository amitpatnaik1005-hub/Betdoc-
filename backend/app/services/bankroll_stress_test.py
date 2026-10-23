"""Monte Carlo bankroll stress test (Group 116). Developed for Amit Ashok Kumar Patnaik.

Simulates ``num_iterations`` paths of ``bets_per_iteration`` sequential bets at one win probability and one average
price, staked the way the real-money desk stakes and guarded the way SHIVAJI guards:

* the stake is the balance x min(kelly_fraction x full Kelly, max_stake_pct) (the desk's 5% single-ticket ceiling);
* ``bets_per_day`` bets make a trading day; once the day's realised loss reaches ``daily_stop_loss_pct`` of the day's
  opening bankroll, the rest of that day is sat out (the 15% daily latch);
* ``loss_streak_limit`` losses in a row sit out the next ``cooldown_bets`` bets (the 30-minute cool-down);
* a path is ruined when its bankroll falls to ``ruin_fraction`` of the start or its stake under ``min_stake``.

The answer is only as true as its inputs: nothing here proves an edge. ``win_probability`` and ``average_odds`` must be
given, either measured from the desk's own settled tickets (``measured_inputs``) or stated as assumptions; inputs that
carry no edge stake nothing (Kelly), and that is reported, never counted as ruin. Each run draws from its own seeded
random generator: the process-wide one is left alone.
"""

from __future__ import annotations

import math
import random
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.real_money import RealBetTicket, RealTicketStatus

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
MAX_ITERATIONS = 20_000
MAX_BETS = 1_000


def _percentile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * q))]


def simulate_bankroll_stress_test(
    *,
    starting_bankroll: float,
    win_probability: float,
    average_odds: float,
    kelly_fraction: float = 0.25,
    num_iterations: int = 10_000,
    bets_per_iteration: int = 100,
    bets_per_day: int = 5,
    daily_stop_loss_pct: float = 0.15,
    max_stake_pct: float = 0.05,
    loss_streak_limit: int = 3,
    cooldown_bets: int = 1,
    ruin_fraction: float = 0.10,
    min_stake: float = 10.0,
    seed: int | None = 42,
) -> dict[str, Any]:
    if starting_bankroll <= 0 or not math.isfinite(starting_bankroll):
        raise ValueError("the starting bankroll must be positive")
    if not 0 < win_probability < 1:
        raise ValueError("the win probability lies strictly between 0 and 1")
    if not average_odds > 1 or not math.isfinite(average_odds):
        raise ValueError("decimal odds must exceed 1.0")
    if not 0 < kelly_fraction <= 1 or not 0 < max_stake_pct <= 1 or not 0 < daily_stop_loss_pct <= 1 or not 0 <= ruin_fraction < 1:
        raise ValueError("the Kelly fraction, stake ceiling, daily stop and ruin level are fractions")
    if not 1 <= num_iterations <= MAX_ITERATIONS or not 1 <= bets_per_iteration <= MAX_BETS or bets_per_day < 1:
        raise ValueError(f"at most {MAX_ITERATIONS:,} paths of {MAX_BETS:,} bets, at least one bet a day")
    rng = random.Random(seed)
    b = average_odds - 1.0
    edge = win_probability * average_odds - 1.0
    full_kelly = max(0.0, (b * win_probability - (1.0 - win_probability)) / b)
    stake_fraction = min(max_stake_pct, full_kelly * kelly_fraction)
    ruin_level = starting_bankroll * ruin_fraction
    finals: list[float] = []
    drawdowns: list[float] = []
    ruined = latched_days = cooldowns = bets_placed = 0
    for _ in range(num_iterations):
        balance = peak = starting_bankroll
        worst = 0.0
        day_open = balance
        day_loss = 0.0
        latched = False
        streak = sitting_out = 0
        for n in range(bets_per_iteration if stake_fraction > 0 else 0):
            if n % bets_per_day == 0:  # a new trading day: the latch lifts, the day's loss resets
                day_open, day_loss, latched = balance, 0.0, False
            if latched:
                continue
            if sitting_out:
                sitting_out -= 1
                continue
            stake = balance * stake_fraction
            if balance <= ruin_level or stake < min_stake:
                ruined += 1
                break
            bets_placed += 1
            if rng.random() < win_probability:
                balance += stake * b
                day_loss -= stake * b
                streak = 0
            else:
                balance -= stake
                day_loss += stake
                streak += 1
                if streak >= loss_streak_limit:
                    sitting_out, streak = cooldown_bets, 0
                    cooldowns += 1
            if day_loss >= day_open * daily_stop_loss_pct:
                latched = True
                latched_days += 1
            peak = max(peak, balance)
            worst = max(worst, (peak - balance) / peak)
        finals.append(balance)
        drawdowns.append(worst)
    finals.sort()
    drawdowns.sort()
    ruin = ruined / num_iterations
    note = None if stake_fraction > 0 else "These inputs carry no edge: Kelly stakes nothing, so no path bets."
    return {
        "starting_bankroll": starting_bankroll, "iterations": num_iterations, "bets_per_trajectory": bets_per_iteration, "bets_per_day": bets_per_day,
        "win_probability": win_probability, "average_odds": average_odds, "edge_per_unit": round(edge, 6), "full_kelly": round(full_kelly, 6),
        "stake_fraction": round(stake_fraction, 6), "kelly_fraction": kelly_fraction, "max_stake_pct": max_stake_pct,
        "ruin_probability": round(ruin, 5), "ruin_shield_passed": ruin < 0.01,  # under 1% of paths ruined, at these inputs
        "median_final_bankroll": round(_percentile(finals, 0.5), 2), "p5_final_bankroll": round(_percentile(finals, 0.05), 2),
        "p95_final_bankroll": round(_percentile(finals, 0.95), 2), "mean_final_bankroll": round(sum(finals) / len(finals), 2),
        "probability_of_profit": round(sum(1 for f in finals if f > starting_bankroll) / len(finals), 5),
        "p95_max_drawdown_pct": round(_percentile(drawdowns, 0.95) * 100, 2), "median_max_drawdown_pct": round(_percentile(drawdowns, 0.5) * 100, 2),
        "stop_loss_activations": latched_days, "cooldowns": cooldowns, "bets_placed": bets_placed, "seed": seed, "note": note,
        "architect": DEVELOPER_CREDIT,
    }


async def measured_inputs(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """The desk's own record: WON and LOST tickets (halves, voids and cashouts are not binary outcomes and are left out),
    their win rate and their average placed odds."""
    rows = (await db.execute(select(RealBetTicket.status, func.count(), func.avg(RealBetTicket.placed_odds))
                             .where(RealBetTicket.user_id == user_id, RealBetTicket.status.in_((RealTicketStatus.WON.value, RealTicketStatus.LOST.value)),
                                    RealBetTicket.placed_odds.is_not(None))
                             .group_by(RealBetTicket.status))).all()
    counts = {status: int(n) for status, n, _ in rows}
    settled = sum(counts.values())
    total_odds = sum((Decimal(str(avg)) * n for _, n, avg in rows if avg is not None), Decimal(0))
    return {"settled": settled, "won": counts.get(RealTicketStatus.WON.value, 0),
            "win_probability": None if not settled else counts.get(RealTicketStatus.WON.value, 0) / settled,
            "average_odds": None if not settled else float(total_odds / settled)}


__all__ = ["DEVELOPER_CREDIT", "MAX_BETS", "MAX_ITERATIONS", "measured_inputs", "simulate_bankroll_stress_test"]
