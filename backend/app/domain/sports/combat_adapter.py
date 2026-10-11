"""
Combat Sports Quantitative Adapter: Strike Differential, Takedown Matrix & Finish-Hazard Decay (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

"home" is fighter A (red corner), "away" is fighter B (blue corner). Three sub-models vote (fortress pillar 1),
four when both fighters carry an Elo:

1. **Strike differential logistic** (weight 0.35; 0.30 with Elo): the spec's ``StrikeDiff = SLpM - SApM`` per
   fighter, projected onto this matchup: A lands ``L_A = (SLpM_A + SApM_B) / 2 x f_A`` significant strikes per
   minute, with the accuracy/defence factor ``f_A = sqrt((acc_A / 0.45) x ((1 - def_B) / 0.45))`` (each part at the
   league average when absent). With no accuracy data ``L_A - L_B = (StrikeDiff_A - StrikeDiff_B) / 2``. The takedown
   matrix gives ``TD_A = td_avg_per_15_A x (1 - td_def_B) [x td_acc_A / 0.40]`` expected takedowns per 15 minutes.
   ``logit P(A) = 0.84 (L_A - L_B) + 0.30 (TD_A - TD_B) + 0.008 reach_gap_cm - 0.05 (age_A - 32)+ + 0.05 (age_B - 32)+``.
2. **Grappling matrix** (0.20; 0.15): a positional time-share model. The fight spends ``g = 0.10 (TD_A + TD_B)``
   (capped at 0.70) of its time on the mat, A on top for ``c_A = (TD_A + 0.1) / (TD_A + TD_B + 0.2)`` of it;
   ``logit P(A) = (1 - g) 0.84 (L_A - L_B) + g 3.0 (2 c_A - 1) + 0.25 (S_A - S_B)`` with the submission threat
   ``S_A = sub_avg_per_15_A x sqrt((1 - td_def_B) / 0.35)``.
3. **Finish hazard** (0.45; 0.40): competing finish hazards with the spec's exponential decay. Each fighter's
   15-minute stoppage threat against an average opponent is ``finish_rate / 2`` (``finish_rate`` = share of the
   fighter's wins inside the distance), split KO/TKO vs submission by ``ko_rate`` / ``sub_rate`` (else by grappling
   style), and its cumulative hazard is scaled by dominance (``exp(0.30 (L_A - L_B))`` for strikes,
   ``exp(0.35 (TD_A - TD_B) + 0.15 (sub_A - sub_B))`` for submissions), the opponent's durability
   (``sqrt(ko_absorbed_rate / 0.12)`` or ``exp(1.4 (0.5 - chin))``, age past 33 adds 4% a year) and the weight
   class (table below). The per-round finish probability given the round is reached decays as
   ``P(finish in round r) = P0 e^{-gamma (r - 1)}`` (gamma = ``ctx.finish_decay_gamma``, default 0.25: the
   conditional reading of the spec's formula, which reproduces the UFC split of roughly 50% / 30% / 20% of
   finishes across rounds 1-3); ``P0`` solves ``1 - prod_{r<=3}(1 - P0 e^{-gamma (r-1)}) = threat``.
   ``P(decision) = 1 - sum_r P(finish in round r)``; the decision goes to the fighter winning more rounds, each
   round a Bernoulli ``logistic((5 (L_A - L_B) + 4 (TD_A - TD_B) / 3 + 2 (sub_A - sub_B) / 3) / 6)`` (strike
   equivalents: a takedown scores about four significant strikes with the judges).
4. **Elo** (0.15, only when both fighters carry ``elo``): ``logistic((elo_A - elo_B) ln10 / 400)``.

The finish-hazard model yields the full outcome distribution: P(A by KO/TKO in round r), P(A by submission in
round r), P(A by decision), the same for B, and the draw (``P(draw | decision) = 0.025 (1 + 1.5 v)``). It is
calibrated onto the consensus by scaling each fighter's cells to the consensus win probability, so every market
(method of victory, round betting, goes the distance, round totals, inside the distance) agrees with the moneyline.
Round totals use uniform finish timing within a round: "over 1.5 rounds" means the fight lasts past 2:30 of round 2;
the elapsed-time distribution is an ``IntPMF`` on half-round bins, exact for every x.5 line.

Context: ``rounds`` (3 or 5; 10/12 for boxing), ``round_minutes`` (5; 3 in boxing), ``finish_decay_gamma``,
``weight_class``; officials (pillar 9) ``judge_variance`` (0..1) or the judges' ``split_decision_rate`` (league
0.15 -> v = (rate - 0.15) / 0.35) shrink the decision winner toward 0.5 by ``(1 - v / 2)`` and raise the draw.
Weather does not reach a cage or a ring, and a missing fighter is a cancelled bout (void), not an absence
adjustment: neither section adjusts this sport.

Weight class (multipliers on the KO and submission hazards): strawweight 0.70 / 1.10, flyweight 0.75 / 1.10,
bantamweight 0.85 / 1.00, featherweight 0.95 / 1.00, lightweight 1.00 / 1.00, welterweight 1.05 / 0.95,
middleweight 1.15 / 0.95, light heavyweight 1.30 / 0.85, heavyweight 1.45 / 0.80; women's divisions x0.70 on KO.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import IntPMF, SubModelEstimate, clamp, logistic
from app.domain.sports.settlement import (
    MONEYLINE,
    PLAYER_PROP,
    NormalizedResult,
    SettlementInputError,
    norm,
    normalize_basic,
    num,
    settle_threshold,
    side_token,
    split_selection,
)
from app.schemas.bet_types import LegStatus

# ------------------------------------------------------------------ league reference values (UFC 2015-2025)
LEAGUE_SLPM = 3.9
LEAGUE_STR_ACC = 0.45
LEAGUE_STR_DEF = 0.55
LEAGUE_TD_AVG = 1.5
LEAGUE_TD_DEF = 0.65
LEAGUE_TD_ACC = 0.40
LEAGUE_SUB_AVG = 0.5
LEAGUE_FINISH_RATE = 0.55
LEAGUE_KO_ABSORBED = 0.12
LEAGUE_SUB_ABSORBED = 0.07
LEAGUE_SPLIT_RATE = 0.15

# ------------------------------------------------------------------ model coefficients
STRIKE_LOGIT = 0.84  # log-odds per significant strike per minute of projected differential
TD_LOGIT = 0.30  # log-odds per expected takedown per 15 minutes
REACH_LOGIT_PER_CM = 0.008
AGE_PEAK = 32.0
AGE_LOGIT_PER_YEAR = 0.05
GROUND_SHARE_PER_TD = 0.10  # a landed takedown keeps the fight down about 1.5 of 15 minutes
GROUND_SHARE_CAP = 0.70
GROUND_LOGIT = 3.0
SUB_THREAT_LOGIT = 0.25
KO_DOMINANCE = 0.30
SUB_DOMINANCE_TD = 0.35
SUB_DOMINANCE_ATT = 0.15
CHIN_SLOPE = 1.4
AGE_CHIN_PER_YEAR = 0.04
AGE_CHIN_FROM = 33.0
DEFAULT_GAMMA = 0.25
BASE_ROUNDS = 3  # the finish threat is quoted over a standard 3 x 5-minute fight
BASE_ROUND_MINUTES = 5.0
TD_POINTS = 4.0  # strike equivalents per takedown on a judge's card
SUB_POINTS = 2.0  # strike equivalents per submission attempt
ROUND_SCORE_SCALE = 6.0  # strike-equivalent margin that moves a round's log-odds by 1
DRAW_GIVEN_DECISION = 0.025
KD_HAZARD_MULT = 1.5  # a scored knockdown raises the scorer's finish hazard by 50% for the rest of the fight
KD_POINTS = 8.0  # strike equivalents of a knockdown on the judges' card
LIVE_PRIOR_MINUTES = 10.0  # prior strength (fight minutes) of the pre-fight strike differential

WEIGHT_CLASS_FINISH: dict[str, tuple[float, float]] = {
    "strawweight": (0.70, 1.10),
    "flyweight": (0.75, 1.10),
    "bantamweight": (0.85, 1.00),
    "featherweight": (0.95, 1.00),
    "lightweight": (1.00, 1.00),
    "welterweight": (1.05, 0.95),
    "middleweight": (1.15, 0.95),
    "light_heavyweight": (1.30, 0.85),
    "heavyweight": (1.45, 0.80),
}
WOMENS_KO_MULT = 0.70

KO_TKO = "ko_tko"
SUBMISSION = "submission"
DECISION = "decision"
DRAW = "draw"
FINISH = "finish"
VOID_METHOD = "void"

METHOD_OF_VICTORY = "method_of_victory"
ROUND_BETTING = "round_betting"
GOES_DISTANCE = "fight_goes_the_distance"
ROUND_TOTALS = "round_totals_over_under"
FIGHT_DRAW = "fight_draw"

_KO_WORDS = frozenset({"ko", "tko", "kotko", "knockout", "dq", "disqualification", "doctor", "corner", "retirement", "rtd", "stoppage"})
_SUB_WORDS = frozenset({"sub", "subs", "submission", "tapout", "tap"})
_DEC_WORDS = frozenset({"dec", "decision", "points", "pts", "ud", "sd", "md", "unanimous", "split", "majority"})
_DRAW_WORDS = frozenset({"draw", "tie", "drawn"})
_VOID_WORDS = frozenset({"nc", "no_contest", "nocontest", "overturned"})
_HOME_WORDS = frozenset({"home", "a", "red"})
_AWAY_WORDS = frozenset({"away", "b", "blue"})
_FILLER = frozenset({"fighter", "team", "player", "corner", "by", "win", "wins", "winner", "in", "the", "to", "via", "method", "of",
                     "victory", "round", "rd", "r", "and", "or", "yes"})
_ROUND_TOKEN = re.compile(r"^(?:r|rd|round)(\d{1,2})$")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text: Any) -> list[str]:
    return [t for t in _SPLIT.split(str(text or "").casefold()) if t]


def classify_method(raw_method: Any) -> str | None:
    """A raw method string as ko_tko / submission / decision / draw / void. A disqualification, doctor or corner
    stoppage settles as KO/TKO (the referee stopped the fight, the common sportsbook rule); a technical submission as
    a submission; a split, majority, unanimous or technical decision as a decision; a no contest voids."""
    text = norm(raw_method)
    if not text:
        return None
    if text in _VOID_WORDS or "no_contest" in text:
        return VOID_METHOD
    words = set(_tokens(raw_method))
    for vocabulary, method in ((_DRAW_WORDS, DRAW), (_SUB_WORDS, SUBMISSION), (_DEC_WORDS, DECISION), (_KO_WORDS, KO_TKO)):
        if words & vocabulary:
            return method
    return None


def solve_base_hazard(threat: float, gamma: float, rounds: int = BASE_ROUNDS) -> float:
    """``P0`` with ``1 - prod_{r=1..rounds} (1 - P0 e^{-gamma (r-1)}) = threat`` (bisection; the left side rises in P0)."""
    threat = clamp(threat, 0.0, 0.995)
    if threat <= 0.0:
        return 0.0
    target = 1.0 - threat
    decays = [math.exp(-gamma * r) for r in range(rounds)]
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        survival = 1.0
        for d in decays:
            survival *= 1.0 - min(1.0, mid * d)
        if survival > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def rounds_won_pmf(probs: Sequence[float]) -> np.ndarray:
    """P(A wins k of the given rounds), k = 0..len(probs): the convolution of independent Bernoullis."""
    pmf = np.array([1.0])
    for p in probs:
        pmf = np.convolve(pmf, np.array([1.0 - p, p]))
    return pmf


# ================================================================ profiles
@dataclass(frozen=True, slots=True)
class FighterProfile:
    slpm: float
    sapm: float
    str_acc: float | None
    str_def: float | None
    td_avg: float
    td_def: float
    td_acc: float | None
    sub_avg: float
    finish_rate: float
    ko_share: float  # share of the fighter's finishes by KO/TKO
    ko_absorbed: float | None
    sub_absorbed: float | None
    chin: float | None
    reach_cm: float | None
    age: float | None
    elo: float | None

    @classmethod
    def parse(cls, stats: Mapping[str, Any]) -> FighterProfile:
        ko_rate, sub_rate = num(stats, "ko_rate", "ko_tko_rate"), num(stats, "sub_rate", "submission_rate")
        finish = num(stats, "finish_rate")
        if finish is None and ko_rate is not None and sub_rate is not None:
            finish = ko_rate + sub_rate
        finish = clamp(finish if finish is not None else LEAGUE_FINISH_RATE, 0.0, 1.0)
        sub_avg = max(0.0, num(stats, "sub_avg_per_15", "sub_avg", default=LEAGUE_SUB_AVG) or 0.0)
        td_avg = max(0.0, num(stats, "td_avg_per_15", "td_avg", default=LEAGUE_TD_AVG) or 0.0)
        if ko_rate is not None and sub_rate is not None and ko_rate + sub_rate > 0:
            ko_share = ko_rate / (ko_rate + sub_rate)
        elif ko_rate is not None and finish > 0:
            ko_share = ko_rate / finish
        elif sub_rate is not None and finish > 0:
            ko_share = 1.0 - sub_rate / finish
        else:  # style prior: submission specialists finish on the mat
            ko_share = 1.0 - (0.25 + 0.15 * (sub_avg - LEAGUE_SUB_AVG) + 0.05 * (td_avg - LEAGUE_TD_AVG))
        return cls(
            slpm=max(0.0, num(stats, "slpm", default=LEAGUE_SLPM) or 0.0),
            sapm=max(0.0, num(stats, "sapm", default=LEAGUE_SLPM) or 0.0),
            str_acc=_pct(num(stats, "str_acc", "striking_accuracy")),
            str_def=_pct(num(stats, "str_def", "striking_defense")),
            td_avg=td_avg,
            td_def=_pct(num(stats, "td_def_pct", "td_def")) if num(stats, "td_def_pct", "td_def") is not None else LEAGUE_TD_DEF,
            td_acc=_pct(num(stats, "td_acc", "td_acc_pct")),
            sub_avg=sub_avg,
            finish_rate=finish,
            ko_share=clamp(ko_share, 0.05, 0.95),
            ko_absorbed=num(stats, "ko_absorbed_rate", "ko_loss_rate"),
            sub_absorbed=num(stats, "sub_absorbed_rate", "sub_loss_rate"),
            chin=num(stats, "chin", "durability"),
            reach_cm=num(stats, "reach_cm"),
            age=num(stats, "age"),
            elo=num(stats, "elo"),
        )


def _pct(value: float | None) -> float | None:
    """Accepts 0.45 or 45 (percent) for a rate."""
    if value is None:
        return None
    return clamp(value / 100.0 if value > 1.0 else value, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Matchup:
    """The matchup quantities every sub-model reads."""

    strike_diff_a: float  # SLpM_A - SApM_A (the spec's per-fighter StrikeDiff)
    strike_diff_b: float
    land_a: float  # projected significant strikes landed per minute in this fight
    land_b: float
    td_a: float  # expected takedowns per 15 minutes (takedown defence matrix)
    td_b: float
    sub_a: float  # submission threat per 15 minutes
    sub_b: float
    physical_logit: float  # reach and age terms

    @property
    def projected_diff(self) -> float:
        return self.land_a - self.land_b

    @property
    def round_score_diff(self) -> float:
        """A's expected strike-equivalent margin on a judge's card over one 5-minute round."""
        return 5.0 * self.projected_diff + TD_POINTS * (self.td_a - self.td_b) / 3.0 + SUB_POINTS * (self.sub_a - self.sub_b) / 3.0


@dataclass(frozen=True)
class FightDistribution:
    """The full outcome distribution of a bout. Arrays are indexed by round (index 0 = round 1)."""

    rounds: int
    ko_a: np.ndarray
    sub_a: np.ndarray
    ko_b: np.ndarray
    sub_b: np.ndarray
    dec_a: float
    dec_b: float
    draw: float

    @property
    def win_a(self) -> float:
        return float(self.ko_a.sum() + self.sub_a.sum() + self.dec_a)

    @property
    def win_b(self) -> float:
        return float(self.ko_b.sum() + self.sub_b.sum() + self.dec_b)

    @property
    def finish_by_round(self) -> np.ndarray:
        return self.ko_a + self.sub_a + self.ko_b + self.sub_b

    @property
    def distance(self) -> float:
        return self.dec_a + self.dec_b + self.draw

    def total(self) -> float:
        return self.win_a + self.win_b + self.draw

    def scaled(self, home: float, away: float, draw: float) -> FightDistribution:
        """Each fighter's cells rescaled to the given win probabilities (shapes kept): the consensus calibration."""
        fa = home / self.win_a if self.win_a > 0 else 0.0
        fb = away / self.win_b if self.win_b > 0 else 0.0
        fd = draw / self.draw if self.draw > 0 else 0.0
        out = FightDistribution(self.rounds, self.ko_a * fa, self.sub_a * fa, self.ko_b * fb, self.sub_b * fb, self.dec_a * fa,
                                self.dec_b * fb, self.draw * fd)
        total = out.total()
        return FightDistribution(out.rounds, out.ko_a / total, out.sub_a / total, out.ko_b / total, out.sub_b / total,
                                 out.dec_a / total, out.dec_b / total, out.draw / total)

    def elapsed_pmf(self) -> IntPMF:
        """Elapsed fight time in half-round bins: bin j covers [j/2, (j+1)/2) rounds; a finish in round r lands in
        bins 2(r-1) and 2r-1 with half its mass each (uniform timing); a decision fills bin 2N (the full fight)."""
        probs = np.zeros(2 * self.rounds + 1)
        for r, p in enumerate(self.finish_by_round):
            probs[2 * r] += 0.5 * p
            probs[2 * r + 1] += 0.5 * p
        probs[2 * self.rounds] += self.distance
        return IntPMF.from_array(probs, 0)

    def expected_rounds(self) -> float:
        return float(sum(p * (r + 0.5) for r, p in enumerate(self.finish_by_round)) + self.distance * self.rounds)


