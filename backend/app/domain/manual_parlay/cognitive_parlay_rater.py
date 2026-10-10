"""The cognitive parlay rater (Group 77): a manual parlay's 0-100 score and tier, from the fortress's own 15 pillars.

A parlay the user builds by hand goes through exactly the fortress Ashoka's slips go through (the twin's vetting
run: model consensus, weather, travel, injuries, lineups, steam and reverse line movement, the de-vigged sharp
price, the crowd's parlays, the referee, motivation, liquidity, leg independence, sizing, the execution gate and
the Never-Forget shield). The rater turns that audit into a score:

    score = 100 x sum(credit[status_i]) / pillars        credit: PASS 1, ADVISORY 0.5, UNVERIFIED 0.25, FAIL 0

``MANUAL_PARLAY_PILLAR_CREDIT``. A pillar without evidence earns a little (it is unknown, not wrong) but never a
pass. One failed pillar caps the score at ``MANUAL_PARLAY_FAIL_CAP``: a parlay a pillar vetoes (a negative-EV
leg, correlated legs, a past trap) cannot rate well however the rest look. Tiers come from ``MANUAL_PARLAY_TIERS``
(PERFECT 95, EXTRAORDINARY 85, BRILLIANT 75, GOOD 60, AVERAGE 45, POOR below).

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
class Rating:
    score: float
    tier: str
    capped: bool
    breakdown: list[dict[str, Any]]
    advice: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "tier": self.tier, "capped": self.capped, "breakdown": self.breakdown, "advice": self.advice, "warnings": self.warnings}


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
