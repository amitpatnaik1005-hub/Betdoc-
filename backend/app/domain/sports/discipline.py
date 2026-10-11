"""FA-2 across nine sports: the Mistake Memory Guard's betting-pattern read and the XP it pays (Group 79).
Developed for Amit Ashok Kumar Patnaik.

A candidate bet is read against the user's recent history (settled and open bets, any sport):

* ``TILT_STAKE_ESCALATION``: after two or more straight losses the stake is at least 1.5x the median of the last
  stakes: chasing a run of losses with size.
* ``REVENGE_NON_CORE``: a bet on a sport outside the user's core sports (the ones holding most of their history)
  placed within 90 minutes of a loss: chasing it somewhere the user has no edge.
* ``LOSS_CHASING_VELOCITY``: more bets in the last hour than the user's usual hourly peak while down on the day.
* ``OVER_KELLY``: a stake over 1.5x the sport-calibrated Kelly stake (FA-4).
* ``NEGATIVE_EV``: the model's probability at the offered odds is at no edge.

The discipline score starts at 100 and loses each flag's penalty. A bet with a positive edge, staked within
1.1x its sport Kelly stake and raising no flag is *disciplined*: it earns the ``DISCIPLINED_BET`` XP award.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TILT_MIN_LOSSES = 2
TILT_STAKE_RATIO = 1.5
REVENGE_WINDOW = timedelta(minutes=90)
OVER_KELLY_RATIO = 1.5
DISCIPLINED_KELLY_RATIO = 1.1
CORE_SHARE = 0.20  # a sport holding at least this share of the history is core
CORE_MIN_HISTORY = 10  # under this many bets every sport counts as core: too little history to call one foreign
PENALTIES: dict[str, float] = {"TILT_STAKE_ESCALATION": 35.0, "REVENGE_NON_CORE": 30.0, "LOSS_CHASING_VELOCITY": 20.0,
                               "OVER_KELLY": 20.0, "NEGATIVE_EV": 25.0}


@dataclass(frozen=True, slots=True)
class BetRecord:
    placed_at: datetime
    sport_key: str
    stake: float
    odds: float
    result: str = "PENDING"  # WON | LOST | VOID | HALF_WON | HALF_LOST | PENDING | CASH_OUT
    pnl: float | None = None

    @property
    def lost(self) -> bool:
        return self.result in ("LOST", "HALF_LOST")


@dataclass(frozen=True, slots=True)
class CandidateBet:
    placed_at: datetime
    sport_key: str
    stake: float
    odds: float
    model_probability: float | None = None
    kelly_stake: float | None = None  # the sport-calibrated Kelly stake for this bet


@dataclass(frozen=True)
class DisciplineReport:
    score: float
    flags: tuple[str, ...]
    reasons: dict[str, str]
    core_sports: tuple[str, ...]
    disciplined: bool
    edge: float | None
    detail: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"score": self.score, "flags": list(self.flags), "reasons": self.reasons, "core_sports": list(self.core_sports),
                "disciplined": self.disciplined, "edge": None if self.edge is None else round(self.edge, 5), "detail": self.detail}


def core_sports(history: Sequence[BetRecord]) -> tuple[str, ...]:
    if len(history) < CORE_MIN_HISTORY:
        return tuple(sorted({b.sport_key for b in history}))
    counts = Counter(b.sport_key for b in history)
    total = sum(counts.values())
    core = sorted(s for s, n in counts.items() if n / total >= CORE_SHARE)
    return tuple(core) if core else (counts.most_common(1)[0][0],)


def _loss_streak(ordered: Sequence[BetRecord]) -> int:
    streak = 0
    for bet in reversed(ordered):
        if bet.result == "PENDING" or bet.result == "VOID":
            continue
        if bet.lost:
            streak += 1
        else:
            break
    return streak


def assess(candidate: CandidateBet, history: Sequence[BetRecord]) -> DisciplineReport:
    ordered = sorted((b for b in history if b.placed_at <= candidate.placed_at), key=lambda b: b.placed_at)
    flags: list[str] = []
    reasons: dict[str, str] = {}
    detail: dict[str, float] = {}

    streak = _loss_streak(ordered)
    detail["loss_streak"] = streak
    recent_stakes = [b.stake for b in ordered[-10:] if b.stake > 0]
    if streak >= TILT_MIN_LOSSES and recent_stakes:
        median = statistics.median(recent_stakes)
        detail["median_recent_stake"] = round(median, 2)
        if candidate.stake >= TILT_STAKE_RATIO * median:
            flags.append("TILT_STAKE_ESCALATION")
            reasons["TILT_STAKE_ESCALATION"] = (f"{streak} straight losses, then a stake of {candidate.stake:,.2f}: "
                                                f"{candidate.stake / median:.1f}x the recent median {median:,.2f}")

    core = core_sports(ordered)
    last_loss = next((b for b in reversed(ordered) if b.lost), None)
    if last_loss is not None and candidate.sport_key not in core and candidate.placed_at - last_loss.placed_at <= REVENGE_WINDOW:
        flags.append("REVENGE_NON_CORE")
        minutes = (candidate.placed_at - last_loss.placed_at).total_seconds() / 60.0
        reasons["REVENGE_NON_CORE"] = (f"{candidate.sport_key} is outside the core sports ({', '.join(core) or 'none'}) and comes "
                                       f"{minutes:.0f} minutes after a {last_loss.sport_key} loss")

    hour_ago = candidate.placed_at - timedelta(hours=1)
    day_ago = candidate.placed_at - timedelta(days=1)
    last_hour = sum(1 for b in ordered if b.placed_at > hour_ago) + 1
    day_pnl = sum(b.pnl for b in ordered if b.placed_at > day_ago and b.pnl is not None)
    hourly: Counter[datetime] = Counter(b.placed_at.replace(minute=0, second=0, microsecond=0) for b in ordered if b.placed_at <= hour_ago)
    usual_peak = max(hourly.values()) if hourly else 3
    detail.update({"bets_last_hour": last_hour, "usual_hourly_peak": usual_peak, "day_pnl": round(day_pnl, 2)})
    if day_pnl < 0 and last_hour > max(3, usual_peak):
        flags.append("LOSS_CHASING_VELOCITY")
        reasons["LOSS_CHASING_VELOCITY"] = f"{last_hour} bets in the last hour (usual peak {usual_peak}) while {day_pnl:,.2f} down on the day"

    if candidate.kelly_stake is not None:
        detail["kelly_stake"] = round(candidate.kelly_stake, 2)
        if candidate.stake > OVER_KELLY_RATIO * max(candidate.kelly_stake, 0.0):
            flags.append("OVER_KELLY")
            reasons["OVER_KELLY"] = f"stake {candidate.stake:,.2f} is over {OVER_KELLY_RATIO}x the sport Kelly stake {candidate.kelly_stake:,.2f}"

    edge = None
    if candidate.model_probability is not None:
        edge = candidate.model_probability * candidate.odds - 1.0
        if edge <= 0:
            flags.append("NEGATIVE_EV")
            reasons["NEGATIVE_EV"] = f"the model gives {candidate.model_probability:.1%} at {candidate.odds:.2f}: EV {edge:.2%}"

    score = max(0.0, 100.0 - sum(PENALTIES[f] for f in flags))
    disciplined = (not flags and edge is not None and edge > 0 and candidate.kelly_stake is not None
                   and candidate.stake <= DISCIPLINED_KELLY_RATIO * candidate.kelly_stake)
    return DisciplineReport(round(score, 2), tuple(flags), reasons, core, disciplined, edge, detail)