@dataclass(frozen=True, slots=True)
class HazardParams:
    h0_a: float
    h0_b: float
    ko_share_a: float
    ko_share_b: float
    threat_a: float  # 15-minute stoppage threat against this opponent
    threat_b: float


def project_fight(hz: HazardParams, *, gamma: float, rounds: int, p_round: float,
                  judge_variance: float = 0.0, current_round: int = 1, elapsed_frac: float = 0.0,
                  rounds_won_a: int | None = None, rounds_won_b: int | None = None, p_current_round: float | None = None,
                  hazard_mult_a: float = 1.0, hazard_mult_b: float = 1.0) -> FightDistribution:
    """The outcome distribution from ``current_round`` (``elapsed_frac`` of it gone, no finish yet) to the final bell.

    Per round ``r`` each fighter's finish probability given the round is reached is ``P0 e^{-gamma (r-1)}`` (times the
    live knockdown multiplier); the round ends early with ``h_r = 1 - (1 - a_r)(1 - b_r)``, split between the fighters
    in proportion ``a_r : b_r``. Timing is uniform within a round, so the rest of a round with ``e`` gone finishes with
    probability ``h_r (1 - e) / (1 - h_r e)``. The decision: unscored rounds are independent Bernoullis (``p_round``,
    the current one ``p_current_round``); the fighter with more rounds wins the cards, level cards are a draw, and a
    10-10 / point-deduction draw takes ``0.025 (1 + 1.5 v)`` of the rest; the judges' variance ``v`` shrinks the winner
    toward 0.5."""
    n = rounds
    ko_a, sub_a, ko_b, sub_b = (np.zeros(n) for _ in range(4))
    survival = 1.0
    start = max(1, current_round)
    for r in range(start, n + 1):
        decay = math.exp(-gamma * (r - 1))
        a = min(0.95, hz.h0_a * decay * hazard_mult_a)
        b = min(0.95, hz.h0_b * decay * hazard_mult_b)
        h = 1.0 - (1.0 - a) * (1.0 - b)
        if r == start and elapsed_frac > 0.0:
            e = clamp(elapsed_frac, 0.0, 1.0)
            h = h * (1.0 - e) / max(1e-12, 1.0 - h * e)
        share_a = a / (a + b) if a + b > 0 else 0.5
        finish = survival * h
        ko_a[r - 1] = finish * share_a * hz.ko_share_a
        sub_a[r - 1] = finish * share_a * (1.0 - hz.ko_share_a)
        ko_b[r - 1] = finish * (1.0 - share_a) * hz.ko_share_b
        sub_b[r - 1] = finish * (1.0 - share_a) * (1.0 - hz.ko_share_b)
        survival *= 1.0 - h

    # the judges' cards
    completed = max(0, min(n, start - 1))
    unknown_completed = 0 if rounds_won_a is not None and rounds_won_b is not None else completed
    known_a = (rounds_won_a or 0) if unknown_completed == 0 else 0
    known_b = (rounds_won_b or 0) if unknown_completed == 0 else 0
    to_score: list[float] = [p_round] * unknown_completed
    if start <= n:
        to_score.append(p_current_round if p_current_round is not None else p_round)
        to_score.extend([p_round] * (n - start))
    pmf = rounds_won_pmf(to_score)
    k = len(to_score)
    cards_a = cards_b = tie = 0.0
    for x, p in enumerate(pmf):
        ra, rb = known_a + x, known_b + (k - x)
        if ra > rb:
            cards_a += p
        elif rb > ra:
            cards_b += p
        else:
            tie += p
    v = clamp(judge_variance, 0.0, 1.0)
    decided = cards_a + cards_b
    if decided > 0:
        share = 0.5 + (cards_a / decided - 0.5) * (1.0 - 0.5 * v)
        cards_a, cards_b = decided * share, decided * (1.0 - share)
    d = DRAW_GIVEN_DECISION * (1.0 + 1.5 * v)
    dec_a = float(survival * cards_a * (1.0 - d))
    dec_b = float(survival * cards_b * (1.0 - d))
    draw = float(survival * (tie + decided * d))
    return FightDistribution(n, ko_a, sub_a, ko_b, sub_b, dec_a, dec_b, draw)


