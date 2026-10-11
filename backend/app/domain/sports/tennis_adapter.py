"""
Tennis Quantitative Adapter: O'Malley-Barnett Point-to-Match Markov Chain & Surface Speed (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Every price comes from one exact Markov chain: points -> games -> tiebreaks -> sets -> match, with the serve
alternating game by game (and across sets), the tiebreak serving one point then two each, and the deciding set
of a Grand Slam settled by a 10-point tiebreak at 6-6.

* **Game** (closed form): ``P(hold) = p^4 (1 + 4q + 10q^2) + 20 p^3 q^3 p^2 / (p^2 + q^2)``, which equals the
  spec's ``20 p^5 q^3 / (1 - 2pq)`` deuce term because ``p^2 + q^2 = 1 - 2pq``.
* **Tiebreak** (exact): first to 7 (or 10) by two; the first server serves point 1, then the serve changes every
  two points. Recursion over the point score; from a tie at an even point count past 6-6 the next two points are
  served one each, so ``P(win from tie) = pA (1 - pB) / (pA (1 - pB) + (1 - pA) pB)``.
* **Set** (exact): a forward DP over game scores with the server fixed by the game count, a tiebreak at 6-6; it
  yields the joint law of (set winner, games A, games B, tiebreak played, who serves first next set).
* **Match** (exact, best of 3 or 5): a DP over set scores carrying the joint (games A, games B) grid, so the
  total-games and game-handicap distributions respect who serves first in every set; the first server of the
  match is ctx ``first_server`` or a 50/50 toss.

Serve point probability: Barnett-Clarke ``p_A = A_serve - (B_return - tour_return)`` when both sides carry
``serve_points_won_pct`` and ``return_points_won_pct``; else ``first_in x first_won + (1 - first_in) x second_won``
from ``first_serve_win_pct`` (``first_serve_in_pct`` default 0.62, ``second_serve_win_pct`` default 0.50). The
player's deviation from the tour average (0.636) is then carried in logit space onto the surface's tour-average
serve point probability, the one whose hold rate the surface speed index sets: grass 0.85 -> 84%, indoor hard 0.78
-> 80.5%, fast hard 0.75 -> 79%, medium hard 0.68 -> 75%, slow hard 0.62 -> 71%, clay 0.50 -> 66%.

Weather (logit shift of both serve point probabilities; none indoors): heat above 30C (+0.006 per degree, at most
+0.06) and altitude above 500 m (+0.035 per 1000 m) speed the ball up; wind above 25 kph (-0.006 per kph, at most
-0.10) disrupts the toss and the serve. The Wire's friction factor, a scoring-pace multiplier, has no direct
meaning in a sport where every rally scores a point; it is used as a serve-dominance proxy instead of our own
terms: a faster, livelier court (friction > 1) favours the server by ``0.25 ln(friction)`` in logit.

Sub-models (fortress pillar 1):

1. **omalley_barnett_markov** (0.60): the exact match probability above.
2. **surface_elo** (0.25): ``1 / (1 + 10^(-dElo / 400))`` on the mean of overall and surface Elo when both sides
   carry them; else the Elo equivalent of the points-dominance edge, ``dElo = 7000 (TPW - 0.5)``, with
   ``TPW = (p_A + 1 - p_B) / 2``; best of five scales the edge by 1.25.
3. **dominance_ratio_logistic** (0.15): ``logistic(3.65 ln(DR_A / DR_B))`` with the dominance ratio
   ``DR = return points won / serve points lost`` (season stats when present, else the matchup's).

Settlement: a retirement leaves the match winner standing; every other market of that match is VOID. A walkover
voids everything.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import Any, ClassVar, override

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext, Weather
from app.domain.sports.quant import IntPMF, LineOutcome, ScoreGrid, SubModelEstimate, clamp, logistic, split_line
from app.domain.sports.settlement import (
    ALT_SPREAD,
    ALT_TOTAL,
    CORRECT_SCORE,
    MONEYLINE,
    PERIOD_MONEYLINE,
    PERIOD_SPREAD,
    PERIOD_TOTAL,
    PLAYER_PROP,
    SPREAD,
    TOTAL,
    NormalizedResult,
    SettlementInputError,
    norm,
    normalize_basic,
    num,
    over_under,
    settle_handicap,
    settle_threshold,
    side_token,
    split_selection,
    yes_no,
)
from app.schemas.bet_types import LegStatus

# ================================================================ surfaces and serve
SURFACE_SPEEDS: dict[str, float] = {
    "grass": 0.85,
    "indoor_hard": 0.78,
    "fast_hard": 0.75,
    "medium_hard": 0.68,
    "slow_hard": 0.62,
    "clay": 0.50,
}
SURFACE_ALIASES: dict[str, str] = {"hard": "medium_hard", "hardcourt": "medium_hard", "indoor": "indoor_hard", "carpet": "indoor_hard",
                                   "red_clay": "clay", "green_clay": "clay", "grass_court": "grass"}
#: (surface speed index, tour-average hold rate) anchors; other indices interpolate linearly.
HOLD_ANCHORS: tuple[tuple[float, float], ...] = ((0.50, 0.66), (0.62, 0.71), (0.68, 0.75), (0.75, 0.79), (0.85, 0.84))
TOUR_FIRST_SERVE_IN = 0.62
TOUR_FIRST_SERVE_WIN = 0.72
TOUR_SECOND_SERVE_WIN = 0.50
TOUR_SERVE_POINTS = TOUR_FIRST_SERVE_IN * TOUR_FIRST_SERVE_WIN + (1.0 - TOUR_FIRST_SERVE_IN) * TOUR_SECOND_SERVE_WIN  # 0.6364
SERVE_PROB_BOUNDS = (0.30, 0.92)

HEAT_THRESHOLD_C = 30.0
HEAT_LOGIT_PER_C = 0.006
HEAT_LOGIT_CAP = 0.06
ALTITUDE_THRESHOLD_M = 500.0
ALTITUDE_LOGIT_PER_KM = 0.035
WIND_THRESHOLD_KPH = 25.0
WIND_LOGIT_PER_KPH = 0.006
WIND_LOGIT_CAP = 0.10
FRICTION_SERVE_LOGIT = 0.25

# ================================================================ sub-models
SUB_MODEL_WEIGHTS: dict[str, float] = {"omalley_barnett_markov": 0.60, "surface_elo": 0.25, "dominance_ratio_logistic": 0.15}
TPW_ELO_PER_POINT = 7000.0
BEST_OF_5_EDGE_SCALE = 1.25
DOMINANCE_K = 3.65

# ================================================================ markets
SET_BETTING = "set_betting_correct_score"
GAME_HANDICAP = "game_handicap"
TOTAL_GAMES = "total_games_over_under"
FIRST_SET_WINNER = "first_set_winner"
TIEBREAK_IN_MATCH = "tiebreak_in_match"
TOTAL_SETS = "total_sets"
RETIRED_STATUSES = frozenset({"retired", "ret", "retirement", "retd"})
WALKOVER_STATUSES = frozenset({"walkover", "w/o", "wo", "w_o"})
GAME_POINT_SCORES: dict[str, int] = {"0": 0, "love": 0, "15": 1, "30": 2, "40": 3, "ad": 4, "a": 4, "adv": 4}
GAME_HANDICAP_LINES = (-3.5, -2.5, -1.5, -0.5, 0.5, 1.5, 2.5, 3.5)


# ================================================================ the point-to-match chain
def hold_probability(p: float) -> float:
    """P(the server holds) with point-on-serve probability ``p`` (O'Malley closed form)."""
    p = clamp(float(p), 0.0, 1.0)
    q = 1.0 - p
    return p ** 4 * (1.0 + 4.0 * q + 10.0 * q * q) + 20.0 * p ** 3 * q ** 3 * (p * p / (p * p + q * q))


def game_probability_from(p: float, server_points: int, receiver_points: int) -> float:
    """P(the server wins the game) from a point score (points won, 0..; 3-3 and beyond is deuce territory)."""
    q = 1.0 - p
    deuce = p * p / (p * p + q * q)

    @cache
    def win(a: int, b: int) -> float:
        if a >= 4 and a - b >= 2:
            return 1.0
        if b >= 4 and b - a >= 2:
            return 0.0
        if a >= 3 and b >= 3:
            if a == b:
                return deuce
            return p + q * deuce if a > b else p * deuce
        return p * win(a + 1, b) + q * win(a, b + 1)

    return win(int(server_points), int(receiver_points))


def tiebreak_server(first_server: str, points_played: int) -> str:
    """The server of the point after ``points_played`` points: the first server serves point 1, then the serve
    changes every two points (points 2-3 the other player, 4-5 the first server, ...)."""
    return first_server if ((points_played + 1) // 2) % 2 == 0 else _other(first_server)


def tiebreak_probability_from(p_a: float, p_b: float, first_server: str, *, points_a: int = 0, points_b: int = 0,
                              target: int = 7) -> float:
    """P(A wins the tiebreak) from a point score, exact. ``p_a`` / ``p_b``: each player's point-on-serve probability."""
    a_two = p_a * (1.0 - p_b)
    b_two = (1.0 - p_a) * p_b
    level = a_two / (a_two + b_two) if a_two + b_two > 0 else 0.5

    @cache
    def win(i: int, j: int) -> float:
        if i >= target and i - j >= 2:
            return 1.0
        if j >= target and j - i >= 2:
            return 0.0
        if i == j and i >= target - 1:
            # past (target-1)-all every tie sits at an even point count: the next two points are served one each
            return level
        server = tiebreak_server(first_server, i + j)
        point = p_a if server == "a" else 1.0 - p_b
        return point * win(i + 1, j) + (1.0 - point) * win(i, j + 1)

    return win(int(points_a), int(points_b))


@dataclass(frozen=True, slots=True)
class SetOutcome:
    winner: str  # "a" | "b"
    games_a: int
    games_b: int
    tiebreak: bool
    next_server: str  # who serves the first game of the next set
    prob: float


def set_outcomes(p_a: float, p_b: float, server: str, *, tiebreak_target: int = 7, games_a: int = 0, games_b: int = 0,
                 tiebreak_points: tuple[int, int] | None = None) -> list[SetOutcome]:
    """The exact joint law of a set's end from a game score with ``server`` to serve the next game (or, with
    ``tiebreak_points``, from inside the 6-6 tiebreak with ``server`` serving the next point).

    Games alternate serve; a tiebreak counts as the 13th game, served first by the player whose turn it is, so the
    player who received first in it serves first in the next set (the alternation carries on)."""
    hold_a, hold_b = hold_probability(p_a), hold_probability(p_b)
    out: list[SetOutcome] = []
    start = games_a + games_b
    if tiebreak_points is not None:
        pa_pts, pb_pts = tiebreak_points
        first = server if ((pa_pts + pb_pts + 1) // 2) % 2 == 0 else _other(server)
        win = tiebreak_probability_from(p_a, p_b, first, points_a=pa_pts, points_b=pb_pts, target=tiebreak_target)
        nxt = _other(first)
        return [SetOutcome("a", 7, 6, True, nxt, win), SetOutcome("b", 6, 7, True, nxt, 1.0 - win)]
    states: dict[tuple[int, int], float] = {(games_a, games_b): 1.0}
    for total in range(start, 13):
        for (i, j), mass in sorted(states.items()):
            if i + j != total or mass <= 0.0:
                continue
            serving = server if (i + j - start) % 2 == 0 else _other(server)
            if i == 6 and j == 6:
                win = tiebreak_probability_from(p_a, p_b, serving, target=tiebreak_target)
                nxt = _other(serving)
                out.append(SetOutcome("a", 7, 6, True, nxt, mass * win))
                out.append(SetOutcome("b", 6, 7, True, nxt, mass * (1.0 - win)))
                continue
            game_a = hold_a if serving == "a" else 1.0 - hold_b
            for (ni, nj), pr in (((i + 1, j), game_a), ((i, j + 1), 1.0 - game_a)):
                if pr <= 0.0:
                    continue
                if (ni >= 6 and ni - nj >= 2) or ni == 7:
                    out.append(SetOutcome("a", ni, nj, False, _next_server(server, ni + nj - start), mass * pr))
                elif (nj >= 6 and nj - ni >= 2) or nj == 7:
                    out.append(SetOutcome("b", ni, nj, False, _next_server(server, ni + nj - start), mass * pr))
                else:
                    states[(ni, nj)] = states.get((ni, nj), 0.0) + mass * pr
        states = {k: v for k, v in states.items() if k[0] + k[1] > total}
    return out


def _next_server(set_server: str, games_since: int) -> str:
    return set_server if games_since % 2 == 0 else _other(set_server)


def _other(player: str) -> str:
    return "b" if player == "a" else "a"


@dataclass
class MatchDistribution:
    """The exact match law from one starting configuration."""

    win_a: float
    set_scores: dict[tuple[int, int], float]
    games: np.ndarray  # P(games A = i, games B = j)
    tiebreak_prob: float
    first_set_a: float
    set_win_a: dict[str, float] = field(default_factory=dict)  # P(A wins a regular set | first server)


def match_distribution(p_a: float, p_b: float, *, best_of: int, first_server: str, final_set_tiebreak: int,
                       sets_a: int = 0, sets_b: int = 0, first_set: list[SetOutcome] | None = None,
                       carry_games: tuple[int, int] = (0, 0)) -> MatchDistribution:
    """DP over set scores carrying the (games A, games B) grid and whether a tiebreak has been played.

    ``first_set`` overrides the law of the set in progress (in play); ``carry_games`` are the games already banked
    in earlier sets."""
    need = best_of // 2 + 1
    size = 7 * best_of + 2
    cache: dict[tuple[str, bool], list[SetOutcome]] = {}

    def law(server: str, deciding: bool) -> list[SetOutcome]:
        key = (server, deciding)
        if key not in cache:
            cache[key] = set_outcomes(p_a, p_b, server, tiebreak_target=final_set_tiebreak if deciding else 7)
        return cache[key]

    start_grid = np.zeros((size, size))
    start_grid[min(carry_games[0], size - 1), min(carry_games[1], size - 1)] = 1.0
    states: dict[tuple[int, int, str, bool], np.ndarray] = {(sets_a, sets_b, first_server, False): start_grid}
    finals: dict[tuple[int, int, bool], np.ndarray] = {}
    first_set_a = 0.0
    is_first = True
    for _ in range(best_of):
        nxt: dict[tuple[int, int, str, bool], np.ndarray] = {}
        for (sa, sb, server, tb), grid in states.items():
            deciding = sa + sb == best_of - 1
            outcomes = first_set if (is_first and first_set is not None) else law(server, deciding)
            for o in outcomes:
                if o.prob <= 0.0:
                    continue
                shifted = np.zeros((size, size))
                shifted[o.games_a:, o.games_b:] = grid[: size - o.games_a, : size - o.games_b] * o.prob
                na, nb = (sa + 1, sb) if o.winner == "a" else (sa, sb + 1)
                if is_first and o.winner == "a":
                    first_set_a += o.prob
                ntb = tb or o.tiebreak
                if need in (na, nb):
                    key_f = (na, nb, ntb)
                    finals[key_f] = finals.get(key_f, 0.0) + shifted
                else:
                    key_s = (na, nb, o.next_server, ntb)
                    nxt[key_s] = nxt.get(key_s, 0.0) + shifted
        states = nxt
        is_first = False
        if not states:
            break
    games = np.zeros((size, size))
    set_scores: dict[tuple[int, int], float] = {}
    tiebreak = 0.0
    win_a = 0.0
    for (sa, sb, tb), grid in finals.items():
        mass = float(grid.sum())
        games += grid
        set_scores[(sa, sb)] = set_scores.get((sa, sb), 0.0) + mass
        if tb:
            tiebreak += mass
        if sa == need:
            win_a += mass
    set_win = {s: sum(o.prob for o in law(s, False) if o.winner == "a") for s in ("a", "b")}
    return MatchDistribution(win_a, set_scores, games, tiebreak, first_set_a, set_win)


def line_outcome(pmf: IntPMF, line: float, *, over: bool) -> LineOutcome:
    """``IntPMF.line_outcome`` vectorised over the support (identical buckets: win, half win, push, half loss, loss)."""
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


def _logit(p: float) -> float:
    p = clamp(p, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def surface_tour_serve_probability(speed_index: float) -> float:
    """The tour-average point-on-serve probability whose hold rate the surface speed index sets (bisection)."""
    xs, ys = zip(*HOLD_ANCHORS, strict=True)
    target = float(np.interp(speed_index, xs, ys))
    lo, hi = 0.3, 0.95
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if hold_probability(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class TennisSportAdapter(BaseSportAdapter):
    """
    Quantitative Tennis Engine implementing the O'Malley & Barnett analytical
    Markov model from individual serve point win probabilities through
    game hold, tiebreak, set, and full best-of-3 / best-of-5 matches.
    """

    kelly_multiplier = 0.30
    model_family = "O'Malley-Barnett Markov"
    has_draw = False
    odds_api_family = "tennis"
    SURFACE_SPEEDS: ClassVar[dict[str, float]] = SURFACE_SPEEDS

    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner": (MONEYLINE, {}),
        "match_odds": (MONEYLINE, {}),
        "to_win_match": (MONEYLINE, {}),
        "set_winner": (PERIOD_MONEYLINE, {}),
        "set_handicap": (SPREAD, {}),
        "set_betting": (SET_BETTING, {}),
        "correct_score_sets": (SET_BETTING, {}),
        "games_handicap": (GAME_HANDICAP, {}),
        "game_spread": (GAME_HANDICAP, {}),
        "total_games": (TOTAL_GAMES, {}),
        "total_games_over_under": (TOTAL_GAMES, {}),
        "first_set_total_games": (PERIOD_TOTAL, {"period": "set1"}),
        "first_set_game_handicap": (PERIOD_SPREAD, {"period": "set1"}),
        "tiebreak_yes_no": (TIEBREAK_IN_MATCH, {}),
        "total_sets_over_under": (TOTAL_SETS, {}),
        "player_aces": (PLAYER_PROP, {"stat": "aces"}),
        "player_double_faults": (PLAYER_PROP, {"stat": "double_faults"}),
    }
    SPECIAL_MARKETS = frozenset({SET_BETTING, GAME_HANDICAP, TOTAL_GAMES, FIRST_SET_WINNER, TIEBREAK_IN_MATCH, TOTAL_SETS})
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, PERIOD_MONEYLINE, PERIOD_SPREAD, PERIOD_TOTAL,
                                   CORRECT_SCORE, PLAYER_PROP})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "period_lines", "parlay", "round_robin", "if_bet", "double_action",
                           "futures", "player_props", "game_props", "correct_score", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "tennis"

    @property
    def display_name(self) -> str:
        return "Tennis"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "match_winner",
            "set_betting_correct_score",
            "game_handicap",
            "total_games_over_under",
            "first_set_winner",
            "tiebreak_in_match",
            "total_sets",
        ]

    # ------------------------------------------------------------------ the chain (public, reference names)
    def hold_probability(self, p: float) -> float:
        """Calculates game hold probability given point win probability on serve p."""
        return hold_probability(p)

    def tiebreak_probability(self, p_a: float, p_b: float, first_server: str = "a", target: int = 7) -> float:
        """Exact P(player A wins a tiebreak) (first to ``target`` by two, serve alternating every two points)."""
        return tiebreak_probability_from(p_a, p_b, first_server, target=target)

    def set_probability(self, p_a: float, p_b: float, first_server: str = "a", tiebreak_target: int = 7) -> float:
        return sum(o.prob for o in set_outcomes(p_a, p_b, first_server, tiebreak_target=tiebreak_target) if o.winner == "a")

    # ------------------------------------------------------------------ inputs
    @staticmethod
    def surface(ctx: MatchContext) -> tuple[str, float]:
        raw = ctx.text("surface", "medium_hard").replace(" ", "_").replace("-", "_")
        key = SURFACE_ALIASES.get(raw, raw)
        if key not in SURFACE_SPEEDS:
            key = "medium_hard"
        speed = ctx.number("surface_speed_index", SURFACE_SPEEDS[key])
        return key, clamp(speed, 0.3, 1.0)

    @staticmethod
    def best_of(ctx: MatchContext) -> int:
        return 5 if int(ctx.number("best_of", 3)) >= 5 else 3

    def final_set_tiebreak(self, ctx: MatchContext, best_of: int) -> int:
        rule = ctx.text("final_set_tiebreak", "10" if best_of == 5 else "7")
        return 10 if rule in ("10", "match_tiebreak", "super", "super_tiebreak") else 7

    @staticmethod
    def raw_serve_points(stats: Mapping[str, Any]) -> float:
        first_in = num(stats, "first_serve_in_pct", default=TOUR_FIRST_SERVE_IN) or TOUR_FIRST_SERVE_IN
        first_won = num(stats, "first_serve_win_pct", default=TOUR_FIRST_SERVE_WIN) or TOUR_FIRST_SERVE_WIN
        second_won = num(stats, "second_serve_win_pct", default=TOUR_SECOND_SERVE_WIN) or TOUR_SECOND_SERVE_WIN
        return first_in * first_won + (1.0 - first_in) * second_won

    @staticmethod
    def weather_logit(weather: Weather | None) -> tuple[float, dict[str, float]]:
        if weather is None or weather.indoor:
            return 0.0, {}
        if weather.friction is not None:
            shift = FRICTION_SERVE_LOGIT * math.log(clamp(weather.friction, 0.5, 2.0))
            return shift, {"weather_serve_logit": round(shift, 4), "weather_friction_proxy": round(weather.friction, 4)}
        shift = 0.0
        if weather.temperature_c is not None and weather.temperature_c > HEAT_THRESHOLD_C:
            shift += min(HEAT_LOGIT_CAP, HEAT_LOGIT_PER_C * (weather.temperature_c - HEAT_THRESHOLD_C))
        if weather.altitude_m is not None and weather.altitude_m > ALTITUDE_THRESHOLD_M:
            shift += ALTITUDE_LOGIT_PER_KM * (weather.altitude_m - ALTITUDE_THRESHOLD_M) / 1000.0
        if weather.wind_kph is not None and weather.wind_kph > WIND_THRESHOLD_KPH:
            shift -= min(WIND_LOGIT_CAP, WIND_LOGIT_PER_KPH * (weather.wind_kph - WIND_THRESHOLD_KPH))
        return shift, {"weather_serve_logit": round(shift, 4)}

    def serve_probabilities(self, a_stats: Mapping[str, Any], b_stats: Mapping[str, Any], ctx: MatchContext) -> tuple[float, float, dict[str, Any]]:
        """Each player's point-on-serve probability in this matchup, on this surface, in this weather."""
        surface, speed = self.surface(ctx)
        tour_serve = ctx.number("tour_serve_points_pct", TOUR_SERVE_POINTS)
        tour_return = 1.0 - tour_serve
        surface_serve = surface_tour_serve_probability(speed)
        keys = ("serve_points_won_pct", "return_points_won_pct")
        barnett = all(num(s, k) is not None for s in (a_stats, b_stats) for k in keys)
        if barnett:
            raw_a = float(num(a_stats, keys[0]) or 0.0) - (float(num(b_stats, keys[1]) or 0.0) - tour_return)
            raw_b = float(num(b_stats, keys[0]) or 0.0) - (float(num(a_stats, keys[1]) or 0.0) - tour_return)
            source = "barnett_clarke"
        else:
            raw_a, raw_b = self.raw_serve_points(a_stats), self.raw_serve_points(b_stats)
            source = "first_serve_components"
        shift, weather_record = self.weather_logit(ctx.weather)
        lo, hi = SERVE_PROB_BOUNDS
        p_a = clamp(logistic(_logit(surface_serve) + _logit(clamp(raw_a, 0.05, 0.95)) - _logit(tour_serve) + shift), lo, hi)
        p_b = clamp(logistic(_logit(surface_serve) + _logit(clamp(raw_b, 0.05, 0.95)) - _logit(tour_serve) + shift), lo, hi)
        record = {"surface": surface, "surface_speed_index": speed, "surface_tour_hold_rate": round(hold_probability(surface_serve), 4),
                  "surface_tour_serve_prob": round(surface_serve, 4), "serve_prob_source": source,
                  "player_a_raw_serve_prob": round(raw_a, 4), "player_b_raw_serve_prob": round(raw_b, 4), **weather_record}
        if ctx.absences:
            record["absences_not_applicable"] = True  # an individual sport: a withdrawal is a walkover (void)
        return p_a, p_b, record

    # ------------------------------------------------------------------ the model
    def match_law(self, p_a: float, p_b: float, ctx: MatchContext) -> tuple[MatchDistribution, dict[str, Any]]:
        """The match law averaged over the toss unless ctx ``first_server`` names the first server."""
        best_of = self.best_of(ctx)
        final_tb = self.final_set_tiebreak(ctx, best_of)
        first = ctx.text("first_server", "")
        first = {"home": "a", "player_a": "a", "away": "b", "player_b": "b"}.get(first, first)
        servers = [first] if first in ("a", "b") else ["a", "b"]
        laws = [match_distribution(p_a, p_b, best_of=best_of, first_server=s, final_set_tiebreak=final_tb) for s in servers]
        w = 1.0 / len(laws)
        set_scores: dict[tuple[int, int], float] = {}
        for law in laws:
            for k, v in law.set_scores.items():
                set_scores[k] = set_scores.get(k, 0.0) + w * v
        merged = MatchDistribution(
            win_a=sum(law.win_a for law in laws) * w,
            set_scores=set_scores,
            games=sum((law.games for law in laws), np.zeros_like(laws[0].games)) * w,
            tiebreak_prob=sum(law.tiebreak_prob for law in laws) * w,
            first_set_a=sum(law.first_set_a for law in laws) * w,
            set_win_a=laws[0].set_win_a,
        )
        return merged, {"best_of_sets": best_of, "final_set_tiebreak": final_tb, "first_server": first if first in ("a", "b") else "toss"}

    @staticmethod
    def _elo_ratings(stats: Mapping[str, Any]) -> float | None:
        overall, surface = num(stats, "elo"), num(stats, "surface_elo")
        if overall is not None and surface is not None:
            return 0.5 * (overall + surface)
        return surface if surface is not None else overall

    def model(self, a_stats: Mapping[str, Any], b_stats: Mapping[str, Any],
              ctx: MatchContext) -> tuple[list[SubModelEstimate], MatchDistribution, dict[str, Any]]:
        p_a, p_b, record = self.serve_probabilities(a_stats, b_stats, ctx)
        law, rules = self.match_law(p_a, p_b, ctx)
        best_of = rules["best_of_sets"]
        edge_scale = BEST_OF_5_EDGE_SCALE if best_of == 5 else 1.0
        expected_games = ScoreGrid.normalised(law.games).total_pmf().mean

        markov = SubModelEstimate("omalley_barnett_markov", law.win_a, 1.0 - law.win_a, 0.0, expected_games)

        elo_a, elo_b = self._elo_ratings(a_stats), self._elo_ratings(b_stats)
        if elo_a is not None and elo_b is not None:
            elo_diff, elo_source = elo_a - elo_b, "ratings"
        else:
            tpw = 0.5 * (p_a + 1.0 - p_b)
            elo_diff, elo_source = TPW_ELO_PER_POINT * (tpw - 0.5), "points_dominance"
        elo_p = 1.0 / (1.0 + 10.0 ** (-edge_scale * elo_diff / 400.0))
        elo = SubModelEstimate("surface_elo", elo_p, 1.0 - elo_p, 0.0, expected_games)

        keys = ("serve_points_won_pct", "return_points_won_pct")
        if all(num(s, k) is not None for s in (a_stats, b_stats) for k in keys):
            dr_a = float(num(a_stats, keys[1]) or 0.0) / max(1e-6, 1.0 - float(num(a_stats, keys[0]) or 0.0))
            dr_b = float(num(b_stats, keys[1]) or 0.0) / max(1e-6, 1.0 - float(num(b_stats, keys[0]) or 0.0))
            dr_source = "season_stats"
        else:
            dr_a, dr_b = (1.0 - p_b) / (1.0 - p_a), (1.0 - p_a) / (1.0 - p_b)
            dr_source = "matchup"
        dr_p = logistic(DOMINANCE_K * edge_scale * math.log(max(dr_a, 1e-6) / max(dr_b, 1e-6)))
        dominance = SubModelEstimate("dominance_ratio_logistic", dr_p, 1.0 - dr_p, 0.0, expected_games)

        estimates = [replace(e, weight=SUB_MODEL_WEIGHTS[e.name]) for e in (markov, elo, dominance)]
        metrics = {
            "player_a_serve_prob": round(p_a, 4),
            "player_b_serve_prob": round(p_b, 4),
            "player_a_hold_prob": round(hold_probability(p_a), 4),
            "player_b_hold_prob": round(hold_probability(p_b), 4),
            "tiebreak_win_prob_a_serving_first": round(self.tiebreak_probability(p_a, p_b, "a"), 4),
            "tiebreak_win_prob_b_serving_first": round(self.tiebreak_probability(p_a, p_b, "b"), 4),
            "set_win_prob_a_serving_first": round(law.set_win_a["a"], 4),
            "set_win_prob_b_serving_first": round(law.set_win_a["b"], 4),
            "expected_total_games": round(expected_games, 3),
            "tiebreak_in_match_prob": round(law.tiebreak_prob, 4),
            "elo_diff": round(elo_diff, 2),
            "elo_source": elo_source,
            "dominance_ratio_a": round(dr_a, 4),
            "dominance_ratio_b": round(dr_b, 4),
            "dominance_source": dr_source,
            **rules,
            **record,
        }
        return estimates, law, metrics

    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        """Player A = ``home_stats``, player B = ``away_stats``."""
        ctx = MatchContext.parse(context)
        estimates, law, metrics = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(estimates, ctx)
        markets = self.price_markets(law, metrics["best_of_sets"], consensus.home_win_prob, consensus.away_win_prob)
        return self._assemble(consensus, ctx, expected_total=metrics["expected_total_games"], metrics=metrics, markets=markets)

    def price_markets(self, law: MatchDistribution, best_of: int, p_a: float, p_b: float) -> dict[str, float]:
        markets: dict[str, float] = {"match_winner_player_a": p_a, "match_winner_player_b": p_b}
        need = best_of // 2 + 1
        for (sa, sb), prob in sorted(law.set_scores.items(), key=lambda kv: (-kv[0][0], kv[0][1])):
            markets[f"set_betting_{sa}-{sb}"] = prob
        markets["straight_sets_player_a"] = law.set_scores.get((need, 0), 0.0)
        markets["straight_sets_player_b"] = law.set_scores.get((0, need), 0.0)
        grid = ScoreGrid.normalised(law.games)
        total = grid.total_pmf()
        margin = grid.margin_pmf()  # games A - games B
        median = total.quantile(0.5)
        for d in (-4, -2, 0, 2, 4):
            line = median + d + 0.5
            markets[f"total_games_over_{line:g}"] = line_outcome(total, line, over=True).effective_probability
            markets[f"total_games_under_{line:g}"] = line_outcome(total, line, over=False).effective_probability
        flipped = margin.negate()
        for h in GAME_HANDICAP_LINES:
            markets[f"game_handicap_player_a_{h:+g}"] = line_outcome(margin, -h, over=True).effective_probability
            markets[f"game_handicap_player_b_{h:+g}"] = line_outcome(flipped, -h, over=True).effective_probability
        markets["first_set_player_a"] = law.first_set_a
        markets["first_set_player_b"] = 1.0 - law.first_set_a
        markets["tiebreak_in_match_yes"] = law.tiebreak_prob
        markets["tiebreak_in_match_no"] = 1.0 - law.tiebreak_prob
        sets_played = IntPMF.from_array([sum(p for (sa, sb), p in law.set_scores.items() if sa + sb == n) for n in range(need, best_of + 1)], need)
        for line in ((2.5,) if best_of == 3 else (3.5, 4.5)):
            markets[f"total_sets_over_{line:g}"] = line_outcome(sets_played, line, over=True).effective_probability
            markets[f"total_sets_under_{line:g}"] = line_outcome(sets_played, line, over=False).effective_probability
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Exact live win probability from the score.

        ``live_state``: ``sets_a``, ``sets_b`` (sets won), ``games_a``, ``games_b`` (current set), ``points_a``,
        ``points_b`` (current game, as points won 0..n or the 0/15/30/40/AD call; tiebreak points as counts),
        ``server`` ("a" or "b", serving the next point) and ``in_tiebreak`` (bool; implied at 6-6).
        Player A = ``home_stats``."""
        ctx = MatchContext.parse(context)
        p_a, p_b, _ = self.serve_probabilities(home_stats, away_stats, ctx)
        best_of = self.best_of(ctx)
        final_tb = self.final_set_tiebreak(ctx, best_of)
        need = best_of // 2 + 1
        sets_a, sets_b = int(live_state.get("sets_a", 0)), int(live_state.get("sets_b", 0))
        detail: dict[str, Any] = {"sets": f"{sets_a}-{sets_b}", "best_of": best_of}
        if sets_a >= need or sets_b >= need:
            won = 1.0 if sets_a >= need else 0.0
            return LiveWinProbability(won, 1.0 - won, 0.0, {**detail, "finished": True})
        games_a, games_b = int(live_state.get("games_a", 0)), int(live_state.get("games_b", 0))
        server = {"home": "a", "player_a": "a", "away": "b", "player_b": "b"}.get(norm(live_state.get("server", "a")), norm(live_state.get("server", "a")))
        if server not in ("a", "b"):
            raise ValueError("live_state server must be 'a' or 'b'")
        deciding = sets_a + sets_b == best_of - 1
        tb_target = final_tb if deciding else 7
        in_tiebreak = bool(live_state.get("in_tiebreak", False)) or (games_a == 6 and games_b == 6)
        pts_a = _game_points(live_state.get("points_a", 0), tiebreak=in_tiebreak)
        pts_b = _game_points(live_state.get("points_b", 0), tiebreak=in_tiebreak)
        if in_tiebreak:
            current = set_outcomes(p_a, p_b, server, tiebreak_target=tb_target, tiebreak_points=(pts_a, pts_b))
        else:
            p_srv = p_a if server == "a" else p_b
            hold = game_probability_from(p_srv, *((pts_a, pts_b) if server == "a" else (pts_b, pts_a)))
            game_a = hold if server == "a" else 1.0 - hold
            current = []
            for (ga, gb), prob in (((games_a + 1, games_b), game_a), ((games_a, games_b + 1), 1.0 - game_a)):
                if prob <= 0.0:
                    continue
                if (ga >= 6 and ga - gb >= 2) or ga == 7:
                    current.append(SetOutcome("a", ga, gb, False, _other(server), prob))
                elif (gb >= 6 and gb - ga >= 2) or gb == 7:
                    current.append(SetOutcome("b", ga, gb, False, _other(server), prob))
                else:
                    rest = set_outcomes(p_a, p_b, _other(server), tiebreak_target=tb_target, games_a=ga, games_b=gb)
                    current.extend(replace(o, prob=o.prob * prob) for o in rest)
        law = match_distribution(p_a, p_b, best_of=best_of, first_server=server, final_set_tiebreak=final_tb, sets_a=sets_a,
                                 sets_b=sets_b, first_set=current)
        detail.update({"games": f"{games_a}-{games_b}", "points": f"{pts_a}-{pts_b}", "server": server, "in_tiebreak": in_tiebreak,
                       "set_win_prob_a": round(sum(o.prob for o in current if o.winner == "a"), 4),
                       "player_a_serve_prob": round(p_a, 4), "player_b_serve_prob": round(p_b, 4)})
        win = clamp(law.win_a, 0.0, 1.0)
        return LiveWinProbability(win, 1.0 - win, 0.0, detail)

    # ------------------------------------------------------------------ settlement
    @override
    def settle_market(
        self,
        market_type: str,
        match_result: Mapping[str, Any],
        selection: str,
        line: float | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> LegStatus:
        """RETIREMENT RULE: when a player retires the match winner stands (settled on the advancing player); every
        other market of the match is VOID. A walkover voids everything."""
        canonical, _ = self.canonical_market(market_type)
        status = norm(match_result.get("status"))
        if status in WALKOVER_STATUSES:
            return LegStatus.VOID
        if status in RETIRED_STATUSES and canonical != MONEYLINE:
            return LegStatus.VOID
        return super().settle_market(market_type, match_result, selection, line, params)

    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """Accepts the lenient forms: ``winner`` (a name or a/b), ``set_score`` ("2-1"), ``sets`` ([[6, 4], [3, 6], ...]
        or [{"a": 6, "b": 4}, ...]), ``periods`` (set1..set5), ``player_a_games`` / ``player_b_games``, ``total_games``,
        ``tiebreaks``, ``first_set_winner``, ``status`` (retired / walkover)."""
        res = normalize_basic(match_result, home_keys=("sets_a", "player_a_sets", "home_sets", "home_score"),
                              away_keys=("sets_b", "player_b_sets", "away_sets", "away_score"))
        sets = _parse_sets(match_result.get("sets"))
        if sets:
            for i, pair in enumerate(sets, start=1):
                res.periods.setdefault(f"set{i}", pair)
        else:
            for i in range(1, 6):
                pair = res.periods.get(f"set{i}") or res.periods.get(f"set_{i}")
                if pair is None:
                    break
                sets.append(pair)
        res.extras["sets"] = sets
        status = norm(match_result.get("status"))
        res.extras["retired"] = status in RETIRED_STATUSES
        if status in WALKOVER_STATUSES:
            res.void = True
        if sets and (res.home is None or res.away is None):
            finished = [(a, b) for a, b in sets if _set_complete(a, b)]
            res.home = float(sum(1 for a, b in finished if a > b))
            res.away = float(sum(1 for a, b in finished if b > a))
        for key in ("total_games", "player_a_games", "player_b_games", "tiebreaks", "total_sets"):
            value = num(match_result, key)
            if value is not None:
                res.extras[key] = value
        for key in ("set_score", "first_set_winner"):
            if match_result.get(key) not in (None, ""):
                res.extras[key] = norm(match_result.get(key))
        return res

    @override
    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == SET_BETTING:
            return LegStatus.WON if _set_pair(selection) == self._set_score(result) else LegStatus.LOST
        if market == FIRST_SET_WINNER:
            return self._settle_first_set(result, selection)
        if market == TIEBREAK_IN_MATCH:
            count = result.extras.get("tiebreaks")
            if count is None:
                sets: list[tuple[float, float]] = result.extras.get("sets", [])
                if not sets:
                    raise SettlementInputError("tiebreak in match needs tiebreaks or the set scores")
                count = sum(1 for a, b in sets if max(a, b) == 7 and min(a, b) == 6)
            return LegStatus.WON if (count > 0) == yes_no(selection) else LegStatus.LOST
        if market not in (GAME_HANDICAP, TOTAL_GAMES, TOTAL_SETS):
            raise SettlementInputError(f"tennis has no market {market!r}")
        if line is None:
            raise SettlementInputError(f"{market} needs a line")
        if market == GAME_HANDICAP:
            a_games, b_games = self._games(result)
            side = _player_side(selection, result)
            if side is None:
                raise SettlementInputError(f"selection {selection!r} names no player")
            team, opp = (a_games, b_games) if side == "a" else (b_games, a_games)
            return settle_handicap(team, opp, line)
        if market == TOTAL_GAMES:
            value = float(result.extras["total_games"]) if "total_games" in result.extras else sum(self._games(result))
        elif "total_sets" in result.extras:
            value = float(result.extras["total_sets"])
        else:
            value = float(sum(self._set_score(result)))
        return settle_threshold(value, line, over=over_under(selection))

    @staticmethod
    def _set_score(result: NormalizedResult) -> tuple[int, int]:
        if "set_score" in result.extras:
            return _set_pair(result.extras["set_score"])
        if result.home is not None and result.away is not None:
            return int(result.home), int(result.away)
        raise SettlementInputError("the result has no set score (set_score or the sets)")

    @staticmethod
    def _settle_first_set(result: NormalizedResult, selection: str) -> LegStatus:
        raw_winner = result.extras.get("first_set_winner")
        if raw_winner is None:
            sets: list[tuple[float, float]] = result.extras.get("sets", [])
            if not sets:
                raise SettlementInputError("first set winner needs first_set_winner or the set scores")
            a, b = sets[0]
            raw_winner = "a" if a > b else "b"
        actual_side, want_side = _player_side(raw_winner, result), _player_side(selection, result)
        if actual_side is not None and want_side is not None:
            return LegStatus.WON if actual_side == want_side else LegStatus.LOST
        return LegStatus.WON if norm(selection) == norm(raw_winner) else LegStatus.LOST

    @staticmethod
    def _games(result: NormalizedResult) -> tuple[float, float]:
        a, b = result.extras.get("player_a_games"), result.extras.get("player_b_games")
        if a is not None and b is not None:
            return float(a), float(b)
        sets: list[tuple[float, float]] = result.extras.get("sets", [])
        if not sets:
            raise SettlementInputError("the result has no games (player_a_games / player_b_games or the set scores)")
        return float(sum(s[0] for s in sets)), float(sum(s[1] for s in sets))


def _game_points(value: Any, *, tiebreak: bool) -> int:
    """Points won in the current game: a count, or (outside a tiebreak) the 0/15/30/40/AD call."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = max(0, int(value))
        return number if tiebreak else {15: 1, 30: 2, 40: 3}.get(number, number)
    token = norm(value)
    if not token:
        return 0
    if not tiebreak and token in GAME_POINT_SCORES:
        return GAME_POINT_SCORES[token]
    try:
        return max(0, int(float(token)))
    except ValueError as exc:
        raise ValueError(f"point score {value!r} is not a count or a 0/15/30/40/AD call") from exc


def _parse_sets(raw: Any) -> list[tuple[float, float]]:
    """``[[6, 4], [3, 6]]`` or ``[{"a": 6, "b": 4}, ...]`` -> [(6.0, 4.0), (3.0, 6.0)]."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    sets: list[tuple[float, float]] = []
    for i, pair in enumerate(raw, start=1):
        a: float | None
        b: float | None
        if isinstance(pair, Mapping):
            a, b = num(pair, "a", "player_a", "home"), num(pair, "b", "player_b", "away")
        elif isinstance(pair, Sequence) and len(pair) == 2:
            a, b = float(pair[0]), float(pair[1])
        else:
            a = b = None
        if a is None or b is None:
            raise SettlementInputError(f"set {i} score {pair!r} is not a pair of games")
        sets.append((a, b))
    return sets


def _set_complete(a: float, b: float) -> bool:
    """A finished set: 6-x by two, 7-5 / 7-6, or a deciding match tiebreak recorded as 1-0."""
    high, low = max(a, b), min(a, b)
    return (high >= 6 and high - low >= 2) or high == 7 or (high == 1 and low == 0)


def _set_pair(value: Any) -> tuple[int, int]:
    text = str(value).strip().replace(":", "-").replace("_", "-").replace(" ", "")
    parts = text.split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise SettlementInputError(f"set score {value!r} is not 'A-B'")
    return int(parts[0]), int(parts[1])


def _player_side(selection: Any, result: NormalizedResult) -> str | None:
    for token in [str(selection), *split_selection(str(selection))]:
        side = side_token(token, result)
        if side in ("home", "away"):
            return "a" if side == "home" else "b"
    return None
