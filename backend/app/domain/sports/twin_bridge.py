"""The sports engine's feed into the True Digital Betting Twin (Group 79). Developed for Amit Ashok Kumar Patnaik.

The brief names its pillars by intent; the fortress (``app.domain.oracle.fortress``) numbers them differently.
This module produces what each pillar consumes, for any of the nine sports:

=================================  ===================  ==================================================
Brief                              Fortress pillar      Here
=================================  ===================  ==================================================
Pillar 1  multi-model consensus    P1 model_consensus   ``consensus_verdict``: weighted EV over >= 3 sub-models,
                                                        any voting sub-model at EV <= 0 vetoes
Pillar 4  weather & pitch physics  P2 weather           adapters read ``context.weather`` (``MatchContext``)
Pillar 5  absences & WAR           P4 injuries          adapters read ``context.absences``
Pillar 6  official bias            P9 referee           adapters read ``context.officials``
Pillar 9  sharp benchmark          P7 sharp_price       ``sharp_benchmark``: Shin de-vig of Pinnacle, else the
                                                        Betfair exchange net of commission
Pillar 15 confidence & sizing      P13 sizing (+ the    ``confidence_index`` (0-100) and ``sport_kelly_sizing``
                                   fortress conviction)  (FA-4: the sport's Kelly multiplier as kappa)
FA-8      OmniRoute                -                    ``route_best_price``: the best net price among Pinnacle,
                                                        Betfair, Parimatch, Stake and 1xBet
=================================  ===================  ==================================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.bookmakers.adapters import canonical_bookmaker
from app.domain.cfo import growth_math as gm
from app.domain.oracle.fortress import shin_devig
from app.domain.sports.quant import SubModelEstimate, clamp

PASS, FAIL, UNVERIFIED = "PASS", "FAIL", "UNVERIFIED"

# FA-4: fractional Kelly multipliers calibrated to each model's parameter uncertainty
SPORT_KELLY_MULTIPLIERS: dict[str, float] = {
    "soccer": 0.35,  # Bivariate Poisson
    "cricket": 0.25,  # DLS / Markov
    "basketball": 0.40,  # Four Factors
    "tennis": 0.30,  # O'Malley Markov
    "baseball": 0.35,  # BaseRuns
    "american_football": 0.50,  # EPA key numbers
    "ice_hockey": 0.30,  # xG Poisson
    "combat": 0.20,  # strike differential
    "esports": 0.25,  # map veto Markov
}

OMNI_VENUES: tuple[str, ...] = ("pinnacle", "betfair", "parimatch", "stake", "1xbet")
SHARP_BOOKS: tuple[str, ...] = ("pinnacle", "betfair")
RETAIL_BOOKS: tuple[str, ...] = ("parimatch", "stake", "1xbet")
PRIMARY_CAPITAL_VENUE = "parimatch"  # the bankroll sits here: singles, Asian handicaps and match winners route here first
MULTI_VENUES: tuple[str, ...] = ("1xbet", "stake")  # accumulators, multi-bets and player props
EXCHANGES = frozenset({"betfair"})
DEFAULT_COMMISSION: dict[str, float] = {"betfair": 0.05}

# Which evidence sections each sport's model can use (the confidence index only counts the relevant ones)
WEATHER_SPORTS = frozenset({"soccer", "cricket", "tennis", "baseball", "american_football"})
ABSENCE_SPORTS = frozenset({"soccer", "basketball", "american_football", "baseball", "ice_hockey"})
OFFICIAL_SPORTS = frozenset({"soccer", "basketball", "baseball", "ice_hockey", "combat", "american_football"})


def net_odds(odds: float, commission: float = 0.0) -> float:
    return 1.0 + (odds - 1.0) * (1.0 - commission)


# ================================================================ pillar 1: consensus
@dataclass(frozen=True, slots=True)
class ConsensusVerdict:
    status: str
    consensus_ev: float
    model_evs: dict[str, float]
    weights: dict[str, float]
    voting: int
    vetoes: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "consensus_ev": round(self.consensus_ev, 5), "model_evs": {k: round(v, 5) for k, v in self.model_evs.items()},
                "weights": {k: round(v, 4) for k, v in self.weights.items()}, "voting": self.voting, "vetoes": list(self.vetoes), "reason": self.reason}


def consensus_verdict(sub_models: Sequence[SubModelEstimate], side: str, odds: float, weights: Mapping[str, float] | None = None, *,
                      min_models: int = 3, min_ev: float = 0.045) -> ConsensusVerdict:
    """Fortress pillar 1 on a sport's sub-models: each model's EV of backing ``side`` at ``odds`` (net), weighted by the
    calibrated weights (unknown model: 1, benched: 0). Fewer than ``min_models`` voting: UNVERIFIED; a voting model
    at EV <= 0 or a consensus under ``min_ev``: FAIL."""
    if side not in ("home", "away", "draw"):
        raise ValueError("side must be home, away or draw")
    evs: dict[str, float] = {}
    for m in sub_models:
        e = m.normalised()
        p = {"home": e.home_win_prob, "away": e.away_win_prob, "draw": e.draw_prob}[side]
        evs[m.name] = p * odds - 1.0
    w = {name: max(0.0, float((weights or {}).get(name, 1.0))) for name in evs}
    voting = [n for n in evs if w[n] > 0]
    if len(voting) < min_models:
        return ConsensusVerdict(UNVERIFIED, 0.0, evs, w, len(voting), (), f"{len(voting)} voting sub-models, {min_models} required")
    total = sum(w[n] for n in voting)
    consensus = sum(w[n] * evs[n] for n in voting) / total
    vetoes = tuple(n for n in voting if evs[n] <= 0.0)
    if vetoes:
        return ConsensusVerdict(FAIL, consensus, evs, w, len(voting), vetoes, f"{', '.join(vetoes)} price the selection at no edge")
    if consensus < min_ev:
        return ConsensusVerdict(FAIL, consensus, evs, w, len(voting), (), f"consensus EV {consensus:.2%} under the {min_ev:.2%} bar")
    return ConsensusVerdict(PASS, consensus, evs, w, len(voting), (), f"{len(voting)} sub-models agree: consensus EV {consensus:.2%}")


# ================================================================ pillar 9 (fortress P7): the sharp benchmark
@dataclass(frozen=True)
class SharpBenchmark:
    book: str
    fair_probabilities: dict[str, float]
    shin_z: float
    overround: float
    retail_edges: dict[str, dict[str, float]] = field(default_factory=dict)  # venue -> outcome -> net odds / fair odds - 1

    def best_retail(self, outcome: str) -> tuple[str, float] | None:
        rows = [(venue, edges[outcome]) for venue, edges in self.retail_edges.items() if outcome in edges]
        return max(rows, key=lambda r: r[1]) if rows else None

    def as_dict(self) -> dict[str, Any]:
        return {"book": self.book, "fair_probabilities": {k: round(v, 5) for k, v in self.fair_probabilities.items()}, "shin_z": round(self.shin_z, 5),
                "overround": round(self.overround, 5), "retail_edges": {v: {o: round(e, 5) for o, e in r.items()} for v, r in self.retail_edges.items()}}


def canonical_quotes(venue_odds: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """Feed bookmaker keys folded onto the five OmniRoute venues (``onexbet`` -> ``1xbet``); other books keep their key.
    Two feed keys for one venue keep the better price per outcome."""
    out: dict[str, dict[str, float]] = {}
    for raw, quotes in venue_odds.items():
        venue = canonical_bookmaker(raw) or str(raw).strip().casefold()
        bucket = out.setdefault(venue, {})
        for outcome, price in quotes.items():
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if value > 1.0 and value == value and value < 1e6:
                bucket[outcome] = max(bucket.get(outcome, 0.0), value)
    return out


def sharp_benchmark(venue_odds: Mapping[str, Mapping[str, float]], outcomes: Sequence[str], *, sharp_books: Sequence[str] = SHARP_BOOKS,
                    commission: Mapping[str, float] | None = None) -> SharpBenchmark | None:
    """The first sharp book quoting every outcome, Shin de-vigged (an exchange's prices net of commission first); then
    every other venue's edge over it. None when no sharp book has a complete market."""
    quotes = canonical_quotes(venue_odds)
    fees = {**DEFAULT_COMMISSION, **(commission or {})}
    for book in sharp_books:
        market = quotes.get(book)
        if not market or any(o not in market for o in outcomes):
            continue
        prices = [net_odds(market[o], fees.get(book, 0.0)) if book in EXCHANGES else market[o] for o in outcomes]
        try:
            fair, z = shin_devig(prices)
        except ValueError:
            continue
        fair_by = dict(zip(outcomes, fair, strict=True))
        edges: dict[str, dict[str, float]] = {}
        for venue, row in quotes.items():
            if venue == book:
                continue
            fee = fees.get(venue, 0.0)
            edges[venue] = {o: net_odds(row[o], fee) * fair_by[o] - 1.0 for o in outcomes if o in row}
        return SharpBenchmark(book, fair_by, z, sum(1.0 / p for p in prices) - 1.0, edges)
    return None


# ================================================================ FA-4: sport-calibrated sizing
def sport_kelly_multiplier(sport_key: str, overrides: Mapping[str, float] | None = None) -> float:
    if overrides and sport_key in overrides:
        return clamp(float(overrides[sport_key]), 0.01, 1.0)
    return SPORT_KELLY_MULTIPLIERS.get(sport_key, 0.25)


def sport_kelly_sizing(sport_key: str, probability: float, odds: float, bankroll: Decimal, drawdown: float, policy: gm.SizingPolicy,
                       step: Decimal = Decimal("1"), *, bss: float | None = None, latched: bool = False,
                       overrides: Mapping[str, float] | None = None) -> gm.Sizing:
    """KUMBHA's sizing (Group 76) with the sport's multiplier as kappa: the skill and longshot factors, the single-stake
    ceiling and the drawdown damper all still apply, in their order."""
    full = gm.kelly_fraction(odds, probability)
    return gm.size(full, odds, bankroll, drawdown, policy, step, bss=bss, latched=latched, kappa=sport_kelly_multiplier(sport_key, overrides))


# ================================================================ pillar 15: the confidence & sizing index
@dataclass(frozen=True, slots=True)
class ConfidenceIndex:
    score: float  # 0-100
    grade: str
    recommendation: str  # SUBMIT_TO_CFO | REDUCE_STAKE | NO_BET
    components: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "grade": self.grade, "recommendation": self.recommendation, "components": self.components}


