"""
Baseball Quantitative Adapter: BaseRuns, Pitch-Count Decay, Negative-Binomial Runs & NRFI Markov Chain (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Run production is David Smyth's **BaseRuns** ``Runs = A x B / (B + C) + D`` with
``A = H + BB + HBP - HR - 0.5 IBB`` (base runners), ``B = 0.78 1B + 1.15 2B + 1.58 3B + 1.85 HR + 0.16 SB - 0.22 CS``
(advancement), ``C = 27`` outs (or the ``outs`` given) and ``D = HR``. The specification's linear ``B`` understates
the absolute level (a 2023 league-average line scores about 3.7 instead of 4.5), so the offence enters the run model
as the index ``BsR(team) / BsR(league line) x league runs per game``: the formula keeps its shape, the league sets
its level. Per-9 inputs (hits, walks, home runs, total bases, stolen bases) are split into 1B/2B/3B from H, TB and
HR: the extra bases beyond the home runs (``TB - H - 3 HR``) are attributed to doubles (triples only when the extra
bases exceed the non-HR hits).

The opposing pitching staff scales the offence by ``weighted ERA / league ERA``: the starter's FIP worsens
``+0.12`` per 15 pitches after pitch 75 (``sp_pitch_count_per_inning`` x outs / 3 pitches thrown) and he is lifted
after ``sp_expected_innings`` (5.2 in baseball notation = 5 2/3) or as soon as his decayed FIP exceeds the bullpen
ERA, whichever comes first; the bullpen covers the rest of the 27 outs.

Four independent sub-models vote (fortress pillar 1):

1. **baseruns_negbin** (weight 0.45): per-team negative-binomial runs (dispersion 4.5 per nine innings, so a team
   averaging 4.5 runs has a standard deviation of 3.0, as in MLB), composed inning-exactly: the home team does not
   bat in the bottom of the 9th when ahead, a bottom-9th (or extra-inning) winning run ends the game by one run (the
   walk-off), and a tie after nine goes to extra innings played with the automatic runner on second (RE24 state
   "runner on 2nd, 0 out") until one side leads after a full inning. Extra-inning wins are re-weighted to the home
   team's 52% walk-off edge (or ``extra_innings_home_win_prob``).
2. **pythagenpat** (0.25): each team's season win expectation ``RS^x / (RS^x + RA^x)`` with the Pythagenpat
   exponent ``x = (RS + RA)^0.287`` per game, combined by log5 with a 0.54 home edge.
3. **pitcher_fip_matchup** (0.20): the run prevention implied by each staff's starter/bullpen weighted ERA,
   as a Pythagenpat win expectation against a league-average offence, combined by log5 with the home edge.
4. **elo** (0.10, only when both teams carry ``elo``): logistic on the rating gap plus 24 home Elo points.

Game props: NRFI/YRFI blends a 24-state base-out Markov chain of the first half-inning (lineup OBP and HR rate
against the opposing starter), the specification's top-3 "traffic" formula ``1 - prod(1 - OBP_i) prod(1 - HR_j)``
rescaled to a scoring probability, and a clustered-Poisson on the starter's first-inning ERA. First five innings
(F5) price the starters' duel; the inning of the highest score is exact under independent inning distributions.

Context: park run factors (Coors 1.34 ... Petco 0.88, 30 MLB parks), weather in outdoor parks (+1.5% runs per 10F
above 70F, -1.5% per 10F below; wind blowing out +3% per 5 mph, in -3% per 5 mph; The Wire's friction factor
replaces this when present), the plate umpire (``strike_zone_run_factor`` multiplier or ``runs_per_game_delta``) and
absences (``WAR x 10 / 162`` runs per game: hitters off their own team, pitchers onto the opponent).
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, override

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext, Weather
from app.domain.sports.quant import IntPMF, ScoreGrid, SubModelEstimate, clamp, logistic, negative_binomial_pmf
from app.domain.sports.settlement import (
    ALT_SPREAD,
    ALT_TOTAL,
    MONEYLINE,
    NEXT_EVENT,
    PERIOD_MONEYLINE,
    PERIOD_SPREAD,
    PERIOD_TOTAL,
    PLAYER_PROP,
    SPREAD,
    TEAM_TOTAL,
    TOTAL,
    NormalizedResult,
    SettlementInputError,
    norm,
    normalize_basic,
    num,
    split_selection,
)
from app.schemas.bet_types import LegStatus

# ================================================================ league constants
LEAGUE_RUNS_PER_GAME = 4.50  # runs per team per nine innings
LEAGUE_ERA = 4.20
RUN_DISPERSION = 4.5  # negative-binomial r over nine innings: variance = m + m^2 / 4.5
INNING_DISPERSION = RUN_DISPERSION / 9.0  # r of one half-inning; NB(r, p) sums keep p, so innings add exactly
RUN_GRID = 26  # 0..25 runs a side over nine innings, tail folded into the last cell
INNING_GRID = 12
EXTRA_GRID = 16
EXTRA_INNINGS_HOME_WIN = 0.52
HOME_FIELD_RUN_FACTOR = 0.035  # home runs x 1.035, away x 0.965: about a 53.5% home win rate between equal teams
PYTHAGENPAT_POWER = 0.287
LOG5_HOME_EDGE = 0.54
ELO_HOME_ADVANTAGE = 24.0

# starter fatigue and the bullpen (specification: +0.12 FIP per 15 pitches after pitch 75, bullpen after 5.2)
SP_EXPECTED_INNINGS = 5.2
SP_PITCHES_PER_INNING = 16.0
DECAY_START_PITCH = 75.0
DECAY_PER_15_PITCHES = 0.12
BULLPEN_ERA = 4.10

RUNS_PER_WAR_PER_GAME = 10.0 / 162.0
PITCHER_POSITIONS = frozenset({"sp", "rp", "p", "pitcher"})

# weather (outdoor parks only)
TEMP_RUN_EFFECT_PER_10F = 0.015
WIND_RUN_EFFECT_PER_5MPH = 0.03

# Approximate multi-year run park factors (1.00 = neutral; FanGraphs/Statcast-style, rounded). The first seven keys
# are the reference's.
PARK_FACTORS: dict[str, float] = {
    "coors_field": 1.34,
    "great_american": 1.15,
    "fenway_park": 1.08,
    "yankee_stadium": 1.04,
    "dodger_stadium": 0.98,
    "petco_park": 0.88,
    "standard": 1.00,
    "great_american_ball_park": 1.15,
    "kauffman_stadium": 1.05,
    "chase_field": 1.04,
    "citizens_bank_park": 1.04,
    "wrigley_field": 1.02,
    "truist_park": 1.01,
    "rogers_centre": 1.01,
    "guaranteed_rate_field": 1.01,
    "nationals_park": 1.00,
    "american_family_field": 1.00,
    "oriole_park_at_camden_yards": 0.98,
    "target_field": 0.99,
    "minute_maid_park": 0.99,
    "angel_stadium": 0.98,
    "busch_stadium": 0.97,
    "pnc_park": 0.97,
    "comerica_park": 0.97,
    "progressive_field": 0.97,
    "globe_life_field": 0.97,
    "citi_field": 0.95,
    "tropicana_field": 0.94,
    "oakland_coliseum": 0.94,
    "loandepot_park": 0.93,
    "oracle_park": 0.92,
    "t_mobile_park": 0.91,
}
FIXED_DOMES = frozenset({"tropicana_field"})
RETRACTABLE_ROOFS = frozenset({"rogers_centre", "chase_field", "minute_maid_park", "american_family_field", "t_mobile_park",
                               "loandepot_park", "globe_life_field"})

# league batting line per nine innings (the reference's defaults) plus league HBP / IBB / CS rates
LEAGUE_LINE: dict[str, float] = {"hits_per_9": 8.2, "walks_per_9": 3.1, "home_runs_per_9": 1.1, "total_bases_per_9": 13.5,
                                 "stolen_bases_per_9": 0.7}
LEAGUE_HBP, LEAGUE_IBB, LEAGUE_CS = 0.40, 0.08, 0.19
DOUBLE_SHARE_OF_NON_HR_HITS, TRIPLE_SHARE_OF_NON_HR_HITS = 0.235, 0.02

# RE24 run expectancy (MLB 2010-2015) and P(at least one run scores) by base-out state; bases are a bit mask
# (1 = first, 2 = second, 4 = third), columns are 0, 1, 2 outs.
RE24: dict[int, tuple[float, float, float]] = {
    0: (0.481, 0.254, 0.098), 1: (0.859, 0.509, 0.224), 2: (1.100, 0.664, 0.319), 4: (1.357, 0.950, 0.353),
    3: (1.437, 0.884, 0.429), 5: (1.784, 1.130, 0.478), 6: (1.964, 1.376, 0.580), 7: (2.292, 1.541, 0.752),
}
SCORE_PROB: dict[int, tuple[float, float, float]] = {
    0: (0.268, 0.155, 0.067), 1: (0.416, 0.265, 0.127), 2: (0.614, 0.397, 0.218), 4: (0.836, 0.654, 0.262),
    3: (0.617, 0.410, 0.224), 5: (0.857, 0.632, 0.272), 6: (0.846, 0.685, 0.265), 7: (0.866, 0.656, 0.315),
}
MANFRED_RUNNER_STATE = 2  # extra innings start with a runner on second

# Inning run factors (mean 1): the 1st inning is above average because the top of the order is guaranteed to bat,
# the 2nd below (the bottom of the order), the late innings face high-leverage relievers.
INNING_RUN_FACTORS = (1.12, 0.92, 1.00, 1.02, 1.02, 1.02, 0.98, 0.98, 0.94)

# NRFI/YRFI
LEAGUE_OBP, LEAGUE_HR_RATE, LEAGUE_SLG = 0.315, 0.030, 0.410
LINEUP_OBP_UPLIFT = (1.05, 1.06, 1.06, 1.03, 1.00, 0.98, 0.96, 0.94, 0.92)  # batting-order spots 1..9 (mean 1)
LINEUP_HR_UPLIFT = (0.80, 1.05, 1.25, 1.25, 1.10, 1.00, 0.90, 0.85, 0.80)
WALK_SHARE, DOUBLE_SHARE, TRIPLE_SHARE = 0.33, 0.155, 0.015  # of non-HR times on base; singles take the rest
PRODUCTIVE_OUT_SCORES = 0.25  # an out with a runner on third and < 2 outs scores him (sac fly / ground ball)
FIRST_INNING_SCORE_RATE = 0.285  # MLB share of first half-innings with a run (YRFI about 0.49)
FIRST_INNING_ERA_FACTOR = 1.12  # first-inning ERA vs full-game FIP when no first_inning_era is supplied
NRFI_BLEND = {"markov": 0.5, "traffic": 0.2, "first_inning_era": 0.3}

RUN_LINES = (-2.5, -1.5, 1.5, 2.5)
TOTAL_LINES = (6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.5)
TEAM_TOTAL_LINES = (2.5, 3.5, 4.5, 5.5)
F5_TOTAL_LINES = (3.5, 4.0, 4.5, 5.0, 5.5)
SUB_MODEL_WEIGHTS = {"baseruns_negbin": 0.45, "pythagenpat": 0.25, "pitcher_fip_matchup": 0.20, "elo": 0.10}

NRFI_MARKET = "nrfi_yrfi"
INNING_HIGHEST_MARKET = "inning_of_highest_score"


# ================================================================ BaseRuns
@dataclass(frozen=True, slots=True)
class BattingLine:
    singles: float
    doubles: float
    triples: float
    home_runs: float
    walks: float
    hbp: float
    ibb: float
    sb: float
    cs: float
    outs: float

    @property
    def hits(self) -> float:
        return self.singles + self.doubles + self.triples + self.home_runs

    @property
    def total_bases(self) -> float:
        return self.singles + 2.0 * self.doubles + 3.0 * self.triples + 4.0 * self.home_runs

    @property
    def plate_appearances(self) -> float:
        return self.outs + self.hits + self.walks + self.hbp


def split_hits(hits: float, home_runs: float, total_bases: float | None) -> tuple[float, float, float]:
    """1B, 2B, 3B from H, HR and TB: the extra bases beyond the home runs (``TB - H - 3 HR``) are doubles; triples only
    absorb what the non-HR hits cannot carry as doubles. Without TB the league shares of non-HR hits are used."""
    non_hr = max(0.0, hits - home_runs)
    if total_bases is None:
        doubles, triples = DOUBLE_SHARE_OF_NON_HR_HITS * non_hr, TRIPLE_SHARE_OF_NON_HR_HITS * non_hr
        return non_hr - doubles - triples, doubles, triples
    extra = clamp(total_bases - hits - 3.0 * home_runs, 0.0, 2.0 * non_hr)
    if extra <= non_hr:
        return non_hr - extra, extra, 0.0
    triples = extra - non_hr
    return 0.0, non_hr - triples, triples


def batting_line(stats: Mapping[str, Any]) -> BattingLine:
    """A per-game batting line from detailed counts (singles, doubles, triples, home_runs, hits, walks, hbp, ibb, sb, cs,
    outs, total_bases) or the reference's per-9 keys. Missing HBP / IBB / CS take league rates."""
    detailed = any(k in stats for k in ("hits", "singles", "doubles", "triples", "walks"))
    hbp = num(stats, "hbp", "hit_by_pitch", "hbp_per_9", default=LEAGUE_HBP) or 0.0
    ibb = num(stats, "ibb", "intentional_walks", "ibb_per_9", default=LEAGUE_IBB) or 0.0
    cs = num(stats, "cs", "caught_stealing", "caught_stealing_per_9", default=LEAGUE_CS) or 0.0
    outs = num(stats, "outs", default=27.0) or 27.0
    if detailed:
        hr = num(stats, "home_runs", "hr", default=LEAGUE_LINE["home_runs_per_9"]) or 0.0
        singles, doubles, triples = num(stats, "singles"), num(stats, "doubles"), num(stats, "triples")
        hits = num(stats, "hits")
        if singles is None or doubles is None or triples is None:
            if hits is None:
                hits = LEAGUE_LINE["hits_per_9"] if singles is None else singles + (doubles or 0.0) + (triples or 0.0) + hr
            if doubles is not None and triples is not None:
                singles = max(0.0, hits - hr - doubles - triples)
            else:
                singles, doubles, triples = split_hits(hits, hr, num(stats, "total_bases"))
        walks = num(stats, "walks", "bb", default=LEAGUE_LINE["walks_per_9"]) or 0.0
        sb = num(stats, "sb", "stolen_bases", default=LEAGUE_LINE["stolen_bases_per_9"]) or 0.0
        return BattingLine(max(0.0, singles), max(0.0, doubles), max(0.0, triples), max(0.0, hr), max(0.0, walks), hbp, ibb, sb, cs, outs)
    hits = num(stats, "hits_per_9", default=LEAGUE_LINE["hits_per_9"]) or 0.0
    hr = num(stats, "home_runs_per_9", default=LEAGUE_LINE["home_runs_per_9"]) or 0.0
    tb = num(stats, "total_bases_per_9", default=LEAGUE_LINE["total_bases_per_9"])
    singles, doubles, triples = split_hits(hits, hr, tb)
    walks = num(stats, "walks_per_9", default=LEAGUE_LINE["walks_per_9"]) or 0.0
    sb = num(stats, "stolen_bases_per_9", default=LEAGUE_LINE["stolen_bases_per_9"]) or 0.0
    return BattingLine(singles, doubles, triples, max(0.0, hr), max(0.0, walks), hbp, ibb, sb, cs, outs)


