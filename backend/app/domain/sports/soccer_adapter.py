"""
Soccer Quantitative Adapter: Dixon-Coles Bivariate Poisson & Low-Score Correlation (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Four independent sub-models vote (fortress pillar 1):

1. **Dixon-Coles** (weight 0.40): independent Poissons ``lambda`` (home) and ``mu`` (away) with the low-score
   correction ``tau``: ``tau(0,0) = 1 - lambda mu rho``, ``tau(1,0) = 1 + mu rho``, ``tau(0,1) = 1 + lambda rho``,
   ``tau(1,1) = 1 - rho``, 1 elsewhere. ``lambda = att_home x def_away x home_advantage x league_rate``.
2. **Karlis-Ntzoufras bivariate Poisson** (0.20): ``X = A + C, Y = B + C`` with a shared component C
   (covariance ``lambda_3``), which lifts the draw the independent model under-prices.
3. **xG Poisson** (0.25): independent Poissons on expected goals for/against per match (shrunk 30% toward the
   league rate) when the request carries xG, else on the ratings shrunk the same way.
4. **Elo** (0.15): a logistic on the rating gap with a Davidson draw term, ``P(draw) = nu sqrt(p_h p_a)``.

The three scoreline models are mixed into one grid; every line market (Asian handicap on the quarter ball,
totals, team totals, BTTS, clean sheet, draw no bet, double chance, correct score, first-half lines) is
priced from that grid with pushes and half results handled exactly.

Context: absences cost ``WAR x 1.6 / 38`` goals of difference per match (attackers off their own rate,
defenders onto the opponent's), the referee's penalty rate above the league's 0.30 per match adds
``0.76`` goals per extra penalty (55% to the home side), and the weather scales scoring (The Wire's friction
factor when present).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, ClassVar

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext, Weather
from app.domain.sports.quant import ScoreGrid, SubModelEstimate, clamp, logistic, poisson_pmf
from app.domain.sports.settlement import (
    ALT_SPREAD,
    ALT_TOTAL,
    ASIAN_HANDICAP,
    BTTS,
    CLEAN_SHEET,
    CORRECT_SCORE,
    DOUBLE_CHANCE,
    DRAW_NO_BET,
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
    normalize_basic,
    num,
)

GRID = 11  # 0..10 goals a side; the tail is folded into the last cell
GOALS_PER_WAR_PER_MATCH = 1.6 / 38.0
LEAGUE_PENALTIES_PER_MATCH = 0.30
PENALTY_CONVERSION = 0.76
ATTACK_POSITIONS = frozenset({"fw", "forward", "striker", "st", "cf", "winger", "lw", "rw", "am", "cam", "mf", "midfielder", "cm"})
DEFENCE_POSITIONS = frozenset({"df", "defender", "cb", "lb", "rb", "fb", "wb", "gk", "goalkeeper", "dm", "cdm"})
FIRST_HALF_SHARE = 0.45  # share of goals scored before half-time
AH_LINES = (-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
TOTAL_LINES = (0.5, 1.5, 2.25, 2.5, 2.75, 3.5, 4.5)


def dixon_coles_tau(x: int, y: int, lambda_: float, mu: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1.0 - lambda_ * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lambda_ * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def dixon_coles_grid(lambda_: float, mu: float, rho: float) -> ScoreGrid:
    grid = np.outer(poisson_pmf(lambda_, GRID), poisson_pmf(mu, GRID))
    for x in (0, 1):
        for y in (0, 1):
            grid[x, y] *= max(0.0, dixon_coles_tau(x, y, lambda_, mu, rho))
    return ScoreGrid.normalised(grid)


def bivariate_poisson_grid(lambda_: float, mu: float, lambda_3: float) -> ScoreGrid:
    """Karlis-Ntzoufras: X = A + C, Y = B + C with A ~ Pois(lambda - l3), B ~ Pois(mu - l3), C ~ Pois(l3)."""
    l3 = clamp(lambda_3, 0.0, 0.9 * min(lambda_, mu))
    a, b, c = poisson_pmf(lambda_ - l3, GRID), poisson_pmf(mu - l3, GRID), poisson_pmf(l3, GRID)
    grid = np.zeros((GRID, GRID))
    for k in range(GRID):
        if c[k] < 1e-14:
            continue
        sub = np.outer(a[: GRID - k], b[: GRID - k]) * c[k]
        grid[k:, k:] += sub
    return ScoreGrid.normalised(grid)


class SoccerSportAdapter(BaseSportAdapter):
    """
    Quantitative Soccer Engine implementing Dixon-Coles bivariate Poisson
    with low-score inflation parameter rho, home advantage, clean sheet, and BTTS.
    """

    kelly_multiplier: ClassVar[float] = 0.35
    model_family: ClassVar[str] = "Dixon-Coles bivariate Poisson"
    has_draw: ClassVar[bool] = True
    odds_api_family: ClassVar[str] = "soccer"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "1x2_match_odds": (MONEYLINE, {}),
        "match_winner": (MONEYLINE, {}),
        "match_odds": (MONEYLINE, {}),
        "over_under_2_5_goals": (TOTAL, {"line": 2.5}),
        "over_under_goals": (TOTAL, {}),
        "both_teams_to_score": (BTTS, {}),
        "asian_handicap": (ASIAN_HANDICAP, {}),
        "asian_total": (TOTAL, {}),
        "correct_score": (CORRECT_SCORE, {}),
        "clean_sheet": (CLEAN_SHEET, {}),
        "draw_no_bet": (DRAW_NO_BET, {}),
        "double_chance": (DOUBLE_CHANCE, {}),
        "team_total_goals": (TEAM_TOTAL, {}),
        "first_half_result": (PERIOD_MONEYLINE, {"period": "h1"}),
        "first_half_handicap": (PERIOD_SPREAD, {"period": "h1"}),
        "first_half_total": (PERIOD_TOTAL, {"period": "h1"}),
        "next_goal": (NEXT_EVENT, {"event_type": "goal"}),
        "player_goals": (PLAYER_PROP, {"stat": "goals"}),
        "player_shots_on_target": (PLAYER_PROP, {"stat": "shots_on_target"}),
    }
    GENERIC_SUPPORTED: ClassVar[frozenset[str]] = frozenset({MONEYLINE, SPREAD, ASIAN_HANDICAP, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE,
                                   PERIOD_SPREAD, PERIOD_TOTAL, DRAW_NO_BET, BTTS, CORRECT_SCORE, CLEAN_SHEET, DOUBLE_CHANCE,
                                   PLAYER_PROP, NEXT_EVENT})
    BET_TYPES: ClassVar[frozenset[str]] = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "round_robin", "if_bet",
                           "double_action", "futures", "player_props", "live_next_event", "draw_no_bet", "btts", "correct_score",
                           "asian_handicap", "clean_sheet", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "soccer"

    @property
    def display_name(self) -> str:
        return "Football (Soccer)"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "1x2_match_odds",
            "over_under_2_5_goals",
            "both_teams_to_score",
            "asian_handicap",
            "correct_score",
            "draw_no_bet",
            "clean_sheet",
            "double_chance",
            "first_half_result",
            "next_goal",
        ]

    def _tau(self, x: int, y: int, lambda_: float, mu: float, rho: float) -> float:
        """Dixon-Coles low-score adjustment factor."""
        return dixon_coles_tau(x, y, lambda_, mu, rho)

    # ------------------------------------------------------------------ rates
    def expected_goals(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> tuple[float, float, dict[str, Any]]:
        home_adv = ctx.number("home_advantage", 1.25)
        league_rate = ctx.number("league_goal_rate", 1.0)
        home_att = float(home_stats.get("attack_rating", 1.35))
        home_def = float(home_stats.get("defense_rating", 1.10))
        away_att = float(away_stats.get("attack_rating", 1.15))
        away_def = float(away_stats.get("defense_rating", 1.20))
        lambda_ = home_att * away_def * home_adv * league_rate
        mu = away_att * home_def * league_rate
        adjustments: dict[str, Any] = {}

        # absences (lineups pillar): WAR -> goal difference per match
        for side in ("home", "away"):
            attack = sum(a.war * a.weight for a in ctx.absences if a.side == side and (a.position or "") in ATTACK_POSITIONS)
            defence = sum(a.war * a.weight for a in ctx.absences if a.side == side and (a.position or "") in DEFENCE_POSITIONS)
            other = ctx.war_lost(side) - attack - defence
            own_loss = (attack + 0.5 * other) * GOALS_PER_WAR_PER_MATCH
            opp_gain = (defence + 0.5 * other) * GOALS_PER_WAR_PER_MATCH
            if side == "home":
                lambda_ -= own_loss
                mu += opp_gain
            else:
                mu -= own_loss
                lambda_ += opp_gain
            adjustments[f"{side}_absence_goal_delta"] = round(-(own_loss + opp_gain), 4)

        # referee (pillar 9): penalties above the league rate add converted penalty goals
        if ctx.officials is not None:
            pens = ctx.officials.rate("penalties_per_match", "penalties_per_90", "penalties_per_game")
            if pens is not None:
                extra = (pens - LEAGUE_PENALTIES_PER_MATCH) * PENALTY_CONVERSION
                lambda_ += 0.55 * extra
                mu += 0.45 * extra
                adjustments["referee_penalty_goal_delta"] = round(extra, 4)

        # weather (pillar 2): scoring multiplier
        mult = self.weather_multiplier(ctx.weather)
        lambda_, mu = lambda_ * mult, mu * mult
        adjustments["weather_scoring_multiplier"] = round(mult, 4)
        return max(0.15, lambda_), max(0.15, mu), adjustments

    @staticmethod
    def weather_multiplier(weather: Weather | None) -> float:
        if weather is None or weather.indoor:
            return 1.0
        if weather.friction is not None:
            return clamp(weather.friction, 0.6, 1.2)
        mult = 1.0
        if weather.wind_kph is not None and weather.wind_kph > 30.0:
            mult *= max(0.88, 1.0 - 0.004 * (weather.wind_kph - 30.0))
        if weather.precipitation_mm is not None and weather.precipitation_mm > 2.0:
            mult *= 0.95
        if weather.temperature_c is not None and weather.temperature_c > 30.0:
            mult *= 0.97
        if weather.altitude_m is not None and weather.altitude_m > 1500.0:
            mult *= 1.0 + min(0.06, 0.02 * (weather.altitude_m - 1500.0) / 1000.0)
        return mult

    # ------------------------------------------------------------------ the sub-models
    def _xg_rates(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], lambda_: float, mu: float, ctx: MatchContext) -> tuple[float, float]:
        league = ctx.number("league_xg_per_team", 1.35)
        h_for, h_against = num(home_stats, "xg_for"), num(home_stats, "xg_against")
        a_for, a_against = num(away_stats, "xg_for"), num(away_stats, "xg_against")
        if None not in (h_for, h_against, a_for, a_against):
            raw_h = h_for * a_against / league * ctx.number("home_advantage", 1.25) ** 0.5  # type: ignore[operator]
            raw_a = a_for * h_against / league / ctx.number("home_advantage", 1.25) ** 0.5  # type: ignore[operator]
        else:
            raw_h, raw_a = lambda_, mu
        mean_rate = (lambda_ + mu) / 2.0
        return 0.7 * raw_h + 0.3 * mean_rate, 0.7 * raw_a + 0.3 * mean_rate

    def _elo(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], lambda_: float, mu: float, ctx: MatchContext) -> tuple[float, float, float]:
        h_elo, a_elo = num(home_stats, "elo"), num(away_stats, "elo")
        if h_elo is not None and a_elo is not None:
            diff = h_elo - a_elo + ctx.number("elo_home_advantage", 65.0)
        else:
            diff = 400.0 * math.log10(max(lambda_, 0.05) / max(mu, 0.05))
        expect = logistic(diff * math.log(10.0) / 400.0)
        nu = ctx.number("davidson_draw", 0.54)
        p_home, p_away = expect, 1.0 - expect
        draw = nu * math.sqrt(p_home * p_away)
        scale = (1.0 - draw)
        return p_home * scale, draw, p_away * scale

    def model(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
              ctx: MatchContext) -> tuple[list[SubModelEstimate], ScoreGrid, dict[str, Any]]:
        lambda_, mu, adjustments = self.expected_goals(home_stats, away_stats, ctx)
        rho = clamp(ctx.number("rho", -0.08), -0.25, 0.25)
        lambda_3 = ctx.number("covariance", 0.10)
        dc = dixon_coles_grid(lambda_, mu, rho)
        bp = bivariate_poisson_grid(lambda_, mu, lambda_3)
        xg_h, xg_a = self._xg_rates(home_stats, away_stats, lambda_, mu, ctx)
        xg = ScoreGrid.independent(poisson_pmf(xg_h, GRID), poisson_pmf(xg_a, GRID))
        elo_home, elo_draw, elo_away = self._elo(home_stats, away_stats, lambda_, mu, ctx)

        def est(name: str, grid: ScoreGrid, weight: float) -> SubModelEstimate:
            h, d, a = grid.outcome()
            return SubModelEstimate(name, h, a, d, grid.total_pmf().mean, weight)

        estimates = [est("dixon_coles", dc, 0.40), est("bivariate_poisson", bp, 0.20), est("xg_poisson", xg, 0.25),
                     SubModelEstimate("elo_davidson", elo_home, elo_away, elo_draw, None, 0.15)]
        w = {e.name: max(0.0, float(ctx.model_weights.get(e.name, e.weight))) for e in estimates}
        grid_w = [w["dixon_coles"], w["bivariate_poisson"], w["xg_poisson"]]
        if sum(grid_w) <= 0:
            grid_w = [1.0, 0.0, 0.0]
        mixed = ScoreGrid.normalised((grid_w[0] * dc.matrix + grid_w[1] * bp.matrix + grid_w[2] * xg.matrix) / sum(grid_w))
        metrics = {"home_xg": round(lambda_, 3), "away_xg": round(mu, 3), "dixon_coles_rho": rho, "bivariate_covariance": lambda_3,
                   "xg_model_rates": [round(xg_h, 3), round(xg_a, 3)], **adjustments}
        return estimates, mixed, metrics

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        estimates, grid, metrics = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(estimates, ctx)
        markets = self.price_markets(grid, metrics["home_xg"], metrics["away_xg"], float(ctx.number("rho", -0.08)))
        metrics["top_correct_scores"] = [[s, round(p, 4)] for s, p in grid.correct_scores(6)]
        return self._assemble(consensus, ctx, expected_total=grid.total_pmf().mean, metrics=metrics, markets=markets)

    def price_markets(self, grid: ScoreGrid, lambda_: float, mu: float, rho: float) -> dict[str, float]:
        markets: dict[str, float] = {}
        margin, total = grid.margin_pmf(), grid.total_pmf()
        home_pmf, away_pmf = grid.home_pmf(), grid.away_pmf()
        for line in TOTAL_LINES:
            markets[f"over_{line:g}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"under_{line:g}"] = total.line_outcome(line, over=False).effective_probability
        for line in AH_LINES:
            markets[f"asian_handicap_home_{line:+g}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"asian_handicap_away_{line:+g}"] = margin.negate().line_outcome(-line, over=True).effective_probability
        btts = grid.btts()
        markets["btts_yes"], markets["btts_no"] = btts, 1.0 - btts
        markets["clean_sheet_home"] = grid.clean_sheet("home")
        markets["clean_sheet_away"] = grid.clean_sheet("away")
        h, d, a = grid.outcome()
        markets["draw_no_bet_home"] = h / (h + a)
        markets["draw_no_bet_away"] = a / (h + a)
        markets["double_chance_1x"], markets["double_chance_12"], markets["double_chance_x2"] = h + d, h + a, d + a
        for line in (0.5, 1.5, 2.5):
            markets[f"home_team_over_{line:g}"] = home_pmf.prob_gt(line)
            markets[f"away_team_over_{line:g}"] = away_pmf.prob_gt(line)
        for score, p in grid.correct_scores(12):
            markets[f"correct_score_{score}"] = p
        half = dixon_coles_grid(lambda_ * FIRST_HALF_SHARE, mu * FIRST_HALF_SHARE, rho)
        hh, hd, ha = half.outcome()
        markets["first_half_home"], markets["first_half_draw"], markets["first_half_away"] = hh, hd, ha
        markets["first_half_over_0.5"] = half.total_pmf().prob_gt(0.5)
        markets["first_half_over_1.5"] = half.total_pmf().prob_gt(1.5)
        return markets

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        ctx = MatchContext.parse(context)
        lambda_, mu, _ = self.expected_goals(home_stats, away_stats, ctx)
        minute = clamp(float(live_state.get("minute", 0.0)), 0.0, 120.0)
        length = 90.0 + float(live_state.get("stoppage_minutes", 4.0))
        remaining = max(0.0, length - minute) / length
        # scoring intensity rises through the match (about 55% of goals come after half-time)
        late_boost = 1.0 + 0.2 * (minute / length)
        h_red, a_red = int(live_state.get("home_red_cards", 0)), int(live_state.get("away_red_cards", 0))
        lam_rem = lambda_ * remaining * late_boost * (0.67 ** h_red) * (1.25 ** a_red)
        mu_rem = mu * remaining * late_boost * (0.67 ** a_red) * (1.25 ** h_red)
        hg, ag = int(live_state.get("home_goals", 0)), int(live_state.get("away_goals", 0))
        rest = ScoreGrid.independent(poisson_pmf(lam_rem, GRID), poisson_pmf(mu_rem, GRID))
        margin = rest.margin_pmf().shift(hg - ag)
        home = margin.prob_gt(0.0)
        draw = margin.prob_eq(0)
        away = margin.prob_lt(0.0)
        return LiveWinProbability(home, away, draw, {"minute": minute, "remaining_home_xg": round(lam_rem, 3),
                                                     "remaining_away_xg": round(mu_rem, 3), "score": f"{hg}-{ag}"})

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        res = normalize_basic(match_result, home_keys=("home_goals", "home_score"), away_keys=("away_goals", "away_score"))
        h1h, h1a = num(match_result, "ht_home_goals", "h1_home_goals"), num(match_result, "ht_away_goals", "h1_away_goals")
        if h1h is not None and h1a is not None:
            res.periods.setdefault("h1", (h1h, h1a))
        return res
