"""
Basketball Quantitative Adapter: Dean Oliver Four Factors, Pace & Morey-Pythagorean Expectation (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Three (four with Elo) independent sub-models vote (fortress pillar 1), each ending in a margin distribution:

1. **Four Factors efficiency** (weight 0.40): Dean Oliver's factors from the box score when present,
   ``eFG = (FGM + 0.5 3PM) / FGA``, ``TOV% = TOV / (FGA + 0.44 FTA + TOV)``, ``ORB% = ORB / (ORB + OppDRB)``,
   ``FTR = FTM / FGA`` (else the ``efg_pct`` / ``tov_pct`` / ``orb_pct`` / ``ftr`` fields). The matchup factor is
   ``offence + opponent's factor allowed - league`` (league eFG 0.54, TOV 0.13, ORB 0.25, FTR 0.20), each
   differential is standardised by its team-season spread (0.020 / 0.010 / 0.025 / 0.030), weighted
   40 / 25 / 20 / 15 (turnovers negative) and converted at ``9.0`` points per 100 possessions per weighted sigma:
   ``ORtg = 114 + 9.0 x sum_f w_f s_f (d_f / sd_f)``. Margin ~ Normal(mu, 11.5).
2. **Morey-Pythagorean** (0.25): season win expectancy ``PF^13.91 / (PF^13.91 + PA^13.91)`` (points per game, else
   offensive / defensive ratings), a log5 matchup ``p_h (1 - p_a) / (p_h (1 - p_a) + p_a (1 - p_h))``, mapped to a
   neutral margin ``11.5 x Phi^-1(p)`` plus the home edge.
3. **Net rating / SRS** (0.35): ``ORtg_adj = ORtg + (opponent DRtg - 114)`` per side, margin
   ``Pace / 100 x (ORtg_home_adj - ORtg_away_adj)`` (``srs`` when both teams carry it).
4. **Elo** (0.15, only when both teams carry ``elo``): ``(Elo_h - Elo_a) / 28`` points plus the home edge.

Pace: ``Poss = FGA + 0.44 FTA - ORB + TOV`` and ``Pace = 48 (Poss_team + Poss_opp) / (2 Minutes / 5)`` from the
box score (``pace`` otherwise); the game pace is ``Pace_h x Pace_a / League pace`` (99.5).
``E[total] = Pace / 100 x (ORtg_home_adj + ORtg_away_adj)``.

A regulation tie goes to overtime: its mass is redistributed over a five-minute overtime margin
(``Normal(mu 5/48, 11.5 sqrt(5/48))``, a further tie folded into repeat overtimes), so spreads, totals and the
moneyline settle on the final score including overtime, as the books do. Totals ~ Normal(E, 18) with the overtime
scoring mixed in; team totals use ``sd = sqrt((18^2 + 11.5^2) / 4)``. Halves and quarters scale the mean by their
share (0.5 / 0.25) and the spread by ``sqrt(share)``; a quarter can tie (2-way quarter lines void on a tie).

Home edge: Ball Arena, Denver (5280 ft) +2.8 points, Delta Center, Utah (4226 ft) +2.1, else
``home_advantage_points`` (league standard 2.2; 0 at a neutral site).

Context: absences cost ``WAR / 2.7`` points of margin per game (1 point of season margin ~ 2.7 wins), split
evenly between the side's own scoring and the opponent's; absent players' usage additionally lowers the side's
offensive rating by 0.04 points per 100 possessions per usage point (the usage-efficiency trade-off of
redistributing their possessions). The crew's fouls per game above the league's 39.5 add 0.85 points to the
total per extra foul (free throws). Basketball is indoor: weather is ignored; altitude enters via the home edge.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, ClassVar

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import IntPMF, LineOutcome, SubModelEstimate, clamp
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
    over_under,
    settle_threshold,
    side_token,
    split_selection,
)
from app.schemas.bet_types import LegStatus

# ---------------------------------------------------------------- league constants (NBA, 2023-25 seasons)
LEAGUE_ORTG = 114.0  # points per 100 possessions
LEAGUE_PACE = 99.5  # possessions per 48 minutes
LEAGUE_FACTORS: dict[str, float] = {"efg": 0.54, "tov": 0.13, "orb": 0.25, "ftr": 0.20}
FACTOR_SD: dict[str, float] = {"efg": 0.020, "tov": 0.010, "orb": 0.025, "ftr": 0.030}  # team-season spread
FACTOR_WEIGHTS: dict[str, float] = {"efg": 0.40, "tov": 0.25, "orb": 0.20, "ftr": 0.15}  # Dean Oliver
FACTOR_SIGNS: dict[str, float] = {"efg": 1.0, "tov": -1.0, "orb": 1.0, "ftr": 1.0}
FF_POINTS_PER_SIGMA = 9.0  # points per 100 possessions per weighted standard deviation of the four factors
PYTHAGOREAN_EXPONENT = 13.91  # Daryl Morey
MARGIN_SD = 11.5  # NBA final margin about the closing spread
TOTAL_SD = 18.0  # NBA game total about the closing total
REGULATION_MINUTES = 48.0
OVERTIME_MINUTES = 5.0
OT_SHARE = OVERTIME_MINUTES / REGULATION_MINUTES
TEAM_MINUTES_PER_GAME = 240.0  # five players x 48 minutes
ELO_POINTS_PER_POINT = 28.0  # FiveThirtyEight NBA Elo: 28 rating points ~ one point of spread
ALTITUDE_HOME_ADVANTAGES: dict[str, float] = {"denver": 2.8, "utah": 2.1}  # Ball Arena 5280 ft, Delta Center 4226 ft
ALTITUDE_ALIASES: dict[str, str] = {"denver": "denver", "ball_arena": "denver", "utah": "utah", "salt_lake_city": "utah",
                                     "salt_lake": "utah", "delta_center": "utah"}
STANDARD_HOME_ADVANTAGE = 2.2
POINTS_PER_WAR_PER_GAME = 1.0 / 2.7  # 1 point of season margin ~ 2.7 wins
USAGE_ORTG_PER_POINT = 0.04  # ORtg points per 100 possessions lost per usage point of absent players
LEAGUE_FOULS_PER_GAME = 39.5
POINTS_PER_EXTRA_FOUL = 0.85
POSSESSION_VALUE = 0.5  # live: holding the ball is worth ~half a point against the alternating baseline
POSSESSION_POINTS_SD = 1.1  # live: spread of the current possession's points (0 / 1 / 2 / 3) about its value
SECONDS_PER_QUARTER = 720.0
SECONDS_PER_OVERTIME = 300.0
REGULATION_SECONDS = 4 * SECONDS_PER_QUARTER
FIRST_HALF_SHARE = 0.5
QUARTER_SHARE = 0.25
#: Teaser shifts offered on basketball sides and totals (the 4 / 4.5 / 5 NBA ladder and the 6 / 6.5 / 7 shared with the NFL).
TEASER_POINT_SHIFTS: tuple[float, ...] = (4.0, 4.5, 5.0, 6.0, 6.5, 7.0)
FINISHED_STATUSES = frozenset({"final", "finished", "complete", "completed", "ended", "ft", "full_time"})
_NORMAL = NormalDist()


# ---------------------------------------------------------------- distribution helpers
def normal_pmf(mu: float, sigma: float, width: float = 6.0, *, floor: int | None = None) -> IntPMF:
    lo = math.floor(mu - width * sigma)
    hi = math.ceil(mu + width * sigma)
    if floor is not None:
        lo = max(lo, floor)
        hi = max(hi, lo + 1)
    return IntPMF.from_normal(mu, sigma, lo, hi)


def overtime_margin(mu: float, sigma: float) -> IntPMF:
    """Margin of the overtime period(s) that decide the game: one OT's margin with its own tie mass folded into
    the repeat overtimes (renormalised on the non-zero margins)."""
    ot = normal_pmf(mu * OT_SHARE, sigma * math.sqrt(OT_SHARE))
    probs = ot.probs.copy()
    zero = -ot.offset
    if 0 <= zero < len(probs):
        probs[zero] = 0.0
    return IntPMF.from_array(probs, ot.offset)


def final_margin(mu: float, sigma: float) -> tuple[IntPMF, float]:
    """(final margin incl. overtime, P(regulation tie)) for a regulation margin ~ Normal(mu, sigma)."""
    lo = min(math.floor(mu - 6.0 * sigma), -25)
    hi = max(math.ceil(mu + 6.0 * sigma), 25)
    reg = IntPMF.from_normal(mu, sigma, lo, hi)
    p_tie = reg.prob_eq(0)
    probs = reg.probs.copy()
    probs[-lo] = 0.0
    ot = overtime_margin(mu, sigma)
    start = ot.offset - lo
    probs[start: start + len(ot.probs)] += p_tie * ot.probs
    return IntPMF.from_array(probs, lo), p_tie


def points_with_overtime(mean: float, sd: float, ot_mean: float, ot_sd: float, p_ot: float) -> IntPMF:
    """Points scored (a total or a team total): regulation ~ Normal(mean, sd), plus an overtime's points with
    probability ``p_ot`` (margin and total treated as independent, as the normal model has them)."""
    reg = normal_pmf(mean, sd, floor=0)
    if p_ot <= 1e-9:
        return reg
    extra = normal_pmf(mean + ot_mean, math.sqrt(sd * sd + ot_sd * ot_sd), floor=0)
    return IntPMF.mix([(reg, 1.0 - p_ot), (extra, p_ot)])


def american_odds(decimal_odds: float) -> float:
    """Fair American odds of decimal odds (+150 / -150)."""
    if decimal_odds >= 2.0:
        return 100.0 * (decimal_odds - 1.0)
    return -100.0 / max(decimal_odds - 1.0, 1e-9)


def _fmt(line: float, signed: bool) -> str:
    value = round(line * 2.0) / 2.0 + 0.0
    return f"{value:+g}" if signed else f"{value:g}"


@dataclass
class BasketballProjection:
    """Everything one model run produces; the market pricer, the teaser cover pricer and the live model read it."""

    estimates: list[SubModelEstimate]
    weights: dict[str, float]
    sub_margins: dict[str, float]  # regulation margin mean per sub-model (home - away, incl. the home edge)
    sub_totals: dict[str, float]
    margin: IntPMF  # final margin (incl. overtime), the weighted mixture of the sub-models
    p_overtime: float
    mu: float  # blended regulation margin mean
    sigma: float
    expected_total: float  # blended regulation total
    total_sd: float
    pace: float
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def team_sd(self) -> float:
        return math.sqrt((self.total_sd ** 2 + self.sigma ** 2) / 4.0)

    def total_pmf(self) -> IntPMF:
        return points_with_overtime(self.expected_total, self.total_sd, self.expected_total * OT_SHARE,
                                    self.total_sd * math.sqrt(OT_SHARE), self.p_overtime)

    def team_pmf(self, side: str) -> IntPMF:
        sign = 1.0 if side == "home" else -1.0
        mean = (self.expected_total + sign * self.mu) / 2.0
        ot_mean = (self.expected_total + sign * self.mu) * OT_SHARE / 2.0
        return points_with_overtime(mean, self.team_sd, ot_mean, self.team_sd * math.sqrt(OT_SHARE), self.p_overtime)

    def period_margin(self, share: float) -> IntPMF:
        return normal_pmf(self.mu * share, self.sigma * math.sqrt(share))

    def period_total(self, share: float) -> IntPMF:
        return normal_pmf(self.expected_total * share, self.total_sd * math.sqrt(share), floor=0)


class BasketballSportAdapter(BaseSportAdapter):
    """
    Quantitative Basketball Engine implementing Dean Oliver's Four Factors:
    1. eFG% (Effective Field Goal Percentage) - 40% weight
    2. TOV% (Turnover Percentage) - 25% weight
    3. ORB% (Offensive Rebounding Percentage) - 20% weight
    4. FTR (Free Throw Rate) - 15% weight
    plus possession pace projection, altitude correction, and Morey-Pythagorean expectation.
    """

    ALTITUDE_HOME_ADVANTAGES: dict[str, float] = ALTITUDE_HOME_ADVANTAGES
    TEASER_POINT_SHIFTS: tuple[float, ...] = TEASER_POINT_SHIFTS
    kelly_multiplier = 0.40
    model_family = "Dean Oliver Four Factors"
    has_draw = False
    odds_api_family = "basketball"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner": (MONEYLINE, {}),
        "money_line": (MONEYLINE, {}),
        "point_spread": (SPREAD, {}),
        "handicap": (SPREAD, {}),
        "game_total_over_under": (TOTAL, {}),
        "game_total": (TOTAL, {}),
        "total_points": (TOTAL, {}),
        "team_totals": (TEAM_TOTAL, {}),
        "team_total_points": (TEAM_TOTAL, {}),
        "alternative_spread": (ALT_SPREAD, {}),
        "alternative_total": (ALT_TOTAL, {}),
        "first_half_moneyline": (PERIOD_MONEYLINE, {"period": "h1", "level_is_void": True}),
        "first_half_winner": (PERIOD_MONEYLINE, {"period": "h1", "level_is_void": True}),
        "first_half_spread": (PERIOD_SPREAD, {"period": "h1"}),
        "first_half_total": (PERIOD_TOTAL, {"period": "h1"}),
        "first_quarter_spread": (PERIOD_SPREAD, {"period": "q1"}),
        "first_quarter_total": (PERIOD_TOTAL, {"period": "q1"}),
        "first_quarter_winner": ("quarter_winner", {"quarter": 1}),
        "first_team_to_score": (NEXT_EVENT, {"event_type": "score"}),
        "next_score": (NEXT_EVENT, {"event_type": "score"}),
        "player_points": (PLAYER_PROP, {"stat": "points"}),
        "player_rebounds": (PLAYER_PROP, {"stat": "rebounds"}),
        "player_assists": (PLAYER_PROP, {"stat": "assists"}),
        "player_threes": (PLAYER_PROP, {"stat": "threes"}),
        "player_steals": (PLAYER_PROP, {"stat": "steals"}),
        "player_blocks": (PLAYER_PROP, {"stat": "blocks"}),
        "points_rebounds_assists": ("player_pra", {}),
    }
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE, PERIOD_SPREAD,
                                   PERIOD_TOTAL, PLAYER_PROP, NEXT_EVENT})
    SPECIAL_MARKETS = frozenset({"quarter_winner", "player_pra"})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "teaser", "pleaser",
                           "round_robin", "if_bet", "double_action", "futures", "player_props", "game_props", "live_next_event",
                           "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "basketball"

    @property
    def display_name(self) -> str:
        return "Basketball"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "moneyline",
            "point_spread",
            "game_total_over_under",
            "team_totals",
            "quarter_winner",
            "first_half_spread",
            "first_half_total",
            "alternative_spread",
            "alternative_total",
            "player_points",
        ]

    # ------------------------------------------------------------------ the four factors and pace
    @staticmethod
    def four_factors(stats: Mapping[str, Any]) -> dict[str, float]:
        """eFG%, TOV%, ORB%, FTR from the box score where its inputs are present, else the summary fields, else the
        league average."""
        fgm, fga, fg3m = num(stats, "fgm"), num(stats, "fga"), num(stats, "fg3m", "three_pm", "3pm")
        tov, fta, ftm = num(stats, "tov", "turnovers"), num(stats, "fta"), num(stats, "ftm")
        orb, opp_drb = num(stats, "orb", "oreb"), num(stats, "opp_drb", "opp_dreb")
        out: dict[str, float] = {}
        if fgm is not None and fga and fga > 0 and fg3m is not None:
            out["efg"] = (fgm + 0.5 * fg3m) / fga
        else:
            out["efg"] = num(stats, "efg_pct", "efg", default=LEAGUE_FACTORS["efg"])  # type: ignore[assignment]
        if tov is not None and fga is not None and fta is not None and fga + 0.44 * fta + tov > 0:
            out["tov"] = tov / (fga + 0.44 * fta + tov)
        else:
            out["tov"] = num(stats, "tov_pct", default=LEAGUE_FACTORS["tov"])  # type: ignore[assignment]
        if orb is not None and opp_drb is not None and orb + opp_drb > 0:
            out["orb"] = orb / (orb + opp_drb)
        else:
            out["orb"] = num(stats, "orb_pct", default=LEAGUE_FACTORS["orb"])  # type: ignore[assignment]
        if ftm is not None and fga and fga > 0:
            out["ftr"] = ftm / fga
        else:
            out["ftr"] = num(stats, "ftr", "ft_rate", default=LEAGUE_FACTORS["ftr"])  # type: ignore[assignment]
        return {k: _as_fraction(v) for k, v in out.items()}

    @staticmethod
    def defensive_factors(stats: Mapping[str, Any]) -> dict[str, float]:
        """The opponent factors a defence allows (eFG allowed, TOV% forced, opponent ORB%, opponent FTR)."""
        return {
            "efg": _as_fraction(num(stats, "def_efg_pct", "opp_efg_pct", default=LEAGUE_FACTORS["efg"])),  # type: ignore[arg-type]
            "tov": _as_fraction(num(stats, "def_tov_pct", "opp_tov_pct", default=LEAGUE_FACTORS["tov"])),  # type: ignore[arg-type]
            "orb": _as_fraction(num(stats, "def_orb_pct", "opp_orb_pct", default=LEAGUE_FACTORS["orb"])),  # type: ignore[arg-type]
            "ftr": _as_fraction(num(stats, "def_ftr", "opp_ftr", default=LEAGUE_FACTORS["ftr"])),  # type: ignore[arg-type]
        }

    @staticmethod
    def four_factor_points(offence: Mapping[str, float], defence: Mapping[str, float] | None = None) -> float:
        """Points per 100 possessions above the league from the weighted, standardised factor differentials of the
        matchup ``offence + defence allowed - league``."""
        index = 0.0
        for f, weight in FACTOR_WEIGHTS.items():
            diff = offence[f] - LEAGUE_FACTORS[f]
            if defence is not None:
                diff += defence[f] - LEAGUE_FACTORS[f]
            index += weight * FACTOR_SIGNS[f] * diff / FACTOR_SD[f]
        return FF_POINTS_PER_SIGMA * index

    def calculate_four_factors_rating(self, stats: dict[str, Any]) -> float:
        """A team's offensive Four Factors efficiency against a league-average defence (points per 100 above 114)."""
        return self.four_factor_points(self.four_factors(stats))

    @staticmethod
    def possessions(stats: Mapping[str, Any], prefix: str = "") -> float | None:
        """``Poss = FGA + 0.44 FTA - ORB + TOV`` (``prefix='opp_'`` for the opponent's box line)."""
        fga, fta = num(stats, f"{prefix}fga"), num(stats, f"{prefix}fta")
        orb, tov = num(stats, f"{prefix}orb", f"{prefix}oreb"), num(stats, f"{prefix}tov", f"{prefix}turnovers")
        if None in (fga, fta, orb, tov):
            return None
        return fga + 0.44 * fta - orb + tov  # type: ignore[operator]

    def team_pace(self, stats: Mapping[str, Any], league_pace: float) -> float:
        """``Pace = 48 (Poss_team + Poss_opp) / (2 Minutes / 5)`` from the box score (team minutes, 240 a game),
        else the ``pace`` field, else the league pace."""
        poss = self.possessions(stats)
        if poss is not None:
            opp = self.possessions(stats, "opp_")
            games = num(stats, "games", default=1.0) or 1.0
            minutes = num(stats, "minutes", "team_minutes", default=TEAM_MINUTES_PER_GAME * games) or TEAM_MINUTES_PER_GAME
            return REGULATION_MINUTES * (poss + (opp if opp is not None else poss)) / (2.0 * (minutes / 5.0))
        return float(num(stats, "pace", default=league_pace))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ home edge and context
    def home_advantage(self, ctx: MatchContext) -> tuple[float, str]:
        if ctx.get("neutral_site") is True or ctx.text("venue_type", "") == "neutral":
            return 0.0, "neutral"
        venue = norm(ctx.get("venue_city") or ctx.get("venue") or "")
        for alias, city in ALTITUDE_ALIASES.items():
            if alias in venue:
                return ALTITUDE_HOME_ADVANTAGES[city], f"{city}_altitude"
        return ctx.number("home_advantage_points", STANDARD_HOME_ADVANTAGE), "standard"

    def context_adjustments(self, ctx: MatchContext, pace: float) -> tuple[float, float, dict[str, Any]]:
        """(home points delta, away points delta, recorded adjustments) from absences and officials."""
        d_home, d_away = 0.0, 0.0
        adjustments: dict[str, Any] = {}
        if ctx.absences:
            for side in ("home", "away"):
                war_margin = ctx.war_lost(side) * POINTS_PER_WAR_PER_GAME
                usage = sum(_usage_points(a.usage_pct) * a.weight for a in ctx.absences if a.side == side)
                ortg_drop = USAGE_ORTG_PER_POINT * usage
                own = -war_margin / 2.0 - pace / 100.0 * ortg_drop
                opp = war_margin / 2.0
                if side == "home":
                    d_home, d_away = d_home + own, d_away + opp
                else:
                    d_away, d_home = d_away + own, d_home + opp
                adjustments[f"{side}_absence_margin_points"] = round(-war_margin, 4)
                adjustments[f"{side}_usage_ortg_delta"] = round(-ortg_drop, 4)
        if ctx.officials is not None:
            fouls = ctx.officials.rate("fouls_per_game", "fouls_per_match", "personal_fouls_per_game")
            if fouls is not None:
                extra = POINTS_PER_EXTRA_FOUL * (fouls - LEAGUE_FOULS_PER_GAME)
                d_home, d_away = d_home + extra / 2.0, d_away + extra / 2.0
                adjustments["officials_foul_points_delta"] = round(extra, 4)
        return d_home, d_away, adjustments

    # ------------------------------------------------------------------ the sub-models
    def project(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> BasketballProjection:
        league = ctx.number("league_ortg", LEAGUE_ORTG)
        league_pace = ctx.number("league_pace", LEAGUE_PACE)
        sigma = clamp(ctx.number("margin_sd", MARGIN_SD), 6.0, 20.0)
        total_sd = clamp(ctx.number("total_sd", TOTAL_SD), 8.0, 30.0)
        hfa, hfa_source = self.home_advantage(ctx)

        pace_h, pace_a = self.team_pace(home_stats, league_pace), self.team_pace(away_stats, league_pace)
        pace = pace_h * pace_a / league_pace
        d_home, d_away, adjustments = self.context_adjustments(ctx, pace)
        d_margin, d_total = d_home - d_away, d_home + d_away

        off_h, off_a = self.four_factors(home_stats), self.four_factors(away_stats)
        def_h, def_a = self.defensive_factors(home_stats), self.defensive_factors(away_stats)

        # 1. Four Factors efficiency: home offence vs away defence and vice versa
        ff_h = self.four_factor_points(off_h, def_a)
        ff_a = self.four_factor_points(off_a, def_h)
        ortg_ff_h, ortg_ff_a = league + ff_h, league + ff_a
        mu_ff = pace / 100.0 * (ortg_ff_h - ortg_ff_a) + hfa + d_margin
        total_ff = pace / 100.0 * (ortg_ff_h + ortg_ff_a) + d_total

        # season ratings (the Four Factors' own implied ratings when the request carries none)
        rating_h_off = num(home_stats, "off_rating", "ortg", default=league + self.four_factor_points(off_h))
        rating_h_def = num(home_stats, "def_rating", "drtg", default=league - self.four_factor_points(def_h))
        rating_a_off = num(away_stats, "off_rating", "ortg", default=league + self.four_factor_points(off_a))
        rating_a_def = num(away_stats, "def_rating", "drtg", default=league - self.four_factor_points(def_a))

        # 3. Net rating / SRS: ORtg adjusted for the opposing defence relative to the league
        ortg_adj_h = rating_h_off + (rating_a_def - league)  # type: ignore[operator]
        ortg_adj_a = rating_a_off + (rating_h_def - league)  # type: ignore[operator]
        srs_h, srs_a = num(home_stats, "srs"), num(away_stats, "srs")
        if srs_h is not None and srs_a is not None:
            mu_nr = srs_h - srs_a + hfa + d_margin
        else:
            mu_nr = pace / 100.0 * (ortg_adj_h - ortg_adj_a) + hfa + d_margin
        total_nr = pace / 100.0 * (ortg_adj_h + ortg_adj_a) + d_total

        # 2. Morey-Pythagorean with a log5 matchup
        pyth_h = self.pythagorean(home_stats, rating_h_off, rating_h_def)  # type: ignore[arg-type]
        pyth_a = self.pythagorean(away_stats, rating_a_off, rating_a_def)  # type: ignore[arg-type]
        p_log5 = log5(pyth_h, pyth_a)
        mu_py = sigma * _NORMAL.inv_cdf(clamp(p_log5, 1e-6, 1 - 1e-6)) + hfa + d_margin
        pf_h, pa_h = num(home_stats, "points_for"), num(home_stats, "points_against")
        pf_a, pa_a = num(away_stats, "points_for"), num(away_stats, "points_against")
        total_py = None
        if None not in (pf_h, pa_h, pf_a, pa_a):
            total_py = (pf_h + pa_a) / 2.0 + (pf_a + pa_h) / 2.0 + d_total  # type: ignore[operator]

        sub_margins = {"four_factors_efficiency": mu_ff, "morey_pythagorean": mu_py, "net_rating_srs": mu_nr}
        sub_totals: dict[str, float | None] = {"four_factors_efficiency": total_ff, "morey_pythagorean": total_py,
                                               "net_rating_srs": total_nr}
        defaults = {"four_factors_efficiency": 0.40, "morey_pythagorean": 0.25, "net_rating_srs": 0.35}

        # 4. Elo, when both teams carry a rating
        elo_h, elo_a = num(home_stats, "elo"), num(away_stats, "elo")
        if elo_h is not None and elo_a is not None:
            sub_margins["elo"] = (elo_h - elo_a) / ELO_POINTS_PER_POINT + hfa + d_margin
            sub_totals["elo"] = None
            defaults = {"four_factors_efficiency": 0.35, "morey_pythagorean": 0.20, "net_rating_srs": 0.30, "elo": 0.15}

        estimates: list[SubModelEstimate] = []
        pmfs: dict[str, IntPMF] = {}
        ties: dict[str, float] = {}
        for name, mu in sub_margins.items():
            pmf, p_tie = final_margin(mu, sigma)
            pmfs[name], ties[name] = pmf, p_tie
            estimates.append(SubModelEstimate(name, pmf.prob_gt(0.0), pmf.prob_lt(0.0), 0.0, sub_totals[name], defaults[name]))

        weights = {e.name: max(0.0, float(ctx.model_weights.get(e.name, e.weight))) for e in estimates}
        if sum(weights.values()) <= 0:
            weights = dict(defaults)
        names = list(sub_margins)
        w = [weights[n] for n in names]
        margin = IntPMF.mix([(pmfs[n], wn) for n, wn in zip(names, w, strict=True)])
        total_w = sum(w)
        mu = sum(sub_margins[n] * weights[n] for n in names) / total_w
        p_ot = sum(ties[n] * weights[n] for n in names) / total_w
        voting_totals = [(t, weights[n]) for n, t in sub_totals.items() if t is not None and weights[n] > 0]
        if not voting_totals:
            voting_totals = [(total_ff, 1.0)]
        expected_total = sum(t * wt for t, wt in voting_totals) / sum(wt for _, wt in voting_totals)

        metrics: dict[str, Any] = {
            "altitude_points": hfa,
            "home_advantage_source": hfa_source,
            "projected_pace": round(pace, 2),
            "home_pace": round(pace_h, 2),
            "away_pace": round(pace_a, 2),
            "projected_spread": round(-mu, 2),  # negative: home favourite
            "home_projected_pts": round((expected_total + mu) / 2.0, 2),
            "away_projected_pts": round((expected_total - mu) / 2.0, 2),
            "four_factors": {"home": {k: round(v, 4) for k, v in off_h.items()}, "away": {k: round(v, 4) for k, v in off_a.items()}},
            "four_factor_points_per_100": {"home": round(ff_h, 3), "away": round(ff_a, 3)},
            "four_factor_weights": dict(FACTOR_WEIGHTS),
            "league_factor_averages": dict(LEAGUE_FACTORS),
            "home_ortg_adj": round(ortg_adj_h, 3),
            "away_ortg_adj": round(ortg_adj_a, 3),
            "pythagorean_exponent": PYTHAGOREAN_EXPONENT,
            "pythagorean_home": round(pyth_h, 4),
            "pythagorean_away": round(pyth_a, 4),
            "log5_neutral_home": round(p_log5, 4),
            "sub_model_margins": {k: round(v, 3) for k, v in sub_margins.items()},
            "margin_sd": sigma,
            "total_sd": total_sd,
            "overtime_probability": round(p_ot, 4),
            **adjustments,
        }
        return BasketballProjection(estimates, weights, sub_margins, {k: v for k, v in sub_totals.items() if v is not None}, margin,
                                    p_ot, mu, sigma, expected_total, total_sd, pace, metrics)

    @staticmethod
    def pythagorean(stats: Mapping[str, Any], off_rating: float, def_rating: float) -> float:
        """Morey: ``PF^13.91 / (PF^13.91 + PA^13.91)`` from points per game, else from the ratings."""
        pf, pa = num(stats, "points_for", "ppg"), num(stats, "points_against", "opp_ppg")
        if pf is None or pa is None or pf <= 0 or pa <= 0:
            pf, pa = off_rating, def_rating
        ratio = (max(pa, 1e-6) / max(pf, 1e-6)) ** PYTHAGOREAN_EXPONENT
        return 1.0 / (1.0 + ratio)

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        consensus = self._consensus(proj.estimates, ctx)
        markets = self.price_markets(proj)
        total_pmf = proj.total_pmf()
        return self._assemble(consensus, ctx, expected_total=total_pmf.mean, metrics=proj.metrics, markets=markets)

    def price_markets(self, proj: BasketballProjection) -> dict[str, float]:
        markets: dict[str, float] = {}
        margin, away_margin = proj.margin, proj.margin.negate()
        markets["moneyline_home"] = margin.prob_gt(0.0)
        markets["moneyline_away"] = margin.prob_lt(0.0)
        markets["overtime_yes"] = proj.p_overtime
        markets["overtime_no"] = 1.0 - proj.p_overtime

        base = round(-margin.mean * 2.0) / 2.0
        for d in (-3.0, -2.5, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            line = base + d
            markets[f"spread_home_{_fmt(line, True)}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"spread_away_{_fmt(-line, True)}"] = away_margin.line_outcome(line, over=True).effective_probability
        for d in (-10.0, -7.5, -5.0, 5.0, 7.5, 10.0):
            line = base + d
            markets[f"alt_spread_home_{_fmt(line, True)}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"alt_spread_away_{_fmt(-line, True)}"] = away_margin.line_outcome(line, over=True).effective_probability
        for shift in TEASER_POINT_SHIFTS:
            markets[f"teaser_{shift:g}_home_{_fmt(base + shift, True)}"] = margin.line_outcome(-(base + shift), over=True).effective_probability
            markets[f"teaser_{shift:g}_away_{_fmt(-base + shift, True)}"] = away_margin.line_outcome(-(-base + shift), over=True).effective_probability

        total = proj.total_pmf()
        tbase = round(total.mean * 2.0) / 2.0
        for d in (-3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0):
            line = tbase + d
            markets[f"total_over_{_fmt(line, False)}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"total_under_{_fmt(line, False)}"] = total.line_outcome(line, over=False).effective_probability
        for d in (-15.0, -10.0, -7.5, 7.5, 10.0, 15.0):
            line = tbase + d
            markets[f"alt_total_over_{_fmt(line, False)}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"alt_total_under_{_fmt(line, False)}"] = total.line_outcome(line, over=False).effective_probability

        for side in ("home", "away"):
            team = proj.team_pmf(side)
            tb = math.floor(team.mean) + 0.5
            for d in (-2.0, -1.0, 0.0, 1.0, 2.0):
                line = tb + d
                markets[f"team_total_{side}_over_{_fmt(line, False)}"] = team.line_outcome(line, over=True).effective_probability
                markets[f"team_total_{side}_under_{_fmt(line, False)}"] = team.line_outcome(line, over=False).effective_probability

        for prefix, share in (("h1", FIRST_HALF_SHARE), ("q1", QUARTER_SHARE)):
            pm, pt = proj.period_margin(share), proj.period_total(share)
            markets[f"{prefix}_home"], markets[f"{prefix}_tie"], markets[f"{prefix}_away"] = pm.prob_gt(0.0), pm.prob_eq(0), pm.prob_lt(0.0)
            markets[f"{prefix}_home_2way"] = pm.line_outcome(0.0, over=True).effective_probability  # a tie voids the 2-way line
            markets[f"{prefix}_away_2way"] = pm.line_outcome(0.0, over=False).effective_probability
            pbase = round(-pm.mean * 2.0) / 2.0
            for d in (-1.0, -0.5, 0.0, 0.5, 1.0):
                line = pbase + d
                markets[f"{prefix}_spread_home_{_fmt(line, True)}"] = pm.line_outcome(-line, over=True).effective_probability
                markets[f"{prefix}_spread_away_{_fmt(-line, True)}"] = pm.negate().line_outcome(line, over=True).effective_probability
            ptb = math.floor(pt.mean) + 0.5
            for d in (-1.0, 0.0, 1.0):
                line = ptb + d
                markets[f"{prefix}_total_over_{_fmt(line, False)}"] = pt.line_outcome(line, over=True).effective_probability
                markets[f"{prefix}_total_under_{_fmt(line, False)}"] = pt.line_outcome(line, over=False).effective_probability
        return markets

    def cover_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        side: str,
        line: float,
        context: dict[str, Any] | None = None,
        market: str = "spread",
    ) -> LineOutcome:
        """One leg's full outcome (win / push / half results) for the teaser and pleaser pricer.

        ``market`` ``"spread"``: ``side`` home/away with ``line`` the handicap on that side (overtime included);
        ``"total"``: ``side`` over/under; ``"team_total"``: ``side`` like ``"home_over"``;
        ``"first_half_spread"`` / ``"first_half_total"`` / ``"first_quarter_spread"`` / ``"first_quarter_total"``."""
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        kind = norm(market)
        if kind in ("spread", "point_spread", "alt_spread", "handicap"):
            return _handicap_outcome(proj.margin, side, line)
        if kind in ("total", "game_total", "alt_total", "game_total_over_under"):
            return proj.total_pmf().line_outcome(line, over=over_under(side))
        if kind in ("team_total", "team_totals", "team_total_points"):
            team = _side_of_selection(side)
            return proj.team_pmf(team).line_outcome(line, over=over_under(side))
        periods = {"first_half_spread": FIRST_HALF_SHARE, "h1_spread": FIRST_HALF_SHARE, "first_quarter_spread": QUARTER_SHARE,
                   "q1_spread": QUARTER_SHARE}
        if kind in periods:
            return _handicap_outcome(proj.period_margin(periods[kind]), side, line)
        totals = {"first_half_total": FIRST_HALF_SHARE, "h1_total": FIRST_HALF_SHARE, "first_quarter_total": QUARTER_SHARE,
                  "q1_total": QUARTER_SHARE}
        if kind in totals:
            return proj.period_total(totals[kind]).line_outcome(line, over=over_under(side))
        raise ValueError(f"basketball cover_probability has no market {market!r}")

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability.

        ``live_state`` keys: ``quarter`` (1-4, 5+ overtime), ``seconds_remaining_in_quarter`` (or
        ``seconds_remaining`` left in regulation), ``home_score``, ``away_score``, ``possession``
        ("home" / "away" / None), optional ``status`` ("final" ends the game).

        The rest of the game is ``Normal(mu_pre f + possession, sqrt(sigma^2 f + 1.1^2))`` with ``f`` the remaining
        share of regulation (an overtime period's seconds over 2880 once in overtime) and 1.1 the spread of the
        current possession's points; a level score at the horn goes to overtime, won with the
        overtime model's strength-adjusted probability."""
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        mu_pre, sigma = proj.mu, proj.sigma
        home_score = float(num(live_state, "home_score", "home_points", default=0.0))  # type: ignore[arg-type]
        away_score = float(num(live_state, "away_score", "away_points", default=0.0))  # type: ignore[arg-type]
        lead = round(home_score - away_score)
        quarter = int(num(live_state, "quarter", "period", default=1) or 1)
        status = norm(live_state.get("status"))
        ot = overtime_margin(mu_pre, sigma)
        p_ot_home = ot.prob_gt(0.0)
        detail: dict[str, Any] = {"lead": lead, "quarter": quarter, "pregame_margin": round(mu_pre, 3)}

        if quarter >= 5:
            secs = num(live_state, "seconds_remaining_in_quarter", "seconds_remaining", default=SECONDS_PER_OVERTIME)
            remaining = clamp(float(secs), 0.0, SECONDS_PER_OVERTIME)  # type: ignore[arg-type]
        else:
            in_q = num(live_state, "seconds_remaining_in_quarter")
            if in_q is not None:
                remaining = (4 - max(quarter, 1)) * SECONDS_PER_QUARTER + clamp(in_q, 0.0, SECONDS_PER_QUARTER)
            else:
                remaining = clamp(float(num(live_state, "seconds_remaining", default=REGULATION_SECONDS)), 0.0, REGULATION_SECONDS)  # type: ignore[arg-type]
        f = remaining / REGULATION_SECONDS
        detail["remaining_fraction"] = round(f, 5)

        if status in FINISHED_STATUSES or remaining <= 0.0:
            if lead > 0:
                return LiveWinProbability(1.0, 0.0, 0.0, {**detail, "state": "final"})
            if lead < 0:
                return LiveWinProbability(0.0, 1.0, 0.0, {**detail, "state": "final"})
            if status in FINISHED_STATUSES:
                return LiveWinProbability(0.5, 0.5, 0.0, {**detail, "state": "final_level"})
            return LiveWinProbability(p_ot_home, 1.0 - p_ot_home, 0.0, {**detail, "state": "overtime_next"})

        possession = norm(live_state.get("possession"))
        pos = POSSESSION_VALUE if possession == "home" else -POSSESSION_VALUE if possession == "away" else 0.0
        mu_rem = mu_pre * f + pos
        sd_rem = math.sqrt(sigma * sigma * f + (POSSESSION_POINTS_SD ** 2 if pos else 0.0) + 0.1)
        rest = normal_pmf(mu_rem, sd_rem).shift(lead)
        home = rest.prob_gt(0.0) + rest.prob_eq(0) * p_ot_home
        away = rest.prob_lt(0.0) + rest.prob_eq(0) * (1.0 - p_ot_home)
        total = home + away
        return LiveWinProbability(home / total, away / total, 0.0,
                                  {**detail, "remaining_margin_mean": round(mu_rem, 3), "remaining_margin_sd": round(sd_rem, 3),
                                   "p_level_at_horn": round(rest.prob_eq(0), 4)})

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        res = normalize_basic(match_result, home_keys=("home_score", "home_points", "home_pts"),
                              away_keys=("away_score", "away_points", "away_pts"))
        _read_quarters(match_result, res, quarters=4)
        return res

    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == "quarter_winner":
            return self._settle_quarter_winner(result, raw, selection, params)
        if market == "player_pra":
            return self._settle_points_rebounds_assists(result, selection, line, params)
        return super().settle_special(market, result, raw, selection, line, params)

    @staticmethod
    def _settle_quarter_winner(result: NormalizedResult, raw: Mapping[str, Any], selection: str, params: Mapping[str, Any]) -> LegStatus:
        """The quarter (``params.quarter`` / ``params.period`` / the raw ``quarter``) winner; a tied quarter voids a
        home/away pick (2-way) and wins a ``tie`` pick."""
        period = norm(params.get("period")) or _quarter_key(params.get("quarter", raw.get("quarter", 1)))
        pair = result.periods.get(period)
        if pair is None:
            raise SettlementInputError(f"the result has no {period!r} quarter score")
        home, away = pair
        side = _side_of_selection(selection, result, allow_draw=True)
        if home == away:
            return LegStatus.WON if side == "draw" else LegStatus.VOID
        if side == "draw":
            return LegStatus.LOST
        return LegStatus.WON if (home > away) == (side == "home") else LegStatus.LOST

    @staticmethod
    def _settle_points_rebounds_assists(result: NormalizedResult, selection: str, line: float | None,
                                        params: Mapping[str, Any]) -> LegStatus:
        """Points + rebounds + assists over/under; a player who did not take part voids the leg."""
        if line is None:
            raise SettlementInputError("points + rebounds + assists needs a line")
        player = norm(params.get("player"))
        if not player:
            raise SettlementInputError("points + rebounds + assists needs params.player")
        stats = result.player_stats.get(player)
        if (result.participants is not None and player not in result.participants) or stats is None:
            return LegStatus.VOID
        value = sum(stats.get(k, 0.0) for k in ("points", "rebounds", "assists"))
        return settle_threshold(value, line, over=over_under(selection))


# ---------------------------------------------------------------- module helpers
def log5(p_home: float, p_away: float) -> float:
    """Bill James' log5: P(A beats B) from their win expectancies against an average team."""
    num_ = p_home * (1.0 - p_away)
    den = num_ + p_away * (1.0 - p_home)
    return 0.5 if den <= 0 else num_ / den


def _quarter_key(quarter: Any) -> str:
    """1, "1", "q1", "Q1" -> "q1"."""
    text = norm(quarter).lstrip("q")
    try:
        return f"q{int(float(text))}"
    except ValueError as exc:
        raise SettlementInputError(f"quarter {quarter!r} is not a quarter number") from exc


def _as_fraction(value: float) -> float:
    """Percentages given as 54.0 instead of 0.54 are read as fractions."""
    return value / 100.0 if value > 1.5 else value


def _usage_points(usage: float | None) -> float:
    if usage is None:
        return 0.0
    return usage * 100.0 if usage <= 1.0 else usage


def _handicap_outcome(margin: IntPMF, side: str, line: float) -> LineOutcome:
    s = _side_of_selection(side)
    pmf = margin if s == "home" else margin.negate()
    return pmf.line_outcome(-line, over=True)


def _side_of_selection(selection: str, result: NormalizedResult | None = None, *, allow_draw: bool = False) -> str:
    for token in [selection, *split_selection(selection)]:
        side = side_token(token, result)
        if side in ("home", "away") or (allow_draw and side == "draw"):
            return side
    raise SettlementInputError(f"selection {selection!r} names no side")


def _listed_quarters(raw: Mapping[str, Any], quarters: int) -> list[tuple[str, tuple[float, float]]]:
    """``quarters`` as ``{"home": [...], "away": [...]}`` or ``[[h, a], ...]``; entries past regulation are overtimes."""
    listed = raw.get("quarters")
    pairs: list[tuple[float, float]] = []
    if isinstance(listed, Mapping):
        hs, as_ = listed.get("home"), listed.get("away")
        if isinstance(hs, (list, tuple)) and isinstance(as_, (list, tuple)):
            pairs = [(float(h), float(a)) for h, a in zip(hs, as_, strict=False)]
    elif isinstance(listed, (list, tuple)):
        pairs = [(float(p[0]), float(p[1])) for p in listed if isinstance(p, (list, tuple)) and len(p) == 2]
    return [(f"q{i}" if i <= quarters else f"ot{i - quarters}", pair) for i, pair in enumerate(pairs, start=1)]


def _read_quarters(raw: Mapping[str, Any], res: NormalizedResult, *, quarters: int) -> None:
    """Quarter scores from ``q1_home`` / ``q1_home_score`` or a ``quarters`` list, the first half from
    ``h1_home_score`` (else Q1 + Q2), the regulation score, the final (regulation + overtimes) when the result
    carries none, and the second half (with overtime) as final minus first half."""
    for key, pair in _listed_quarters(raw, quarters):
        res.periods.setdefault(key, pair)
    for i in range(1, quarters + 1):
        h = num(raw, f"q{i}_home", f"q{i}_home_score", f"q{i}_home_points")
        a = num(raw, f"q{i}_away", f"q{i}_away_score", f"q{i}_away_points")
        if h is not None and a is not None:
            res.periods.setdefault(f"q{i}", (h, a))
    h1h, h1a = num(raw, "h1_home_score", "ht_home_score", "h1_home"), num(raw, "h1_away_score", "ht_away_score", "h1_away")
    if h1h is not None and h1a is not None:
        res.periods.setdefault("h1", (h1h, h1a))
    elif "q1" in res.periods and "q2" in res.periods:
        res.periods.setdefault("h1", (res.periods["q1"][0] + res.periods["q2"][0], res.periods["q1"][1] + res.periods["q2"][1]))
    regulation = [res.periods[f"q{i}"] for i in range(1, quarters + 1) if f"q{i}" in res.periods]
    if len(regulation) == quarters:
        reg_h, reg_a = sum(p[0] for p in regulation), sum(p[1] for p in regulation)
        if res.regulation_home is None or res.regulation_away is None:
            res.regulation_home, res.regulation_away = reg_h, reg_a
        if res.home is None or res.away is None:
            overtime = [v for k, v in res.periods.items() if k.startswith("ot")]
            res.home, res.away = reg_h + sum(p[0] for p in overtime), reg_a + sum(p[1] for p in overtime)
    if "h1" in res.periods and res.home is not None and res.away is not None:
        res.periods.setdefault("h2", (res.home - res.periods["h1"][0], res.away - res.periods["h1"][1]))
