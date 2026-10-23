"""The street-smart synthesizer, the three-ticket system splitter and the day's market score (Group 115, points b.5 and
b.16): pure maths over legs already priced at one book. Developed for Amit Ashok Kumar Patnaik.

* ``synthesize``: the accumulator punter's playbook, measured. The safest legs on the board (the consensus's
  probability, highest first), at most one per fixture (two legs of one match move together), no sport filling more
  than its share of the slip while another sport is priced (a washed-out pitch or a red card should not sink every leg),
  until the slip's odds reach the target (stake -> return) with at least ``min_legs`` legs. A target the anchors cannot
  reach is said so, with the odds they do reach; nothing is added to make it.
* ``split``: N legs into three nested tickets, safest first: Slip A (Fortress) the fewest anchors whose odds cover the
  whole session stake at A's share (3 to 5 legs; when none do, the shield is reported off), Slip B (Growth) about two
  thirds of the legs (up to 9), Slip C (Moonshot) all of them. The tickets are nested, so the four outcomes are exact:
  every leg wins; A and B pay but C loses; only A pays; and an anchor loses, which loses all three. The session's
  expected return and the chance of getting the stake back come from those four, beside one ticket of all N legs.
* ``score_day``: 100 less the measured deductions (the desk books' overround, a thin slate, rotation and absences, no
  anchors), a tier from the thresholds, and TODAR MAL's advice in words drawn from the deductions themselves.

Probabilities multiply across fixtures (the legs are of different matches: independence is the stated assumption).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

PAISA = Decimal("0.01")
ODDS = Decimal("0.0001")
ZERO = Decimal("0.00")
TIER_NAMES = {"A": ("Fortress Anchor Slip", "CAPITAL SHIELD"), "B": ("Growth Multi-Sport Slip", "ACCUMULATOR COMPOUNDER"),
              "C": ("Moonshot Accumulator", "JACKPOT STRIKE")}


def paisa(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PricedLeg:
    """One board leg at one book: its price there, the consensus probability, the EV of that price."""

    leg_id: str
    fixture_id: str
    home: str
    away: str
    sport_key: str | None
    sport: str  # the family's label (Football, Cricket ...)
    league: str | None
    kickoff: datetime | None
    market: str  # Ashoka's market key
    selection: str
    label: str
    book: str
    odds: Decimal
    probability: float
    ev: float
    consensus_books: int = 0
    code: str = ""  # the book's own code for the selection

    @property
    def fixture(self) -> str:
        return f"{self.home} vs {self.away}"

    def as_dict(self) -> dict[str, Any]:
        return {"leg_id": self.leg_id, "fixture_id": self.fixture_id, "fixture": self.fixture, "home": self.home, "away": self.away,
                "sport_key": self.sport_key, "sport": self.sport, "league": self.league, "kickoff": None if self.kickoff is None else self.kickoff.isoformat(),
                "market": self.market, "selection": self.selection, "label": self.label, "book": self.book, "code": self.code, "odds": str(self.odds),
                "fair_probability": round(self.probability, 4), "ev": round(self.ev, 4), "consensus_books": self.consensus_books}


def product(values: Iterable[Decimal]) -> Decimal:
    out = Decimal(1)
    for v in values:
        out *= v
    return out


def _order(legs: Iterable[PricedLeg]) -> list[PricedLeg]:
    """Safest first: probability, then EV, then the earlier kickoff (a deterministic order)."""
    return sorted(legs, key=lambda leg: (-leg.probability, -leg.ev, leg.kickoff.isoformat() if leg.kickoff else "~", leg.leg_id))


def one_per_fixture(legs: Iterable[PricedLeg]) -> list[PricedLeg]:
    best: dict[str, PricedLeg] = {}
    for leg in _order(legs):
        best.setdefault(leg.fixture_id, leg)
    return _order(best.values())


# ================================================================ the synthesizer
@dataclass(slots=True)
class Synthesis:
    legs: list[PricedLeg]
    stake: Decimal
    target_return: Decimal
    target_odds: Decimal
    total_odds: Decimal
    projected_return: Decimal
    joint_probability: float
    joint_ev: float
    expected_return: Decimal
    reached_target: bool
    diversification_relaxed: bool
    anchors_available: int
    shortfall: str | None = None

    @property
    def sports(self) -> list[str]:
        return sorted({leg.sport for leg in self.legs})


def synthesize(anchors: Sequence[PricedLeg], *, stake: Decimal, target_return: Decimal, min_legs: int, max_legs: int, max_sport_share: float,
               preferred_sports: Iterable[str] | None = None) -> Synthesis:
    if stake <= 0 or target_return <= 0:
        raise ValueError("the stake and the target return must be positive")
    wanted = {s.casefold() for s in preferred_sports or () if s.strip()}
    pool = one_per_fixture(leg for leg in anchors if not wanted or leg.sport.casefold() in wanted)
    target_odds = (target_return / stake).quantize(ODDS, rounding=ROUND_HALF_UP)
    sports = {leg.sport for leg in pool}
    cap = max(1, math.ceil(max_legs * max_sport_share)) if len(sports) > 1 else max_legs
    chosen: list[PricedLeg] = []
    held: list[PricedLeg] = []
    per_sport: Counter[str] = Counter()

    def done() -> bool:
        return len(chosen) >= max_legs or (len(chosen) >= min_legs and product(leg.odds for leg in chosen) >= target_odds)

    for leg in pool:
        if done():
            break
        if per_sport[leg.sport] >= cap:
            held.append(leg)
            continue
        chosen.append(leg)
        per_sport[leg.sport] += 1
    relaxed = False
    for leg in held:  # a one-sport fill, only when the other sports ran out before the target
        if done():
            break
        chosen.append(leg)
        relaxed = True
    total = product(leg.odds for leg in chosen).quantize(ODDS, rounding=ROUND_HALF_UP) if chosen else Decimal(1)
    joint = math.prod(leg.probability for leg in chosen) if chosen else 0.0
    reached = bool(chosen) and len(chosen) >= min_legs and total >= target_odds
    shortfall = None
    if not pool:
        shortfall = "No leg on the live board is an anchor at this book right now: nothing to build from."
    elif not reached:
        stop = f"the {max_legs}-leg limit" if len(chosen) >= max_legs else f"the board's {len(pool)} anchor fixture{'s' if len(pool) != 1 else ''}"
        shortfall = (f"{stop[:1].upper() + stop[1:]} reach{'es' if len(chosen) >= max_legs else ''} {total:.2f}x with {len(chosen)} leg{'s' if len(chosen) != 1 else ''}, "
                     f"short of the {target_odds:.2f}x target{f' and of the {min_legs}-leg minimum' if len(chosen) < min_legs else ''}. "
                     "Lower the target, or wait for a fuller board.")
    projected = paisa(stake * total) if chosen else ZERO
    return Synthesis(chosen, stake, target_return, target_odds, total, projected, joint, joint * float(total) - 1.0 if chosen else 0.0,
                     paisa(projected * Decimal(str(joint))) if chosen else ZERO, reached, relaxed, len(pool), shortfall)


# ================================================================ the system splitter
@dataclass(slots=True)
class SplitSlip:
    tier: str  # A | B | C
    legs: list[PricedLeg]
    stake: Decimal
    odds: Decimal
    payout: Decimal
    probability: float
    covers_session: bool = False

    @property
    def name(self) -> str:
        return TIER_NAMES[self.tier][0]

    @property
    def tag(self) -> str:
        return TIER_NAMES[self.tier][1]

    @property
    def ev(self) -> float:
        return self.probability * float(self.odds) - 1.0


@dataclass(slots=True)
class Split:
    session_stake: Decimal
    slips: list[SplitSlip]
    needed_a_odds: Decimal  # the odds at which Slip A alone returns the session stake
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    expected_return: Decimal = ZERO
    recover_probability: float = 0.0  # the chance the session returns at least its stake
    single: dict[str, Any] = field(default_factory=dict)  # the same stake on one ticket of every leg

    @property
    def shield_active(self) -> bool:
        return self.slips[0].covers_session


def split(legs: Sequence[PricedLeg], session_stake: Decimal, shares: Sequence[Decimal], *, min_legs: int = 5) -> Split:
    ordered = _order(legs)
    n = len(ordered)
    if n < max(min_legs, 5):
        raise ValueError(f"a three-ticket split needs at least {max(min_legs, 5)} legs, got {n}")
    if len({leg.fixture_id for leg in ordered}) != n:
        raise ValueError("two legs of one fixture move together: one leg per fixture")
    if len(shares) != 3 or any(s <= 0 for s in shares) or sum(shares) != 1:
        raise ValueError("three positive stake shares that sum to 1")
    stake_a, stake_b = paisa(session_stake * shares[0]), paisa(session_stake * shares[1])
    stake_c = session_stake - stake_a - stake_b
    if min(stake_a, stake_b, stake_c) <= 0:
        raise ValueError("the session stake is too small to split")
    needed = (session_stake / stake_a).quantize(ODDS, rounding=ROUND_HALF_UP)
    most_a = min(5, n - 2)
    count_a = next((k for k in range(3, most_a + 1) if product(leg.odds for leg in ordered[:k]) >= needed), most_a)
    count_b = max(count_a + 1, min(9, max(count_a + 2, math.ceil(2 * n / 3)), n - 1))
    slips: list[SplitSlip] = []
    for tier, count, stake in (("A", count_a, stake_a), ("B", count_b, stake_b), ("C", n, stake_c)):
        chosen = ordered[:count]
        odds = product(leg.odds for leg in chosen).quantize(ODDS, rounding=ROUND_HALF_UP)
        slips.append(SplitSlip(tier, chosen, stake, odds, paisa(stake * odds), math.prod(leg.probability for leg in chosen)))
    a, b, c = slips
    a.covers_session = a.payout >= session_stake
    only_c, only_b = n - len(b.legs), len(b.legs) - len(a.legs)
    outcomes = (
        ("ALL_WIN", "Every leg wins: all three tickets pay", c.probability, a.payout + b.payout + c.payout),
        ("A_AND_B", f"Slips A and B pay; one of the {only_c} leg{'s' if only_c != 1 else ''} only Slip C carries loses", b.probability - c.probability,
         a.payout + b.payout),
        ("A_ONLY", f"Only Slip A pays; one of the {only_b} leg{'s' if only_b != 1 else ''} Slip B adds loses", a.probability - b.probability, a.payout),
        ("NONE", f"One of Slip A's {len(a.legs)} anchors loses: it is on every ticket, so all three lose", 1.0 - a.probability, ZERO),
    )
    scenarios = [{"code": code, "label": label, "probability": round(max(p, 0.0), 6), "return_inr": str(back), "net_inr": str(back - session_stake)}
                 for code, label, p, back in outcomes]
    expected = paisa(sum((Decimal(str(max(p, 0.0))) * back for _, _, p, back in outcomes), ZERO))
    recover = sum(max(p, 0.0) for _, _, p, back in outcomes if back >= session_stake)
    single_payout = paisa(session_stake * c.odds)
    single = {"legs": n, "odds": str(c.odds), "payout_inr": str(single_payout), "probability": round(c.probability, 6),
              "expected_return_inr": str(paisa(single_payout * Decimal(str(c.probability)))), "recover_probability": round(c.probability, 6)}
    return Split(session_stake, slips, needed, scenarios, expected, recover, single)


# ================================================================ the day's market score
@dataclass(slots=True)
class HealthInputs:
    fixtures: int  # kicking off today, priced at a desk book
    top_tier_fixtures: int | None  # None: no league tier is known for today's fixtures
    margin: float | None  # the desk books' average Match Odds overround; None: no complete market
    margins_measured: int
    rotation: int | None  # fixtures with rotation news or a depleted squad; None: the Wire has read nothing recent
    anchors: list[PricedLeg]
    min_anchor_legs: int


@dataclass(slots=True)
class HealthCall:
    score: int
    tier: str
    deductions: list[dict[str, Any]]
    unmeasured: list[str]
    title: str
    body: str

    @property
    def message(self) -> str:
        return f"{self.title} {self.body}"


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    rest_below: int = 60
    prime_from: int = 80
    margin_elevated: float = 0.055
    margin_severe: float = 0.075


def score_day(inputs: HealthInputs, thresholds: HealthThresholds) -> HealthCall:
    deductions: list[dict[str, Any]] = []
    unmeasured: list[str] = []

    def take(code: str, points: int, reason: str) -> None:
        deductions.append({"code": code, "points": points, "reason": reason})

    if inputs.margin is None:
        unmeasured.append("bookmaker margin: no complete Match Odds market at Parimatch or 1xBet today")
    elif inputs.margin > thresholds.margin_severe:
        take("MARGIN_SEVERE", 28, f"excessive bookmaker juice ({inputs.margin:.1%} average Match Odds overround)")
    elif inputs.margin > thresholds.margin_elevated:
        take("MARGIN_ELEVATED", 14, f"elevated bookmaker margin ({inputs.margin:.1%} average Match Odds overround)")
    depth = inputs.top_tier_fixtures
    if depth is None:
        depth = inputs.fixtures
        if inputs.fixtures:
            unmeasured.append("league tiers: none of today's leagues is in the registry, so every priced fixture counts")
    if inputs.fixtures == 0:
        take("SLATE_EMPTY", 30, "the live board prices no fixture at Parimatch or 1xBet for the rest of today")
    elif depth < 3:
        take("SLATE_THIN", 30, f"a thin slate ({depth} top-tier fixture{'s' if depth != 1 else ''} today)")
    elif depth < 6:
        take("SLATE_MODERATE", 10, f"moderate fixture depth ({depth} top-tier fixtures today)")
    if inputs.rotation is None:
        unmeasured.append("rotation and absences: the Wire has read no news or squad lists recently")
    elif inputs.rotation >= 4:
        take("ROTATION_HEAVY", 25, f"heavy squad rotation or absences across {inputs.rotation} of today's fixtures")
    elif inputs.rotation >= 2:
        take("ROTATION_MINOR", 10, f"squad rotation or absences in {inputs.rotation} of today's fixtures")
    anchors = len(inputs.anchors)
    if anchors == 0:
        take("VALUE_NONE", 20, "no anchor leg is priced at or above fair")
    elif anchors < inputs.min_anchor_legs:
        take("VALUE_THIN", 10, f"only {anchors} anchor leg{'s' if anchors != 1 else ''} priced at or above fair")
    score = max(0, min(100, 100 - sum(d["points"] for d in deductions)))
    reasons = "; ".join(d["reason"] for d in deductions)
    if score < thresholds.rest_below:
        tier = "REST_DAY"
        title = f"⏸️ TODAR MAL ADVICE: NO BETTING TODAY ({score}/100)."
        body = (f"Unfavourable market conditions: {reasons}. True mastery is capital preservation. Sit out today and await optimal conditions.")
    elif score < thresholds.prime_from:
        tier = "SELECTIVE"
        title = f"⚠️ SELECTIVE WINDOW ({score}/100)."
        body = (f"{reasons[:1].upper() + reasons[1:]}. " if reasons else "") + "Keep stakes light and stick strictly to the anchors."
    else:
        tier = "PRIME_HARVEST"
        names = " + ".join(f"{leg.label} ({leg.sport})" for leg in inputs.anchors[:3])
        title = f"🔥 PRIME MULTI-SPORT OPPORTUNITY ({score}/100)."
        body = (f"{names}: {anchors} anchor{'s' if anchors != 1 else ''} priced at or above fair across {inputs.fixtures} fixtures. "
                + (f"{reasons[:1].upper() + reasons[1:]}. " if reasons else "")
                + "Open the Street-Smart Synthesizer on BetDoc; you place every slip yourself.")
    return HealthCall(score, tier, deductions, unmeasured, title, body)


__all__ = ["HealthCall", "HealthInputs", "HealthThresholds", "PricedLeg", "Split", "SplitSlip", "Synthesis", "one_per_fixture", "paisa", "product",
           "score_day", "split", "synthesize"]