CONFIDENCE_WEIGHTS: dict[str, float] = {"consensus": 30.0, "sharp_alignment": 25.0, "edge_quality": 15.0, "evidence": 20.0, "model_depth": 10.0}


def confidence_index(sport_key: str, *, agreement: float, model_count: int, model_probability: float, sharp_probability: float | None,
                     edge: float, evidence: Mapping[str, bool], consensus_status: str = PASS) -> ConfidenceIndex:
    """One 0-100 score before anything reaches the CFO:

    * consensus (30): the sub-models' agreement (1 - dispersion / 0.25);
    * sharp alignment (25): 1 - |model - sharp| / 0.10 (no sharp market: 0);
    * edge quality (15): full credit for an edge of 2-12%, less as it shrinks to 0 or grows past 25% (a huge edge is
      more often a stale price or a model error than a gift);
    * evidence (20): the share of the sport's relevant sections (weather, absences, officials, calibrated weights) fed;
    * model depth (10): 3 sub-models 0.75, 4 or more 1.

    A failed consensus caps the score at 49 (no bet)."""
    c_consensus = clamp(agreement, 0.0, 1.0)
    c_sharp = 0.0 if sharp_probability is None else clamp(1.0 - abs(model_probability - sharp_probability) / 0.10, 0.0, 1.0)
    if edge <= 0:
        c_edge = 0.0
    elif edge < 0.02:
        c_edge = edge / 0.02
    elif edge <= 0.12:
        c_edge = 1.0
    else:
        c_edge = clamp(1.0 - (edge - 0.12) / 0.13, 0.0, 1.0)
    relevant = ["model_weights"]
    if sport_key in WEATHER_SPORTS:
        relevant.append("weather")
    if sport_key in ABSENCE_SPORTS:
        relevant.append("absences")
    if sport_key in OFFICIAL_SPORTS:
        relevant.append("officials")
    c_evidence = sum(1.0 for k in relevant if evidence.get(k)) / len(relevant)
    c_depth = 0.0 if model_count < 3 else 0.75 if model_count == 3 else 1.0
    parts = {"consensus": c_consensus, "sharp_alignment": c_sharp, "edge_quality": c_edge, "evidence": c_evidence, "model_depth": c_depth}
    score = sum(CONFIDENCE_WEIGHTS[k] * v for k, v in parts.items())
    if consensus_status != PASS:
        score = min(score, 49.0)
    score = round(score, 2)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"
    recommendation = "SUBMIT_TO_CFO" if score >= 65 else "REDUCE_STAKE" if score >= 50 else "NO_BET"
    return ConfidenceIndex(score, grade, recommendation, {k: round(v, 4) for k, v in parts.items()})


