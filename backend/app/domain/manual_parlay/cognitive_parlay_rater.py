"""The cognitive parlay rater (Group 77): a manual parlay's 0-100 score and tier, from the fortress's own 15 pillars.

A parlay the user builds by hand goes through exactly the fortress Ashoka's slips go through (the twin's vetting
run: model consensus, weather, travel, injuries, lineups, steam and reverse line movement, the de-vigged sharp
price, the crowd's parlays, the referee, motivation, liquidity, leg independence, sizing, the execution gate and
the Never-Forget shield). The rater turns that audit into a score:

    score = 100 x sum(credit[status_i]) / pillars        credit: PASS 1, ADVISORY 0.5, UNVERIFIED 0.25, FAIL 0

``MANUAL_PARLAY_PILLAR_CREDIT``. A pillar without evidence earns a little (it is unknown, not wrong) but never a
pass. One failed pillar caps the score at ``MANUAL_PARLAY_FAIL_CAP``: a parlay a pillar vetoes (a negative-EV
leg, correlated legs, a past trap) cannot rate well however the rest look. Tiers come from ``MANUAL_PARLAY_TIERS``
(Group 104: DIAMOND FORTRESS 90, GOLD GRADE 75, SILVER CAUTION 55, BRONZE / DANGER TRAP below).

The BetDoc Slip Score (Group 104, point b.5) is that fortress score with badges on top, each earned from what the
inspection measured and worth ``MANUAL_PARLAY_BADGE_POINTS``: Vig Crusher (the legs' mean margin at the account's book
at most ``MANUAL_PARLAY_VIG_CRUSHER_MAX``), Heavy Juice Trap (at least ``MANUAL_PARLAY_HEAVY_JUICE_MIN``), Same-Match Tax
(two legs on one fixture), Zero-Correlation Shield (two or more legs, the independence pillar passed), Score-Synced
(every leg has a scoreline model and wins at its fixture's likeliest score) and Positive EV King (the Monte Carlo
joint EV at least ``MANUAL_PARLAY_EV_KING_MIN``). The sum is clamped to 0-100 and a vetoed slip stays under the cap.

Margins are measured, never assumed: a book's overround on a leg is ``sum(1 / odds) - 1`` over the market's
selections it quotes, and the best-price line (each selection at the best book) is what line shopping achieves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# What each failed or unverified pillar asks of the user, by pillar key (the fortress's own words are the reason)
TACTICS: dict[str, tuple[str, str]] = {
    "model_consensus": ("Drop or replace the leg the models price at a loss: no edge, no bet.", "Some leg is not priced by enough models to judge its edge."),
    "weather": ("Swap the weather-exposed leg (wind and rain decide totals and underdogs).", "No fresh weather reading for an outdoor fixture."),
    "travel_fatigue": ("Avoid backing the side on a short turnaround or a delayed charter.", "No travel and rest evidence yet."),
    "injuries": ("A key absence changes the price: re-check the leg after the team news.", "The injury wire has not reported for every fixture."),
    "lineups": ("Wait for both official team sheets before staking.", "Lineups are not out yet: re-inspect closer to kickoff."),
    "market_microstructure": ("The sharp money is moving against a leg: drop it, or wait for the move to settle.", "The steam feed cannot be read right now."),
    "sharp_price": ("Line-shop: the price does not beat the de-vigged sharp line by enough.", "No fresh complete sharp market to compare the price with."),
    "crowd_parlays": ("A leg sits in a public-trap parlay: the crowd's favourite is rarely the value.", "No crowd data."),
    "referee": ("A penalty-prone referee: avoid totals and handicaps on that fixture.", "The referee is not known yet."),
    "motivation": ("The opponent wants this more: drop the leg or demand a bigger edge.", "No motivation evidence."),
    "liquidity": ("The stake is over what the book takes on this fixture: stake less or split.", "The book's maximum stake is not known."),
    "independence": ("Correlated legs: one fixture twice, or one team twice inside 36 hours. Keep one.", "Correlation could not be checked."),
    "sizing": ("The drawdown regime or the Kelly fraction leaves no stake: wait.", "No bankroll to size against: fund the CFO main account."),
    "execution_gate": ("Trading is halted or a price is stale: re-price before placing.", "The kill switch cannot be read: nothing executes."),
    "never_forget": ("This situation lost before: a memorised lesson vetoes it.", "A lesson could not be compared: the evidence it needs is missing."),
}


@dataclass(frozen=True, slots=True)
class RaterPolicy:
    credits: Mapping[str, float]
    fail_cap: float
    tiers: tuple[tuple[str, float], ...]  # (name, floor), highest floor first

    @classmethod
    def from_settings(cls, settings: Any) -> RaterPolicy:
        tiers = tuple(sorted(((str(k), float(v)) for k, v in settings.MANUAL_PARLAY_TIERS.items()), key=lambda t: -t[1]))
        if not tiers or tiers[-1][1] != 0.0:
            raise ValueError("MANUAL_PARLAY_TIERS: the lowest tier starts at 0")
        return cls({k.upper(): float(v) for k, v in settings.MANUAL_PARLAY_PILLAR_CREDIT.items()}, float(settings.MANUAL_PARLAY_FAIL_CAP), tiers)


@dataclass(frozen=True, slots=True)
class Badge:
    code: str
    label: str
    points: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "points": self.points, "positive": self.points > 0, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Rating:
    score: float
    tier: str
    capped: bool
    breakdown: list[dict[str, Any]]
    advice: list[str]
    warnings: list[str]
    fortress_score: float | None = None  # the 15 pillars' own score, before badges (None: no badges applied)
    badges: tuple[Badge, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "tier": self.tier, "capped": self.capped, "breakdown": self.breakdown, "advice": self.advice, "warnings": self.warnings,
                "fortress_score": self.fortress_score, "badges": [b.as_dict() for b in self.badges]}


def tier_for(score: float, policy: RaterPolicy) -> str:
    return next(name for name, floor in policy.tiers if score >= floor - 1e-9)


def rate(pillars: Sequence[Mapping[str, Any]], policy: RaterPolicy) -> Rating:
    """The rating of a fortress audit's pillars (``PillarResult.as_dict`` rows)."""
    if not pillars:
        raise ValueError("no pillar to rate")
    breakdown, advice, warnings = [], [], []
    total = 0.0
    failed = False
    for p in pillars:
        status = str(p.get("status", "UNVERIFIED")).upper()
        credit = float(policy.credits.get(status, 0.0))
        total += credit
        key = str(p.get("key", ""))
        breakdown.append({"number": p.get("number"), "key": key, "title": p.get("title"), "status": status, "credit": credit, "reason": p.get("reason", "")})
        fix, missing = TACTICS.get(key, ("Review this pillar's reason.", "Evidence for this pillar is missing."))
        if status == "FAIL":
            failed = True
            advice.append(f"{p.get('title')}: {fix} ({p.get('reason', '')})")
            if key == "independence":
                warnings.append(f"Correlation: {p.get('reason', '')}")
        elif status == "UNVERIFIED":
            advice.append(f"{p.get('title')}: {missing}")
    raw = round(100.0 * total / len(pillars), 2)
    score = min(raw, policy.fail_cap) if failed else raw
    return Rating(score, tier_for(score, policy), failed and raw > policy.fail_cap, breakdown, advice, warnings)


