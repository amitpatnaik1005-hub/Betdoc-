"""
Ice Hockey Quantitative Adapter: Shot-Level xG Poisson, Corsi/Fenwick Possession & Empty-Net Late Game (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Shot quality is a logistic regression ``logit P(goal) = b0 + b1 distance + b2 |angle| + b3 rebound + b4 rush`` with
``b0 = -1.258``, ``b1 = -0.0252`` per foot, ``b2 = -0.010`` per degree, ``b3 = +0.75``, ``b4 = +0.30``: an average
NHL unblocked shot (35 ft, 30 degrees) is worth 0.080 xG, a slot shot (12 ft, 10 degrees) 0.16, a slot rebound 0.29,
a 60-foot shot from a 45 degree angle 0.038. Team scoring rates come from ``expected_goals_per_60`` (or a shot sample)
against the opponent's ``expected_goals_against_per_60`` (``xGF_home x xGA_away / league``), less the opposing
goalie's goals saved above expected (``gsax_per_60``), with home ice worth 0.22 goals (half added, half removed).

Four independent sub-models vote (fortress pillar 1), every one on the moneyline including overtime:

1. **xg_poisson** (weight 0.45): Poisson regulation goals played to 57.5 minutes (rates x 57.5 / 60) with the
   level scores inflated by ``1 + theta`` (theta = 0.60, a diagonal-inflated Poisson: score effects, the trailing
   team pushing and the leader sitting back, make ties more common than independent Poissons imply; it restores the
   NHL's ~21-23% of games reaching overtime), then the late game: with a one- or two-goal margin the trailing
   coach pulls his goalie and the leader scores into the empty net with probability 0.224 (the specification's rate) while the trailer scores 6-on-5 with
   probability 0.08 (at most one late goal); level or 3+ margins play the last 2.5 minutes at the normal rates.
   A regulation tie goes to overtime: 3-on-3 for five minutes (scoring 1.7x the regulation rate) then a shootout
   (home 51%), about 0.52 for the home side between equal teams (``ot_home_win_prob`` overrides).
2. **corsi_fenwick_possession** (0.25): shot-attempt share (CF%, or 0.4 CF% + 0.6 FF% when Fenwick is supplied)
   regressed to an expected goal share ``0.5 + 0.70 (share - 0.5)`` (shot volume persists, finishing and goaltending
   only partly), combined by log5, shifted by home ice, then a Pythagorean win probability (exponent 2.05).
3. **goal_differential_pythagorean** (0.20): each team's ``GF^2.05 / (GF^2.05 + GA^2.05)`` from
   ``goals_for_per_game`` / ``goals_against_per_game`` (else its xG for / against), log5 with a 0.535 home edge.
4. **elo** (0.10, only when both teams carry ``elo``): logistic on the gap plus 35 home Elo points.

The result's headline market is the moneyline including overtime, so ``home_win_prob + away_win_prob = 1`` and
``draw_prob = 0``; the regulation grid (with the empty-net transformation) prices the regulation 3-way
(``regulation_home`` / ``regulation_draw`` / ``regulation_away`` markets, ``regulation_draw_prob`` metric), the
puck line and totals on the final score (an overtime or shootout win adds one goal to the winner), team totals,
both teams to score, the empty-net goal market and the first two periods (31% / 34% of the scoring).

Context: the referee's penalties per game above the league's 6.6 create extra power plays worth 0.18 goals each
(split between the sides); absences cost ``WAR x 6 / 82`` goals per game (skaters off their own team, goalies onto
the opponent). Hockey is played indoors, so weather (and The Wire's friction factor) is ignored.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, override

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import ScoreGrid, SubModelEstimate, clamp, logistic, poisson_pmf
from app.domain.sports.settlement import (
    ALT_SPREAD,
    ALT_TOTAL,
    BTTS,
    MONEYLINE,
    NEXT_EVENT,
    PERIOD_MONEYLINE,
    PERIOD_SPREAD,
    PERIOD_TOTAL,
    PLAYER_PROP,
    SPREAD,
    TEAM_TOTAL,
    THREE_WAY,
    TOTAL,
    NormalizedResult,
    SettlementInputError,
    norm,
    normalize_basic,
    num,
    yes_no,
)
from app.schemas.bet_types import LegStatus

# ================================================================ shot-level xG
XG_INTERCEPT = -1.258
XG_DISTANCE = -0.0252  # per foot
XG_ANGLE = -0.010  # per degree off the centre line
XG_REBOUND = 0.75
XG_RUSH = 0.30

# ================================================================ league constants
LEAGUE_GOALS_PER_60 = 3.05  # per team, all situations
HOME_ICE_ADVANTAGE = 0.22  # goals per game, half added to the home rate and half removed from the away rate
TIE_INFLATION = 0.60  # diagonal inflation of the regulation grid (score effects)
GRID = 13  # regulation goals 0..12 a side at 57.5 minutes (one late goal can reach 13)
PULL_MINUTE = 57.5
LATE_SECONDS = 150.0  # the last 2.5 minutes of regulation
EMPTY_NET_RATE = 0.224  # P(the leader scores into the empty net | goalie pulled), the specification's rate
SIX_ON_FIVE_RATE = 0.08  # P(the trailer scores 6-on-5 | goalie pulled)
OT_SECONDS = 300.0
OT_SCORING_FACTOR = 1.7  # 3-on-3 scoring rate vs regulation (about 58% of overtimes end before the shootout)
SHOOTOUT_HOME_WIN = 0.51
POSSESSION_SLOPE = 0.70  # expected goal share = 0.5 + 0.70 (shot-attempt share - 0.5)
PYTHAG_EXPONENT = 2.05
LOG5_HOME_EDGE = 0.535
ELO_HOME_ADVANTAGE = 35.0
LEAGUE_PENALTIES_PER_GAME = 6.6
GOALS_PER_POWER_PLAY = 0.18
GOALS_PER_WAR_PER_GAME = 6.0 / 82.0
GOALIE_POSITIONS = frozenset({"g", "goalie", "goaltender", "gk"})
PERIOD_SHARES = {"p1": 0.31, "p2": 0.34}  # share of regulation scoring in the first two periods
POWER_PLAY_FACTOR = {1: 2.4, 2: 4.5}  # scoring-rate multiplier for the side with 1 / 2 extra skaters
SHORT_HANDED_FACTOR = {1: 0.35, 2: 0.25}
DEFAULT_PENALTY_SECONDS = 120.0

PUCK_LINES = (-2.5, -1.5, 1.5, 2.5)
TOTAL_LINES = (4.5, 5.0, 5.5, 6.0, 6.5, 7.5)
TEAM_TOTAL_LINES = (2.5, 3.5)
SUB_MODEL_WEIGHTS = {"xg_poisson": 0.45, "corsi_fenwick_possession": 0.25, "goal_differential_pythagorean": 0.20, "elo": 0.10}

EMPTY_NET_MARKET = "empty_net_goal"


def shot_goal_probability(distance_ft: float, angle_deg: float, *, rebound: bool = False, rush: bool = False) -> float:
    """P(goal) of one unblocked shot from the logistic xG regression."""
    z = XG_INTERCEPT + XG_DISTANCE * max(0.0, distance_ft) + XG_ANGLE * abs(angle_deg) + XG_REBOUND * float(rebound) + XG_RUSH * float(rush)
    return logistic(z)


def expected_goals_from_shots(shots: Iterable[Mapping[str, Any]]) -> float:
    """Sum of the shots' goal probabilities; each shot carries ``distance_ft`` (or ``distance``), ``angle_deg`` (or
    ``angle``), and optional ``rebound`` / ``rush`` flags."""
    total = 0.0
    for shot in shots:
        if not isinstance(shot, Mapping):
            continue
        distance = num(shot, "distance_ft", "distance", default=35.0) or 0.0
        angle = num(shot, "angle_deg", "angle", default=30.0) or 0.0
        total += shot_goal_probability(distance, angle, rebound=bool(shot.get("rebound")), rush=bool(shot.get("rush")))
    return total


# ================================================================ the late game and overtime
def late_game(matrix: np.ndarray, lam_home: float, lam_away: float, p_en: float, p_tr: float) -> tuple[np.ndarray, float]:
    """The score grid at the end of regulation from the grid when the late window opens: one- or two-goal margins
    become a pulled-goalie state (leader +1 with ``p_en``, trailer +1 with ``p_tr``, at most one goal); level and
    3+ margins play the window at the normal Poisson rates ``lam_home`` / ``lam_away``. Returns (grid, P(pull))."""
    rows, cols = matrix.shape
    out = np.zeros((rows + 5, cols + 5))
    window = np.outer(poisson_pmf(lam_home, 5), poisson_pmf(lam_away, 5))
    pulled = 0.0
    for i in range(rows):
        for j in range(cols):
            m = matrix[i, j]
            if m < 1e-15:
                continue
            margin = i - j
            if abs(margin) in (1, 2):
                pulled += m
                lead_home = margin > 0
                out[i, j] += m * (1.0 - p_en - p_tr)
                out[i + (1 if lead_home else 0), j + (0 if lead_home else 1)] += m * p_en
                out[i + (0 if lead_home else 1), j + (1 if lead_home else 0)] += m * p_tr
            else:
                out[i: i + 5, j: j + 5] += m * window
    return out, float(pulled)


def inflate_ties(matrix: np.ndarray, theta: float) -> np.ndarray:
    """Diagonal-inflated grid: level cells x (1 + theta), renormalised (theta = 0 is the independent model)."""
    out = matrix.copy()
    idx = np.arange(min(out.shape))
    out[idx, idx] *= 1.0 + max(theta, -0.9)
    return out / out.sum()


def overtime_home_win(lam_home: float, lam_away: float, seconds: float = OT_SECONDS, *, playoff: bool = False) -> float:
    """P(home wins a tied game in overtime): playoff overtime is sudden death until a goal (the rate share); regular
    season overtime is 3-on-3 for ``seconds`` then a shootout."""
    share = lam_home / max(lam_home + lam_away, 1e-9)
    if playoff:
        return share
    p_goal = 1.0 - math.exp(-(lam_home + lam_away) * OT_SCORING_FACTOR * max(0.0, seconds) / 3600.0)
    return p_goal * share + (1.0 - p_goal) * SHOOTOUT_HOME_WIN


def resolve_overtime(regulation: np.ndarray, p_ot_home: float) -> np.ndarray:
    """The final score grid: a regulation tie k-k becomes k+1 - k (home OT/shootout win) or k - k+1."""
    rows, cols = regulation.shape
    out = regulation.copy()
    out = np.pad(out, ((0, 1), (0, 1)))
    for k in range(min(rows, cols)):
        m = regulation[k, k]
        out[k, k] -= m
        out[k + 1, k] += m * p_ot_home
        out[k, k + 1] += m * (1.0 - p_ot_home)
    return out


def _pythag(gf: float, ga: float, exponent: float = PYTHAG_EXPONENT) -> float:
    gf, ga = max(gf, 0.05), max(ga, 0.05)
    return gf ** exponent / (gf ** exponent + ga ** exponent)


def _log5(p_home: float, p_away: float, home_edge: float) -> float:
    p_home, p_away = clamp(p_home, 0.01, 0.99), clamp(p_away, 0.01, 0.99)
    odds = p_home / (1.0 - p_home) * (1.0 - p_away) / p_away * home_edge / (1.0 - home_edge)
    return odds / (1.0 + odds)


def _share(value: float | None, default: float = 0.5) -> float:
    """A percentage (54.0) or a fraction (0.54) as a fraction."""
    if value is None:
        return default
    return clamp(value / 100.0 if value > 1.0 else value, 0.05, 0.95)


@dataclass(frozen=True)
class GoalRates:
    home: float  # regulation goals per 60
    away: float
    home_xgf: float  # team xG for / against per 60 before the matchup
    away_xgf: float
    home_xga: float
    away_xga: float
    metrics: dict[str, Any]


# ================================================================ the adapter
class IceHockeySportAdapter(BaseSportAdapter):
    """
    Quantitative NHL Engine implementing Expected Goals (xG), Corsi/Fenwick
    possession metrics, and empty-net goal surge probabilities in late trailing states.
    """

    kelly_multiplier = 0.30
    model_family = "xG Poisson, Corsi & Empty Net"
    has_draw = False
    odds_api_family = "icehockey"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "moneyline_incl_ot": (MONEYLINE, {}),
        "match_winner": (MONEYLINE, {}),
        "puck_line_handicap": (SPREAD, {}),
        "puck_line": (SPREAD, {}),
        "alt_puck_line": (ALT_SPREAD, {}),
        "regulation_3_way": (THREE_WAY, {}),
        "60_minute_line": (THREE_WAY, {}),
        "game_total_goals": (TOTAL, {}),
        "alt_total_goals": (ALT_TOTAL, {}),
        "team_total_goals": (TEAM_TOTAL, {}),
        "empty_net_goal_yes_no": (EMPTY_NET_MARKET, {}),
        "first_period_winner": (PERIOD_MONEYLINE, {"period": "p1"}),
        "first_period_puck_line": (PERIOD_SPREAD, {"period": "p1"}),
        "first_period_total": (PERIOD_TOTAL, {"period": "p1"}),
        "second_period_winner": (PERIOD_MONEYLINE, {"period": "p2"}),
        "second_period_total": (PERIOD_TOTAL, {"period": "p2"}),
        "third_period_winner": (PERIOD_MONEYLINE, {"period": "p3"}),
        "third_period_total": (PERIOD_TOTAL, {"period": "p3"}),
        "both_teams_to_score": (BTTS, {}),
        "next_goal": (NEXT_EVENT, {"event_type": "goal"}),
        "player_goals": (PLAYER_PROP, {"stat": "goals"}),
        "player_assists": (PLAYER_PROP, {"stat": "assists"}),
        "player_points": (PLAYER_PROP, {"stat": "points"}),
        "player_shots_on_goal": (PLAYER_PROP, {"stat": "shots_on_goal"}),
        "goalie_saves": (PLAYER_PROP, {"stat": "saves"}),
    }
    SPECIAL_MARKETS = frozenset({EMPTY_NET_MARKET})
    GENERIC_SUPPORTED = frozenset({MONEYLINE, THREE_WAY, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE,
                                   PERIOD_SPREAD, PERIOD_TOTAL, BTTS, PLAYER_PROP, NEXT_EVENT})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "round_robin", "if_bet",
                           "double_action", "futures", "player_props", "game_props", "live_next_event", "btts", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "ice_hockey"

    @property
    def display_name(self) -> str:
        return "Ice Hockey"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "moneyline_incl_ot",
            "puck_line_handicap",
            "regulation_3_way",
            "game_total_goals",
            "empty_net_goal_yes_no",
            "team_total_goals",
            "first_period_winner",
            "both_teams_to_score",
            "next_goal",
            "player_shots_on_goal",
        ]

    # ------------------------------------------------------------------ rates
    @staticmethod
    def team_xg_for(stats: Mapping[str, Any], league: float) -> float:
        """xG for per 60: ``expected_goals_per_60``, else a shot sample (``shots`` over ``games_in_sample`` games),
        else ``goals_for_per_game``, else the league rate."""
        xg = num(stats, "expected_goals_per_60", "xgf_per_60", "xg_per_60")
        if xg is not None:
            return xg
        shots = stats.get("shots")
        if isinstance(shots, list) and shots:
            games = max(1.0, num(stats, "games_in_sample", default=1.0) or 1.0)
            return expected_goals_from_shots(shots) / games
        return num(stats, "goals_for_per_game", default=league) or league

    def expected_goals(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> GoalRates:
        league = ctx.number("league_goals_per_60", LEAGUE_GOALS_PER_60)
        home_xgf, away_xgf = self.team_xg_for(home_stats, league), self.team_xg_for(away_stats, league)
        home_xga = num(home_stats, "expected_goals_against_per_60", "xga_per_60", default=league) or league
        away_xga = num(away_stats, "expected_goals_against_per_60", "xga_per_60", default=league) or league
        home = home_xgf * away_xga / league - (num(away_stats, "gsax_per_60", default=0.0) or 0.0)
        away = away_xgf * home_xga / league - (num(home_stats, "gsax_per_60", default=0.0) or 0.0)
        hia = ctx.number("home_ice_advantage", HOME_ICE_ADVANTAGE)
        home, away = home + hia / 2.0, away - hia / 2.0
        metrics: dict[str, Any] = {"home_ice_advantage": hia}

        # referee (pillar 9): extra penalties are extra power plays on both sides
        if ctx.officials is not None:
            pens = ctx.officials.rate("penalties_per_game", "penalties_per_60", "minors_per_game")
            if pens is not None:
                extra = (pens - ctx.number("league_penalties_per_game", LEAGUE_PENALTIES_PER_GAME)) * GOALS_PER_POWER_PLAY
                home, away = home + extra / 2.0, away + extra / 2.0
                metrics["referee_power_play_goal_delta"] = round(extra, 4)

        # absences (pillar 4): skaters off their own rate, goalies onto the opponent's
        for side in ("home", "away"):
            goalies = ctx.war_lost(side, GOALIE_POSITIONS)
            skaters = ctx.war_lost(side) - goalies
            own, opp = skaters * GOALS_PER_WAR_PER_GAME, goalies * GOALS_PER_WAR_PER_GAME
            if side == "home":
                home, away = home - own, away + opp
            else:
                away, home = away - own, home + opp
            if ctx.absences:
                metrics[f"{side}_absence_goals_lost"] = round(own, 4)
                metrics[f"{side}_absence_goals_conceded"] = round(opp, 4)
        metrics["weather_applied"] = False  # indoor sport
        return GoalRates(max(0.5, home), max(0.5, away), home_xgf, away_xgf, home_xga, away_xga, metrics)

    # ------------------------------------------------------------------ the regulation grid
    def regulation_grid(self, lam_home: float, lam_away: float, theta: float = TIE_INFLATION) -> tuple[ScoreGrid, float]:
        """Regulation goals: tie-inflated Poisson to 57.5 minutes, then the late-game (empty-net) window.
        Returns (grid, P(pull))."""
        early = PULL_MINUTE / 60.0
        base = inflate_ties(np.outer(poisson_pmf(lam_home * early, GRID), poisson_pmf(lam_away * early, GRID)), theta)
        window = LATE_SECONDS / 3600.0
        matrix, pulled = late_game(base, lam_home * window, lam_away * window, EMPTY_NET_RATE, SIX_ON_FIVE_RATE)
        return ScoreGrid.normalised(matrix), pulled

    def ot_probability(self, rates: GoalRates, ctx: MatchContext) -> float:
        override = num(ctx.raw, "ot_home_win_prob")
        if override is not None:
            return clamp(override, 0.01, 0.99)
        return overtime_home_win(rates.home, rates.away, playoff=bool(ctx.get("playoff", False)))

    # ------------------------------------------------------------------ sub-models
    def possession(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], rates: GoalRates,
                   ctx: MatchContext) -> tuple[SubModelEstimate, dict[str, float]]:
        def attempt_share(stats: Mapping[str, Any]) -> float:
            cf = _share(num(stats, "corsi_for_pct", "cf_pct"))
            ff = num(stats, "fenwick_for_pct", "ff_pct")
            return cf if ff is None else 0.4 * cf + 0.6 * _share(ff)

        sh, sa = attempt_share(home_stats), attempt_share(away_stats)
        gh, ga = 0.5 + POSSESSION_SLOPE * (sh - 0.5), 0.5 + POSSESSION_SLOPE * (sa - 0.5)
        odds = gh / (1.0 - gh) * (1.0 - ga) / ga
        share = odds / (1.0 + odds)
        total = rates.home + rates.away
        hia = ctx.number("home_ice_advantage", HOME_ICE_ADVANTAGE)
        share = clamp(share + (hia / 2.0) / total, 0.05, 0.95)
        p = _pythag(share, 1.0 - share)
        est = SubModelEstimate("corsi_fenwick_possession", p, 1.0 - p, 0.0, total, SUB_MODEL_WEIGHTS["corsi_fenwick_possession"])
        return est, {"home_attempt_share": round(sh, 4), "away_attempt_share": round(sa, 4), "home_expected_goal_share": round(share, 4)}

    def goal_differential(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], rates: GoalRates, ctx: MatchContext) -> SubModelEstimate:
        def team(stats: Mapping[str, Any], xgf: float, xga: float) -> tuple[float, float]:
            gf, ga = num(stats, "goals_for_per_game"), num(stats, "goals_against_per_game")
            return (xgf, xga) if gf is None or ga is None else (gf, ga)

        gf_h, ga_h = team(home_stats, rates.home_xgf, rates.home_xga)
        gf_a, ga_a = team(away_stats, rates.away_xgf, rates.away_xga)
        p = _log5(_pythag(gf_h, ga_h), _pythag(gf_a, ga_a), ctx.number("log5_home_edge", LOG5_HOME_EDGE))
        return SubModelEstimate("goal_differential_pythagorean", p, 1.0 - p, 0.0, (gf_h + ga_a + gf_a + ga_h) / 2.0,
                                SUB_MODEL_WEIGHTS["goal_differential_pythagorean"])

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        rates = self.expected_goals(home_stats, away_stats, ctx)
        theta = ctx.number("tie_inflation", TIE_INFLATION)
        regulation, pulled = self.regulation_grid(rates.home, rates.away, theta)
        p_ot = self.ot_probability(rates, ctx)
        final = ScoreGrid.normalised(resolve_overtime(regulation.matrix, p_ot))
        reg_home, reg_draw, reg_away = regulation.outcome()
        ml_home = reg_home + reg_draw * p_ot

        estimates = [SubModelEstimate("xg_poisson", ml_home, 1.0 - ml_home, 0.0, final.total_pmf().mean, SUB_MODEL_WEIGHTS["xg_poisson"])]
        possession, possession_metrics = self.possession(home_stats, away_stats, rates, ctx)
        estimates += [possession, self.goal_differential(home_stats, away_stats, rates, ctx)]
        h_elo, a_elo = num(home_stats, "elo"), num(away_stats, "elo")
        if h_elo is not None and a_elo is not None:
            p = logistic((h_elo - a_elo + ctx.number("elo_home_advantage", ELO_HOME_ADVANTAGE)) * math.log(10.0) / 400.0)
            estimates.append(SubModelEstimate("elo", p, 1.0 - p, 0.0, None, SUB_MODEL_WEIGHTS["elo"]))
        total_w = sum(e.weight for e in estimates)
        estimates = [SubModelEstimate(e.name, e.home_win_prob, e.away_win_prob, e.draw_prob, e.expected_total, e.weight / total_w)
                     for e in estimates]
        consensus = self._consensus(estimates, ctx)

        markets = self.price_markets(regulation, final, rates, pulled)
        metrics: dict[str, Any] = {
            **rates.metrics,
            **possession_metrics,
            "projected_home_xg": round(rates.home, 3),
            "projected_away_xg": round(rates.away, 3),
            "regulation_home_prob": round(reg_home, 4),
            "regulation_draw_prob": round(reg_draw, 4),
            "regulation_away_prob": round(reg_away, 4),
            "ot_home_win_prob": round(p_ot, 4),
            "goalie_pull_probability": round(pulled, 4),
            "tie_inflation_theta": theta,
            "empty_net_probability": EMPTY_NET_RATE,
            "six_on_five_goal_probability": SIX_ON_FIVE_RATE,
            "empty_net_goal_yes": round(pulled * EMPTY_NET_RATE, 4),
            "top_correct_scores": [[s, round(p, 4)] for s, p in final.correct_scores(6)],
        }
        # the headline market is the moneyline including overtime: a coherent 2-way partition (draw_prob = 0); the
        # regulation 3-way lives in the regulation_* markets and the regulation_draw_prob metric
        return self._assemble(consensus, ctx, expected_total=final.total_pmf().mean, metrics=metrics, markets=markets)

    def price_markets(self, regulation: ScoreGrid, final: ScoreGrid, rates: GoalRates, pulled: float) -> dict[str, float]:
        markets: dict[str, float] = {}
        rh, rd, ra = regulation.outcome()
        markets["regulation_home"], markets["regulation_draw"], markets["regulation_away"] = rh, rd, ra
        markets["overtime_yes"], markets["overtime_no"] = rd, 1.0 - rd
        fh, _, fa = final.outcome()
        markets["moneyline_home"], markets["moneyline_away"] = fh, fa
        margin, total = final.margin_pmf(), final.total_pmf()
        for line in PUCK_LINES:
            markets[f"puck_line_home_{line:+g}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"puck_line_away_{line:+g}"] = margin.negate().line_outcome(-line, over=True).effective_probability
        for line in TOTAL_LINES:
            markets[f"total_over_{line:g}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"total_under_{line:g}"] = total.line_outcome(line, over=False).effective_probability
        home_pmf, away_pmf = final.home_pmf(), final.away_pmf()
        for line in TEAM_TOTAL_LINES:
            markets[f"home_team_total_over_{line:g}"] = home_pmf.line_outcome(line, over=True).effective_probability
            markets[f"away_team_total_over_{line:g}"] = away_pmf.line_outcome(line, over=True).effective_probability
        btts = final.btts()
        markets["btts_yes"], markets["btts_no"] = btts, 1.0 - btts
        en = pulled * EMPTY_NET_RATE
        markets["empty_net_goal_yes"], markets["empty_net_goal_no"] = en, 1.0 - en
        for period, share in PERIOD_SHARES.items():
            grid = ScoreGrid.independent(poisson_pmf(rates.home * share, GRID), poisson_pmf(rates.away * share, GRID))
            ph, pd, pa = grid.outcome()
            markets[f"{period}_home"], markets[f"{period}_draw"], markets[f"{period}_away"] = ph, pd, pa
            for line in (0.5, 1.5, 2.5):
                markets[f"{period}_total_over_{line:g}"] = grid.total_pmf().prob_gt(line)
        # first goal of the game: the rate share of P(a regulation goal), a 0-0 regulation goes to the overtime split
        rate_total = rates.home + rates.away
        p_goal = 1.0 - math.exp(-rate_total)
        markets["first_goal_home"] = p_goal * rates.home / rate_total
        markets["first_goal_away"] = p_goal * rates.away / rate_total
        markets["first_goal_none_in_regulation"] = 1.0 - p_goal
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability (moneyline including overtime). ``live_state``: ``period`` (1-3, 4 = overtime),
        ``seconds_remaining_in_period``, ``home_score`` / ``away_score``, ``home_skaters`` / ``away_skaters`` (a power
        play; ``penalty_seconds_remaining`` defaults to 120), ``goalie_pulled`` ("home" / "away" / None), optional
        ``status`` ("final"). The rest of regulation is Poisson with the power-play rates, the last 2.5 minutes follow
        the empty-net logic (a goalie already pulled with <= 5 minutes left starts it now), a tie goes to overtime."""
        ctx = MatchContext.parse(context)
        rates = self.expected_goals(home_stats, away_stats, ctx)
        period = max(1, int(num(live_state, "period", default=1.0) or 1))
        seconds = clamp(num(live_state, "seconds_remaining_in_period", "seconds_remaining", default=1200.0) or 0.0, 0.0, 1200.0)
        hs = int(num(live_state, "home_score", "home_goals", default=0.0) or 0)
        as_ = int(num(live_state, "away_score", "away_goals", default=0.0) or 0)
        playoff = bool(ctx.get("playoff", False))
        detail: dict[str, Any] = {"period": period, "seconds_remaining_in_period": seconds, "score": f"{hs}-{as_}"}
        finished = norm(live_state.get("status")) in ("final", "finished", "ended", "complete", "completed")
        finished = finished or (period >= 4 and hs != as_) or (period == 3 and seconds <= 0 and hs != as_)
        if finished:
            if hs == as_:
                return LiveWinProbability(0.5, 0.5, 0.0, {**detail, "final": True})
            won = hs > as_
            return LiveWinProbability(1.0 if won else 0.0, 0.0 if won else 1.0, 0.0, {**detail, "final": True})

        if period >= 4:  # tied in overtime
            ot_left = OT_SECONDS if playoff else seconds
            p = overtime_home_win(rates.home, rates.away, ot_left, playoff=playoff)
            override = num(ctx.raw, "ot_home_win_prob")
            if override is not None and seconds >= OT_SECONDS:
                p = clamp(override, 0.01, 0.99)
            return LiveWinProbability(p, 1.0 - p, 0.0, {**detail, "overtime": True})

        remaining = (3 - period) * 1200.0 + seconds
        pulled_now = live_state.get("goalie_pulled") in ("home", "away") and remaining <= 2.0 * LATE_SECONDS
        late = remaining if pulled_now else min(remaining, LATE_SECONDS)
        normal = remaining - late
        lam_h, lam_a = self._live_rates(rates, live_state, normal)
        size = max(hs, as_) + GRID
        base = np.zeros((size, size))
        base[hs:, as_:] = np.outer(poisson_pmf(lam_h, size - hs), poisson_pmf(lam_a, size - as_))
        base = inflate_ties(base, ctx.number("tie_inflation", TIE_INFLATION) * remaining / 3600.0)
        scale = late / LATE_SECONDS
        en, tr = EMPTY_NET_RATE * scale, SIX_ON_FIVE_RATE * scale
        regulation, pulled = late_game(base, rates.home * late / 3600.0, rates.away * late / 3600.0, en, tr)
        grid = ScoreGrid.normalised(regulation)
        reg_home, reg_draw, _ = grid.outcome()
        p_ot = self.ot_probability(rates, ctx)
        home = reg_home + reg_draw * p_ot
        detail.update({"remaining_regulation_seconds": remaining, "remaining_home_goals": round(lam_h + rates.home * late / 3600.0, 3),
                       "remaining_away_goals": round(lam_a + rates.away * late / 3600.0, 3), "regulation_draw_prob": round(reg_draw, 4),
                       "goalie_pull_probability": round(pulled, 4), "ot_home_win_prob": round(p_ot, 4)})
        return LiveWinProbability(home, 1.0 - home, 0.0, detail)

    @staticmethod
    def _live_rates(rates: GoalRates, live_state: Mapping[str, Any], normal_seconds: float) -> tuple[float, float]:
        """Expected goals over the normal-play segment, with the current power play at its boosted rates."""
        diff = int(num(live_state, "home_skaters", default=5.0) or 5) - int(num(live_state, "away_skaters", default=5.0) or 5)
        diff = int(clamp(diff, -2, 2))
        pp = min(normal_seconds, num(live_state, "penalty_seconds_remaining", default=DEFAULT_PENALTY_SECONDS) or 0.0) if diff else 0.0
        if diff > 0:
            fh, fa = POWER_PLAY_FACTOR[diff], SHORT_HANDED_FACTOR[diff]
        elif diff < 0:
            fh, fa = SHORT_HANDED_FACTOR[-diff], POWER_PLAY_FACTOR[-diff]
        else:
            fh = fa = 1.0
        lam_h = rates.home / 3600.0 * ((normal_seconds - pp) + pp * fh)
        lam_a = rates.away / 3600.0 * ((normal_seconds - pp) + pp * fa)
        return lam_h, lam_a

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """Accepts ``home_score`` / ``away_score`` (or ``home_goals`` / ``away_goals``), ``reg_home_score`` /
        ``reg_away_score``, ``period_scores`` ([[home, away] per period]) or ``periods`` {"p1": ...}, ``decided_in``
        ("regulation" / "ot" / "so"), ``empty_net_goal_scored`` (bool) or ``empty_net_goals`` (count), and events
        (``{"type": "goal", "team": "home", "empty_net": true, "t": 3540}``)."""
        res = normalize_basic(match_result, home_keys=("home_score", "home_goals"), away_keys=("away_score", "away_goals"))
        period_scores = match_result.get("period_scores")
        if isinstance(period_scores, (list, tuple)):
            for k, pair in enumerate(period_scores, start=1):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    h, a = num({"v": pair[0]}, "v"), num({"v": pair[1]}, "v")
                    if h is not None and a is not None:
                        res.periods.setdefault(f"p{k}", (h, a))
        if res.regulation_home is None or res.regulation_away is None:
            self._regulation_from(res, norm(match_result.get("decided_in") or match_result.get("ended_in")))
        flag = match_result.get("empty_net_goal_scored")
        count = num(match_result, "empty_net_goals")
        goals = [e for e in res.events if norm(e.get("type")) == "goal"]
        if isinstance(flag, bool):
            res.extras["empty_net_goal"] = flag
        elif count is not None:
            res.extras["empty_net_goal"] = count > 0
        elif goals:
            res.extras["empty_net_goal"] = any(bool(e.get("empty_net")) for e in goals)
        return res

    @staticmethod
    def _regulation_from(res: NormalizedResult, decided_in: str) -> None:
        if all(p in res.periods for p in ("p1", "p2", "p3")):
            res.regulation_home = sum(res.periods[p][0] for p in ("p1", "p2", "p3"))
            res.regulation_away = sum(res.periods[p][1] for p in ("p1", "p2", "p3"))
        elif res.home is not None and res.away is not None and decided_in:
            if decided_in in ("ot", "so", "overtime", "shootout"):
                level = min(res.home, res.away)
                res.regulation_home = res.regulation_away = level
            elif decided_in in ("regulation", "reg", "60"):
                res.regulation_home, res.regulation_away = res.home, res.away

    @override
    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == EMPTY_NET_MARKET:
            scored = result.extras.get("empty_net_goal")
            if scored is None:
                raise SettlementInputError("the result does not say whether an empty-net goal was scored")
            return LegStatus.WON if bool(scored) == yes_no(selection) else LegStatus.LOST
        raise SettlementInputError(f"ice hockey has no market {market!r}")