# ================================================================ FA-8: OmniRoute
@dataclass(frozen=True, slots=True)
class RouteDecision:
    venue: str
    odds: float
    net_odds: float
    reason: str
    ranked: tuple[tuple[str, float], ...]  # (venue, net odds), best first

    def as_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "odds": self.odds, "net_odds": round(self.net_odds, 5), "reason": self.reason,
                "ranked": [{"venue": v, "net_odds": round(n, 5)} for v, n in self.ranked]}


def route_best_price(quotes: Mapping[str, float], *, bet_kind: str = "single", commission: Mapping[str, float] | None = None,
                     preference_tolerance: float = 0.01) -> RouteDecision | None:
    """The venue to send a slip to among Pinnacle, Betfair, Parimatch, Stake and 1xBet: the best price net of commission.
    The exchange takes singles only. Parimatch (the primary capital account) takes a single when its net price is within
    ``preference_tolerance`` (relative) of the best; 1xBet / Stake take accumulators and props on the same terms."""
    fees = {**DEFAULT_COMMISSION, **(commission or {})}
    rows: dict[str, tuple[float, float]] = {}
    for raw, price in quotes.items():
        venue = canonical_bookmaker(raw)
        if venue not in OMNI_VENUES:
            continue
        try:
            odds = float(price)
        except (TypeError, ValueError):
            continue
        if not odds > 1.0:
            continue
        if bet_kind != "single" and venue in EXCHANGES:
            continue
        net = net_odds(odds, fees.get(venue, 0.0))
        if venue not in rows or net > rows[venue][1]:
            rows[venue] = (odds, net)
    if not rows:
        return None
    ranked = tuple(sorted(((v, n) for v, (_, n) in rows.items()), key=lambda r: (-r[1], OMNI_VENUES.index(r[0]))))
    best_venue, best_net = ranked[0]
    preferred = (PRIMARY_CAPITAL_VENUE,) if bet_kind == "single" else MULTI_VENUES
    for venue in preferred:
        if venue in rows and venue != best_venue and rows[venue][1] - 1.0 >= (best_net - 1.0) * (1.0 - preference_tolerance):
            return RouteDecision(venue, rows[venue][0], rows[venue][1],
                                 f"{venue} is within {preference_tolerance:.0%} of the best price ({best_venue}) and is the "
                                 + ("primary capital account" if bet_kind == "single" else "multi-bet venue"), ranked)
    return RouteDecision(best_venue, rows[best_venue][0], best_net, f"best net price among {len(ranked)} venues", ranked)