@dataclass(frozen=True, slots=True)
class ParsedSelection:
    side: str | None  # "home" | "away" | "name:<x>" | None
    method: str | None  # ko_tko | submission | decision | finish | draw | None
    round: int | None


class CombatSportAdapter(BaseSportAdapter):
    """
    Quantitative MMA / UFC & Boxing Engine implementing Significant Strike Differential,
    Takedown Defense Matrix, and round-by-round finish rate exponential decay.
    """

    kelly_multiplier = 0.20
    model_family = "Strike Differential & Finish Decay"
    has_draw = False  # a draw voids the 2-way moneyline; it is still priced
    odds_api_family = "mma"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "moneyline": (MONEYLINE, {}),
        "match_winner": (MONEYLINE, {}),
        "fight_winner": (MONEYLINE, {}),
        "bout_winner": (MONEYLINE, {}),
        "method_of_victory": (METHOD_OF_VICTORY, {}),
        "method": (METHOD_OF_VICTORY, {}),
        "fight_result": (METHOD_OF_VICTORY, {}),
        "round_betting": (ROUND_BETTING, {}),
        "winning_round": (ROUND_BETTING, {}),
        "fight_goes_the_distance": (GOES_DISTANCE, {}),
        "goes_the_distance": (GOES_DISTANCE, {}),
        "go_the_distance": (GOES_DISTANCE, {}),
        "round_totals_over_under": (ROUND_TOTALS, {}),
        "total_rounds": (ROUND_TOTALS, {}),
        "round_totals": (ROUND_TOTALS, {}),
        "draw": (FIGHT_DRAW, {}),
        "fight_draw": (FIGHT_DRAW, {}),
        "fighter_significant_strikes": (PLAYER_PROP, {"stat": "sig_strikes"}),
        "fighter_takedowns": (PLAYER_PROP, {"stat": "takedowns"}),
    }
    GENERIC_SUPPORTED = frozenset({MONEYLINE, PLAYER_PROP})
    SPECIAL_MARKETS = frozenset({METHOD_OF_VICTORY, ROUND_BETTING, GOES_DISTANCE, ROUND_TOTALS, FIGHT_DRAW})
    BET_TYPES = frozenset({"moneyline", "game_total", "parlay", "round_robin", "if_bet", "double_action", "futures", "player_props",
                           "game_props", "method_of_victory", "round_betting", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "combat"

    @property
    def display_name(self) -> str:
        return "MMA / Combat Sports"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "moneyline",
            "method_of_victory",
            "round_totals_over_under",
            "fight_goes_the_distance",
            "round_betting",
            "draw",
        ]

    # ------------------------------------------------------------------ context
    @staticmethod
    def scheduled_rounds(ctx: MatchContext) -> int:
        return int(clamp(round(ctx.number("rounds", 3.0)), 1, 15))

    @staticmethod
    def judge_variance(ctx: MatchContext) -> float | None:
        """Officials (pillar 9): a judges' variance in [0, 1], or one implied by their split-decision rate."""
        if ctx.officials is None:
            return None
        v = ctx.officials.rate("judge_variance", "judges_variance", "scoring_variance")
        if v is not None:
            return clamp(v, 0.0, 1.0)
        split = ctx.officials.rate("split_decision_rate", "split_rate")
        if split is not None:
            return clamp((split - LEAGUE_SPLIT_RATE) / (0.5 - LEAGUE_SPLIT_RATE), 0.0, 1.0)
        return None

    @staticmethod
    def weight_class_multipliers(ctx: MatchContext) -> tuple[float, float, str | None]:
        raw = norm(ctx.text("weight_class", ""))
        if not raw:
            return 1.0, 1.0, None
        womens = raw.startswith(("womens_", "women_", "w_"))
        key = raw.split("_", 1)[1] if womens else raw
        key = {"lhw": "light_heavyweight", "hw": "heavyweight", "mw": "middleweight", "ww": "welterweight", "lw": "lightweight",
               "fw": "featherweight", "bw": "bantamweight", "flw": "flyweight", "sw": "strawweight",
               "lightheavyweight": "light_heavyweight"}.get(key, key)
        ko, sub = WEIGHT_CLASS_FINISH.get(key, (1.0, 1.0))
        if womens:
            ko *= WOMENS_KO_MULT
        return ko, sub, key

    # ------------------------------------------------------------------ matchup
    def matchup(self, a: FighterProfile, b: FighterProfile) -> Matchup:
        def efficiency(att: FighterProfile, dfn: FighterProfile) -> float:
            acc = (att.str_acc / LEAGUE_STR_ACC) if att.str_acc is not None else 1.0
            leak = ((1.0 - dfn.str_def) / (1.0 - LEAGUE_STR_DEF)) if dfn.str_def is not None else 1.0
            return math.sqrt(max(acc, 0.05) * max(leak, 0.05))

        land_a = 0.5 * (a.slpm + b.sapm) * efficiency(a, b)
        land_b = 0.5 * (b.slpm + a.sapm) * efficiency(b, a)
        td_a = a.td_avg * (1.0 - b.td_def) * ((a.td_acc / LEAGUE_TD_ACC) if a.td_acc is not None else 1.0)
        td_b = b.td_avg * (1.0 - a.td_def) * ((b.td_acc / LEAGUE_TD_ACC) if b.td_acc is not None else 1.0)
        sub_a = a.sub_avg * math.sqrt(max(1.0 - b.td_def, 0.0) / (1.0 - LEAGUE_TD_DEF))
        sub_b = b.sub_avg * math.sqrt(max(1.0 - a.td_def, 0.0) / (1.0 - LEAGUE_TD_DEF))
        physical = 0.0
        if a.reach_cm is not None and b.reach_cm is not None:
            physical += REACH_LOGIT_PER_CM * (a.reach_cm - b.reach_cm)
        if a.age is not None:
            physical -= AGE_LOGIT_PER_YEAR * max(0.0, a.age - AGE_PEAK)
        if b.age is not None:
            physical += AGE_LOGIT_PER_YEAR * max(0.0, b.age - AGE_PEAK)
        return Matchup(a.slpm - a.sapm, b.slpm - b.sapm, land_a, land_b, td_a, td_b, sub_a, sub_b, physical)

    # ------------------------------------------------------------------ the sub-models
    @staticmethod
    def strike_differential_logit(m: Matchup) -> float:
        return STRIKE_LOGIT * m.projected_diff + TD_LOGIT * (m.td_a - m.td_b) + m.physical_logit

    @staticmethod
    def grappling_matrix_logit(m: Matchup) -> tuple[float, float, float]:
        ground = min(GROUND_SHARE_CAP, GROUND_SHARE_PER_TD * (m.td_a + m.td_b))
        control_a = (m.td_a + 0.1) / (m.td_a + m.td_b + 0.2)
        logit = ((1.0 - ground) * STRIKE_LOGIT * m.projected_diff + ground * GROUND_LOGIT * (2.0 * control_a - 1.0)
                 + SUB_THREAT_LOGIT * (m.sub_a - m.sub_b))
        return logit, ground, control_a

    @staticmethod
    def round_win_probability(m: Matchup) -> float:
        """P(A wins a round on the cards) from the strike-equivalent round margin."""
        return logistic(m.round_score_diff / ROUND_SCORE_SCALE)

    def hazard_params(self, a: FighterProfile, b: FighterProfile, m: Matchup, ctx: MatchContext, *, gamma: float,
                      round_minutes: float) -> tuple[HazardParams, dict[str, Any]]:
        ko_wc, sub_wc, wc_key = self.weight_class_multipliers(ctx)

        def threat(att: FighterProfile, dfn: FighterProfile, land_gap: float, td_gap: float, sub_gap: float) -> tuple[float, float, float]:
            base = clamp(0.5 * att.finish_rate, 0.0, 0.95)
            base_ko, base_sub = base * att.ko_share, base * (1.0 - att.ko_share)
            # opponent durability: KO chin and submission defence
            if dfn.ko_absorbed is not None:
                chin = clamp(math.sqrt(max(dfn.ko_absorbed, 0.0) / LEAGUE_KO_ABSORBED), 0.5, 2.0)
            elif dfn.chin is not None:
                chin = clamp(math.exp(CHIN_SLOPE * (0.5 - clamp(dfn.chin, 0.0, 1.0))), 0.5, 2.0)
            else:
                chin = 1.0
            if dfn.age is not None and dfn.age > AGE_CHIN_FROM:
                chin *= 1.0 + AGE_CHIN_PER_YEAR * (dfn.age - AGE_CHIN_FROM)
            if dfn.sub_absorbed is not None:
                sub_vuln = clamp(math.sqrt(max(dfn.sub_absorbed, 0.0) / LEAGUE_SUB_ABSORBED), 0.5, 2.0)
            else:
                sub_vuln = clamp(math.sqrt(max(1.0 - dfn.td_def, 0.0) / (1.0 - LEAGUE_TD_DEF)), 0.5, 2.0)
            h_ko = -math.log(max(1e-9, 1.0 - min(base_ko, 0.95))) * math.exp(KO_DOMINANCE * land_gap) * chin * ko_wc
            h_sub = (-math.log(max(1e-9, 1.0 - min(base_sub, 0.95))) * math.exp(SUB_DOMINANCE_TD * td_gap + SUB_DOMINANCE_ATT * sub_gap)
                     * sub_vuln * sub_wc)
            total = h_ko + h_sub
            return 1.0 - math.exp(-total), (h_ko / total if total > 0 else att.ko_share), chin

        threat_a, ko_share_a, chin_b = threat(a, b, m.projected_diff, m.td_a - m.td_b, m.sub_a - m.sub_b)
        threat_b, ko_share_b, chin_a = threat(b, a, -m.projected_diff, m.td_b - m.td_a, m.sub_b - m.sub_a)
        h0_a = solve_base_hazard(threat_a, gamma)
        h0_b = solve_base_hazard(threat_b, gamma)
        if abs(round_minutes - BASE_ROUND_MINUTES) > 1e-9:  # a boxing round (3 minutes) carries a shorter hazard
            h0_a = 1.0 - (1.0 - h0_a) ** (round_minutes / BASE_ROUND_MINUTES)
            h0_b = 1.0 - (1.0 - h0_b) ** (round_minutes / BASE_ROUND_MINUTES)
        metrics: dict[str, Any] = {"fighter_a_finish_threat": round(threat_a, 4), "fighter_b_finish_threat": round(threat_b, 4),
                                   "fighter_a_round1_finish_prob": round(h0_a, 4), "fighter_b_round1_finish_prob": round(h0_b, 4),
                                   "fighter_a_ko_share": round(ko_share_a, 4), "fighter_b_ko_share": round(ko_share_b, 4),
                                   "fighter_a_chin_factor": round(chin_a, 4), "fighter_b_chin_factor": round(chin_b, 4)}
        if wc_key is not None:
            metrics["weight_class"] = wc_key
            metrics["weight_class_ko_multiplier"] = round(ko_wc, 3)
            metrics["weight_class_sub_multiplier"] = round(sub_wc, 3)
        return HazardParams(h0_a, h0_b, ko_share_a, ko_share_b, threat_a, threat_b), metrics

    def model(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
              ctx: MatchContext) -> tuple[list[SubModelEstimate], FightDistribution, Matchup, HazardParams, dict[str, Any]]:
        a, b = FighterProfile.parse(home_stats), FighterProfile.parse(away_stats)
        rounds = self.scheduled_rounds(ctx)
        round_minutes = clamp(ctx.number("round_minutes", BASE_ROUND_MINUTES), 1.0, 10.0)
        gamma = clamp(ctx.number("finish_decay_gamma", DEFAULT_GAMMA), 0.0, 2.0)
        variance = self.judge_variance(ctx)
        m = self.matchup(a, b)
        hz, hz_metrics = self.hazard_params(a, b, m, ctx, gamma=gamma, round_minutes=round_minutes)
        p_round = self.round_win_probability(m)
        dist = project_fight(hz, gamma=gamma, rounds=rounds, p_round=p_round, judge_variance=variance or 0.0)
        draw = dist.draw

        strike_p = logistic(self.strike_differential_logit(m))
        grapple_logit, ground, control_a = self.grappling_matrix_logit(m)
        grapple_p = logistic(grapple_logit)
        expected_minutes = dist.expected_rounds() * round_minutes

        def two_way(name: str, p: float, weight: float) -> SubModelEstimate:
            return SubModelEstimate(name, (1.0 - draw) * p, (1.0 - draw) * (1.0 - p), draw, expected_minutes, weight)

        has_elo = a.elo is not None and b.elo is not None
        weights = (0.30, 0.15, 0.40, 0.15) if has_elo else (0.35, 0.20, 0.45, 0.0)
        estimates = [
            two_way("strike_differential_logistic", strike_p, weights[0]),
            two_way("grappling_matrix", grapple_p, weights[1]),
            SubModelEstimate("finish_hazard", dist.win_a, dist.win_b, dist.draw, expected_minutes, weights[2]),
        ]
        if has_elo:
            estimates.append(two_way("elo", logistic((float(a.elo) - float(b.elo)) * math.log(10.0) / 400.0), weights[3]))  # type: ignore[arg-type]
        metrics: dict[str, Any] = {
            "fighter_a_strike_diff": round(m.strike_diff_a, 3),
            "fighter_b_strike_diff": round(m.strike_diff_b, 3),
            "projected_sig_strikes_per_min": [round(m.land_a, 3), round(m.land_b, 3)],
            "projected_strike_differential": round(m.projected_diff, 3),
            "expected_takedowns_per_15": [round(m.td_a, 3), round(m.td_b, 3)],
            "submission_threat_per_15": [round(m.sub_a, 3), round(m.sub_b, 3)],
            "physical_logit": round(m.physical_logit, 4),
            "ground_time_share": round(ground, 4),
            "fighter_a_ground_control_share": round(control_a, 4),
            "round_win_prob_a": round(p_round, 4),
            "finish_decay_gamma": gamma,
            "scheduled_rounds": rounds,
            "round_minutes": round_minutes,
            **hz_metrics,
        }
        if variance is not None:
            metrics["judge_variance"] = round(variance, 4)
        return estimates, dist, m, hz, metrics

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        estimates, dist, _, _, metrics = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(estimates, ctx)
        calibrated = dist.scaled(consensus.home_win_prob, consensus.away_win_prob, consensus.draw_prob)
        markets = self.price_markets(calibrated)
        round_minutes = float(metrics["round_minutes"])
        metrics.update({
            "distance_probability": round(calibrated.distance, 4),
            "finish_probability": round(1.0 - calibrated.distance, 4),
            "finish_by_round": [round(float(p), 4) for p in calibrated.finish_by_round],
            "decision_probability": round(calibrated.dec_a + calibrated.dec_b, 4),
            "expected_fight_minutes": round(calibrated.expected_rounds() * round_minutes, 3),
            "method_distribution": {
                "home_ko_tko": round(float(calibrated.ko_a.sum()), 4), "home_submission": round(float(calibrated.sub_a.sum()), 4),
                "home_decision": round(calibrated.dec_a, 4), "away_ko_tko": round(float(calibrated.ko_b.sum()), 4),
                "away_submission": round(float(calibrated.sub_b.sum()), 4), "away_decision": round(calibrated.dec_b, 4),
                "draw": round(calibrated.draw, 4)},
        })
        return self._assemble(consensus, ctx, expected_total=calibrated.expected_rounds() * round_minutes, metrics=metrics, markets=markets)

    def price_markets(self, dist: FightDistribution) -> dict[str, float]:
        markets: dict[str, float] = {}
        win_a, win_b = dist.win_a, dist.win_b
        markets["moneyline_home"] = win_a / (win_a + win_b)  # a draw voids the 2-way line: fair price on the decided mass
        markets["moneyline_away"] = win_b / (win_a + win_b)
        markets["draw"] = dist.draw
        for side, ko, sub, dec in (("home", dist.ko_a, dist.sub_a, dist.dec_a), ("away", dist.ko_b, dist.sub_b, dist.dec_b)):
            markets[f"method_{side}_ko_tko"] = float(ko.sum())
            markets[f"method_{side}_submission"] = float(sub.sum())
            markets[f"method_{side}_decision"] = dec
            markets[f"{side}_inside_distance"] = float(ko.sum() + sub.sum())
            for r in range(dist.rounds):
                markets[f"round_{side}_r{r + 1}"] = float(ko[r] + sub[r])
            markets[f"round_{side}_decision"] = dec
        markets["method_draw"] = dist.draw
        markets["method_ko_tko"] = float(dist.ko_a.sum() + dist.ko_b.sum())
        markets["method_submission"] = float(dist.sub_a.sum() + dist.sub_b.sum())
        markets["method_decision"] = dist.dec_a + dist.dec_b
        for r, p in enumerate(dist.finish_by_round):
            markets[f"finish_in_round_{r + 1}"] = float(p)
        markets["goes_distance_yes"] = dist.distance
        markets["goes_distance_no"] = 1.0 - dist.distance
        elapsed = dist.elapsed_pmf()
        for k in range(dist.rounds):
            line = k + 0.5
            markets[f"rounds_over_{line:g}"] = elapsed.line_outcome(2.0 * line - 0.5, over=True).effective_probability
            markets[f"rounds_under_{line:g}"] = elapsed.line_outcome(2.0 * line - 0.5, over=False).effective_probability
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability. ``live_state`` keys: ``round`` (1-based, current), ``seconds_elapsed_in_round``,
        ``scorecard_rounds_a`` / ``scorecard_rounds_b`` (completed rounds won on the cards so far), ``knockdowns_a`` /
        ``knockdowns_b`` (knockdowns scored, fight total), ``sig_strikes_a`` / ``sig_strikes_b`` (fight totals),
        optional ``round_sig_strikes_a`` / ``round_sig_strikes_b`` / ``round_knockdowns_a`` / ``round_knockdowns_b``
        (current round only) and ``finished`` ({"winner": "a" | "b" | "home" | "away" | "draw"}).

        The remaining finish hazards run from the current moment (uniform timing within the round, each knockdown
        scored lifting the scorer's hazard by 50%); the pre-fight strike differential is updated with the observed
        significant strikes (a 10-minute prior); the unscored rounds go to the cards. The pre-fight consensus edge over
        the hazard model is carried in log-odds in proportion to the fight time still to run."""
        finished = live_state.get("finished") or ({"winner": live_state["winner"]} if live_state.get("winner") else None)
        if finished:
            winner = side_token(norm(finished.get("winner") if isinstance(finished, Mapping) else finished)) or ""
            if winner in ("home", "away", "draw"):
                return LiveWinProbability(float(winner == "home"), float(winner == "away"), float(winner == "draw"),
                                          {"finished": True, "winner": winner})
        ctx = MatchContext.parse(context)
        estimates, dist0, m, hz, metrics = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(estimates, ctx)
        rounds = int(metrics["scheduled_rounds"])
        round_minutes = float(metrics["round_minutes"])
        gamma = float(metrics["finish_decay_gamma"])
        variance = float(metrics.get("judge_variance", 0.0))

        current = int(clamp(int(live_state.get("round", 1) or 1), 1, rounds + 1))
        seconds = clamp(float(live_state.get("seconds_elapsed_in_round", 0.0) or 0.0), 0.0, round_minutes * 60.0)
        elapsed_frac = seconds / (round_minutes * 60.0)
        if current <= rounds and elapsed_frac >= 1.0:
            current, elapsed_frac = current + 1, 0.0
        kd_a, kd_b = int(live_state.get("knockdowns_a", 0) or 0), int(live_state.get("knockdowns_b", 0) or 0)
        sig_a, sig_b = float(live_state.get("sig_strikes_a", 0.0) or 0.0), float(live_state.get("sig_strikes_b", 0.0) or 0.0)
        minutes_gone = ((min(current, rounds + 1) - 1) + elapsed_frac) * round_minutes
        prior_diff = m.projected_diff
        post_diff = (prior_diff * LIVE_PRIOR_MINUTES + (sig_a - sig_b)) / (LIVE_PRIOR_MINUTES + minutes_gone)
        grapple_part = TD_POINTS * (m.td_a - m.td_b) / 3.0 + SUB_POINTS * (m.sub_a - m.sub_b) / 3.0
        expected_round_margin = 5.0 * post_diff + grapple_part
        p_round = logistic(expected_round_margin / ROUND_SCORE_SCALE)
        if "round_sig_strikes_a" in live_state or "round_knockdowns_a" in live_state:
            observed = (float(live_state.get("round_sig_strikes_a", 0.0) or 0.0) - float(live_state.get("round_sig_strikes_b", 0.0) or 0.0)
                        + KD_POINTS * (float(live_state.get("round_knockdowns_a", 0) or 0) - float(live_state.get("round_knockdowns_b", 0) or 0)))
        else:
            observed = elapsed_frac * expected_round_margin
        p_current = logistic((observed + (1.0 - elapsed_frac) * expected_round_margin) / ROUND_SCORE_SCALE)
        sa, sb = live_state.get("scorecard_rounds_a"), live_state.get("scorecard_rounds_b")
        rounds_a = int(sa) if sa is not None else None
        rounds_b = int(sb) if sb is not None else None
        live = project_fight(hz, gamma=gamma, rounds=rounds, p_round=p_round, judge_variance=variance,
                             current_round=current, elapsed_frac=elapsed_frac, rounds_won_a=rounds_a, rounds_won_b=rounds_b,
                             p_current_round=p_current, hazard_mult_a=KD_HAZARD_MULT ** min(kd_a, 3),
                             hazard_mult_b=KD_HAZARD_MULT ** min(kd_b, 3))
        home, away, draw = live.win_a, live.win_b, live.draw
        total = home + away + draw
        home, away, draw = home / total, away / total, draw / total
        # carry the consensus edge over the hazard model, fading with the time left
        remaining_share = clamp(1.0 - minutes_gone / (rounds * round_minutes), 0.0, 1.0)
        pre_a = dist0.win_a / (dist0.win_a + dist0.win_b)
        cons_a = consensus.home_win_prob / (consensus.home_win_prob + consensus.away_win_prob)
        offset = (_logit(cons_a) - _logit(pre_a)) * remaining_share
        decided = home + away
        if decided > 0:
            share = logistic(_logit(home / decided) + offset)
            home, away = decided * share, decided * (1.0 - share)
        return LiveWinProbability(home, away, draw, {
            "round": current, "elapsed_in_round": round(elapsed_frac, 4), "remaining_finish_prob": round(1.0 - live.distance / max(total, 1e-12), 4),
            "posterior_strike_differential": round(post_diff, 3), "round_win_prob_a": round(p_round, 4),
            "current_round_win_prob_a": round(p_current, 4), "knockdowns": [kd_a, kd_b], "consensus_offset_logit": round(offset, 4)})

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """Lenient raw results: ``{"winner": "a"}``, ``{"winner": "jones", "fighter_a": "Jones"}``, ``{"method": "ko_tko"}``,
        ``{"went_distance": False}``, ``{"rounds_completed": 2.5}``, ``{"round": 2, "time": "2:30"}``, ``status`` /
        ``"no contest"`` voids. Extras: method, round, elapsed_rounds, went_distance, scheduled_rounds, decision_type."""
        res = normalize_basic(match_result, home_keys=("home_score", "fighter_a_score"), away_keys=("away_score", "fighter_b_score"))
        res.home_name = res.home_name or norm(match_result.get("fighter_a") or match_result.get("fighter_a_name") or match_result.get("red_corner"))
        res.away_name = res.away_name or norm(match_result.get("fighter_b") or match_result.get("fighter_b_name") or match_result.get("blue_corner"))
        if res.winner is None and res.winner_name:
            names = {"red": "home", "red_corner": "home", "blue": "away", "blue_corner": "away"}
            if res.home_name:
                names[res.home_name] = "home"
            if res.away_name:
                names[res.away_name] = "away"
            if res.winner_name in names:
                res.winner, res.winner_name = names[res.winner_name], ""
        method = classify_method(match_result.get("method") or match_result.get("method_of_victory") or match_result.get("result_method"))
        res.void = res.void or method == VOID_METHOD
        if method == DRAW and res.winner is None:
            res.winner = "draw"
        if method is None and res.winner == "draw":
            method = DRAW
        scheduled = num(match_result, "scheduled_rounds", "rounds")
        rnd = num(match_result, "round", "end_round", "finish_round")
        elapsed = _elapsed_rounds(match_result, method, rnd, scheduled)
        went = match_result.get("went_distance")
        if went is None:
            went = _went_distance(method, elapsed, scheduled)
        res.extras.update({"method": method, "round": int(rnd) if rnd is not None else None, "elapsed_rounds": elapsed,
                           "went_distance": None if went is None else bool(went), "scheduled_rounds": scheduled,
                           "decision_type": norm(match_result.get("decision_type")) or None})
        return res

    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,  # noqa: PLR0917 - the base interface
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == GOES_DISTANCE:
            went = result.extras.get("went_distance")
            if went is None:
                raise SettlementInputError("the result says neither whether the fight went the distance nor how it ended")
            return _status(bool(went) == _yes_no(selection))
        if market == ROUND_TOTALS:
            if line is None:
                raise SettlementInputError("round totals need a line")
            elapsed = result.extras.get("elapsed_rounds")
            if elapsed is None:
                raise SettlementInputError("the result has no rounds_completed, round + time, or decision with scheduled rounds")
            return settle_threshold(float(elapsed), line, over=_over_under(selection))
        if market == FIGHT_DRAW:
            return self._settle_draw(result, selection)
        if market == METHOD_OF_VICTORY:
            return self._settle_method(result, selection)
        if market == ROUND_BETTING:
            return self._settle_round_betting(result, selection)
        raise SettlementInputError(f"{self.display_name} has no market {market!r}")

    @staticmethod
    def _settle_draw(result: NormalizedResult, selection: str) -> LegStatus:
        method = result.extras.get("method")
        if result.winner is None and method is None:
            raise SettlementInputError("the result names no winner and no method")
        sel = norm(selection)
        if sel in ("draw", "yes", "y", "x", "tie"):
            wants = True
        elif sel in ("no", "n", "no_draw"):
            wants = False
        else:
            raise SettlementInputError(f"draw selection {selection!r} is not yes/no")
        return _status((result.winner == "draw" or method == DRAW) == wants)

    def _settle_method(self, result: NormalizedResult, selection: str) -> LegStatus:
        """Method of victory: a method alone (``"ko_tko"``: either fighter), or fighter + method (``"home_ko_tko"``,
        ``"a:submission"``, ``"b_decision"``, ``"home_inside_distance"``), or ``"draw"``. The draw is its own outcome:
        a fighter selection loses on it."""
        method = result.extras.get("method")
        if method is None:
            raise SettlementInputError("the result has no method")
        parsed = self.parse_selection(selection, result)
        if parsed.method == DRAW:
            return _status(method == DRAW)
        if parsed.side is None and parsed.method is None:
            raise SettlementInputError(f"method selection {selection!r} names neither a fighter nor a method")
        if method == DRAW:
            return LegStatus.LOST
        return _status(_method_matches(parsed.method, method) and (parsed.side is None or self._side_won(parsed.side, result)))

    def _settle_round_betting(self, result: NormalizedResult, selection: str) -> LegStatus:
        """Round betting: ``"home_r2"``, ``"a:round_3"``, ``"b_ko_r1"`` (fighter, round, optional method), ``"decision"`` /
        ``"home_decision"``, or ``"draw"``. A finish in another round, a decision against a round pick, or the draw loses."""
        method = result.extras.get("method")
        if method is None and result.winner is None:
            raise SettlementInputError("the result has no method")
        parsed = self.parse_selection(selection, result)
        if parsed.method == DRAW:
            return _status(method == DRAW or result.winner == "draw")
        if method is None:
            raise SettlementInputError("the result has no method")
        if method == DRAW:
            return LegStatus.LOST
        side_ok = parsed.side is None or self._side_won(parsed.side, result)
        if parsed.method == DECISION:
            return _status(method == DECISION and side_ok)
        if parsed.round is None:
            raise SettlementInputError(f"round selection {selection!r} names no round")
        if method == DECISION:
            return LegStatus.LOST
        actual_round = result.extras.get("round")
        if actual_round is None:
            raise SettlementInputError("the result has no finishing round")
        return _status(side_ok and actual_round == parsed.round and _method_matches(parsed.method, method))

    # ------------------------------------------------------------------ selection parsing
    @staticmethod
    def parse_selection(selection: str, result: NormalizedResult) -> ParsedSelection:
        """``"ko_tko"``, ``"home_ko_tko"``, ``"a:submission"``, ``"b_decision"``, ``"home_r2"``, ``"a:round_3"``,
        ``"jones_ko_r1"``, ``"draw"``: a fighter (side token, corner or name), a method and a round."""
        toks = _tokens(selection)
        side: str | None = None
        for name, label in ((result.home_name, "home"), (result.away_name, "away")):
            name_toks = _tokens(name)
            if name_toks and toks[: len(name_toks)] == name_toks:
                side, toks = label, toks[len(name_toks):]
                break
        method: str | None = None
        rnd: int | None = None
        words: list[str] = []
        for t in toks:
            match = _ROUND_TOKEN.match(t)
            if match or t.isdigit():
                rnd = int(match.group(1)) if match else int(t)
                continue
            if side is None and t in _HOME_WORDS | _AWAY_WORDS:
                side = "home" if t in _HOME_WORDS else "away"
                continue
            found = _selection_method(t, toks)
            if found in (DRAW, FINISH):
                method = found
            elif found is not None:
                method = method or found
            elif t not in _FILLER:
                words.append(t)
        if side is None and words:
            side = "name:" + "_".join(words)
        return ParsedSelection(side, method, rnd)

    @staticmethod
    def _side_won(side: str, result: NormalizedResult) -> bool:
        if side.startswith("name:"):
            name = side[5:]
            if result.winner_name:
                return name == result.winner_name
            if result.winner in ("home", "away"):
                actual = result.home_name if result.winner == "home" else result.away_name
                if actual:
                    return name == actual
            raise SettlementInputError(f"cannot match fighter {name!r} to the result")
        if result.winner in ("home", "away", "draw"):
            return side == result.winner
        if result.winner_name:
            names = {"home": result.home_name, "away": result.away_name}
            if names.get(side):
                return names[side] == result.winner_name
        raise SettlementInputError("the result names no winning side")


def _logit(p: float) -> float:
    p = clamp(p, 1e-9, 1.0 - 1e-9)
    return math.log(p / (1.0 - p))


def _method_matches(wanted: str | None, actual: str) -> bool:
    if wanted is None:
        return True
    if wanted == FINISH:
        return actual in (KO_TKO, SUBMISSION)
    return wanted == actual


def _seconds(value: Any) -> float | None:
    """``"2:30"`` -> 150.0; a number is seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        mins, _, secs = text.partition(":")
        try:
            return float(mins) * 60.0 + float(secs)
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _yes_no(selection: str) -> bool:
    tokens = split_selection(selection)
    if any(t in ("yes", "y", "distance", "goes") for t in tokens):
        return True
    if any(t in ("no", "n", "inside") for t in tokens):
        return False
    raise SettlementInputError(f"selection {selection!r} names neither yes nor no")


def _over_under(selection: str) -> bool:
    tokens = split_selection(selection)
    if "over" in tokens:
        return True
    if "under" in tokens:
        return False
    raise SettlementInputError(f"selection {selection!r} names neither over nor under")


def _status(won: bool) -> LegStatus:
    return LegStatus.WON if won else LegStatus.LOST


def _selection_method(word: str, words: Sequence[str]) -> str | None:
    if word in _DRAW_WORDS:
        return DRAW
    if word in ("inside", "itd", "finish", "finishes"):
        return FINISH
    if word == "distance":
        return FINISH if "inside" in words else DECISION
    for vocabulary, method in ((_SUB_WORDS, SUBMISSION), (_KO_WORDS, KO_TKO), (_DEC_WORDS, DECISION)):
        if word in vocabulary:
            return method
    return None


def _elapsed_rounds(raw: Mapping[str, Any], method: str | None, rnd: float | None, scheduled: float | None) -> float | None:
    """Elapsed fight time in rounds: ``rounds_completed``, else ``(round - 1) + seconds / round length`` from a
    ``time`` ("2:30") or ``time_seconds``, else the scheduled rounds after a decision or a draw."""
    elapsed = num(raw, "rounds_completed", "elapsed_rounds")
    if elapsed is not None:
        return elapsed
    round_minutes = num(raw, "round_minutes", default=BASE_ROUND_MINUTES) or BASE_ROUND_MINUTES
    if rnd is not None:
        secs = _seconds(raw.get("time", raw.get("time_seconds", raw.get("seconds"))))
        if secs is not None:
            return (rnd - 1.0) + clamp(secs / (round_minutes * 60.0), 0.0, 1.0)
    if method in (DECISION, DRAW) and scheduled is not None:
        return scheduled
    return None


def _went_distance(method: str | None, elapsed: float | None, scheduled: float | None) -> bool | None:
    if method in (DECISION, DRAW):
        return True
    if method in (KO_TKO, SUBMISSION):
        return False
    if elapsed is not None and scheduled is not None:
        return elapsed >= scheduled - 1e-9
    return None