def baseruns(line: BattingLine) -> float:
    a = max(0.1, line.hits + line.walks + line.hbp - line.home_runs - 0.5 * line.ibb)
    b = max(0.1, 0.78 * line.singles + 1.15 * line.doubles + 1.58 * line.triples + 1.85 * line.home_runs + 0.16 * line.sb - 0.22 * line.cs)
    c = max(1.0, line.outs)
    return a * b / (b + c) + line.home_runs


def innings_from_notation(value: float) -> float:
    """Baseball innings notation: 5.1 = 5 1/3, 5.2 = 5 2/3. Any other fraction is read as a decimal."""
    whole = math.floor(value)
    frac = value - whole
    if abs(frac - 0.1) < 1e-6:
        return whole + 1.0 / 3.0
    if abs(frac - 0.2) < 1e-6:
        return whole + 2.0 / 3.0
    return value


# ================================================================ pitching
@dataclass(frozen=True, slots=True)
class PitchingProfile:
    sp_fip: float
    bullpen_era: float
    sp_outs: int  # outs the starter records before the hook
    hook_reason: str  # "expected_innings" | "fatigue"
    game_era: float  # 27-out weighted run rate the offence faces
    f5_era: float  # the starter's decayed rate over the first 15 outs

    def as_dict(self) -> dict[str, Any]:
        return {"sp_fip": round(self.sp_fip, 3), "bullpen_era": round(self.bullpen_era, 3), "sp_innings": round(self.sp_outs / 3.0, 3),
                "hook_reason": self.hook_reason, "weighted_era": round(self.game_era, 3), "f5_era": round(self.f5_era, 3)}