# ================================================================ the Slip Score (Group 104)
LABELS: dict[str, str] = {
    "VIG_CRUSHER": "⚡ Vig Crusher", "HEAVY_JUICE": "🪤 Heavy Juice Trap", "SAME_MATCH_TAX": "⚠️ Same-Match Tax",
    "ZERO_CORRELATION": "🛡️ Zero-Correlation Shield", "SCORE_SYNCED": "🎯 Score-Synced", "EV_KING": "💎 Positive EV King",
}


@dataclass(frozen=True, slots=True)
class BadgePolicy:
    points: Mapping[str, float]
    vig_crusher_max: float
    heavy_juice_min: float
    ev_king_min: float

    @classmethod
    def from_settings(cls, settings: Any) -> BadgePolicy:
        return cls({str(k).upper(): float(v) for k, v in settings.MANUAL_PARLAY_BADGE_POINTS.items()}, float(settings.MANUAL_PARLAY_VIG_CRUSHER_MAX),
                   float(settings.MANUAL_PARLAY_HEAVY_JUICE_MIN), float(settings.MANUAL_PARLAY_EV_KING_MIN))


@dataclass(frozen=True, slots=True)
class SlipEvidence:
    """What the inspection measured that the badges read, one entry per leg."""

    fixtures: Sequence[str]
    margins: Sequence[float | None]  # each leg's overround at the account's book (None: the book does not quote its whole market)
    score_sync: Sequence[bool | None]  # wins (or half wins) at its fixture's likeliest score; None: no scoreline model
    joint_ev: float | None


