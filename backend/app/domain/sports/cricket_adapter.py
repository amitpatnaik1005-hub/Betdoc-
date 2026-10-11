"""
Cricket Quantitative Adapter: Ball-by-Ball Markov Chain, DLS Standard Edition & Evening Dew (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

An innings is a Markov chain on (balls bowled, wickets lost, runs). Each legal delivery ends in one of
``{0, 1, 2, 3, 4, 6, W}``; before it the bowler may send down wides / no-balls, each worth one run and re-bowled
without consuming a ball (a geometric number of extras per legal ball, truncated at three). The per-ball
distribution starts from a base table per format and phase (T20: powerplay overs 1-6, middle 7-15, death 16-20;
ODI: 1-10, 11-40, 41-50; Test: one phase), calibrated so a neutral T20 innings averages ~165 and an ODI innings
~275, and is modulated by:

* strength ``s = batting_rating / opposing bowling_rating`` (100 = average): 1s/2s/3s x ``s^0.5``, fours x ``s^1.4``,
  sixes x ``s^1.8``, the wicket x ``s^-1.3``; dot balls absorb the difference;
* wickets in hand: boundary aggression falls 9% per wicket beyond the fourth, and from the sixth wicket down the
  lower order gets out 8% more often per wicket;
* pitch wear (ctx ``pitch_wear`` 0..1; a Test pitch wears 0.15 per innings from 0.1 on day one): the wicket
  x ``(1 + wear (0.2 pace + 0.85 spin))`` by the bowling attack's spin share, scoring shots damped by 30% of that;
* the evening dew (night match, local hour >= 19.5, humidity > 75% or a stated dew intensity): the side bowling
  SECOND loses spin grip, the spinners' wicket probability falls 42% x intensity (light 0.5, moderate 0.75,
  heavy 1.0) weighted by its spin share (stats ``spin_share``, default 0.40), and the wet ball leaks boundaries;
  two equal T20 sides in heavy dew leave the chasing side about 0.64;
* weather: The Wire's friction factor scales the scoring shots when present; otherwise a wet outfield (rain)
  slows 2s/3s/4s, altitude carries sixes and a humid day game helps the seamers swing it.

Limited-overs innings are propagated exactly, ball by ball, with numpy: the runs axis is diagonalised by the real
DFT (the probability generating function at the roots of unity), so each delivery is one vectorised multiply over
every wicket state and run total and the inverse DFT returns the exact run PMF. A Test innings has no over limit,
so its generating function is the closed-form product over the ten partnerships
``prod_w E(z) pW_w / (1 - E(z) A_w(z))`` (``A_w`` the non-wicket shots, ``E`` the extras). A chase in play is
solved by a direct DP over the runs axis with the target absorbing.

Sub-models (fortress pillar 1):

1. **markov_ball_by_ball** (0.55; 0.45 with Elo): P(first innings > second) from the two exact innings PMFs; a
   T20 tie goes to a 50/50 super over, an ODI tie is a tie (shown in ``draw_prob``); a Test sums each side's two
   innings and takes the draw from the time model below.
2. **dls_resource_par** (0.20): expected totals ``G x R(overs, 0)% x s`` (``G`` = 170 T20 / 245 ODI; 330 per Test
   innings) with a normal margin, sd 16% of a T20 total, 15% of an ODI total, 30% of a Test innings.
3. **rating_logistic** (0.25; 0.15 with Elo): the run-difference logistic ``1 / (1 + exp(-dRuns / k))``, expected
   runs ``rr x bat/100 x 100/bowl x overs`` (rr 8.5 / 5.4 / 3.3, k 18 / 35 / 60).
4. **elo_logistic** (0.20, only when both sides carry ``elo``): ``1 / (1 + 10^(-dElo / 400))``.

Test draw: ``logit P(draw) = logit(0.20) + 1.5 (overs needed - overs available + 50) / 90`` where the overs needed
are the Markov chain's expected balls for four completed innings, rain removes ~25 overs per forecast mm of daily
rain and a flat pitch (ctx ``pitch_flatness``, 0.5 neutral) slows the wickets.

DLS: ``R(u, w) = R0(w) (1 - exp(-b(w) u)) / (1 - exp(-50 b(w)))`` (the spec's R0 row is the 50-over column of the
ICC Standard Edition table, so ``R(50, w) = R0(w)``), non-increasing in ``w`` for every ``u``, ``u`` capped to
[0, 50]; the revised target is ``floor(S1 R2/R1) + 1`` when ``R2 < R1``, else ``S1 + floor((R2 - R1)/100 G) + 1``.

Context: absences cost 4 rating points per status-weighted WAR (batters off batting, bowlers off bowling,
all-rounders half each); an umpire's wides per innings scales the extras rate and an ``lbw_factor`` the wicket.

Markets: the match winner (and tie / Test draw); each side's innings total and, in limited overs, its runs as
played (the chase stops at the target: the second innings is ``U`` when ``U <= t`` and otherwise passes ``t + 1`` by
the renewal overshoot of a scoring shot); the first-six-overs runs; total match runs; most sixes (independent
Poissons on each side's expected sixes, summed ball by ball over the alive wicket states). The totals are mixed
over a shared pitch factor ``kappa ~ lognormal(0, 0.13 / 0.12 / 0.25)`` (T20 / ODI / Test), which cancels in the
result but widens the totals to the spread seen in the field (a T20 innings sd of ~30 runs).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, ClassVar, override

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext, Weather
from app.domain.sports.quant import (
    IntPMF,
    LineOutcome,
    ScoreGrid,
    SubModelEstimate,
    clamp,
    logistic,
    normal_cdf,
    poisson_pmf,
    split_line,
)
from app.domain.sports.settlement import (
    ALT_SPREAD,
    ALT_TOTAL,
    MONEYLINE,
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
    over_under,
    settle_threshold,
    side_token,
    split_selection,
)
from app.schemas.bet_types import LegStatus

# ================================================================ the ball
RUN_VALUES = np.array([0, 1, 2, 3, 4, 6])
COL_SIX = 5
COL_WICKET = 6
OUTCOMES = ("0", "1", "2", "3", "4", "6", "W")

#: Base legal-ball outcome probabilities (0, 1, 2, 3, 4, 6, W) per format and phase, for average sides.
BALL_BASE: dict[str, dict[str, tuple[float, ...]]] = {
    "t20": {
        "powerplay": (0.425, 0.300, 0.060, 0.004, 0.142, 0.040, 0.029),
        "middle": (0.310, 0.432, 0.084, 0.004, 0.086, 0.052, 0.032),
        "death": (0.262, 0.346, 0.092, 0.004, 0.128, 0.100, 0.068),
    },
    "odi": {
        "powerplay": (0.600, 0.220, 0.044, 0.006, 0.098, 0.012, 0.020),
        "middle": (0.455, 0.385, 0.068, 0.006, 0.054, 0.015, 0.017),
        "death": (0.322, 0.365, 0.090, 0.006, 0.100, 0.064, 0.053),
    },
    "test": {
        "test": (0.7344, 0.142, 0.033, 0.008, 0.065, 0.004, 0.0136),
    },
}
FORMAT_OVERS: dict[str, int] = {"t20": 20, "odi": 50}
#: (powerplay share, death share) of an innings' overs; a full T20 is 6 / 5 overs, a full ODI 10 / 10.
PHASE_SPLITS: dict[str, tuple[float, float]] = {"t20": (0.30, 0.25), "odi": (0.20, 0.20)}
EXTRAS_RATE: dict[str, float] = {"t20": 0.040, "odi": 0.035, "test": 0.020}  # wides + no-balls per delivery
MAX_EXTRAS = 3
TEST_FFT_SIZE = 2048  # a Test innings above 2047 runs carries no measurable mass
FFT_SIGMAS = 9.0  # the DFT length covers the runs mean bound plus nine standard deviations (no wrap-around)
POWERPLAY_BALLS = 36
DOT_FLOOR = 0.02
#: sd of the log of the shared pitch / conditions factor that widens the totals (an innings sd of ~30 in a T20,
#: ~50 in an ODI and ~140 in a Test first innings); it cancels in the result, which compares innings on one pitch.
PITCH_SIGMA: dict[str, float] = {"t20": 0.13, "odi": 0.12, "test": 0.25}

STRENGTH_EXP_RUNS = 0.5
STRENGTH_EXP_FOUR = 1.4
STRENGTH_EXP_SIX = 1.8
STRENGTH_EXP_WICKET = 1.3
AGGRESSION_FROM_WICKET = 4
AGGRESSION_DROP = 0.09
TAIL_FROM_WICKET = 5
TAIL_WICKET_UPLIFT = 0.08
WEAR_PACE = 0.20
WEAR_SPIN = 0.85
WEAR_SCORING_DAMP = 0.30

# ================================================================ conditions
DEFAULT_SPIN_SHARE = 0.40
DEW_INTENSITY: dict[str, float] = {"none": 0.0, "light": 0.5, "moderate": 0.75, "heavy": 1.0}
DEW_SPIN_WICKET_DROP = 0.42
DEW_BOUNDARY_SPIN = 0.16  # wet-ball boundary leak per unit dew intensity, spin overs
DEW_BOUNDARY_PACE = 0.06  # ... seam overs (slower balls and yorkers slip)
NIGHT_HOUR = 19.5
DEW_HUMIDITY = 75.0
SWING_HUMIDITY = 80.0
SWING_WICKET_UPLIFT = 0.08
WET_OUTFIELD_MULT = 0.94
RATING_POINTS_PER_WAR = 4.0
BAT_POSITIONS = frozenset({"batsman", "batter", "bat", "opener", "top_order", "middle_order", "wicketkeeper", "wk", "keeper",
                           "finisher", "wicket_keeper"})
BOWL_POSITIONS = frozenset({"bowler", "spinner", "pacer", "seamer", "fast_bowler", "spin", "pace", "fast", "quick"})
ALLROUNDER_POSITIONS = frozenset({"allrounder", "all_rounder", "all-rounder", "ar"})
LEAGUE_EXTRAS_PER_INNINGS: dict[str, float] = {"t20": 7.0, "odi": 11.0, "test": 14.0}

# ================================================================ Test match
TEST_BASE_WEAR = 0.10
TEST_WEAR_PER_INNINGS = 0.15
TEST_SCHEDULED_OVERS = 450.0
TEST_DRAW_BASE = 0.20
TEST_DRAW_TIME_COEF = 1.5  # logit per day (90 overs) of time pressure
TEST_NEUTRAL_SLACK = 50.0  # overs to spare between four neutral innings and five full days
RAIN_OVERS_PER_MM = 25.0  # overs lost per mm of forecast daily rain over the match
RAIN_OVERS_CAP = 360.0
FLATNESS_WICKET_COEF = 0.40

# ================================================================ sub-models
DLS_G: dict[str, float] = {"t20": 170.0, "odi": 245.0}
TEST_PAR_INNINGS = 330.0
DLS_SD_FRACTION: dict[str, float] = {"t20": 0.16, "odi": 0.15, "test": 0.30}
RATING_RUN_RATE: dict[str, float] = {"t20": 8.5, "odi": 5.4, "test": 3.3}
RATING_SCALE: dict[str, float] = {"t20": 18.0, "odi": 35.0, "test": 60.0}
TEST_RATING_OVERS = 180.0
SUB_MODEL_WEIGHTS: dict[str, float] = {"markov_ball_by_ball": 0.55, "dls_resource_par": 0.20, "rating_logistic": 0.25}
SUB_MODEL_WEIGHTS_ELO: dict[str, float] = {"markov_ball_by_ball": 0.45, "dls_resource_par": 0.20, "rating_logistic": 0.15,
                                           "elo_logistic": 0.20}

# ================================================================ markets
INNINGS_TOTAL_RUNS = "innings_total_runs"
FIRST_6_OVERS_RUNS = "first_6_overs_runs"
MOST_SIXES = "most_sixes"
TOP_BATSMAN_RUNS = "top_batsman_runs"
INNING_OF_HIGHEST_SCORE = "inning_of_highest_score"
MATCH_RESULT = "match_result"
NO_RESULT_STATUSES = frozenset({"no_result", "nr", "rained_out", "washed_out"})
ORDINALS: dict[str, int] = {"1": 1, "1st": 1, "first": 1, "2": 2, "2nd": 2, "second": 2, "3": 3, "3rd": 3, "third": 3,
                            "4": 4, "4th": 4, "fourth": 4}


# ================================================================ DLS (module-level so the maths is testable alone)
def dls_raw_resource(u: float, w: int, constants: Mapping[int, Mapping[str, float]]) -> float:
    """``R(u, w)`` as a percentage of a full 50-over innings, non-increasing in ``w`` for every ``u``."""
    if w >= 10:
        return 0.0
    u = clamp(float(u), 0.0, 50.0)
    best = math.inf
    for wk in range(0, max(0, int(w)) + 1):
        r0, b = constants[wk]["R0"], constants[wk]["b"]
        best = min(best, r0 * (1.0 - math.exp(-b * u)) / (1.0 - math.exp(-50.0 * b)))
    return best


def balls_from_overs(overs: float) -> int:
    """Cricket notation: 12.3 overs = 12 overs and 3 balls = 75 balls."""
    tenths = round(float(overs) * 10.0)
    whole, part = divmod(tenths, 10)
    return whole * 6 + min(part, 5)


# ================================================================ the per-ball distribution
@dataclass(frozen=True, slots=True)
class InningsConditions:
    """Everything the per-ball distribution of one innings depends on."""

    fmt: str
    strength: float = 1.0  # batting rating / opposing bowling rating
    wear: float = 0.0
    spin_share: float = DEFAULT_SPIN_SHARE  # of the BOWLING attack
    dew: float = 0.0  # dew intensity scale while this innings is bowled (0 = dry)
    run_mult: float = 1.0  # 2s, 3s and 4s (outfield speed)
    six_mult: float = 1.0  # sixes (carry)
    wicket_mult: float = 1.0  # swing, umpire, flat / dusty surface
    extras_rate: float = 0.03


def phase_of_over(fmt: str, over_index: int, overs: int) -> str:
    if fmt == "test":
        return "test"
    pp_share, death_share = PHASE_SPLITS[fmt]
    pp_end = max(1, round(overs * pp_share))
    death_start = overs - max(1, round(overs * death_share))
    if over_index < pp_end:
        return "powerplay"
    if over_index >= death_start:
        return "death"
    return "middle"


def ball_matrix(cond: InningsConditions, phase: str) -> np.ndarray:
    """Legal-ball outcome probabilities, one row per wickets lost (0..9), columns 0, 1, 2, 3, 4, 6, W."""
    base = np.array(BALL_BASE[cond.fmt][phase], dtype=float)
    m = np.tile(base, (10, 1))
    w = np.arange(10, dtype=float)
    s = clamp(cond.strength, 0.5, 2.0)
    m[:, 1:4] *= s ** STRENGTH_EXP_RUNS
    m[:, 4] *= s ** STRENGTH_EXP_FOUR
    m[:, 5] *= s ** STRENGTH_EXP_SIX
    m[:, 6] *= s ** -STRENGTH_EXP_WICKET
    # wickets in hand: fewer -> less boundary aggression, and the lower order gets out more easily
    m[:, 4:6] *= np.clip(1.0 - AGGRESSION_DROP * np.clip(w - AGGRESSION_FROM_WICKET, 0.0, None), 0.3, 1.0)[:, None]
    m[:, 6] *= 1.0 + TAIL_WICKET_UPLIFT * np.clip(w - TAIL_FROM_WICKET, 0.0, None)
    # pitch wear: more wickets and dots, mostly for the spinners
    sp = clamp(cond.spin_share, 0.0, 1.0)
    wear_eff = clamp(cond.wear, 0.0, 1.0) * (WEAR_PACE * (1.0 - sp) + WEAR_SPIN * sp)
    m[:, 6] *= 1.0 + wear_eff
    m[:, 1:6] *= 1.0 - WEAR_SCORING_DAMP * wear_eff
    # evening dew: the spinners lose grip (wickets -42% x intensity on their share) and the wet ball leaks boundaries
    if cond.dew > 0.0:
        m[:, 6] *= 1.0 - DEW_SPIN_WICKET_DROP * cond.dew * sp
        m[:, 4:6] *= 1.0 + cond.dew * (DEW_BOUNDARY_SPIN * sp + DEW_BOUNDARY_PACE * (1.0 - sp))
    m[:, 2:5] *= cond.run_mult
    m[:, 5] *= cond.six_mult
    m[:, 6] *= cond.wicket_mult
    m[:, 0] = 1.0 - m[:, 1:].sum(axis=1)
    short = m[:, 0] < DOT_FLOOR
    if np.any(short):
        rest = m[short, 1:]
        m[short, 1:] = rest * ((1.0 - DOT_FLOOR) / rest.sum(axis=1, keepdims=True))
        m[short, 0] = DOT_FLOOR
    return m


def extras_kernel(rate: float) -> np.ndarray:
    """P(j extras before the legal ball), geometric in the wide / no-ball rate, the tail folded into the last cell."""
    e = clamp(rate, 0.0, 0.3)
    kernel = np.array([(1.0 - e) * e ** j for j in range(MAX_EXTRAS)] + [e ** MAX_EXTRAS])
    return kernel


def ball_schedule(cond: InningsConditions, overs: int, start_ball: int) -> list[tuple[np.ndarray, int, int]]:
    """``(matrix, first ball, ball count)`` segments of constant phase from ``start_ball`` to the end of the innings."""
    segments: list[tuple[np.ndarray, int, int]] = []
    cache: dict[str, np.ndarray] = {}
    total = overs * 6
    ball = start_ball
    while ball < total:
        phase = phase_of_over(cond.fmt, ball // 6, overs)
        over = ball // 6 + 1
        while over < overs and phase_of_over(cond.fmt, over, overs) == phase:
            over += 1
        end = min(total, over * 6)
        if phase not in cache:
            cache[phase] = ball_matrix(cond, phase)
        segments.append((cache[phase], ball, end - ball))
        ball = end
    return segments


def _dft_length(schedule: Sequence[tuple[np.ndarray, int, int]], ext: np.ndarray) -> int:
    """A DFT length no innings total can wrap around: the per-ball mean and variance bounds (the most expensive
    wicket row of each phase) summed over the balls left, plus ``FFT_SIGMAS`` standard deviations, rounded up to 32."""
    ext_runs = np.arange(len(ext))
    ext_mean = float(ext @ ext_runs)
    ext_var = float(ext @ ext_runs ** 2) - ext_mean ** 2
    mean = var = 0.0
    for matrix, _, count in schedule:
        legal = (matrix[:, :6] * RUN_VALUES).sum(axis=1)
        legal_sq = (matrix[:, :6] * RUN_VALUES ** 2).sum(axis=1)
        mean += count * (float(legal.max()) + ext_mean)
        var += count * (float((legal_sq - legal ** 2).max()) + ext_var)
    bound = mean + FFT_SIGMAS * math.sqrt(var) + 16.0
    return math.ceil(bound / 32.0) * 32


def _gf(probs: np.ndarray, powers: np.ndarray) -> np.ndarray:
    """Generating function ``sum_k probs[..., k] z^(r_k)`` at every frequency. An einsum, not a BLAS matmul: these
    products are tiny and a threaded GEMM call costs milliseconds of thread wake-up under load."""
    return np.einsum("...k,kf->...f", probs, powers)


@lru_cache(maxsize=64)
def _roots(n: int) -> tuple[np.ndarray, np.ndarray]:
    """``z^k`` at the rfft frequencies for k = 0..9 (enough for a six plus three extras)."""
    f = np.arange(n // 2 + 1)
    z = np.exp(-2j * np.pi * f / n)
    powers = np.vstack([z ** k for k in range(10)])
    return z, powers


def _pmf_from_spectrum(spectrum: np.ndarray, n: int, shift: int = 0) -> IntPMF:
    arr = np.clip(np.fft.irfft(spectrum, n), 0.0, None)
    nonzero = np.nonzero(arr > 1e-13)[0]
    last = int(nonzero[-1]) + 1 if len(nonzero) else 1
    return IntPMF.from_array(arr[:last], shift)


def line_outcome(pmf: IntPMF, line: float, *, over: bool) -> LineOutcome:
    """``IntPMF.line_outcome`` vectorised over the support (identical buckets: win, half win, push, half loss, loss).
    The foundation's version loops in Python over every support point, which is too slow for 300..2000-run supports."""
    halves = split_line(line)
    sign = 1.0 if over else -1.0
    diff = sign * (pmf.values.astype(float)[:, None] - np.array(halves)[None, :])
    results = np.where(diff > 1e-9, 1, np.where(diff < -1e-9, -1, 0))
    probs = pmf.probs
    if results.shape[1] == 1:
        r = results[:, 0]
        return LineOutcome(float(probs[r == 1].sum()), 0.0, float(probs[r == 0].sum()), 0.0, float(probs[r == -1].sum()))
    a, b = results[:, 0], results[:, 1]
    same = a == b
    total = a + b
    win = float(probs[same & (a == 1)].sum())
    loss = float(probs[same & (a == -1)].sum())
    half_win = float(probs[~same & (total == 1)].sum())
    half_loss = float(probs[~same & (total == -1)].sum())
    push = float(probs.sum()) - win - loss - half_win - half_loss
    return LineOutcome(win, half_win, max(push, 0.0), half_loss, loss)