def pitching_profile(stats: Mapping[str, Any], league_era: float = LEAGUE_ERA) -> PitchingProfile:
    """Out-by-out run rate of a staff: the starter's FIP decays ``+0.12`` per 15 pitches after pitch 75 and he leaves
    after his expected innings or once (past pitch 75) his decayed FIP exceeds the bullpen ERA."""
    fip = num(stats, "sp_fip", "sp_era", default=league_era) or league_era
    bullpen = num(stats, "bullpen_era", default=BULLPEN_ERA) or BULLPEN_ERA
    innings = innings_from_notation(num(stats, "sp_expected_innings", default=SP_EXPECTED_INNINGS) or SP_EXPECTED_INNINGS)
    per_inning = max(8.0, num(stats, "sp_pitch_count_per_inning", default=SP_PITCHES_PER_INNING) or SP_PITCHES_PER_INNING)
    planned = round(clamp(innings, 0.0, 9.0) * 3.0)
    rates: list[float] = []
    starter_rates: list[float] = []
    sp_outs, hooked, reason = 0, False, "expected_innings"
    for k in range(27):
        pitches = (k + 0.5) * per_inning / 3.0
        decayed = fip + DECAY_PER_15_PITCHES * max(0.0, pitches - DECAY_START_PITCH) / 15.0
        starter_rates.append(decayed)
        fatigued = pitches > DECAY_START_PITCH and decayed > bullpen
        if not hooked and k < planned and not fatigued:
            rates.append(decayed)
            sp_outs += 1
        else:
            if not hooked:
                reason = "fatigue" if (k < planned and fatigued) else "expected_innings"
                hooked = True
            rates.append(bullpen)
    return PitchingProfile(fip, bullpen, sp_outs, reason, float(np.mean(rates)), float(np.mean(starter_rates[:15])))


# ================================================================ run distributions
def nb_innings(mean_per_game: float, halves: int, size: int = RUN_GRID) -> np.ndarray:
    """Runs over ``halves`` half-innings for a team averaging ``mean_per_game`` over nine (NB with r = 0.5 per inning)."""
    if halves <= 0:
        out = np.zeros(size)
        out[0] = 1.0
        return out
    return negative_binomial_pmf(mean_per_game * halves / 9.0, INNING_DISPERSION * halves, size)


def state_runs_pmf(outs: int, bases: int, strength: float, size: int = INNING_GRID) -> np.ndarray:
    """Runs still to score in a half-inning from a base-out state: P(0) = 1 - P(score) and, given a run, a shifted
    geometric with the RE24 conditional mean ``E / P(score)``. Both are scaled to the batting team's strength
    (``strength`` = its expected runs / league): ``P' = 1 - (1 - P)^s``, ``E' = E s``."""
    out = np.zeros(size)
    if outs >= 3:
        out[0] = 1.0
        return out
    s = max(0.05, strength)
    p_score = 1.0 - (1.0 - SCORE_PROB[bases][outs]) ** s
    expect = max(RE24[bases][outs] * s, p_score * 1.0001)
    q = 1.0 - p_score / expect  # geometric continuation probability given at least one run
    out[0] = 1.0 - p_score
    k = np.arange(1, size)
    out[1:] = p_score * (1.0 - q) * q ** (k - 1)
    out[-1] += max(0.0, 1.0 - float(out.sum()))
    return out