def badges(pillars: Sequence[Mapping[str, Any]], evidence: SlipEvidence, policy: BadgePolicy) -> list[Badge]:
    out: list[Badge] = []

    def earn(code: str, detail: str) -> None:
        if code in policy.points:
            out.append(Badge(code, LABELS[code], policy.points[code], detail))

    known = [m for m in evidence.margins if m is not None]
    if known and len(known) == len(evidence.margins):
        mean = sum(known) / len(known)
        if mean <= policy.vig_crusher_max:
            earn("VIG_CRUSHER", f"the legs' margin at this book averages {mean:.1%} (at most {policy.vig_crusher_max:.1%})")
        elif mean >= policy.heavy_juice_min:
            earn("HEAVY_JUICE", f"the legs' margin at this book averages {mean:.1%} (from {policy.heavy_juice_min:.1%} it is a trap)")
    repeated = sorted({f for f in evidence.fixtures if list(evidence.fixtures).count(f) > 1})
    independence = next((str(p.get("status", "")).upper() for p in pillars if p.get("key") == "independence"), None)
    if repeated:
        earn("SAME_MATCH_TAX", f"{len(repeated)} fixture{'s' if len(repeated) > 1 else ''} carr{'y' if len(repeated) > 1 else 'ies'} two or more legs: their results move together")
    elif len(evidence.fixtures) >= 2 and independence == "PASS":
        earn("ZERO_CORRELATION", "every leg on a different fixture, and the independence pillar passed")
    if evidence.score_sync and all(s is True for s in evidence.score_sync):
        earn("SCORE_SYNCED", "every leg wins at its fixture's likeliest score (the market-fitted Dixon-Coles matrix)")
    if evidence.joint_ev is not None and evidence.joint_ev >= policy.ev_king_min:
        earn("EV_KING", f"joint EV {evidence.joint_ev:+.1%} over the Monte Carlo paths (from {policy.ev_king_min:+.0%})")
    return out


def slip_score(rating: Rating, earned: Sequence[Badge], policy: RaterPolicy) -> Rating:
    """The BetDoc Slip Score: the fortress score plus the badges' points, clamped to 0-100; a vetoed slip stays under the cap."""
    failed = any(row["status"] == "FAIL" for row in rating.breakdown)
    raw = max(0.0, min(100.0, rating.score + sum(b.points for b in earned)))
    score = round(min(raw, policy.fail_cap) if failed else raw, 2)
    return Rating(score, tier_for(score, policy), failed and raw > policy.fail_cap, rating.breakdown, rating.advice, rating.warnings,
                  fortress_score=rating.score, badges=tuple(earned))


# ================================================================ margins
def overround(prices: Mapping[str, float]) -> float | None:
    """sum(1 / odds) - 1 over one book's prices for every selection of a market; None when any is missing."""
    if not prices or any(o <= 1.0 for o in prices.values()):
        return None
    return sum(1.0 / o for o in prices.values()) - 1.0


def market_margins(selections: Sequence[str], quotes: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    """``quotes``: selection -> book -> odds. Each book's overround (books quoting every selection) and the best-price line's."""
    books = sorted({b for per in quotes.values() for b in per})
    per_book: dict[str, float] = {}
    for book in books:
        prices = {s: quotes.get(s, {}).get(book) for s in selections}
        if all(v is not None for v in prices.values()):
            margin = overround(prices)  # type: ignore[arg-type]
            if margin is not None:
                per_book[book] = round(margin, 6)
    best = {s: max(quotes.get(s, {}).values(), default=0.0) for s in selections}
    best_line = overround(best) if all(v > 1.0 for v in best.values()) else None
    return {"by_book": per_book, "best_price": None if best_line is None else round(best_line, 6),
            "best_books": {s: max(quotes.get(s, {}).items(), key=lambda kv: kv[1])[0] for s in selections if quotes.get(s)}}