def prob_exceeds(b: IntPMF, a: IntPMF) -> tuple[float, float]:
    """``(P(B > A), P(B = A))`` for independent integer totals A and B."""
    cdf = np.cumsum(b.probs)
    idx = a.values - b.offset
    inside = (idx >= 0) & (idx < len(b.probs))
    clipped = np.clip(idx, 0, len(b.probs) - 1)
    at_most = np.where(idx < 0, 0.0, np.where(inside, cdf[clipped], 1.0))
    equal = np.where(inside, b.probs[clipped], 0.0)
    return float(a.probs @ (1.0 - at_most)), float(a.probs @ equal)


@lru_cache(maxsize=8)
def _hermite_nodes(count: int) -> tuple[np.ndarray, np.ndarray]:
    nodes, weights = np.polynomial.hermite_e.hermegauss(count)
    return nodes, weights / weights.sum()


def pitch_mixture(pmf: IntPMF, sigma: float) -> IntPMF:
    """Mix ``pmf`` over a shared, unobserved pitch / conditions factor ``kappa ~ lognormal(-sigma^2/2, sigma)`` (mean 1)
    that multiplies every run of the match: each value ``v`` lands on ``kappa v``, split linearly between the two
    neighbouring integers, on a 9-point Gauss-Hermite grid. The match result compares two innings on the same pitch,
    so kappa cancels there; it widens the totals to the spread seen in the field."""
    if sigma <= 0.0:
        return pmf
    nodes, weights = _hermite_nodes(9)
    kappas = np.exp(sigma * nodes - 0.5 * sigma * sigma)
    values = pmf.values.astype(float)
    top = math.ceil(values.max() * kappas.max()) + 2
    lo_bound = math.floor(max(0.0, values.min() * kappas.min()))
    out = np.zeros(top - lo_bound + 1)
    for kappa, weight in zip(kappas, weights, strict=True):
        scaled = values * kappa
        low = np.floor(scaled).astype(int)
        frac = scaled - low
        np.add.at(out, low - lo_bound, weight * pmf.probs * (1.0 - frac))
        np.add.at(out, low + 1 - lo_bound, weight * pmf.probs * frac)
    return IntPMF.from_array(out, lo_bound)