def _one_extra_inning(a_dist: np.ndarray, h_dist: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    """One extra inning from a tie: (resolved (home, away) runs matrix, distribution of the runs each side scored
    when it stays tied). The away team scores ``a``; the home team walks off by one run when its half would exceed
    ``a``, loses when it scores less, ties when it scores exactly ``a``."""
    resolved = np.zeros((size, size))
    tied = np.zeros(size)
    h_tail = np.concatenate([np.cumsum(h_dist[::-1])[::-1], [0.0]])
    for a, pa in enumerate(a_dist[: size - 1]):
        if pa < 1e-15:
            continue
        resolved[a + 1, a] += pa * (h_tail[a + 1] if a + 1 < len(h_tail) else 0.0)  # a one-run walk-off
        k = min(a, len(h_dist))
        resolved[:k, a] += pa * h_dist[:k]
        if a < len(h_dist):
            tied[a] += pa * h_dist[a]
    return resolved, tied


def extras_joint(away_first: np.ndarray, home_first: np.ndarray, away_next: np.ndarray, home_next: np.ndarray,
                 max_innings: int = 8) -> tuple[np.ndarray, float]:
    """Joint (home extra runs, away extra runs) of extra innings from a tie, and the home share: inning by inning the
    resolved outcomes are added at the runs both sides carried while level (``c``, ``c``); the negligible remainder
    after ``max_innings`` is split as one-run decisions."""
    size = EXTRA_GRID
    joint = np.zeros((size, size))
    first = _one_extra_inning(away_first, home_first, size)
    following = _one_extra_inning(away_next, home_next, size)
    carry = np.zeros(size)
    carry[0] = 1.0
    for inning in range(max_innings):
        resolved, tied = first if inning == 0 else following
        for c in np.nonzero(carry > 1e-15)[0]:
            span = size - c
            joint[c:, c:] += carry[c] * resolved[:span, :span]
        carry = np.convolve(carry, tied)[:size]
    for c in np.nonzero(carry > 1e-15)[0]:
        k = min(int(c), size - 2)
        joint[k + 1, k] += 0.5 * carry[c]
        joint[k, k + 1] += 0.5 * carry[c]
    joint /= joint.sum()
    return joint, float(np.tril(joint, -1).sum())


def trimmed(pmf: IntPMF, eps: float = 1e-7) -> IntPMF:
    """The PMF without its negligible tails: the cells below ``eps`` outside the bulk are folded into the outermost
    kept cell, so every line strictly inside the kept range prices exactly as before (only the speed changes)."""
    keep = np.nonzero(pmf.probs > eps)[0]
    if len(keep) == 0:
        return pmf
    lo, hi = int(keep[0]), int(keep[-1])
    probs = pmf.probs[lo: hi + 1].copy()
    probs[0] += pmf.probs[:lo].sum()
    probs[-1] += pmf.probs[hi + 1:].sum()
    return IntPMF(pmf.offset + lo, probs)


def fold_grid(matrix: np.ndarray, size: int) -> np.ndarray:
    """The grid cut to ``size`` x ``size`` with the tail rows / columns folded into the last ones."""
    if matrix.shape[0] <= size:
        return matrix
    out = matrix[:size, :size].copy()
    out[size - 1, :] += matrix[size:, :size].sum(axis=0)
    out[:, size - 1] += matrix[:size, size:].sum(axis=1)
    out[size - 1, size - 1] += matrix[size:, size:].sum()
    return out


def compose_game(h_pre: np.ndarray, a_total: np.ndarray, h_last: np.ndarray, extras: np.ndarray, *,
                 home_offset: int = 0, away_offset: int = 0) -> tuple[np.ndarray, float, float]:
    """The final score grid from: home runs before its last regulation half (``h_pre``), away runs through the top of
    the 9th (``a_total``), the home team's last half (``h_last``) and the extra-innings joint. The home team skips its
    last half when ahead; scoring past the tie in it is a one-run walk-off; a tie goes to extras.
    Returns (final matrix, P(extra innings), P(home skips its last half))."""
    size = max(len(h_pre) + home_offset, len(a_total) + away_offset) + extras.shape[0] + 2
    final = np.zeros((size, size))
    tie = np.zeros(size)
    tail = np.concatenate([np.cumsum(h_last[::-1])[::-1], [0.0]])
    skip = 0.0
    n_last = len(h_last)
    for jj, pa in enumerate(a_total):
        if pa < 1e-14:
            continue
        j = jj + away_offset
        for ii, ph in enumerate(h_pre):
            m = pa * ph
            if m < 1e-14:
                continue
            i = ii + home_offset
            if i > j:
                final[i, j] += m
                skip += m
                continue
            d = j - i
            final[j + 1, j] += m * (tail[d + 1] if d + 1 < len(tail) else 0.0)
            if d < n_last:
                tie[j] += m * h_last[d]
            k = min(d, n_last)
            final[i: i + k, j] += m * h_last[:k]
    ext = extras.shape[0]
    for t, m in enumerate(tie):
        if m < 1e-14:
            continue
        hi = min(ext, size - t)
        final[t: t + hi, t: t + hi] += m * extras[:hi, :hi]
    return final, float(tie.sum()), skip


# ================================================================ first inning (NRFI / YRFI)
def _advance(bases: int, event: str) -> tuple[int, bool]:
    """Base state after a non-out event and whether a run scored (documented simplification: a single scores runners
    from second and third and moves a runner on first to second; a double scores runners from second and third and
    moves a runner on first to third; walks force; triples and homers clear the bases)."""
    on1, ahead = bool(bases & 1), bool(bases & 6)
    if event == "walk":
        forced = 1 if not on1 else 3 if not bases & 2 else 7
        return bases | forced, bases == 7
    moves = {"single": ((1 | (2 if on1 else 0)), ahead), "double": ((2 | (4 if on1 else 0)), ahead), "triple": (4, bases != 0)}
    return moves.get(event, (0, True))  # anything else is a home run


def first_half_inning_markov(obps: Sequence[float], hr_rates: Sequence[float], xbh_scales: Sequence[float], reach_factor: float) -> float:
    """P(at least one run) in a half-inning that starts with batter 1, from the 24 base-out states. The batter due is
    ``outs + runners on`` (no run has scored yet), so the 24 states carry the lineup position exactly. Outs do not
    advance runners except the productive out (runner on third, < 2 outs, probability 0.25); double plays ignored."""
    events: list[dict[str, float]] = []
    for i in range(9):
        hr = clamp(hr_rates[i] * reach_factor, 0.0, 0.15)
        on = clamp(obps[i] * reach_factor, hr + 0.01, 0.75)
        rest = on - hr
        xb = clamp(xbh_scales[i], 0.4, 2.5)
        dbl, trp, walk = DOUBLE_SHARE * xb * rest, TRIPLE_SHARE * xb * rest, WALK_SHARE * rest
        single = max(0.0, rest - dbl - trp - walk)
        events.append({"walk": walk, "single": single, "double": dbl, "triple": trp, "home_run": hr, "out": 1.0 - on})
    states: dict[tuple[int, int], float] = {(0, 0): 1.0}
    scored = 0.0
    while states:
        nxt: dict[tuple[int, int], float] = {}
        for (outs, bases), mass in states.items():
            batter = (outs + bin(bases).count("1")) % 9
            ev = events[batter]
            for name, p in ev.items():
                m = mass * p
                if m <= 0.0:
                    continue
                if name == "out":
                    if bases & 4 and outs < 2:
                        scored += m * PRODUCTIVE_OUT_SCORES
                        m *= 1.0 - PRODUCTIVE_OUT_SCORES
                    if outs + 1 < 3:
                        nxt[(outs + 1, bases)] = nxt.get((outs + 1, bases), 0.0) + m
                    continue
                new_bases, run = _advance(bases, name)
                if run:
                    scored += m
                else:
                    nxt[(outs, new_bases)] = nxt.get((outs, new_bases), 0.0) + m
        states = nxt
    return clamp(scored, 0.0, 1.0)


def top_three_traffic(obps: Sequence[float], hr_rates: Sequence[float], reach_factor: float = 1.0) -> float:
    """The specification's ``1 - prod(1 - OBP_i) prod(1 - HR_j)`` over the top three batters (it measures traffic: at
    least one of them reaching or homering)."""
    stay = 1.0
    for i in range(3):
        stay *= (1.0 - clamp(obps[i] * reach_factor, 0.0, 0.95)) * (1.0 - clamp(hr_rates[i] * reach_factor, 0.0, 0.5))
    return 1.0 - stay


LEAGUE_TRAFFIC = top_three_traffic([LEAGUE_OBP * u for u in LINEUP_OBP_UPLIFT], [LEAGUE_HR_RATE * u for u in LINEUP_HR_UPLIFT])
TRAFFIC_TO_SCORING = FIRST_INNING_SCORE_RATE / LEAGUE_TRAFFIC  # converts traffic into P(score) at league level


# ================================================================ live state
FINAL_STATUSES = frozenset({"final", "finished", "complete", "completed", "ended"})


@dataclass(frozen=True, slots=True)
class LiveState:
    inning: int
    half: str  # "top" | "bottom"
    outs: int
    bases: int  # bit mask: 1 = first, 2 = second, 4 = third
    home: int
    away: int
    is_final: bool

    @classmethod
    def parse(cls, live: Mapping[str, Any]) -> LiveState:
        home = int(num(live, "home_runs", "home_score", default=0.0) or 0)
        away = int(num(live, "away_runs", "away_score", default=0.0) or 0)
        inning = max(1, int(num(live, "inning", default=1.0) or 1))
        half = "bottom" if norm(live.get("half")) in ("bottom", "bot", "b", "home") else "top"
        outs = int(clamp(num(live, "outs", default=0.0) or 0.0, 0.0, 3.0))
        bases = 0
        raw_bases = live.get("bases") or []
        for b in raw_bases if isinstance(raw_bases, (list, tuple, set)) else []:
            base = int(num({"b": b}, "b", default=0.0) or 0)
            if base in (1, 2, 3):
                bases |= 1 << (base - 1)
        if outs >= 3:  # the half-inning is over: move to the next one
            outs, bases = 0, 0
            half, inning = ("bottom", inning) if half == "top" else ("top", inning + 1)
        final = norm(live.get("status")) in FINAL_STATUSES
        final = final or (inning > 9 and half == "top" and home != away)  # a full extra inning ended with a leader
        final = final or (inning >= 9 and half == "bottom" and home > away)  # walk-off / home led after the top of the 9th
        return cls(inning, half, outs, bases, home, away, final)


# ================================================================ the adapter
@dataclass(frozen=True)
class RunModel:
    home_runs: float  # expected runs over nine innings
    away_runs: float
    f5_home_runs: float
    f5_away_runs: float
    home_offence: float  # calibrated BaseRuns vs league pitching, neutral park
    away_offence: float
    home_pitching: PitchingProfile
    away_pitching: PitchingProfile
    environment: float  # park x weather x umpire
    park: str
    metrics: dict[str, Any]


class BaseballSportAdapter(BaseSportAdapter):
    """
    Quantitative Baseball Engine implementing David Smyth's BaseRuns (BsR)
    run estimation formula, starting pitcher pitch count decay,
    and first-inning NRFI/YRFI joint probabilities.
    """

    PARK_FACTORS: ClassVar[dict[str, float]] = PARK_FACTORS
    kelly_multiplier = 0.35
    model_family = "BaseRuns BsR & NRFI Markov"
    has_draw = False
    odds_api_family = "baseball"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner": (MONEYLINE, {}),
        "money_line": (MONEYLINE, {}),
        "run_line_handicap": (SPREAD, {}),
        "run_line": (SPREAD, {}),
        "alt_run_line": (ALT_SPREAD, {}),
        "game_total_runs": (TOTAL, {}),
        "total_runs": (TOTAL, {}),
        "alt_total_runs": (ALT_TOTAL, {}),
        "team_total_runs": (TEAM_TOTAL, {}),
        "nrfi_yrfi_first_inning": (NRFI_MARKET, {}),
        "nrfi": (NRFI_MARKET, {}),
        "yrfi": (NRFI_MARKET, {}),
        "first_inning_winner": (PERIOD_MONEYLINE, {"period": "i1"}),
        "first_inning_total": (PERIOD_TOTAL, {"period": "i1"}),
        "first_5_innings_winner": (PERIOD_MONEYLINE, {"period": "f5", "level_is_void": True}),
        "first_5_innings_3_way": (PERIOD_MONEYLINE, {"period": "f5"}),
        "first_5_innings_run_line": (PERIOD_SPREAD, {"period": "f5"}),
        "first_5_innings_total": (PERIOD_TOTAL, {"period": "f5"}),
        "highest_scoring_inning": (INNING_HIGHEST_MARKET, {}),
        "next_team_to_score": (NEXT_EVENT, {"event_type": "run"}),
        "player_hits": (PLAYER_PROP, {"stat": "hits"}),
        "player_total_bases": (PLAYER_PROP, {"stat": "total_bases"}),
        "player_home_runs": (PLAYER_PROP, {"stat": "home_runs"}),
        "player_strikeouts": (PLAYER_PROP, {"stat": "strikeouts"}),
        "pitcher_strikeouts": (PLAYER_PROP, {"stat": "strikeouts"}),
    }
    SPECIAL_MARKETS = frozenset({NRFI_MARKET, INNING_HIGHEST_MARKET})
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE, PERIOD_SPREAD,
                                   PERIOD_TOTAL, PLAYER_PROP, NEXT_EVENT})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "round_robin", "if_bet",
                           "double_action", "futures", "player_props", "game_props", "live_next_event", "first_5_innings",
                           "inning_of_highest_score", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "baseball"

    @property
    def display_name(self) -> str:
        return "Baseball"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "moneyline",
            "run_line_handicap",
            "game_total_runs",
            "nrfi_yrfi_first_inning",
            "first_5_innings_winner",
            "first_5_innings_run_line",
            "first_5_innings_total",
            "team_total_runs",
            "inning_of_highest_score",
            "player_strikeouts",
        ]

    # ------------------------------------------------------------------ formulas
    def calculate_baseruns(self, stats: Mapping[str, Any]) -> float:
        """BaseRuns ``A x B / (B + C) + D`` on the team's per-game batting line (detailed or per-9 inputs)."""
        return baseruns(batting_line(stats))

    def park_factor(self, ctx: MatchContext) -> tuple[str, float]:
        park = norm(ctx.get("park") or ctx.get("venue") or "standard")
        explicit = num(ctx.raw, "park_factor")
        if explicit is not None and explicit > 0:
            return park, explicit
        return park, self.PARK_FACTORS.get(park, 1.0)

    @staticmethod
    def weather_multiplier(weather: Weather | None, park: str, roof: str = "") -> float:
        """Outdoor parks only: temperature +/-1.5% runs per 10F around 70F, wind out +3% / in -3% per 5 mph (cross
        none); The Wire's friction factor replaces this when present. Fixed domes, closed (or unspecified) retractable
        roofs and ``indoor`` weather apply nothing."""
        if weather is None or weather.indoor or park in FIXED_DOMES or (park in RETRACTABLE_ROOFS and roof != "open"):
            return 1.0
        if weather.friction is not None:
            return clamp(weather.friction, 0.6, 1.4)
        mult = 1.0
        temp_f = weather.temperature_f
        if temp_f is not None:
            mult *= 1.0 + TEMP_RUN_EFFECT_PER_10F * (temp_f - 70.0) / 10.0
        mph = weather.wind_mph
        if mph is not None and weather.wind_direction in ("out", "blowing_out", "out_to_center"):
            mult *= 1.0 + WIND_RUN_EFFECT_PER_5MPH * mph / 5.0
        elif mph is not None and weather.wind_direction in ("in", "blowing_in", "in_from_center"):
            mult *= 1.0 - WIND_RUN_EFFECT_PER_5MPH * mph / 5.0
        return clamp(mult, 0.70, 1.35)

    def run_model(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> RunModel:
        league_runs = ctx.number("league_runs_per_game", LEAGUE_RUNS_PER_GAME)
        league_era = ctx.number("league_era", LEAGUE_ERA)
        bsr_league = baseruns(batting_line(LEAGUE_LINE))
        bsr_home, bsr_away = self.calculate_baseruns(home_stats), self.calculate_baseruns(away_stats)
        off_home, off_away = bsr_home / bsr_league * league_runs, bsr_away / bsr_league * league_runs
        pitch_home, pitch_away = pitching_profile(home_stats, league_era), pitching_profile(away_stats, league_era)
        park, park_mult = self.park_factor(ctx)
        weather_mult = self.weather_multiplier(ctx.weather, park, ctx.text("roof", ""))
        umpire_mult, umpire_delta = 1.0, 0.0
        metrics: dict[str, Any] = {"home_baseruns_raw": round(bsr_home, 3), "away_baseruns_raw": round(bsr_away, 3),
                                   "league_baseruns_raw": round(bsr_league, 3), "park": park, "park_factor": park_mult,
                                   "weather_run_multiplier": round(weather_mult, 4)}
        if ctx.officials is not None:
            factor = ctx.officials.rate("strike_zone_run_factor", "run_factor")
            delta = ctx.officials.rate("runs_per_game_delta")
            if factor is not None and factor > 0:
                umpire_mult = clamp(factor, 0.8, 1.2)
                metrics["umpire_run_factor"] = round(umpire_mult, 4)
            if delta is not None:
                umpire_delta = clamp(delta, -2.0, 2.0)
                metrics["umpire_runs_per_game_delta"] = round(umpire_delta, 3)
        env = park_mult * weather_mult * umpire_mult
        hfa = ctx.number("home_field_run_factor", HOME_FIELD_RUN_FACTOR)
        home = off_home * pitch_away.game_era / league_era * env * (1.0 + hfa) + umpire_delta / 2.0
        away = off_away * pitch_home.game_era / league_era * env * (1.0 - hfa) + umpire_delta / 2.0
        f5_home = off_home * 5.0 / 9.0 * pitch_away.f5_era / league_era * env * (1.0 + hfa) + umpire_delta * 5.0 / 18.0
        f5_away = off_away * 5.0 / 9.0 * pitch_home.f5_era / league_era * env * (1.0 - hfa) + umpire_delta * 5.0 / 18.0

        # absences (pillar 4): hitters' WAR off their own runs, pitchers' WAR onto the opponent's
        for side in ("home", "away"):
            pitchers = ctx.war_lost(side, PITCHER_POSITIONS)
            hitters = ctx.war_lost(side) - pitchers
            own, opp = hitters * RUNS_PER_WAR_PER_GAME, pitchers * RUNS_PER_WAR_PER_GAME
            if side == "home":
                home, away = home - own, away + opp
                f5_home, f5_away = f5_home - own * 5.0 / 9.0, f5_away + opp * 5.0 / 9.0
            else:
                away, home = away - own, home + opp
                f5_away, f5_home = f5_away - own * 5.0 / 9.0, f5_home + opp * 5.0 / 9.0
            if ctx.absences:
                metrics[f"{side}_absence_runs_lost"] = round(own, 4)
                metrics[f"{side}_absence_runs_conceded"] = round(opp, 4)
        return RunModel(max(0.5, home), max(0.5, away), max(0.25, f5_home), max(0.25, f5_away), off_home, off_away, pitch_home,
                        pitch_away, env, park, metrics)

    # ------------------------------------------------------------------ sub-models
    def game_grid(self, rm: RunModel, ctx: MatchContext) -> tuple[ScoreGrid, float, float]:
        league = ctx.number("league_runs_per_game", LEAGUE_RUNS_PER_GAME)
        a_total = nb_innings(rm.away_runs, 9)
        h_pre = nb_innings(rm.home_runs, 8)
        h_last = nb_innings(rm.home_runs, 1, INNING_GRID)
        extras = self._extras(rm.home_runs / league, rm.away_runs / league, ctx)
        final, p_extra, p_skip = compose_game(h_pre, a_total, h_last, extras)
        return ScoreGrid.normalised(fold_grid(final, RUN_GRID)), p_extra, p_skip

    def _extras(self, home_strength: float, away_strength: float, ctx: MatchContext,
                away_first: np.ndarray | None = None, home_first: np.ndarray | None = None) -> np.ndarray:
        if ctx.get("extra_inning_runner", True) is False:
            a_next = negative_binomial_pmf(away_strength * LEAGUE_RUNS_PER_GAME / 9.0, INNING_DISPERSION, INNING_GRID)
            h_next = negative_binomial_pmf(home_strength * LEAGUE_RUNS_PER_GAME / 9.0, INNING_DISPERSION, INNING_GRID)
        else:
            a_next = state_runs_pmf(0, MANFRED_RUNNER_STATE, away_strength)
            h_next = state_runs_pmf(0, MANFRED_RUNNER_STATE, home_strength)
        override = num(ctx.raw, "extra_innings_home_win_prob")
        joint, q = extras_joint(a_next if away_first is None else away_first, h_next if home_first is None else home_first, a_next, h_next)
        q = clamp(q, 1e-6, 1.0 - 1e-6)
        if override is not None:
            target = clamp(override, 0.01, 0.99)
        else:
            # the strength-based split plus the walk-off edge: logit(p) = logit(q) + logit(0.52)
            target = logistic(math.log(q / (1.0 - q)) + math.log(EXTRA_INNINGS_HOME_WIN / (1.0 - EXTRA_INNINGS_HOME_WIN)))
        home_mask = np.tril(np.ones(joint.shape), -1).astype(bool)
        joint = joint.copy()
        joint[home_mask] *= target / q
        joint[~home_mask] *= (1.0 - target) / (1.0 - q)
        return joint

    @staticmethod
    def _pythag(rs: float, ra: float) -> float:
        x = max(rs + ra, 0.5) ** PYTHAGENPAT_POWER
        return rs ** x / (rs ** x + ra ** x)

    @staticmethod
    def _log5(p_home: float, p_away: float, home_edge: float = LOG5_HOME_EDGE) -> float:
        p_home, p_away = clamp(p_home, 0.01, 0.99), clamp(p_away, 0.01, 0.99)
        odds = p_home / (1.0 - p_home) * (1.0 - p_away) / p_away * home_edge / (1.0 - home_edge)
        return odds / (1.0 + odds)

    def pythagenpat(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], rm: RunModel, ctx: MatchContext) -> SubModelEstimate:
        """Season Pythagenpat per team (``runs_scored_per_game`` / ``runs_allowed_per_game`` when given, else the
        calibrated BaseRuns offence and the staff's weighted ERA as runs allowed), then log5 with the home edge."""
        league_runs, league_era = ctx.number("league_runs_per_game", LEAGUE_RUNS_PER_GAME), ctx.number("league_era", LEAGUE_ERA)
        ra_scale = league_runs / league_era

        def team(stats: Mapping[str, Any], offence: float, pitching: PitchingProfile) -> tuple[float, float]:
            rs = num(stats, "runs_scored_per_game", "runs_per_game")
            ra = num(stats, "runs_allowed_per_game")
            return (offence if rs is None else rs), (pitching.game_era * ra_scale if ra is None else ra)

        rs_h, ra_h = team(home_stats, rm.home_offence, rm.home_pitching)
        rs_a, ra_a = team(away_stats, rm.away_offence, rm.away_pitching)
        p = self._log5(self._pythag(rs_h, ra_h), self._pythag(rs_a, ra_a), ctx.number("log5_home_edge", LOG5_HOME_EDGE))
        return SubModelEstimate("pythagenpat", p, 1.0 - p, 0.0, (rs_h + ra_a + rs_a + ra_h) / 2.0 * rm.environment,
                                SUB_MODEL_WEIGHTS["pythagenpat"])

    def pitcher_matchup(self, rm: RunModel, ctx: MatchContext) -> SubModelEstimate:
        """Run prevention only: each staff's weighted ERA (starter decay + bullpen) as runs allowed against a
        league-average offence, Pythagenpat per staff, log5 with the home edge."""
        league_runs, league_era = ctx.number("league_runs_per_game", LEAGUE_RUNS_PER_GAME), ctx.number("league_era", LEAGUE_ERA)
        ra_h = rm.home_pitching.game_era * league_runs / league_era
        ra_a = rm.away_pitching.game_era * league_runs / league_era
        p = self._log5(self._pythag(league_runs, ra_h), self._pythag(league_runs, ra_a), ctx.number("log5_home_edge", LOG5_HOME_EDGE))
        return SubModelEstimate("pitcher_fip_matchup", p, 1.0 - p, 0.0, (ra_h + ra_a) * rm.environment, SUB_MODEL_WEIGHTS["pitcher_fip_matchup"])

    # ------------------------------------------------------------------ NRFI / YRFI
    @staticmethod
    def lineup(stats: Mapping[str, Any]) -> tuple[list[float], list[float], list[float]]:
        """OBP, HR rate per PA and extra-base scale for batting spots 1..9: ``top_order_obp`` / ``top_order_hr_rate`` /
        ``top_order_slg`` lists first, then the team's ``obp`` / ``hr_rate`` / ``slg`` with batting-order uplifts; with
        neither, the batting line relative to the league line sets them."""
        line, league_line = batting_line(stats), batting_line(LEAGUE_LINE)
        team_obp = num(stats, "obp")
        if team_obp is None:
            rel = ((line.hits + line.walks + line.hbp) / line.plate_appearances) / ((league_line.hits + league_line.walks + league_line.hbp)
                                                                                     / league_line.plate_appearances)
            team_obp = LEAGUE_OBP * rel
        team_hr = num(stats, "hr_rate")
        if team_hr is None:
            team_hr = LEAGUE_HR_RATE * (line.home_runs / line.plate_appearances) / (league_line.home_runs / league_line.plate_appearances)
        team_slg = num(stats, "slg")
        if team_slg is None:
            team_slg = LEAGUE_SLG * (line.total_bases / (line.outs + line.hits)) / (league_line.total_bases / (league_line.outs + league_line.hits))

        def listed(key: str) -> list[float]:
            raw = stats.get(key)
            if not isinstance(raw, (list, tuple)):
                return []
            out: list[float] = []
            for v in raw:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    break
                if not math.isfinite(f):
                    break
                out.append(f)
            return out

        top_obp, top_hr, top_slg = listed("top_order_obp"), listed("top_order_hr_rate"), listed("top_order_slg")
        obps = [top_obp[i] if i < len(top_obp) else team_obp * LINEUP_OBP_UPLIFT[i] for i in range(9)]
        hrs = [top_hr[i] if i < len(top_hr) else team_hr * LINEUP_HR_UPLIFT[i] for i in range(9)]
        slgs = [top_slg[i] if i < len(top_slg) else team_slg * LINEUP_OBP_UPLIFT[i] for i in range(9)]
        xbh = [(slgs[i] / LEAGUE_SLG) / max(0.05, obps[i] / LEAGUE_OBP) for i in range(9)]
        return obps, hrs, xbh

    def first_inning_scoring(self, batting: Mapping[str, Any], pitching: Mapping[str, Any], environment: float,
                             league_era: float) -> dict[str, float]:
        """P(the batting team scores in its first half-inning): the base-out Markov chain, the top-3 traffic formula
        times ``TRAFFIC_TO_SCORING`` (0.285 / the formula's league value), and the starter's first-inning ERA as a
        clustered Poisson (gamma-mixed, r = 0.5: a plain Poisson ``1 - exp(-ERA/9)`` overstates P(score) because runs
        come in bunches). The opposing starter and the environment scale on-base events by ``sqrt(run multiplier)``
        (runs grow roughly with the square of the on-base rate)."""
        sp = num(pitching, "sp_fip", "sp_era", default=league_era) or league_era
        run_mult = max(0.2, sp / league_era * environment)
        reach = math.sqrt(run_mult)
        obps, hrs, xbh = self.lineup(batting)
        markov = first_half_inning_markov(obps, hrs, xbh, reach)
        traffic = clamp(top_three_traffic(obps, hrs, reach) * TRAFFIC_TO_SCORING, 0.01, 0.95)
        fi_era = num(pitching, "first_inning_era")
        if fi_era is None:
            fi_era = sp * FIRST_INNING_ERA_FACTOR
        lam = max(0.0, fi_era) / 9.0 * environment
        r = INNING_DISPERSION
        era_prob = 1.0 - (r / (r + lam)) ** r
        blended = NRFI_BLEND["markov"] * markov + NRFI_BLEND["traffic"] * traffic + NRFI_BLEND["first_inning_era"] * era_prob
        return {"markov": markov, "traffic": traffic, "first_inning_era": era_prob, "blended": clamp(blended, 0.01, 0.99)}

    def calculate_nrfi_yrfi(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                            context: Mapping[str, Any] | None = None) -> float:
        """P(YRFI): one minus the product of each side failing to score in its first half-inning."""
        ctx = MatchContext.parse(dict(context) if context else None)
        rm = self.run_model(home_stats, away_stats, ctx)
        league_era = ctx.number("league_era", LEAGUE_ERA)
        top = self.first_inning_scoring(away_stats, home_stats, rm.environment, league_era)["blended"]
        bottom = self.first_inning_scoring(home_stats, away_stats, rm.environment, league_era)["blended"]
        return 1.0 - (1.0 - top) * (1.0 - bottom)

    # ------------------------------------------------------------------ inning of the highest score
    @staticmethod
    def inning_highest_probabilities(home_runs: float, away_runs: float, p_skip_ninth: float) -> dict[str, float]:
        """P(inning k alone has the most runs) = sum_m P(X_k = m) prod_{j != k} P(X_j < m) over the combined runs of
        each inning (independent NB innings scaled by ``INNING_RUN_FACTORS``; the bottom of the 9th is not played with
        probability ``p_skip_ninth``); the rest is a tie for the highest. Extra innings do not count."""
        combined: list[np.ndarray] = []
        for k, factor in enumerate(INNING_RUN_FACTORS):
            away = negative_binomial_pmf(away_runs / 9.0 * factor, INNING_DISPERSION, INNING_GRID)
            home = negative_binomial_pmf(home_runs / 9.0 * factor, INNING_DISPERSION, INNING_GRID)
            if k == 8:
                home = (1.0 - p_skip_ninth) * home
                home[0] += p_skip_ninth
            combined.append(np.convolve(away, home))
        cdf_below = [np.concatenate([[0.0], np.cumsum(c)[:-1]]) for c in combined]  # P(X < m)
        out: dict[str, float] = {}
        for k in range(9):
            others = np.ones(len(combined[k]))
            for j in range(9):
                if j != k:
                    others *= cdf_below[j]
            out[str(k + 1)] = float(np.dot(combined[k], others))
        out["tie"] = max(0.0, 1.0 - sum(out.values()))
        return out

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        rm = self.run_model(home_stats, away_stats, ctx)
        grid, p_extra, p_skip = self.game_grid(rm, ctx)
        h, _, a = grid.outcome()
        estimates = [SubModelEstimate("baseruns_negbin", h, a, 0.0, grid.total_pmf().mean, SUB_MODEL_WEIGHTS["baseruns_negbin"]),
                     self.pythagenpat(home_stats, away_stats, rm, ctx), self.pitcher_matchup(rm, ctx)]
        h_elo, a_elo = num(home_stats, "elo"), num(away_stats, "elo")
        if h_elo is not None and a_elo is not None:
            p = logistic((h_elo - a_elo + ctx.number("elo_home_advantage", ELO_HOME_ADVANTAGE)) * math.log(10.0) / 400.0)
            estimates.append(SubModelEstimate("elo", p, 1.0 - p, 0.0, None, SUB_MODEL_WEIGHTS["elo"]))
        total_w = sum(e.weight for e in estimates)
        estimates = [SubModelEstimate(e.name, e.home_win_prob, e.away_win_prob, e.draw_prob, e.expected_total, e.weight / total_w)
                     for e in estimates]
        consensus = self._consensus(estimates, ctx)

        league_era = ctx.number("league_era", LEAGUE_ERA)
        top = self.first_inning_scoring(away_stats, home_stats, rm.environment, league_era)
        bottom = self.first_inning_scoring(home_stats, away_stats, rm.environment, league_era)
        nrfi = (1.0 - top["blended"]) * (1.0 - bottom["blended"])
        f5 = ScoreGrid.independent(negative_binomial_pmf(rm.f5_home_runs, INNING_DISPERSION * 5, RUN_GRID),
                                   negative_binomial_pmf(rm.f5_away_runs, INNING_DISPERSION * 5, RUN_GRID))
        innings = self.inning_highest_probabilities(rm.home_runs, rm.away_runs, p_skip)
        markets = self.price_markets(grid, f5, nrfi, innings, p_extra)

        metrics: dict[str, Any] = {
            **rm.metrics,
            "home_baseruns_proj": round(rm.home_offence, 3),
            "away_baseruns_proj": round(rm.away_offence, 3),
            "home_expected_runs": round(rm.home_runs, 3),
            "away_expected_runs": round(rm.away_runs, 3),
            "f5_home_expected_runs": round(rm.f5_home_runs, 3),
            "f5_away_expected_runs": round(rm.f5_away_runs, 3),
            "home_pitching": rm.home_pitching.as_dict(),
            "away_pitching": rm.away_pitching.as_dict(),
            "run_environment_multiplier": round(rm.environment, 4),
            "negative_binomial_dispersion": RUN_DISPERSION,
            "extra_innings_probability": round(p_extra, 4),
            "home_skips_bottom_ninth_probability": round(p_skip, 4),
            "yrfi_probability": round(1.0 - nrfi, 3),
            "nrfi_probability": round(nrfi, 3),
            "first_inning_away_score_prob": {k: round(v, 4) for k, v in top.items()},
            "first_inning_home_score_prob": {k: round(v, 4) for k, v in bottom.items()},
            "top_correct_scores": [[s, round(p, 4)] for s, p in grid.correct_scores(6)],
        }
        return self._assemble(consensus, ctx, expected_total=grid.total_pmf().mean, metrics=metrics, markets=markets)

    def price_markets(self, grid: ScoreGrid, f5: ScoreGrid, nrfi: float, innings: Mapping[str, float], p_extra: float) -> dict[str, float]:
        markets: dict[str, float] = {}
        margin, total = trimmed(grid.margin_pmf()), trimmed(grid.total_pmf())
        h, _, a = grid.outcome()
        markets["moneyline_home"], markets["moneyline_away"] = h, a
        for line in RUN_LINES:
            markets[f"run_line_home_{line:+g}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"run_line_away_{line:+g}"] = margin.negate().line_outcome(-line, over=True).effective_probability
        for line in TOTAL_LINES:
            markets[f"total_over_{line:g}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"total_under_{line:g}"] = total.line_outcome(line, over=False).effective_probability
        home_pmf, away_pmf = trimmed(grid.home_pmf()), trimmed(grid.away_pmf())
        for line in TEAM_TOTAL_LINES:
            markets[f"home_team_total_over_{line:g}"] = home_pmf.line_outcome(line, over=True).effective_probability
            markets[f"away_team_total_over_{line:g}"] = away_pmf.line_outcome(line, over=True).effective_probability
        markets["nrfi"], markets["yrfi"] = nrfi, 1.0 - nrfi
        markets["extra_innings_yes"], markets["extra_innings_no"] = p_extra, 1.0 - p_extra

        fh, fd, fa = f5.outcome()
        f5_margin, f5_total = trimmed(f5.margin_pmf()), trimmed(f5.total_pmf())
        markets["f5_home"], markets["f5_tie"], markets["f5_away"] = fh, fd, fa
        markets["f5_home_2way"] = f5_margin.line_outcome(0.0, over=True).effective_probability  # a level F5 pushes
        markets["f5_away_2way"] = f5_margin.negate().line_outcome(0.0, over=True).effective_probability
        for line in (-1.5, -0.5, 0.5, 1.5):
            markets[f"f5_run_line_home_{line:+g}"] = f5_margin.line_outcome(-line, over=True).effective_probability
            markets[f"f5_run_line_away_{line:+g}"] = f5_margin.negate().line_outcome(-line, over=True).effective_probability
        for line in F5_TOTAL_LINES:
            markets[f"f5_total_over_{line:g}"] = f5_total.line_outcome(line, over=True).effective_probability
            markets[f"f5_total_under_{line:g}"] = f5_total.line_outcome(line, over=False).effective_probability
        for key, p in innings.items():
            markets[f"inning_highest_{key}"] = p
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability. ``live_state``: ``inning`` (1..), ``half`` ("top" / "bottom"), ``outs`` (0..3),
        ``bases`` (occupied bases, e.g. [1, 3]), ``home_runs`` / ``away_runs`` (current score; ``home_score`` /
        ``away_score`` accepted), optional ``status`` ("final"). The current half-inning runs come from its RE24 state
        (expected runs and P(score), scaled to the batting team), the remaining half-innings from the per-inning
        negative binomial; the bottom of the 9th is skipped when the home team leads, a home lead in the bottom of
        the 9th or later ends the game (walk-off), and a tie after nine goes to extra innings."""
        ctx = MatchContext.parse(context)
        rm = self.run_model(home_stats, away_stats, ctx)
        league = ctx.number("league_runs_per_game", LEAGUE_RUNS_PER_GAME)
        hs, as_ = rm.home_runs / league, rm.away_runs / league
        state = LiveState.parse(live_state)
        detail: dict[str, Any] = {"inning": state.inning, "half": state.half, "outs": state.outs, "bases": state.bases,
                                  "score": f"{state.home}-{state.away}"}
        if state.is_final:
            if state.home == state.away:
                return LiveWinProbability(0.5, 0.5, 0.0, {**detail, "final": True})
            won = state.home > state.away
            return LiveWinProbability(1.0 if won else 0.0, 0.0 if won else 1.0, 0.0, {**detail, "final": True})

        current = state_runs_pmf(state.outs, state.bases, as_ if state.half == "top" else hs)
        detail["current_half_expected_runs"] = round(float(np.dot(np.arange(len(current)), current)), 3)
        detail["current_half_score_prob"] = round(1.0 - float(current[0]), 4)
        a_total, h_pre, h_last = self._remaining(state, current, rm, hs, manfred=ctx.get("extra_inning_runner", True) is not False)
        extras = self._extras(hs, as_, ctx)
        final_grid, p_extra, _ = compose_game(h_pre, a_total, h_last, extras, home_offset=state.home, away_offset=state.away)
        grid = ScoreGrid.normalised(final_grid)
        p_home, _, p_away = grid.outcome()
        total = p_home + p_away
        detail["extra_innings_probability"] = round(p_extra, 4)
        detail["expected_final_total"] = round(grid.total_pmf().mean, 3)
        return LiveWinProbability(p_home / total, p_away / total, 0.0, detail)

    @staticmethod
    def _remaining(state: LiveState, current: np.ndarray, rm: RunModel, home_strength: float, *,
                   manfred: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(away runs still to come through the top of the 9th / current extra inning, home runs before its last
        half, the home team's last half) from the current half-inning distribution and the innings left."""
        none = np.array([1.0])
        inning = state.inning
        if inning > 9:
            if state.half == "top":
                bottom = state_runs_pmf(0, MANFRED_RUNNER_STATE, home_strength) if manfred else nb_innings(rm.home_runs, 1, INNING_GRID)
                return current, none, bottom
            return none, none, current
        if state.half == "top":
            h_pre = nb_innings(rm.home_runs, 9 - inning) if inning <= 8 else none
            return np.convolve(current, nb_innings(rm.away_runs, 9 - inning)), h_pre, nb_innings(rm.home_runs, 1, INNING_GRID)
        if inning < 9:
            return nb_innings(rm.away_runs, 9 - inning), np.convolve(current, nb_innings(rm.home_runs, 8 - inning)), \
                nb_innings(rm.home_runs, 1, INNING_GRID)
        return none, none, current

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """Accepts ``home_runs`` / ``away_runs`` (or ``home_score`` / ``away_score``), ``first_inning_runs``,
        ``f5_home_runs`` / ``f5_away_runs``, ``innings`` ([[away, home], ...] per inning; an unplayed bottom 9th may be
        None or "x"), ``inning_runs`` ([combined, ...]) and the shared fields (periods, events, player_stats, ...)."""
        res = normalize_basic(match_result, home_keys=("home_runs", "home_score"), away_keys=("away_runs", "away_score"))
        per_inning = _parse_innings(match_result.get("innings"))
        if per_inning:
            for k, (a, h) in enumerate(per_inning, start=1):
                res.periods.setdefault(f"i{k}", (h, a))
            if len(per_inning) >= 5:
                res.periods.setdefault("f5", (sum(h for _, h in per_inning[:5]), sum(a for a, _ in per_inning[:5])))
            if res.home is None and res.away is None:
                res.home, res.away = sum(h for _, h in per_inning), sum(a for a, _ in per_inning)
            res.extras["inning_runs"] = [a + h for a, h in per_inning]
        combined = match_result.get("inning_runs")
        if isinstance(combined, (list, tuple)) and combined:
            res.extras["inning_runs"] = [_runs(v) for v in combined]
        f5h, f5a = num(match_result, "f5_home_runs", "f5_home_score"), num(match_result, "f5_away_runs", "f5_away_score")
        if f5h is not None and f5a is not None:
            res.periods["f5"] = (f5h, f5a)
        first = num(match_result, "first_inning_runs")
        if first is not None:
            res.extras["first_inning_runs"] = first
        elif "i1" in res.periods:
            res.extras["first_inning_runs"] = sum(res.periods["i1"])
        elif res.extras.get("inning_runs"):
            res.extras["first_inning_runs"] = res.extras["inning_runs"][0]
        return res

    @override
    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == NRFI_MARKET:
            runs = result.extras.get("first_inning_runs")
            if runs is None:
                raise SettlementInputError("the result has no first-inning runs")
            want_run = _nrfi_selection(selection)
            return LegStatus.WON if (float(runs) > 0) == want_run else LegStatus.LOST
        if market == INNING_HIGHEST_MARKET:
            runs_by_inning = result.extras.get("inning_runs")
            if not runs_by_inning:
                raise SettlementInputError("the result has no per-inning runs")
            regulation = [float(r) for r in runs_by_inning[:9]]
            top = max(regulation)
            leaders = [i + 1 for i, r in enumerate(regulation) if r == top]
            actual = str(leaders[0]) if len(leaders) == 1 else "tie"
            return LegStatus.WON if _inning_selection(selection) == actual else LegStatus.LOST
        raise SettlementInputError(f"baseball has no market {market!r}")


def _parse_innings(raw: Any) -> list[tuple[float, float]]:
    """``[[away, home], ...]`` or ``[{"away": a, "home": h}, ...]`` as (away, home) pairs."""
    pairs: list[tuple[float, float]] = []
    if not isinstance(raw, (list, tuple)):
        return pairs
    for entry in raw:
        if isinstance(entry, Mapping):
            pairs.append((_runs(entry.get("away")), _runs(entry.get("home"))))
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            pairs.append((_runs(entry[0]), _runs(entry[1])))
    return pairs


def _runs(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0  # an unplayed half ("x", None) scores nothing
    return f if math.isfinite(f) else 0.0


def _nrfi_selection(selection: str) -> bool:
    """True when the selection backs a first-inning run (YRFI)."""
    tokens = set(split_selection(selection)) | {norm(selection)}
    if tokens & {"yrfi", "yes", "y", "over", "yes_run"}:
        return True
    if tokens & {"nrfi", "no", "n", "under", "no_run"}:
        return False
    raise SettlementInputError(f"selection {selection!r} is neither nrfi nor yrfi")


def _inning_selection(selection: str) -> str:
    s = norm(selection)
    if s in ("tie", "draw", "tied", "tie_for_highest", "x"):
        return "tie"
    digits = re.findall(r"\d+", s)
    if digits and 1 <= int(digits[0]) <= 9:
        return str(int(digits[0]))
    raise SettlementInputError(f"selection {selection!r} names no inning 1..9 or tie")