@dataclass(frozen=True)
class InningsDistribution:
    """One innings' exact run distribution and its by-products."""

    total: IntPMF
    powerplay: IntPMF | None
    expected_sixes: float
    expected_balls: float
    format: str = "t20"

    @property
    def mean(self) -> float:
        return self.total.mean


def simulate_limited_innings(cond: InningsConditions, overs: int, *, start_ball: int = 0, wickets: int = 0, runs: int = 0) -> InningsDistribution:
    """Exact ball-by-ball propagation of the (wickets, runs) chain over the remaining legal balls.

    State ``P[w, f]``: the DFT over the runs axis of the mass with ``w`` wickets down (row 10 = all out). A legal
    ball maps row ``w`` to ``P[w] G_w`` (no wicket) plus ``P[w] W_w`` into row ``w + 1``, where
    ``G_w(z) = E(z) sum_k p_k z^k`` and ``W_w(z) = E(z) pW``; ``E`` is the extras kernel."""
    schedule = ball_schedule(cond, overs, start_ball)
    ext = extras_kernel(cond.extras_rate)
    n = _dft_length(schedule, ext)
    _, powers = _roots(n)
    e_z = _gf(ext, powers[: len(ext)])
    state = np.zeros((11, n // 2 + 1), dtype=complex)
    state[min(max(wickets, 0), 10)] = 1.0
    sixes = 0.0
    balls = 0.0
    powerplay: IntPMF | None = None
    for matrix, first, count in schedule:
        g = _gf(matrix[:, :6], powers[RUN_VALUES]) * e_z
        wk = matrix[:, COL_WICKET:COL_WICKET + 1] * e_z
        six_col = matrix[:, COL_SIX]
        for ball in range(first, first + count):
            if ball == POWERPLAY_BALLS and start_ball == 0:
                powerplay = _pmf_from_spectrum(state.sum(axis=0), n, runs)
            alive = state[:10, 0].real
            sixes += float(alive @ six_col)
            balls += float(alive.sum())
            prev = state[:10].copy()
            np.multiply(prev, g, out=state[:10])
            state[1:] += prev * wk
    total = _pmf_from_spectrum(state.sum(axis=0), n, runs)
    if powerplay is None and start_ball == 0:
        powerplay = total  # an innings of six overs or fewer
    return InningsDistribution(total, powerplay, sixes, balls, cond.fmt)


def simulate_test_innings(cond: InningsConditions, *, wickets: int = 0, runs: int = 0) -> InningsDistribution:
    """A Test innings (no over limit) as the closed-form product of the remaining partnerships' generating functions.
    Partnership ``w`` lasts a geometric number of legal balls (wicket probability ``pW_w``), so its PGF is
    ``E(z) pW_w / (1 - E(z) A_w(z))``; the expected sixes are ``sum_w p6_w / pW_w``."""
    n = TEST_FFT_SIZE
    _, powers = _roots(n)
    ext = extras_kernel(cond.extras_rate)
    e_z = _gf(ext, powers[: len(ext)])
    matrix = ball_matrix(cond, "test")
    spectrum = np.ones(n // 2 + 1, dtype=complex)
    sixes = 0.0
    balls = 0.0
    for w in range(max(0, wickets), 10):
        p_w = matrix[w, COL_WICKET]
        a_z = _gf(matrix[w, :6], powers[RUN_VALUES])
        spectrum *= e_z * p_w / (1.0 - e_z * a_z)
        sixes += matrix[w, COL_SIX] / p_w
        balls += 1.0 / p_w
    total = _pmf_from_spectrum(spectrum, n, runs)
    powerplay = simulate_limited_innings(cond, 6).total if wickets == 0 and runs == 0 else None
    return InningsDistribution(total, powerplay, sixes, balls, "test")


@dataclass(frozen=True, slots=True)
class ChaseOutcome:
    reached: float  # target reached
    level_all_out: float  # all out exactly one short (scores level)
    short_all_out: float
    level_alive: float  # balls ran out with wickets in hand, scores level
    short_alive: float

    @property
    def level(self) -> float:
        return self.level_all_out + self.level_alive


def chase_probabilities(cond: InningsConditions, *, need: int, balls_left: int, wickets: int, start_ball: int,
                        overs: int | None) -> ChaseOutcome:
    """Exact chase DP on (wickets, runs still to score) with the target absorbing.

    ``P[w, r]``: probability of ``w`` wickets down and ``r`` runs scored since now. Each ball first adds the extras
    (which can win the chase on their own), then the legal ball; any mass reaching ``need`` runs is absorbed as a
    win, a tenth wicket is absorbed as all out. ``overs`` is the innings length (None: a Test, one phase)."""
    if need <= 0:
        return ChaseOutcome(1.0, 0.0, 0.0, 0.0, 0.0)
    if wickets >= 10 or balls_left <= 0:
        lvl = 1.0 if need == 1 else 0.0
        if wickets >= 10:
            return ChaseOutcome(0.0, lvl, 1.0 - lvl, 0.0, 0.0)
        return ChaseOutcome(0.0, 0.0, 0.0, lvl, 1.0 - lvl)
    width = need + 10
    state = np.zeros((10, width))
    state[wickets, 0] = 1.0
    all_out = np.zeros(width)
    reached = 0.0
    ext = extras_kernel(cond.extras_rate)
    matrices: dict[str, np.ndarray] = {}
    for i in range(balls_left):
        ball = start_ball + i
        phase = "test" if overs is None else phase_of_over(cond.fmt, ball // 6, overs)
        if phase not in matrices:
            matrices[phase] = ball_matrix(cond, phase)
        m = matrices[phase]
        ex = state * ext[0]
        for j in range(1, len(ext)):
            ex[:, j:] += state[:, :-j] * ext[j]
        reached += float(ex[:, need:].sum())
        ex[:, need:] = 0.0
        new = ex * m[:, 0:1]
        for col, runs in enumerate(RUN_VALUES[1:], start=1):
            new[:, runs:] += ex[:, :-runs] * m[:, col:col + 1]
        out = ex * m[:, COL_WICKET:COL_WICKET + 1]
        new[1:] += out[:-1]
        all_out += out[9]
        reached += float(new[:, need:].sum())
        new[:, need:] = 0.0
        state = new
        if state.sum() < 1e-12:
            break
    alive = state.sum(axis=0)
    return ChaseOutcome(reached, float(all_out[need - 1]), float(all_out[: need - 1].sum()), float(alive[need - 1]),
                        float(alive[: need - 1].sum()))


# ================================================================ match setup
@dataclass
class MatchSetup:
    fmt: str
    overs: int
    batting_first: str  # "home" | "away"
    strength: dict[str, float]  # batting side -> bat / opposing bowl
    ratings: dict[str, dict[str, float]]
    spin_share: dict[str, float]  # bowling side -> spin share
    wear: float
    dew: float
    run_mult: float
    six_mult: float
    wicket_mult: float
    extras_rate: float
    flatness: float
    adjustments: dict[str, Any] = field(default_factory=dict)

    @property
    def chasing(self) -> str:
        return "away" if self.batting_first == "home" else "home"

    def innings_order(self, follow_on: bool = False) -> list[str]:
        """The batting side of innings 1..4 (Tests); limited overs use the first two."""
        first, second = self.batting_first, self.chasing
        return [first, second, second, first] if follow_on else [first, second, first, second]

    def conditions(self, batting: str, innings_no: int) -> InningsConditions:
        bowling = "away" if batting == "home" else "home"
        if self.fmt == "test":
            wear = clamp(self.wear + TEST_WEAR_PER_INNINGS * (innings_no - 1), 0.0, 1.0)
            dew = 0.0
            wicket_mult = self.wicket_mult * (1.0 - FLATNESS_WICKET_COEF * (self.flatness - 0.5))
        else:
            wear = self.wear
            dew = self.dew if innings_no == 2 else 0.0
            wicket_mult = self.wicket_mult
        return InningsConditions(self.fmt, self.strength[batting], wear, self.spin_share[bowling], dew, self.run_mult,
                                 self.six_mult, wicket_mult, self.extras_rate)


class CricketSportAdapter(BaseSportAdapter):
    """
    Quantitative Cricket Engine implementing ball-by-ball Markov transitions,
    Duckworth-Lewis-Stern (DLS) standard edition resource calculations,
    and night-match dew factor degradation for defending teams.
    """

    kelly_multiplier = 0.25
    model_family = "Ball-by-ball Markov & DLS"
    has_draw = False  # a Test draw is still priced, as draw_prob
    odds_api_family = "cricket"

    # ICC Standard DLS Resource Constants: R0 (resource with 50 overs left) and decay factor b, by wickets lost (0..9)
    DLS_CONSTANTS: ClassVar[dict[int, dict[str, float]]] = {
        0: {"R0": 100.0, "b": 0.0350},
        1: {"R0": 93.4, "b": 0.0360},
        2: {"R0": 85.1, "b": 0.0375},
        3: {"R0": 74.9, "b": 0.0400},
        4: {"R0": 62.7, "b": 0.0430},
        5: {"R0": 49.0, "b": 0.0465},
        6: {"R0": 34.9, "b": 0.0520},
        7: {"R0": 22.0, "b": 0.0600},
        8: {"R0": 11.9, "b": 0.0750},
        9: {"R0": 4.7, "b": 0.1000},
    }

    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner": (MONEYLINE, {}),
        "match_odds": (MONEYLINE, {}),
        "to_win_match": (MONEYLINE, {}),
        "test_match_result": (MATCH_RESULT, {}),
        "match_result_3way": (MATCH_RESULT, {}),
        "total_match_runs": (TOTAL, {}),
        "match_total_runs": (TOTAL, {}),
        "team_total_runs": (TEAM_TOTAL, {}),
        "runs_handicap": (SPREAD, {}),
        "match_handicap": (SPREAD, {}),
        "first_innings_lead": (PERIOD_MONEYLINE, {"period": "first_innings"}),
        "first_innings_runs_handicap": (PERIOD_SPREAD, {"period": "first_innings"}),
        "first_innings_total_runs": (PERIOD_TOTAL, {"period": "first_innings"}),
        "powerplay_runs": (FIRST_6_OVERS_RUNS, {}),
        "first_6_overs": (FIRST_6_OVERS_RUNS, {}),
        "innings_runs": (INNINGS_TOTAL_RUNS, {}),
        "innings_of_highest_score": (INNING_OF_HIGHEST_SCORE, {}),
        "top_batter_runs": (TOP_BATSMAN_RUNS, {}),
        "player_runs": (PLAYER_PROP, {"stat": "runs"}),
        "player_wickets": (PLAYER_PROP, {"stat": "wickets"}),
        "player_sixes": (PLAYER_PROP, {"stat": "sixes"}),
    }
    SPECIAL_MARKETS = frozenset({INNINGS_TOTAL_RUNS, FIRST_6_OVERS_RUNS, MOST_SIXES, TOP_BATSMAN_RUNS, INNING_OF_HIGHEST_SCORE,
                                 MATCH_RESULT})
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE, PERIOD_SPREAD,
                                   PERIOD_TOTAL, PLAYER_PROP})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "round_robin", "if_bet",
                           "double_action", "futures", "player_props", "game_props", "inning_of_highest_score", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "cricket"

    @property
    def display_name(self) -> str:
        return "Cricket"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "match_winner",
            "innings_total_runs",
            "first_6_overs_runs",
            "most_sixes",
            "top_batsman_runs",
            "inning_of_highest_score",
            "total_match_runs",
            "test_match_result",
        ]

    # ------------------------------------------------------------------ DLS standard edition
    def calculate_dls_resource(self, overs_remaining: float, wickets_lost: int, format_overs: float = 50.0) -> float:
        """Percentage of a full ``format_overs`` innings' resources still available with ``overs_remaining`` overs
        and ``wickets_lost`` wickets down (``(50, 0) -> 100`` on the 50-over table; a T20 rescales by R(20, 0))."""
        full = dls_raw_resource(clamp(float(format_overs), 1.0, 50.0), 0, self.DLS_CONSTANTS)
        u = min(clamp(float(overs_remaining), 0.0, 50.0), max(float(format_overs), 0.0))
        return 100.0 * dls_raw_resource(u, int(wickets_lost), self.DLS_CONSTANTS) / full

    def resources_lost_to_interruption(self, overs_left_before: float, overs_left_after: float, wickets_lost: int,
                                       format_overs: float = 50.0) -> float:
        """Resource percentage removed by a stoppage that cuts the overs left from ``before`` to ``after``."""
        before = self.calculate_dls_resource(overs_left_before, wickets_lost, format_overs)
        after = self.calculate_dls_resource(min(overs_left_after, overs_left_before), wickets_lost, format_overs)
        return max(0.0, before - after)

    @staticmethod
    def _g_reference(format_overs: float) -> float:
        return DLS_G["t20"] if format_overs <= 20.0 else DLS_G["odi"]

    def revised_target(self, team1_runs: float, team1_resource_pct: float, team2_resource_pct: float, format_overs: float = 50.0) -> int:
        """The DLS target: ``floor(S1 R2 / R1) + 1`` when team 2 has fewer resources, else
        ``S1 + floor((R2 - R1) / 100 x G) + 1`` (G = 170 for a T20, 245 for 50 overs)."""
        r1, r2 = float(team1_resource_pct), float(team2_resource_pct)
        if r1 <= 0.0:
            return 1
        if r2 < r1:
            return math.floor(team1_runs * r2 / r1 + 1e-9) + 1
        return int(team1_runs + math.floor((r2 - r1) / 100.0 * self._g_reference(format_overs) + 1e-9)) + 1

    def interrupted_chase_target(self, team1_runs: float, team1_resource_pct: float, *, team2_overs_at_start: float,
                                 overs_left_before: float, overs_left_after: float, wickets_lost: int,
                                 format_overs: float = 50.0) -> int:
        """A chase cut short mid-innings: ``R2 = R(team 2's starting overs, 0) - resources lost`` (100 - lost when the
        chase began with the full allocation)."""
        start = self.calculate_dls_resource(team2_overs_at_start, 0, format_overs)
        lost = self.resources_lost_to_interruption(overs_left_before, overs_left_after, wickets_lost, format_overs)
        return self.revised_target(team1_runs, team1_resource_pct, start - lost, format_overs)

    def calculate_dls_par_score(
        self,
        team1_runs: int,
        team1_overs: float,
        team2_overs_available: float,
        team2_wickets_lost: int,
        format_overs: float = 20.0,
    ) -> int:
        """Revised target / par score: team 1 used ``R(team1_overs, 0)``, team 2 has ``R(team2_overs_available,
        team2_wickets_lost)``, both relative to a full ``format_overs`` innings."""
        r1 = self.calculate_dls_resource(team1_overs, 0, format_overs)
        r2 = self.calculate_dls_resource(team2_overs_available, team2_wickets_lost, format_overs)
        if r1 <= 0:
            return 0
        return self.revised_target(team1_runs, r1, r2, format_overs)

    # ------------------------------------------------------------------ setup
    @staticmethod
    def match_format(ctx: MatchContext) -> str:
        raw = ctx.text("format", "t20").replace("-", "_").replace(" ", "_")
        if raw in ("odi", "50", "50_over", "50_overs", "one_day", "list_a", "oneday"):
            return "odi"
        if raw in ("test", "tests", "first_class", "multi_day"):
            return "test"
        return "t20"

    @staticmethod
    def batting_first(ctx: MatchContext) -> str:
        first = ctx.text("batting_first", "")
        if first in ("home", "away"):
            return first
        chasing = ctx.text("chasing_team", "")
        if chasing in ("home", "away"):
            return "away" if chasing == "home" else "home"
        return "home"

    @staticmethod
    def dew_scale(ctx: MatchContext, fmt: str) -> tuple[float, str]:
        """The dew intensity scale (0, 0.5, 0.75, 1.0) and its label; only a night limited-overs match qualifies."""
        if fmt == "test":
            return 0.0, "none"
        weather = ctx.weather
        night = bool(ctx.get("is_night_match", False)) or (weather is not None and weather.local_hour is not None
                                                          and weather.local_hour >= NIGHT_HOUR)
        if not night:
            return 0.0, "none"
        stated = weather.dew if weather is not None else None
        if stated in ("light", "moderate", "heavy"):
            return DEW_INTENSITY[stated], stated
        humidity = weather.humidity_pct if weather is not None else None
        if humidity is not None and humidity > DEW_HUMIDITY:
            label = "heavy" if humidity > 92.0 else "moderate" if humidity > 85.0 else "light"
            return DEW_INTENSITY[label], label
        return 0.0, "none"

    @staticmethod
    def weather_effects(weather: Weather | None, night: bool) -> tuple[float, float, float, dict[str, Any]]:
        """(run multiplier, six multiplier, wicket multiplier, record). The Wire's friction replaces our own terms."""
        if weather is None or weather.indoor:
            return 1.0, 1.0, 1.0, {}
        if weather.friction is not None:
            f = clamp(weather.friction, 0.7, 1.3)
            return f, f, 1.0, {"weather_friction_multiplier": round(f, 4)}
        run_mult, six_mult, wicket_mult = 1.0, 1.0, 1.0
        if weather.precipitation_mm is not None and weather.precipitation_mm > 0.5:
            run_mult *= WET_OUTFIELD_MULT
        if weather.altitude_m is not None and weather.altitude_m > 1000.0:
            six_mult *= 1.0 + min(0.10, 0.05 * (weather.altitude_m - 1000.0) / 1000.0)
        if not night and weather.humidity_pct is not None and weather.humidity_pct > SWING_HUMIDITY:
            wicket_mult *= 1.0 + SWING_WICKET_UPLIFT
        record = {"weather_run_multiplier": round(run_mult, 4), "weather_six_multiplier": round(six_mult, 4),
                  "weather_swing_wicket_multiplier": round(wicket_mult, 4)}
        return run_mult, six_mult, wicket_mult, record

    def match_setup(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> MatchSetup:
        fmt = self.match_format(ctx)
        overs = round(clamp(ctx.number("overs_per_side", FORMAT_OVERS.get(fmt, 90)), 1.0, FORMAT_OVERS.get(fmt, 90))) if fmt != "test" else 0
        adjustments: dict[str, Any] = {}
        home_adv = clamp(ctx.number("home_advantage", 1.0), 0.8, 1.25)
        ratings: dict[str, dict[str, float]] = {}
        for side, stats in (("home", home_stats), ("away", away_stats)):
            bat = float(num(stats, "batting_rating", default=100.0) or 100.0)
            bowl = float(num(stats, "bowling_rating", default=100.0) or 100.0)
            if side == "home":
                bat, bowl = bat * home_adv, bowl * home_adv
            bat_war = bowl_war = 0.0
            for a in ctx.absences:
                if a.side != side:
                    continue
                pos = (a.position or "").replace(" ", "_")
                if pos in BAT_POSITIONS:
                    bat_war += a.war * a.weight
                elif pos in BOWL_POSITIONS:
                    bowl_war += a.war * a.weight
                else:  # all-rounders and unknown roles cost both disciplines half each
                    bat_war += 0.5 * a.war * a.weight
                    bowl_war += 0.5 * a.war * a.weight
            if bat_war or bowl_war:
                bat -= RATING_POINTS_PER_WAR * bat_war
                bowl -= RATING_POINTS_PER_WAR * bowl_war
                adjustments[f"{side}_absence_rating_delta"] = {"batting": round(-RATING_POINTS_PER_WAR * bat_war, 3),
                                                               "bowling": round(-RATING_POINTS_PER_WAR * bowl_war, 3)}
            ratings[side] = {"batting": max(40.0, bat), "bowling": max(40.0, bowl)}
        strength = {"home": ratings["home"]["batting"] / ratings["away"]["bowling"],
                    "away": ratings["away"]["batting"] / ratings["home"]["bowling"]}
        spin = {side: clamp(float(num(stats, "spin_share", default=DEFAULT_SPIN_SHARE) or 0.0), 0.0, 1.0)
                for side, stats in (("home", home_stats), ("away", away_stats))}
        dew, dew_label = self.dew_scale(ctx, fmt)
        night = dew > 0.0 or bool(ctx.get("is_night_match", False))
        run_mult, six_mult, wicket_mult, weather_record = self.weather_effects(ctx.weather, night)
        adjustments.update(weather_record)
        extras_rate = EXTRAS_RATE[fmt]
        if ctx.officials is not None:
            wides = ctx.officials.rate("wides_per_innings", "extras_per_innings")
            if wides is not None:
                factor = clamp(wides / LEAGUE_EXTRAS_PER_INNINGS[fmt], 0.5, 2.0)
                extras_rate *= factor
                adjustments["umpire_extras_factor"] = round(factor, 4)
            lbw = ctx.officials.rate("lbw_factor", "dismissal_factor")
            if lbw is not None:
                factor = clamp(lbw, 0.85, 1.2)
                wicket_mult *= factor
                adjustments["umpire_wicket_factor"] = round(factor, 4)
        default_wear = TEST_BASE_WEAR if fmt == "test" else 0.0
        wear = clamp(ctx.number("pitch_wear", default_wear), 0.0, 1.0)
        flatness = clamp(ctx.number("pitch_flatness", 0.5), 0.0, 1.0)
        adjustments.update({"dew_intensity": dew_label, "dew_scale": dew, "pitch_wear": round(wear, 4)})
        return MatchSetup(fmt, overs, self.batting_first(ctx), strength, ratings, spin, wear, dew, run_mult, six_mult, wicket_mult,
                          extras_rate, flatness, adjustments)

    def simulate_innings_distribution(self, setup: MatchSetup, batting: str, innings_no: int, *, start_ball: int = 0,
                                      wickets: int = 0, runs: int = 0, dew: bool = True) -> InningsDistribution:
        """The exact run distribution of ``batting``'s innings number ``innings_no`` (from a live state if given),
        with its powerplay (first six overs) distribution and expected sixes."""
        cond = setup.conditions(batting, innings_no)
        if not dew:
            cond = replace(cond, dew=0.0)
        if setup.fmt == "test":
            return simulate_test_innings(cond, wickets=wickets, runs=runs)
        return simulate_limited_innings(cond, setup.overs, start_ball=start_ball, wickets=wickets, runs=runs)

    # ------------------------------------------------------------------ Test draw
    def test_draw_probability(self, setup: MatchSetup, ctx: MatchContext, overs_needed: float,
                              overs_available: float | None = None) -> tuple[float, dict[str, float]]:
        """``logit P(draw) = logit(base) + coef (overs needed - overs available + slack) / 90``; rain removes overs."""
        rain_lost = 0.0
        weather = ctx.weather
        if weather is not None and not weather.indoor and weather.precipitation_mm is not None:
            rain_lost = min(RAIN_OVERS_CAP, RAIN_OVERS_PER_MM * max(0.0, weather.precipitation_mm))
        available = (TEST_SCHEDULED_OVERS if overs_available is None else overs_available) - rain_lost
        base_logit = math.log(TEST_DRAW_BASE / (1.0 - TEST_DRAW_BASE))
        z = base_logit + TEST_DRAW_TIME_COEF * (overs_needed - available + TEST_NEUTRAL_SLACK) / 90.0
        draw = clamp(logistic(z), 0.005, 0.95)
        return draw, {"overs_needed": round(overs_needed, 2), "overs_available": round(available, 2),
                      "overs_lost_to_rain": round(rain_lost, 2), "pitch_flatness": setup.flatness}

    # ------------------------------------------------------------------ the model
    def model(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> dict[str, Any]:
        setup = self.match_setup(home_stats, away_stats, ctx)
        if setup.fmt == "test":
            return self._model_test(setup, home_stats, away_stats, ctx)
        return self._model_limited(setup, home_stats, away_stats, ctx)

    @staticmethod
    def chase_grid(first: InningsDistribution, chase: InningsDistribution) -> ScoreGrid:
        """Joint (first innings, second innings) with the chase stopping once the target is passed.

        Given a first-innings total ``t``, the second innings ends at ``x = U`` when ``U <= t`` and otherwise passes
        the target ``t + 1`` by an overshoot ``j`` with the renewal excess ``P(j) = P(X > j) / E[X]`` of the scoring
        shot ``X`` (a single 1..6 run stroke). Rows: first innings runs; columns: second innings runs."""
        p1 = np.zeros(first.total.offset + len(first.total.probs))
        p1[first.total.offset:] = first.total.probs
        u = np.zeros(chase.total.offset + len(chase.total.probs))
        u[chase.total.offset:] = chase.total.probs
        shots = np.array([1, 2, 3, 4, 6])
        weights = np.array(BALL_BASE["t20"]["death"] if chase.format == "t20" else BALL_BASE["odi"]["death"])[1:6]
        weights = weights / weights.sum()
        mean_shot = float(weights @ shots)
        overshoot = np.array([float(weights[shots > j].sum()) / mean_shot for j in range(6)])
        width = max(len(p1), len(u)) + 7
        u_pad = np.zeros(width)
        u_pad[: len(u)] = u
        tail = np.clip(1.0 - np.cumsum(u_pad), 0.0, None)  # P(U > t)
        cond = np.tril(np.broadcast_to(u_pad, (len(p1), width)))  # x <= t: the chase fell short or tied at x
        rows = np.arange(len(p1))
        for j, weight in enumerate(overshoot):  # x = t + 1 + j: the target passed by j
            cols = rows + 1 + j
            ok = cols < width
            cond[rows[ok], cols[ok]] += tail[rows[ok]] * weight
        joint = p1[:, None] * cond
        return ScoreGrid.normalised(joint)

    @staticmethod
    def _weighted(estimates: list[SubModelEstimate], elo: SubModelEstimate | None) -> list[SubModelEstimate]:
        """Attach the default weights (the Elo set when both sides carry an Elo rating)."""
        weights = SUB_MODEL_WEIGHTS_ELO if elo is not None else SUB_MODEL_WEIGHTS
        pool = [*estimates, elo] if elo is not None else list(estimates)
        return [replace(e, weight=weights[e.name]) for e in pool]

    def _limited_secondary_models(self, setup: MatchSetup, dew_runs: float, chase_mean: float,
                                  tie_draw: float) -> tuple[SubModelEstimate, SubModelEstimate, float, float]:
        """The DLS resource-par normal model and the spec's run-difference logistic for a limited-overs match."""
        chase_side = setup.chasing
        resource = self.calculate_dls_resource(setup.overs, 0, FORMAT_OVERS[setup.fmt])
        par = DLS_G[setup.fmt] * resource / 100.0
        exp = {side: par * setup.strength[side] * setup.run_mult for side in ("home", "away")}
        exp[chase_side] += dew_runs
        margin_sd = math.hypot(*(DLS_SD_FRACTION[setup.fmt] * exp[side] for side in ("home", "away")))
        diff = exp["home"] - exp["away"]
        d_home = 1.0 - normal_cdf(0.5, diff, margin_sd)
        d_away = normal_cdf(-0.5, diff, margin_sd)
        d_tie = max(0.0, 1.0 - d_home - d_away)
        if setup.fmt == "t20":
            dls = SubModelEstimate("dls_resource_par", d_home + d_tie / 2.0, d_away + d_tie / 2.0, 0.0, exp["home"] + exp["away"])
        else:
            dls = SubModelEstimate("dls_resource_par", d_home, d_away, tie_draw, exp["home"] + exp["away"])
        # the spec's run-difference logistic; dew lifts the chasing side by the Markov chain's run shift
        rr = RATING_RUN_RATE[setup.fmt]
        runs = {side: rr * setup.ratings[side]["batting"] / 100.0 * 100.0 / max(50.0, setup.ratings[other]["bowling"]) * setup.overs
                for side, other in (("home", "away"), ("away", "home"))}
        if dew_runs:
            runs[chase_side] *= 1.0 + dew_runs / max(chase_mean - dew_runs, 1.0)
        r_home = logistic((runs["home"] - runs["away"]) / RATING_SCALE[setup.fmt])
        rating = SubModelEstimate("rating_logistic", r_home * (1.0 - tie_draw), (1.0 - r_home) * (1.0 - tie_draw), tie_draw,
                                  runs["home"] + runs["away"])
        return dls, rating, resource, par

    def _model_limited(self, setup: MatchSetup, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                       ctx: MatchContext) -> dict[str, Any]:
        first_side, chase_side = setup.batting_first, setup.chasing
        first = self.simulate_innings_distribution(setup, first_side, 1)
        chase = self.simulate_innings_distribution(setup, chase_side, 2)
        grid_first = self.chase_grid(first, chase)
        f_win, tie, c_win = grid_first.outcome()
        dew_record: dict[str, Any] = {"dew_impact_applied": setup.dew > 0.0}
        dew_runs = 0.0
        if setup.dew > 0.0:
            dry = self.simulate_innings_distribution(setup, chase_side, 2, dew=False)
            dry_chase = prob_exceeds(dry.total, first.total)[0]
            wet_chase = prob_exceeds(chase.total, first.total)[0]
            dew_runs = chase.mean - dry.mean
            dew_record.update({"dew_runs_shift": round(dew_runs, 3), "dew_chase_win_shift": round(wet_chase - dry_chase, 4),
                               "dew_bowling_spin_share": round(setup.spin_share[first_side], 3)})
        matrix = grid_first.matrix if first_side == "home" else grid_first.matrix.T
        grid = ScoreGrid(matrix)
        total_pmf = grid.total_pmf()
        as_played = {"home": grid.home_pmf(), "away": grid.away_pmf()}
        home_first = first_side == "home"
        p_home = f_win if home_first else c_win
        p_away = c_win if home_first else f_win
        if setup.fmt == "t20":  # a tie goes to a super over, a coin flip between equal-ish sides
            markov = SubModelEstimate("markov_ball_by_ball", p_home + tie / 2.0, p_away + tie / 2.0, 0.0, total_pmf.mean)
            tie_draw = 0.0
        else:
            markov = SubModelEstimate("markov_ball_by_ball", p_home, p_away, tie, total_pmf.mean)
            tie_draw = tie

        dls, rating, resource, par = self._limited_secondary_models(setup, dew_runs, chase.mean, tie_draw)
        estimates = self._weighted([markov, dls, rating], self._elo_estimate(home_stats, away_stats, ctx, tie_draw, markov))

        innings = {first_side: first, chase_side: chase}
        metrics: dict[str, Any] = {
            "format": setup.fmt,
            "overs_per_side": setup.overs,
            "batting_first": first_side,
            "home_expected_runs": round(innings["home"].mean, 2),
            "away_expected_runs": round(innings["away"].mean, 2),
            "home_expected_runs_as_played": round(as_played["home"].mean, 2),
            "away_expected_runs_as_played": round(as_played["away"].mean, 2),
            "home_strength": round(setup.strength["home"], 4),
            "away_strength": round(setup.strength["away"], 4),
            "home_expected_sixes": round(innings["home"].expected_sixes, 3),
            "away_expected_sixes": round(innings["away"].expected_sixes, 3),
            "home_powerplay_expected_runs": round(innings["home"].powerplay.mean, 2) if innings["home"].powerplay else None,
            "away_powerplay_expected_runs": round(innings["away"].powerplay.mean, 2) if innings["away"].powerplay else None,
            "markov_tie_prob": round(tie, 5),
            "dls_resource_pct": round(resource, 3),
            "dls_par_score": round(par, 2),
            **dew_record,
            **setup.adjustments,
        }
        return {"setup": setup, "estimates": estimates, "grid": grid, "innings": innings, "metrics": metrics, "tie": tie,
                "total_pmf": total_pmf, "as_played": as_played, "expected_total": total_pmf.mean}

    def _model_test(self, setup: MatchSetup, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                    ctx: MatchContext) -> dict[str, Any]:
        order = setup.innings_order()
        innings = [self.simulate_innings_distribution(setup, side, k + 1) for k, side in enumerate(order)]
        totals: dict[str, IntPMF] = {}
        for side in ("home", "away"):
            parts = [inn.total for inn, s in zip(innings, order, strict=True) if s == side]
            totals[side] = parts[0].convolve(parts[1])
        margin = totals["home"].convolve(totals["away"].negate())
        decisive_home = margin.prob_gt(0.0)
        decisive_away = margin.prob_lt(0.0)
        overs_needed = sum(inn.expected_balls for inn in innings) / 6.0
        draw, draw_record = self.test_draw_probability(setup, ctx, overs_needed)
        split = decisive_home / max(decisive_home + decisive_away, 1e-12)
        markov = SubModelEstimate("markov_ball_by_ball", (1.0 - draw) * split, (1.0 - draw) * (1.0 - split), draw,
                                  totals["home"].mean + totals["away"].mean)
        exp = {side: 2.0 * TEST_PAR_INNINGS * setup.strength[side] * setup.run_mult for side in ("home", "away")}
        sd = {side: math.sqrt(2.0) * DLS_SD_FRACTION["test"] * exp[side] / 2.0 for side in exp}
        d_home = 1.0 - normal_cdf(0.0, exp["home"] - exp["away"], math.hypot(sd["home"], sd["away"]))
        dls = SubModelEstimate("dls_resource_par", (1.0 - draw) * d_home, (1.0 - draw) * (1.0 - d_home), draw, exp["home"] + exp["away"])
        rr = RATING_RUN_RATE["test"]
        runs = {side: rr * setup.ratings[side]["batting"] / 100.0 * 100.0 / max(50.0, setup.ratings[other]["bowling"]) * TEST_RATING_OVERS
                for side, other in (("home", "away"), ("away", "home"))}
        r_home = logistic((runs["home"] - runs["away"]) / RATING_SCALE["test"])
        rating = SubModelEstimate("rating_logistic", (1.0 - draw) * r_home, (1.0 - draw) * (1.0 - r_home), draw, runs["home"] + runs["away"])
        estimates = self._weighted([markov, dls, rating], self._elo_estimate(home_stats, away_stats, ctx, draw, markov))
        first_innings = {side: next(inn for inn, s in zip(innings, order, strict=True) if s == side) for side in ("home", "away")}
        metrics: dict[str, Any] = {
            "format": "test",
            "batting_first": setup.batting_first,
            "home_expected_runs": round(totals["home"].mean, 2),
            "away_expected_runs": round(totals["away"].mean, 2),
            "innings_expected_runs": [round(inn.mean, 2) for inn in innings],
            "innings_batting_side": order,
            "home_strength": round(setup.strength["home"], 4),
            "away_strength": round(setup.strength["away"], 4),
            "home_expected_sixes": round(sum(inn.expected_sixes for inn, s in zip(innings, order, strict=True) if s == "home"), 3),
            "away_expected_sixes": round(sum(inn.expected_sixes for inn, s in zip(innings, order, strict=True) if s == "away"), 3),
            "home_powerplay_expected_runs": round(first_innings["home"].powerplay.mean, 2) if first_innings["home"].powerplay else None,
            "away_powerplay_expected_runs": round(first_innings["away"].powerplay.mean, 2) if first_innings["away"].powerplay else None,
            "test_draw_prob": round(draw, 4),
            "dew_impact_applied": False,
            **draw_record,
            **setup.adjustments,
        }
        return {"setup": setup, "estimates": estimates, "totals": totals, "innings": innings, "order": order,
                "first_innings": first_innings, "metrics": metrics,
                "expected_total": totals["home"].mean + totals["away"].mean}

    @staticmethod
    def _elo_estimate(home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext, draw: float,
                      markov: SubModelEstimate) -> SubModelEstimate | None:
        h_elo, a_elo = num(home_stats, "elo"), num(away_stats, "elo")
        if h_elo is None or a_elo is None:
            return None
        diff = h_elo - a_elo + ctx.number("elo_home_advantage", 0.0)
        p_home = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
        return SubModelEstimate("elo_logistic", (1.0 - draw) * p_home, (1.0 - draw) * (1.0 - p_home), draw, markov.expected_total)

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        built = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(built["estimates"], ctx)
        markets = self.price_markets(built, consensus.home_win_prob, consensus.away_win_prob, consensus.draw_prob)
        return self._assemble(consensus, ctx, expected_total=built["expected_total"], metrics=built["metrics"], markets=markets)

    @staticmethod
    def _lines(median: int, step: int) -> list[float]:
        return [median - step + 0.5, median + 0.5, median + step + 0.5] if step > 0 else [median + 0.5]

    @staticmethod
    def _key(line: float) -> str:
        return f"{line:g}".replace(".", "_")

    def price_markets(self, built: Mapping[str, Any], p_home: float, p_away: float, p_draw: float) -> dict[str, float]:
        """Every line from an exact distribution: innings totals (and, in limited overs, each side's runs as played,
        a chase stopping at the target), the home / away first-six-overs runs, total match runs (all mixed over the
        shared pitch factor), and most sixes from independent Poisson six counts."""
        markets: dict[str, float] = {"match_winner_home": p_home, "match_winner_away": p_away}
        setup: MatchSetup = built["setup"]
        sigma = PITCH_SIGMA[setup.fmt]
        if setup.fmt == "test":
            markets["match_draw"] = p_draw
            team_innings: dict[str, InningsDistribution] = built["first_innings"]
            step, total_step, prefix = 25, 50, "first_innings"
            total_pmf: IntPMF = built["totals"]["home"].convolve(built["totals"]["away"])
        else:
            markets["match_tie"] = float(built["tie"])
            team_innings = built["innings"]
            step, total_step = (10, 15) if setup.fmt == "t20" else (15, 25)
            prefix = "innings"
            total_pmf = built["total_pmf"]
            for side in ("home", "away"):  # the side's runs as the match is played (a chase stops at the target)
                pmf = pitch_mixture(built["as_played"][side], sigma)
                for line in self._lines(pmf.quantile(0.5), step):
                    markets[f"{side}_team_total_over_{line:g}"] = line_outcome(pmf, line, over=True).effective_probability
                    markets[f"{side}_team_total_under_{line:g}"] = line_outcome(pmf, line, over=False).effective_probability
        for side in ("home", "away"):
            pmf = pitch_mixture(team_innings[side].total, sigma)
            for line in self._lines(pmf.quantile(0.5), step):
                markets[f"{side}_{prefix}_over_{line:g}"] = line_outcome(pmf, line, over=True).effective_probability
                markets[f"{side}_{prefix}_under_{line:g}"] = line_outcome(pmf, line, over=False).effective_probability
            raw_pp = team_innings[side].powerplay
            if raw_pp is not None:
                pp = pitch_mixture(raw_pp, sigma)
                tag = "first_6_overs" if side == "home" else "away_first_6_overs"
                for line in sorted({48.5, *self._lines(pp.quantile(0.5), 5)}):
                    markets[f"{tag}_over_{self._key(line)}"] = line_outcome(pp, line, over=True).effective_probability
                    markets[f"{tag}_under_{self._key(line)}"] = line_outcome(pp, line, over=False).effective_probability
        total_mixed = pitch_mixture(total_pmf, sigma)
        for line in self._lines(total_mixed.quantile(0.5), total_step):
            markets[f"total_match_runs_over_{line:g}"] = line_outcome(total_mixed, line, over=True).effective_probability
            markets[f"total_match_runs_under_{line:g}"] = line_outcome(total_mixed, line, over=False).effective_probability
        sixes = {side: float(built["metrics"][f"{side}_expected_sixes"]) for side in ("home", "away")}
        size = int(max(sixes.values()) * 4 + 15)
        six_grid = ScoreGrid.independent(poisson_pmf(sixes["home"], size), poisson_pmf(sixes["away"], size))
        s_home, s_tie, s_away = six_grid.outcome()
        markets["most_sixes_home"], markets["most_sixes_tie"], markets["most_sixes_away"] = s_home, s_tie, s_away
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability from the batting state.

        ``live_state``: ``innings`` (1 or 2; 1..4 in a Test), ``runs`` and ``wickets`` of the batting side in the
        current innings, ``balls_bowled`` (or ``overs`` in cricket notation, 12.3 = 12 overs 3 balls), ``target``
        (second innings; else ``first_innings_runs`` + 1), ``max_overs`` (a DLS-revised innings length). Tests also take
        ``completed_innings`` (totals of the innings already finished, in order), ``overs_left_in_match`` and
        ``follow_on``. The batting order comes from ctx ``batting_first``.

        A limited-overs chase is solved exactly by the chase DP with the target absorbing; a first innings projects
        the final-total distribution from the current state and compares it with the opponent's full innings."""
        ctx = MatchContext.parse(context)
        setup = self.match_setup(home_stats, away_stats, ctx)
        innings_no = int(live_state.get("innings", 1))
        runs = int(num(live_state, "runs", default=0.0) or 0)
        wickets = int(clamp(num(live_state, "wickets", default=0.0) or 0.0, 0.0, 10.0))
        if "balls_bowled" in live_state:
            balls = int(num(live_state, "balls_bowled", default=0.0) or 0)
        else:
            balls = balls_from_overs(float(num(live_state, "overs", default=0.0) or 0.0))
        if setup.fmt == "test":
            return self._in_play_test(setup, ctx, live_state, innings_no=innings_no, runs=runs, wickets=wickets, balls=balls)
        overs = int(num(live_state, "max_overs", default=float(setup.overs)) or setup.overs)
        total_balls = overs * 6
        first_side, chase_side = setup.batting_first, setup.chasing
        detail: dict[str, Any] = {"innings": innings_no, "runs": runs, "wickets": wickets, "balls_bowled": balls, "format": setup.fmt}
        if innings_no >= 2:
            target = num(live_state, "target")
            if target is None:
                first_runs = num(live_state, "first_innings_runs")
                if first_runs is None:
                    raise ValueError("a second-innings state needs target or first_innings_runs")
                target = first_runs + 1.0
            need = int(target) - runs
            cond = setup.conditions(chase_side, 2)
            outcome = chase_probabilities(cond, need=need, balls_left=max(0, total_balls - balls), wickets=wickets,
                                          start_ball=balls, overs=overs)
            chase_win, tie = outcome.reached, outcome.level
            defend_win = max(0.0, 1.0 - chase_win - tie)
            detail.update({"target": int(target), "runs_needed": need, "balls_left": max(0, total_balls - balls)})
        else:
            if wickets < 10 and balls < total_balls:
                cond_first = setup.conditions(first_side, 1)
                projected = simulate_limited_innings(cond_first, overs, start_ball=balls, wickets=wickets, runs=runs).total
            else:
                projected = IntPMF(runs, np.array([1.0]))
            chase = self.simulate_innings_distribution(setup, chase_side, 2)
            chase_win, tie = prob_exceeds(chase.total, projected)
            defend_win = max(0.0, 1.0 - chase_win - tie)
            detail.update({"projected_total_mean": round(projected.mean, 2), "projected_total_p10": projected.quantile(0.1),
                           "projected_total_p90": projected.quantile(0.9)})
        if setup.fmt == "t20":
            chase_win, defend_win, draw = chase_win + tie / 2.0, defend_win + tie / 2.0, 0.0
        else:
            draw = tie
        home = defend_win if first_side == "home" else chase_win
        away = chase_win if first_side == "home" else defend_win
        total = home + away + draw
        return LiveWinProbability(home / total, away / total, draw / total, detail)

    def _in_play_test(self, setup: MatchSetup, ctx: MatchContext, live_state: Mapping[str, Any], *, innings_no: int, runs: int,
                      wickets: int, balls: int) -> LiveWinProbability:
        order = setup.innings_order(bool(live_state.get("follow_on", False)))
        innings_no = int(clamp(innings_no, 1, 4))
        completed = [float(x) for x in (live_state.get("completed_innings") or [])][: innings_no - 1]
        if len(completed) < innings_no - 1:
            raise ValueError(f"a Test state in innings {innings_no} needs {innings_no - 1} completed_innings totals")
        default_left = max(0.0, TEST_SCHEDULED_OVERS - 90.0 * (innings_no - 1) - balls / 6.0)
        overs_left = float(num(live_state, "overs_left_in_match", default=default_left) or 0.0)
        batting = order[innings_no - 1]
        done = {"home": 0.0, "away": 0.0}
        for total, side in zip(completed, order[: len(completed)], strict=True):
            done[side] += total
        detail: dict[str, Any] = {"innings": innings_no, "runs": runs, "wickets": wickets, "overs_left_in_match": overs_left,
                                  "format": "test", "batting": batting}
        if innings_no == 4:
            bowling = "away" if batting == "home" else "home"
            target = num(live_state, "target", default=done[bowling] - done[batting] + 1.0)
            need = int(target or 0) - runs
            cond = setup.conditions(batting, 4)
            outcome = chase_probabilities(cond, need=need, balls_left=int(overs_left * 6), wickets=wickets, start_ball=balls, overs=None)
            bat_win, tie = outcome.reached, outcome.level_all_out
            bowl_win = outcome.short_all_out
            draw = outcome.level_alive + outcome.short_alive + tie
            home, away = (bat_win, bowl_win) if batting == "home" else (bowl_win, bat_win)
            detail.update({"target": int(target or 0), "runs_needed": need})
            total = home + away + draw
            return LiveWinProbability(home / total, away / total, draw / total, detail)
        current = self.simulate_innings_distribution(setup, batting, innings_no, wickets=wickets, runs=runs)
        rest = [self.simulate_innings_distribution(setup, side, k + 1) for k, side in enumerate(order) if k + 1 > innings_no]
        totals = {side: IntPMF(int(done[side]), np.array([1.0])) for side in ("home", "away")}
        totals[batting] = totals[batting].convolve(current.total)
        balls_needed = current.expected_balls
        for inn, side in zip(rest, order[innings_no:], strict=True):
            totals[side] = totals[side].convolve(inn.total)
            balls_needed += inn.expected_balls
        margin = totals["home"].convolve(totals["away"].negate())
        dh, da = margin.prob_gt(0.0), margin.prob_lt(0.0)
        draw, record = self.test_draw_probability(setup, ctx, balls_needed / 6.0, overs_available=overs_left)
        split = dh / max(dh + da, 1e-12)
        detail.update(record)
        return LiveWinProbability((1.0 - draw) * split, (1.0 - draw) * (1.0 - split), draw, detail)

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        res = normalize_basic(match_result, home_keys=("home_runs", "home_score", "home_total"),
                              away_keys=("away_runs", "away_score", "away_total"))
        status = norm(match_result.get("status"))
        if status in NO_RESULT_STATUSES:
            res.void = True
        if res.winner is None and not res.winner_name:
            outcome = norm(match_result.get("result"))
            if outcome in ("draw", "drawn", "tie", "tied"):
                res.winner = "draw"
        innings = match_result.get("innings")
        if isinstance(innings, Sequence) and not isinstance(innings, (str, bytes)):
            values: list[float] = []
            for item in innings:
                value = num(item, "runs", "total") if isinstance(item, Mapping) else (float(item) if _is_number(item) else None)
                if value is None:
                    raise SettlementInputError(f"innings entry {item!r} carries no runs")
                values.append(value)
            res.extras["innings"] = values
        for key in ("home_sixes", "away_sixes", "powerplay_runs", "home_powerplay_runs", "away_powerplay_runs", "innings_runs",
                    "player_runs", "top_batsman_runs"):
            value = num(match_result, key)
            if value is not None:
                res.extras[key] = value
        return res

    @override
    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market in (MATCH_RESULT, MOST_SIXES, INNING_OF_HIGHEST_SCORE):
            return self._settle_outcome(market, result, selection)
        if market not in (INNINGS_TOTAL_RUNS, FIRST_6_OVERS_RUNS, TOP_BATSMAN_RUNS):
            raise SettlementInputError(f"cricket has no market {market!r}")
        if line is None:
            raise SettlementInputError(f"{market} needs a line")
        over = over_under(selection)
        if market == INNINGS_TOTAL_RUNS:
            value: float | None = self._innings_value(result, selection, params)
        elif market == FIRST_6_OVERS_RUNS:
            value = self._powerplay_value(result, selection)
        else:
            value = self._top_batsman_value(result, params)
        if value is None:
            return LegStatus.VOID  # the named batter did not take part
        return settle_threshold(value, line, over=over)

    @staticmethod
    def _settle_outcome(market: str, result: NormalizedResult, selection: str) -> LegStatus:
        """3-way match result, most sixes (home / away / tie) and the innings of the highest score."""
        if market == INNING_OF_HIGHEST_SCORE:
            values = result.extras.get("innings")
            if not values:
                raise SettlementInputError("inning of highest score needs the innings totals")
            top = max(values)
            leaders = [i + 1 for i, v in enumerate(values) if v == top]
            pick = norm(selection).replace("innings_", "").replace("inning_", "")
            if pick in ("tie", "draw", "x"):
                return LegStatus.WON if len(leaders) > 1 else LegStatus.LOST
            if pick not in ORDINALS:
                raise SettlementInputError(f"selection {selection!r} names no innings")
            return LegStatus.WON if leaders == [ORDINALS[pick]] else LegStatus.LOST
        if market == MOST_SIXES:
            home, away = result.extras.get("home_sixes"), result.extras.get("away_sixes")
            if home is None or away is None:
                raise SettlementInputError("most sixes needs home_sixes and away_sixes")
            actual: str | None = "home" if home > away else "away" if away > home else "draw"
        else:
            actual = result.side_winner()
            if actual is None:
                raise SettlementInputError("the result names no winner")
        return LegStatus.WON if _side(selection, result) == actual else LegStatus.LOST

    @staticmethod
    def _powerplay_value(result: NormalizedResult, selection: str) -> float:
        side = _side_or_none(selection, result)
        key = f"{side}_powerplay_runs" if side in ("home", "away") else "powerplay_runs"
        value = result.extras.get(key)
        if value is None:
            raise SettlementInputError(f"the result has no {key}")
        return float(value)

    @staticmethod
    def _top_batsman_value(result: NormalizedResult, params: Mapping[str, Any]) -> float | None:
        """The named batter's runs (``params.player`` with ``player_stats``; a non-participant is None) or ``player_runs``."""
        player = norm(params.get("player"))
        value = None
        if player:
            if result.participants is not None and player not in result.participants:
                return None
            value = result.player_stats.get(player, {}).get("runs")
        if value is None:
            value = result.extras.get("player_runs", result.extras.get("top_batsman_runs"))
        if value is None:
            raise SettlementInputError("top batsman runs needs player_runs")
        return float(value)

    @staticmethod
    def _innings_value(result: NormalizedResult, selection: str, params: Mapping[str, Any]) -> float:
        innings_no = params.get("innings")
        values = result.extras.get("innings")
        if innings_no is not None and values:
            index = ORDINALS.get(norm(innings_no))
            if index is None or index > len(values):
                raise SettlementInputError(f"the result has no innings {innings_no!r}")
            return float(values[index - 1])
        if "innings_runs" in result.extras:
            return float(result.extras["innings_runs"])
        side = _side_or_none(selection, result)
        if side == "home" and result.home is not None:
            return result.home
        if side == "away" and result.away is not None:
            return result.away
        raise SettlementInputError("innings total needs innings_runs, params.innings or a side with a score")


def _is_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _side_or_none(selection: str, result: NormalizedResult) -> str | None:
    for token in [selection, *split_selection(selection)]:
        side = side_token(token, result)
        if side is not None:
            return side
    return None


def _side(selection: str, result: NormalizedResult) -> str:
    side = _side_or_none(selection, result)
    if side is None:
        raise SettlementInputError(f"selection {selection!r} names no side")
    return side
