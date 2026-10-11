"""
American Football Quantitative Adapter: EPA per Play, Success-Rate Drives & Key Numbers Distribution (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Three independent sub-models vote (fortress pillar 1), each ending in a key-number margin distribution:

1. **EPA per play** (weight 0.45): offensive EPA per play ``pass_rate x pass_epa + (1 - pass_rate) x rush_epa``
   (pass rate 0.58) plus the opposing defence's EPA allowed per play (league 0 = average, so the matchup is additive),
   times the side's plays per game (65): ``points = 22.5 + plays x (EPA_off + EPA_def_allowed_opp)``. EPA per play
   is points above an average offence per snap, so plays x EPA is points above the 22.5 league mean.
2. **Success-rate drives** (0.30): the matchup success rate ``SR_off + (SR_allowed_opp - 0.45)`` (when absent,
   ``SR = 0.45 + 0.5 EPA``, the league-wide SR-EPA slope) drives a series model: a set of downs converts with
   ``c = 1 - (1 - SR)^2``, a drive reaches field-goal range with ``c^2.47`` and the end zone with ``c^4.20``
   (calibrated so a league 0.45 offence scores a TD on 22% and a made FG (84%) on 16% of drives).
   ``points = drives (11.2) x (6.95 P(TD) + 3 P(FG))``. The same drive probabilities price the first-score-method prop.
3. **SRS / Elo** (0.25): the simple rating system ``srs`` or the margin of victory ``points_for - points_against``
   and/or Elo ``(Elo_h - Elo_a) / 25``; with none of them, the EPA margin regressed a third to the mean.

Home field ``home_field_advantage`` (1.8 points) is added to every sub-model's margin.

KEY NUMBERS: a final margin ~ discretised Normal(mu, 13.8) is reweighted on ``|margin|`` so that for a pick'em game
the share of games decided by exactly 3 / 7 / 6 / 10 / 14 / 4 points (both signs) is the empirical 15.1% / 9.4% /
6.1% / 5.8% / 4.9% / 3.8%, a tie (margin 0) is 0.3%, and every other margin keeps its normal shape scaled to the rest
of the mass. The same multiplicative factors apply for a non-zero mu, then the PMF is renormalised; sub-model PMFs
are mixed with the consensus weights so the moneyline agrees with the consensus. A tie voids a 2-way moneyline
(``moneyline_*`` is the tie-void price; ``draw_prob`` ~ 0.003 is reported).

Totals ~ Normal(E, 13.5) (the residual spread of NFL game totals about the closing total; total key numbers 37 / 41 /
44 / 47 are weak and not reweighted). Team totals use ``sd = sqrt((13.5^2 + 13.8^2) / 4)``; the first half takes
half the points with the spread scaled by ``sqrt(0.5)``.

Context: weather (outdoor only; none indoors or under a closed roof): wind above 15 mph costs 0.3 points of total
per mph, precipitation above 2 mm 1.5 points, a temperature below 0 C 1.0 point; The Wire's ``friction`` factor, when
present, scales the total instead. Absences cost ``WAR x 2.1`` points of margin per game (1 point of average
margin ~ 0.48 wins over 17 games): offensive players off the side's own scoring, defenders onto the opponent's, other
positions half each. The crew's accepted flags per game above the league's 13.0 add 0.3 points of total per flag
(defensive penalties extend drives).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, ClassVar

import numpy as np

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext, Weather
from app.domain.sports.quant import IntPMF, LineOutcome, SubModelEstimate, clamp, normal_cdf
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
    side_token,
    split_selection,
)
from app.schemas.bet_types import LegStatus

# ---------------------------------------------------------------- league constants (NFL)
BASELINE_POINTS = 22.5  # league points per team per game
DEFAULT_PASS_RATE = 0.58
DEFAULT_PLAYS = 65.0
LEAGUE_SUCCESS_RATE = 0.45
SR_PER_EPA = 0.5  # league-wide slope of success rate on EPA per play
SERIES_EXPONENT = 2.0  # c = 1 - (1 - SR)^2
SERIES_TO_FG_RANGE = 2.47
SERIES_TO_TD = 4.20
FG_MAKE = 0.84
TD_POINTS = 6.95  # touchdown plus the expected extra point / two-point try
FG_POINTS = 3.0
DRIVES_PER_GAME = 11.2
SAFETY_PER_DRIVE = 0.004
ELO_POINTS_PER_POINT = 25.0  # FiveThirtyEight NFL Elo
SRS_FALLBACK_REGRESSION = 2.0 / 3.0
HOME_FIELD_ADVANTAGE = 1.8
MARGIN_SD = 13.8
TOTAL_SD = 13.5
MARGIN_SPAN = 75
#: Share of NFL games decided by exactly k points (both signs together), the spec's key numbers.
KEY_NUMBER_PROBABILITIES: dict[int, float] = {3: 0.151, 7: 0.094, 6: 0.061, 10: 0.058, 14: 0.049, 4: 0.038}
TIE_PROBABILITY = 0.003
KEY_NUMBERS = frozenset(KEY_NUMBER_PROBABILITIES)
#: Teaser shifts (spec: NFL / NBA teasers move the line 6, 6.5 or 7 points).
TEASER_POINT_SHIFTS: tuple[float, ...] = (6.0, 6.5, 7.0)
POINTS_PER_WAR_PER_GAME = 2.1  # 1 point of average margin ~ 0.48 wins over 17 games
OFFENCE_POSITIONS = frozenset({"qb", "rb", "fb", "wr", "te", "ol", "ot", "og", "lt", "rt", "lg", "rg", "c", "g", "t", "k"})
DEFENCE_POSITIONS = frozenset({"dl", "de", "dt", "nt", "edge", "lb", "ilb", "olb", "mlb", "cb", "s", "fs", "ss", "db"})
WIND_THRESHOLD_MPH = 15.0
POINTS_PER_MPH = 0.3
RAIN_THRESHOLD_MM = 2.0
RAIN_POINTS = 1.5
FREEZING_POINTS = 1.0
LEAGUE_FLAGS_PER_GAME = 13.0
POINTS_PER_EXTRA_FLAG = 0.3
INDOOR_ROOFS = frozenset({"dome", "closed", "indoor", "indoors", "retractable_closed"})
FIRST_HALF_SHARE = 0.5
QUARTER_SHARE = 0.25
SECONDS_PER_QUARTER = 900.0
REGULATION_SECONDS = 3600.0
OVERTIME_SECONDS = 600.0
OT_TIE_PROBABILITY = 0.04  # regular-season overtime ending level (0 in the playoffs)
OT_POSSESSION_EDGE = 0.04  # the team receiving the overtime kickoff
#: Expected points of a 1st-and-10 by yards to the opponent's end zone (smoothed after Burke / nflfastR).
EP_CURVE_YARDS = (1.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 99.0)
EP_CURVE_POINTS = (6.0, 5.4, 4.7, 4.1, 3.4, 2.8, 2.2, 1.7, 1.2, 0.6, 0.0, -0.6)
DOWN_EP_PENALTY: dict[int, float] = {1: 0.0, 2: 0.45, 3: 1.05, 4: 1.7}
EP_PER_YARD_TO_GO = 0.055
FG_RANGE_YARDLINE = 35.0  # a drive inside the opponent's 35 is in field-goal range
YARDS_PER_SERIES_TD = 75.0 / SERIES_TO_TD  # the drive model's league calibration from the own 25
YARDS_PER_SERIES_FG = 40.0 / SERIES_TO_FG_RANGE
SECONDS_PER_SERIES = 22.0  # a hurry-up set of downs (three snaps with the clock stopping on some)
FINISHED_STATUSES = frozenset({"final", "finished", "complete", "completed", "ended", "final_ot"})
SCORE_METHODS: dict[str, str] = {"td": "td", "touchdown": "td", "pass_td": "td", "rush_td": "td", "return_td": "td",
                                 "defensive_td": "td", "pick_six": "td", "fumble_return_td": "td", "fg": "fg",
                                 "field_goal": "fg", "safety": "safety"}


# ---------------------------------------------------------------- distribution helpers
def normal_pmf(mu: float, sigma: float, *, floor: int | None = None) -> IntPMF:
    lo = math.floor(mu - 6.0 * sigma)
    hi = math.ceil(mu + 6.0 * sigma)
    if floor is not None:
        lo = max(lo, floor)
        hi = max(hi, lo + 1)
    return IntPMF.from_normal(mu, sigma, lo, hi)


@lru_cache(maxsize=16)
def key_number_factors(sigma: float = MARGIN_SD) -> tuple[tuple[float, ...], float]:
    """Multiplicative factors on ``|margin|`` (index 0..MARGIN_SPAN) that bring a pick'em Normal(0, sigma) to the
    empirical key-number shares, and the common factor of every non-key margin."""
    base = IntPMF.from_normal(0.0, sigma, -MARGIN_SPAN, MARGIN_SPAN)
    targets = {0: TIE_PROBABILITY, **KEY_NUMBER_PROBABILITIES}
    factors = [1.0] * (MARGIN_SPAN + 1)
    key_base = 0.0
    for k, target in targets.items():
        mass = base.prob_eq(k) + (base.prob_eq(-k) if k else 0.0)
        factors[k] = target / mass
        key_base += mass
    other = (1.0 - sum(targets.values())) / (1.0 - key_base)
    for k in range(MARGIN_SPAN + 1):
        if k not in targets:
            factors[k] = other
    return tuple(factors), other


def key_number_margin(mu: float, sigma: float = MARGIN_SD) -> IntPMF:
    """The NFL final margin: discretised Normal(mu, sigma) reweighted by the key-number factors, renormalised."""
    factors, _ = key_number_factors(round(sigma, 4))
    base = IntPMF.from_normal(mu, sigma, -MARGIN_SPAN, MARGIN_SPAN)
    weights = np.array([factors[abs(int(v))] for v in base.values])
    return IntPMF.from_array(base.probs * weights, base.offset)


def american_odds(decimal_odds: float) -> float:
    """Fair American odds of decimal odds (+150 / -150)."""
    if decimal_odds >= 2.0:
        return 100.0 * (decimal_odds - 1.0)
    return -100.0 / max(decimal_odds - 1.0, 1e-9)


def american_cents(american: float) -> float:
    """American odds on a continuous 'cents' scale where -100 and +100 coincide (-120 -> -20, +110 -> +10)."""
    return american - 100.0 if american >= 100.0 else american + 100.0


def expected_points(yardline_100: float, down: int = 1, distance: float = 10.0) -> float:
    """Expected points of the possession for the team with the ball: the 1st-and-10 curve by field position, less
    the down penalty and 0.055 points per yard to go beyond the down's typical distance (10 on 1st, 7 later)."""
    y = clamp(float(yardline_100), 1.0, 99.0)
    ep = float(np.interp(y, EP_CURVE_YARDS, EP_CURVE_POINTS))
    d = int(clamp(float(down), 1.0, 4.0))
    typical = 10.0 if d == 1 else 7.0
    ep -= DOWN_EP_PENALTY[d] + EP_PER_YARD_TO_GO * (min(float(distance), y) - typical)
    return clamp(ep, -2.5, 7.0)


def remaining_seconds(live_state: Mapping[str, Any], quarter: int) -> float:
    """Seconds left in regulation (quarters 1-4) or in the overtime period (quarter 5+), from ``seconds_remaining``
    or ``seconds_remaining_in_quarter``."""
    in_ot = quarter >= 5
    secs = num(live_state, "seconds_remaining")
    if secs is None:
        in_q = num(live_state, "seconds_remaining_in_quarter")
        if in_ot:
            secs = in_q if in_q is not None else OVERTIME_SECONDS
        else:
            secs = (4 - max(quarter, 1)) * SECONDS_PER_QUARTER + clamp(in_q if in_q is not None else SECONDS_PER_QUARTER, 0.0,
                                                                       SECONDS_PER_QUARTER)
    return clamp(float(secs), 0.0, OVERTIME_SECONDS if in_ot else REGULATION_SECONDS)


def possession_outcomes(yardline_100: float, down: int, distance: float, seconds_left: float) -> tuple[float, float, float]:
    """(P(touchdown), P(field goal), residual points) of the current possession for the team with the ball.

    The EP curve's positive part is split into a touchdown and a field goal in the proportion the league drive model
    gives from this field position (``P(TD) = c^(yards / 17.9)``, FG range ``c^((yards - 35) / 16.2)``), so that
    ``6.95 P(TD) + 3 P(FG) = EP``; each is then scaled by the share of the series it needs that the clock still allows
    (22 seconds a hurry-up series). A negative EP (backed up, the opponent likelier to score next) is kept as a
    continuous residual."""
    ep = expected_points(yardline_100, down, distance)
    y = clamp(float(yardline_100), 1.0, 99.0)
    c = drive_profile(LEAGUE_SUCCESS_RATE).series_conversion
    series_td = y / YARDS_PER_SERIES_TD
    series_fg = max(0.0, y - FG_RANGE_YARDLINE) / YARDS_PER_SERIES_FG
    raw_td = c ** series_td
    raw_fg = FG_MAKE * max(0.0, c ** series_fg - raw_td)
    positive = max(ep, 0.0)
    p_td = positive / (TD_POINTS + FG_POINTS * raw_fg / raw_td)
    p_fg = p_td * raw_fg / raw_td
    scale = min(1.0, 0.97 / max(p_td + p_fg, 1e-12))
    available = max(float(seconds_left), 0.0) / SECONDS_PER_SERIES
    p_td *= scale * (min(1.0, available / series_td) if series_td > 0 else 1.0)
    p_fg *= scale * (min(1.0, available / series_fg) if series_fg > 0 else 1.0)
    return p_td, p_fg, min(ep, 0.0)


def _fmt(line: float, signed: bool) -> str:
    value = round(line * 2.0) / 2.0 + 0.0
    return f"{value:+g}" if signed else f"{value:g}"


@dataclass(frozen=True, slots=True)
class DriveProfile:
    """Per-drive outcome probabilities of one offence in the success-rate sub-model."""

    success_rate: float
    series_conversion: float
    touchdown: float
    field_goal: float

    @property
    def points_per_drive(self) -> float:
        return TD_POINTS * self.touchdown + FG_POINTS * self.field_goal

    def as_dict(self) -> dict[str, float]:
        return {"success_rate": round(self.success_rate, 4), "series_conversion": round(self.series_conversion, 4),
                "p_touchdown": round(self.touchdown, 4), "p_field_goal": round(self.field_goal, 4),
                "points_per_drive": round(self.points_per_drive, 4)}


def drive_profile(success_rate: float) -> DriveProfile:
    sr = clamp(success_rate, 0.2, 0.7)
    c = 1.0 - (1.0 - sr) ** SERIES_EXPONENT
    td = c ** SERIES_TO_TD
    fg = FG_MAKE * max(0.0, c ** SERIES_TO_FG_RANGE - td)
    return DriveProfile(sr, c, td, fg)


@dataclass
class FootballProjection:
    estimates: list[SubModelEstimate]
    weights: dict[str, float]
    sub_margins: dict[str, float]
    margin: IntPMF  # key-number final margin, mixture of the sub-models
    mu: float
    sigma: float
    expected_total: float
    total_sd: float
    home_drive: DriveProfile
    away_drive: DriveProfile
    drives: float
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def team_sd(self) -> float:
        return math.sqrt((self.total_sd ** 2 + self.sigma ** 2) / 4.0)

    def total_pmf(self) -> IntPMF:
        return normal_pmf(self.expected_total, self.total_sd, floor=0)

    def team_pmf(self, side: str) -> IntPMF:
        sign = 1.0 if side == "home" else -1.0
        return normal_pmf((self.expected_total + sign * self.mu) / 2.0, self.team_sd, floor=0)

    def period_margin(self, share: float) -> IntPMF:
        return normal_pmf(self.mu * share, self.sigma * math.sqrt(share))

    def period_total(self, share: float) -> IntPMF:
        return normal_pmf(self.expected_total * share, self.total_sd * math.sqrt(share), floor=0)


class AmericanFootballSportAdapter(BaseSportAdapter):
    """
    Quantitative NFL Engine implementing Expected Points Added (EPA) per play,
    success rates, and empirical mass probability clustering on key margins (3, 7, 10, 14, 6).
    """

    KEY_NUMBER_PROBABILITIES: dict[int, float] = KEY_NUMBER_PROBABILITIES
    TEASER_POINT_SHIFTS: tuple[float, ...] = TEASER_POINT_SHIFTS
    kelly_multiplier = 0.50
    model_family = "EPA & Key Numbers"
    has_draw = False
    odds_api_family = "americanfootball"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner": (MONEYLINE, {}),
        "money_line": (MONEYLINE, {}),
        "point_spread": (SPREAD, {}),
        "handicap": (SPREAD, {}),
        "game_total_over_under": (TOTAL, {}),
        "game_total": (TOTAL, {}),
        "total_points": (TOTAL, {}),
        "team_total_points": (TEAM_TOTAL, {}),
        "team_totals": (TEAM_TOTAL, {}),
        "alternative_spread": (ALT_SPREAD, {}),
        "alternative_total": (ALT_TOTAL, {}),
        "first_half_spread": (PERIOD_SPREAD, {"period": "h1"}),
        "first_half_total": (PERIOD_TOTAL, {"period": "h1"}),
        "first_half_moneyline": (PERIOD_MONEYLINE, {"period": "h1", "level_is_void": True}),
        "first_quarter_spread": (PERIOD_SPREAD, {"period": "q1"}),
        "first_quarter_total": (PERIOD_TOTAL, {"period": "q1"}),
        "next_score": (NEXT_EVENT, {"event_type": "score"}),
        "first_team_to_score": (NEXT_EVENT, {"event_type": "score"}),
        "passing_yards": (PLAYER_PROP, {"stat": "passing_yards"}),
        "rushing_yards": (PLAYER_PROP, {"stat": "rushing_yards"}),
        "receiving_yards": (PLAYER_PROP, {"stat": "receiving_yards"}),
        "receptions": (PLAYER_PROP, {"stat": "receptions"}),
        "passing_touchdowns": (PLAYER_PROP, {"stat": "passing_touchdowns"}),
        "interceptions_thrown": (PLAYER_PROP, {"stat": "interceptions"}),
        "first_score": ("first_score_method", {}),
    }
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE, PERIOD_SPREAD,
                                   PERIOD_TOTAL, PLAYER_PROP, NEXT_EVENT})
    SPECIAL_MARKETS = frozenset({"first_score_method"})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "teaser", "pleaser",
                           "round_robin", "if_bet", "double_action", "futures", "player_props", "game_props", "live_next_event",
                           "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "american_football"

    @property
    def display_name(self) -> str:
        return "American Football"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "moneyline",
            "point_spread",
            "game_total_over_under",
            "first_half_spread",
            "team_total_points",
            "first_half_total",
            "first_score_method",
            "alternative_spread",
            "passing_yards",
        ]

    # ------------------------------------------------------------------ inputs
    @staticmethod
    def offensive_epa(stats: Mapping[str, Any]) -> float:
        direct = num(stats, "off_epa", "epa_per_play", "off_epa_per_play")
        if direct is not None:
            return direct
        rate = clamp(float(num(stats, "pass_rate", default=DEFAULT_PASS_RATE)), 0.0, 1.0)  # type: ignore[arg-type]
        return rate * float(num(stats, "pass_epa", default=0.0)) + (1.0 - rate) * float(num(stats, "rush_epa", default=0.0))  # type: ignore[arg-type]

    @staticmethod
    def defensive_epa_allowed(defence: Mapping[str, Any], offence: Mapping[str, Any]) -> float:
        """EPA per play the defence allows, weighted by the attacking offence's pass rate (league 0)."""
        direct = num(defence, "def_epa_allowed", "def_epa_per_play")
        if direct is not None:
            return direct
        rate = clamp(float(num(offence, "pass_rate", default=DEFAULT_PASS_RATE)), 0.0, 1.0)  # type: ignore[arg-type]
        return (rate * float(num(defence, "def_pass_epa_allowed", default=0.0))  # type: ignore[arg-type]
                + (1.0 - rate) * float(num(defence, "def_rush_epa_allowed", default=0.0)))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ context
    def weather_total_delta(self, weather: Weather | None, ctx: MatchContext, raw_total: float) -> tuple[float, dict[str, Any]]:
        """Points of total the conditions add (negative) and what was applied."""
        if weather is None:
            return 0.0, {}
        if weather.indoor or ctx.text("roof", "") in INDOOR_ROOFS:
            return 0.0, {"weather": "indoor"}
        if weather.friction is not None:
            mult = clamp(weather.friction, 0.6, 1.2)
            return raw_total * (mult - 1.0), {"weather_friction_multiplier": round(mult, 4)}
        delta = 0.0
        applied: dict[str, Any] = {}
        mph = weather.wind_mph
        if mph is not None and mph > WIND_THRESHOLD_MPH:
            wind = -POINTS_PER_MPH * (mph - WIND_THRESHOLD_MPH)
            delta += wind
            applied["weather_wind_points"] = round(wind, 3)
        if weather.precipitation_mm is not None and weather.precipitation_mm > RAIN_THRESHOLD_MM:
            delta -= RAIN_POINTS
            applied["weather_precipitation_points"] = -RAIN_POINTS
        if weather.temperature_c is not None and weather.temperature_c < 0.0:
            delta -= FREEZING_POINTS
            applied["weather_cold_points"] = -FREEZING_POINTS
        return delta, applied

    def context_adjustments(self, ctx: MatchContext) -> tuple[float, float, float, dict[str, Any]]:
        """(home points delta, away points delta, total delta from officials, recorded adjustments)."""
        d_home, d_away, d_total = 0.0, 0.0, 0.0
        adjustments: dict[str, Any] = {}
        if ctx.absences:
            for side in ("home", "away"):
                offence = ctx.war_lost(side, OFFENCE_POSITIONS)
                defence = ctx.war_lost(side, DEFENCE_POSITIONS)
                other = ctx.war_lost(side) - offence - defence
                own = (offence + 0.5 * other) * POINTS_PER_WAR_PER_GAME
                opp = (defence + 0.5 * other) * POINTS_PER_WAR_PER_GAME
                if side == "home":
                    d_home, d_away = d_home - own, d_away + opp
                else:
                    d_away, d_home = d_away - own, d_home + opp
                adjustments[f"{side}_absence_margin_points"] = round(-(own + opp), 4)
        if ctx.officials is not None:
            flags = ctx.officials.rate("flags_per_game", "penalties_per_game", "accepted_penalties_per_game")
            if flags is not None:
                d_total = POINTS_PER_EXTRA_FLAG * (flags - LEAGUE_FLAGS_PER_GAME)
                adjustments["officials_flag_points_delta"] = round(d_total, 4)
                adjustments["officials_flags_per_game"] = flags
        return d_home, d_away, d_total, adjustments

    # ------------------------------------------------------------------ the sub-models
    def project(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> FootballProjection:
        hfa = 0.0 if ctx.get("neutral_site") is True else ctx.number("home_field_advantage", HOME_FIELD_ADVANTAGE)
        sigma = clamp(ctx.number("margin_sd", MARGIN_SD), 8.0, 20.0)
        total_sd = clamp(ctx.number("total_sd", TOTAL_SD), 6.0, 20.0)
        d_home, d_away, d_total_off, adjustments = self.context_adjustments(ctx)

        epa_h = self.offensive_epa(home_stats) + self.defensive_epa_allowed(away_stats, home_stats)
        epa_a = self.offensive_epa(away_stats) + self.defensive_epa_allowed(home_stats, away_stats)
        plays_h = float(num(home_stats, "plays_per_game", "plays", default=DEFAULT_PLAYS))  # type: ignore[arg-type]
        plays_a = float(num(away_stats, "plays_per_game", "plays", default=DEFAULT_PLAYS))  # type: ignore[arg-type]

        weather_applied: dict[str, Any] = {}

        def finish(raw_home: float, raw_away: float) -> tuple[float, float]:
            """Home field, conditions, absences and officials on a sub-model's raw team points."""
            raw_total = raw_home + raw_away
            w_delta, applied = self.weather_total_delta(ctx.weather, ctx, raw_total)
            weather_applied.update(applied)
            share = raw_home / raw_total if raw_total > 0 else 0.5
            extra = w_delta + d_total_off
            home = raw_home + hfa / 2.0 + d_home + extra * share
            away = raw_away - hfa / 2.0 + d_away + extra * (1.0 - share)
            return max(home, 0.0), max(away, 0.0)

        # 1. EPA per play
        ph, pa = finish(BASELINE_POINTS + plays_h * epa_h, BASELINE_POINTS + plays_a * epa_a)
        sub: dict[str, tuple[float, float | None]] = {"epa_per_play": (ph - pa, ph + pa)}

        # 2. success-rate drives
        drive_h, drive_a, drives = self.drive_profiles(home_stats, away_stats)
        sh, sa = finish(drives * drive_h.points_per_drive, drives * drive_a.points_per_drive)
        sub["success_rate_drive"] = (sh - sa, sh + sa)

        # 3. SRS / Elo
        neutral, srs_total = self.rating_margin(home_stats, away_stats)
        srs_source = "ratings"
        if neutral is None:
            neutral = SRS_FALLBACK_REGRESSION * (plays_h * epa_h - plays_a * epa_a)
            srs_source = "epa_regressed"
        if srs_total is not None:
            rh, ra = finish(srs_total / 2.0 + neutral / 2.0, srs_total / 2.0 - neutral / 2.0)
            sub["srs_elo"] = (rh - ra, rh + ra)
        else:
            rh, ra = finish(BASELINE_POINTS + neutral / 2.0, BASELINE_POINTS - neutral / 2.0)
            sub["srs_elo"] = (rh - ra, None)

        defaults = {"epa_per_play": 0.45, "success_rate_drive": 0.30, "srs_elo": 0.25}
        estimates: list[SubModelEstimate] = []
        pmfs: dict[str, IntPMF] = {}
        for name, (mu_i, total_i) in sub.items():
            pmf = key_number_margin(mu_i, sigma)
            pmfs[name] = pmf
            estimates.append(SubModelEstimate(name, pmf.prob_gt(0.0), pmf.prob_lt(0.0), pmf.prob_eq(0), total_i, defaults[name]))
        weights = {e.name: max(0.0, float(ctx.model_weights.get(e.name, e.weight))) for e in estimates}
        if sum(weights.values()) <= 0:
            weights = dict(defaults)
        names = list(sub)
        margin = IntPMF.mix([(pmfs[n], weights[n]) for n in names])
        total_w = sum(weights[n] for n in names)
        mu = sum(sub[n][0] * weights[n] for n in names) / total_w
        voting = [(sub[n][1], weights[n]) for n in names if sub[n][1] is not None and weights[n] > 0]
        if not voting:
            voting = [(sub["epa_per_play"][1], 1.0)]
        expected_total = sum(t * w for t, w in voting) / sum(w for _, w in voting)  # type: ignore[operator]

        abs3 = margin.prob_eq(3) + margin.prob_eq(-3)
        abs7 = margin.prob_eq(7) + margin.prob_eq(-7)
        metrics: dict[str, Any] = {
            "projected_spread": round(-mu, 2),  # negative: home favourite
            "home_projected_pts": round((expected_total + mu) / 2.0, 2),
            "away_projected_pts": round((expected_total - mu) / 2.0, 2),
            "home_field_advantage": hfa,
            "home_epa_per_play_matchup": round(epa_h, 4),
            "away_epa_per_play_matchup": round(epa_a, 4),
            "home_drive": drive_h.as_dict(),
            "away_drive": drive_a.as_dict(),
            "drives_per_game": drives,
            "srs_source": srs_source,
            "sub_model_margins": {k: round(v[0], 3) for k, v in sub.items()},
            "key_number_cluster_3": KEY_NUMBER_PROBABILITIES[3],
            "key_number_cluster_7": KEY_NUMBER_PROBABILITIES[7],
            "model_p_margin_3": round(abs3, 4),
            "model_p_margin_7": round(abs7, 4),
            "model_p_tie": round(margin.prob_eq(0), 4),
            "margin_sd": sigma,
            "total_sd": total_sd,
            **adjustments,
            **weather_applied,
        }
        return FootballProjection(estimates, weights, {k: v[0] for k, v in sub.items()}, margin, mu, sigma, expected_total, total_sd,
                                  drive_h, drive_a, drives, metrics)

    def drive_profiles(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any]) -> tuple[DriveProfile, DriveProfile, float]:
        """Each offence's drive profile at the matchup success rate ``SR_off + SR_allowed_opp - 0.45``, and the drives
        per game (the two teams' mean, 11.2 by default)."""
        sr_h = self._success_rate(home_stats, "success_rate", "off_success_rate", self.offensive_epa(home_stats))
        sr_a = self._success_rate(away_stats, "success_rate", "off_success_rate", self.offensive_epa(away_stats))
        sr_allow_h = self._success_rate(home_stats, "def_success_rate_allowed", "def_success_rate",
                                        self.defensive_epa_allowed(home_stats, away_stats))
        sr_allow_a = self._success_rate(away_stats, "def_success_rate_allowed", "def_success_rate",
                                        self.defensive_epa_allowed(away_stats, home_stats))
        drive_h = drive_profile(sr_h + sr_allow_a - LEAGUE_SUCCESS_RATE)
        drive_a = drive_profile(sr_a + sr_allow_h - LEAGUE_SUCCESS_RATE)
        drives = (float(num(home_stats, "drives_per_game", default=DRIVES_PER_GAME))  # type: ignore[arg-type]
                  + float(num(away_stats, "drives_per_game", default=DRIVES_PER_GAME))) / 2.0  # type: ignore[arg-type]
        return drive_h, drive_a, drives

    @staticmethod
    def rating_margin(home_stats: Mapping[str, Any], away_stats: Mapping[str, Any]) -> tuple[float | None, float | None]:
        """(neutral-site margin, expected total) of the SRS / Elo sub-model: the mean of the SRS gap (else the margin
        of victory gap ``(PF - PA)_h - (PF - PA)_a``) and the Elo gap / 25; the total from points for and against."""
        ratings: list[float] = []
        srs_h, srs_a = num(home_stats, "srs"), num(away_stats, "srs")
        if srs_h is not None and srs_a is not None:
            ratings.append(srs_h - srs_a)
        pf_h, pa_h = num(home_stats, "points_for"), num(home_stats, "points_against")
        pf_a, pa_a = num(away_stats, "points_for"), num(away_stats, "points_against")
        total = None
        if pf_h is not None and pa_h is not None and pf_a is not None and pa_a is not None:
            total = (pf_h + pa_a) / 2.0 + (pf_a + pa_h) / 2.0
            if not ratings:
                ratings.append((pf_h - pa_h) - (pf_a - pa_a))
        elo_h, elo_a = num(home_stats, "elo"), num(away_stats, "elo")
        if elo_h is not None and elo_a is not None:
            ratings.append((elo_h - elo_a) / ELO_POINTS_PER_POINT)
        return (sum(ratings) / len(ratings) if ratings else None), total

    @staticmethod
    def _success_rate(stats: Mapping[str, Any], key: str, alt: str, epa: float) -> float:
        value = num(stats, key, alt)
        if value is None:
            return LEAGUE_SUCCESS_RATE + SR_PER_EPA * epa
        return value / 100.0 if value > 1.0 else value

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
        markets = self.price_markets(proj, ctx)
        return self._assemble(consensus, ctx, expected_total=proj.expected_total, metrics=proj.metrics, markets=markets)

    def price_markets(self, proj: FootballProjection, ctx: MatchContext) -> dict[str, float]:
        markets: dict[str, float] = {}
        margin, away_margin = proj.margin, proj.margin.negate()
        markets["moneyline_home"] = margin.line_outcome(0.0, over=True).effective_probability  # a tie voids the 2-way line
        markets["moneyline_away"] = margin.line_outcome(0.0, over=False).effective_probability
        markets["tie"] = margin.prob_eq(0)
        for k in sorted(KEY_NUMBERS):
            markets[f"margin_exactly_{k}"] = margin.prob_eq(k) + margin.prob_eq(-k)

        base = round(-margin.mean * 2.0) / 2.0
        lines = {base + d for d in (-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0)}
        lines |= {s * k for s in (-1.0, 1.0) for k in (2.5, 3.0, 3.5, 6.5, 7.0, 7.5, 9.5, 10.0, 10.5, 13.5, 14.0, 14.5)}
        for line in sorted(lines):
            markets[f"spread_home_{_fmt(line, True)}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"spread_away_{_fmt(-line, True)}"] = away_margin.line_outcome(line, over=True).effective_probability
        for d in (-10.0, -7.0, 7.0, 10.0):
            line = base + d
            markets[f"alt_spread_home_{_fmt(line, True)}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"alt_spread_away_{_fmt(-line, True)}"] = away_margin.line_outcome(line, over=True).effective_probability
        for shift in TEASER_POINT_SHIFTS:
            markets[f"teaser_{shift:g}_home_{_fmt(base + shift, True)}"] = margin.line_outcome(-(base + shift), over=True).effective_probability
            markets[f"teaser_{shift:g}_away_{_fmt(-base + shift, True)}"] = away_margin.line_outcome(base - shift, over=True).effective_probability

        total = proj.total_pmf()
        tbase = round(proj.expected_total * 2.0) / 2.0
        for d in (-7.0, -3.5, -3.0, -1.0, -0.5, 0.0, 0.5, 1.0, 3.0, 3.5, 7.0):
            line = tbase + d
            markets[f"total_over_{_fmt(line, False)}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"total_under_{_fmt(line, False)}"] = total.line_outcome(line, over=False).effective_probability
        for d in (-10.0, 10.0):
            line = tbase + d
            markets[f"alt_total_over_{_fmt(line, False)}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"alt_total_under_{_fmt(line, False)}"] = total.line_outcome(line, over=False).effective_probability

        for side in ("home", "away"):
            team = proj.team_pmf(side)
            tb = math.floor(team.mean) + 0.5
            for d in (-3.0, -1.0, 0.0, 1.0, 3.0):
                line = tb + d
                markets[f"team_total_{side}_over_{_fmt(line, False)}"] = team.line_outcome(line, over=True).effective_probability
                markets[f"team_total_{side}_under_{_fmt(line, False)}"] = team.line_outcome(line, over=False).effective_probability

        h1m, h1t = proj.period_margin(FIRST_HALF_SHARE), proj.period_total(FIRST_HALF_SHARE)
        markets["h1_home"], markets["h1_tie"], markets["h1_away"] = h1m.prob_gt(0.0), h1m.prob_eq(0), h1m.prob_lt(0.0)
        markets["h1_home_2way"] = h1m.line_outcome(0.0, over=True).effective_probability
        markets["h1_away_2way"] = h1m.line_outcome(0.0, over=False).effective_probability
        hbase = round(-h1m.mean * 2.0) / 2.0
        for d in (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5):
            line = hbase + d
            markets[f"h1_spread_home_{_fmt(line, True)}"] = h1m.line_outcome(-line, over=True).effective_probability
            markets[f"h1_spread_away_{_fmt(-line, True)}"] = h1m.negate().line_outcome(line, over=True).effective_probability
        htb = math.floor(h1t.mean) + 0.5
        for d in (-1.0, 0.0, 1.0):
            line = htb + d
            markets[f"h1_total_over_{_fmt(line, False)}"] = h1t.line_outcome(line, over=True).effective_probability
            markets[f"h1_total_under_{_fmt(line, False)}"] = h1t.line_outcome(line, over=False).effective_probability

        for key, p in self.first_score_distribution(proj, ctx).items():
            markets[f"first_score_{key}"] = p
        return markets

    def first_score_distribution(self, proj: FootballProjection, ctx: MatchContext) -> dict[str, float]:
        """Who scores first and how: alternating drives from the success-rate drive profiles (each drive a TD, a
        made FG, a safety conceded or nothing), over ``drives`` possessions a side, averaged over who receives."""
        n = max(1, round(proj.drives))
        h, a = proj.home_drive, proj.away_drive
        out = {"home_td": 0.0, "home_fg": 0.0, "home_safety": 0.0, "away_td": 0.0, "away_fg": 0.0, "away_safety": 0.0}
        receives = ctx.get("home_receives_first")
        orders = [(1.0, "home")] if receives is True else [(1.0, "away")] if receives is False else [(0.5, "home"), (0.5, "away")]
        none_total = 0.0
        for weight, first in orders:
            x, y = (h, a) if first == "home" else (a, h)
            xs, ys = first, ("away" if first == "home" else "home")
            nx = 1.0 - x.touchdown - x.field_goal - SAFETY_PER_DRIVE
            ny = 1.0 - y.touchdown - y.field_goal - SAFETY_PER_DRIVE
            cycle = nx * ny
            geo = (1.0 - cycle ** n) / (1.0 - cycle)
            out[f"{xs}_td"] += weight * x.touchdown * geo
            out[f"{xs}_fg"] += weight * x.field_goal * geo
            out[f"{ys}_safety"] += weight * SAFETY_PER_DRIVE * geo
            out[f"{ys}_td"] += weight * nx * y.touchdown * geo
            out[f"{ys}_fg"] += weight * nx * y.field_goal * geo
            out[f"{xs}_safety"] += weight * nx * SAFETY_PER_DRIVE * geo
            none_total += weight * cycle ** n
        out["td"] = out["home_td"] + out["away_td"]
        out["fg"] = out["home_fg"] + out["away_fg"]
        out["safety"] = out["home_safety"] + out["away_safety"]
        out["home"] = out["home_td"] + out["home_fg"] + out["home_safety"]
        out["away"] = out["away_td"] + out["away_fg"] + out["away_safety"]
        out["none"] = none_total
        return out

    def cover_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        side: str,
        line: float,
        context: dict[str, Any] | None = None,
        market: str = "spread",
    ) -> LineOutcome:
        """One leg's full outcome on the key-number margin for the teaser and pleaser pricer.

        ``market`` ``"spread"``: ``side`` home/away with ``line`` the handicap on that side; ``"total"``: ``side``
        over/under; ``"team_total"``: ``side`` like ``"home_over"``; ``"first_half_spread"`` / ``"first_half_total"``."""
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        kind = norm(market)
        if kind in ("spread", "point_spread", "alt_spread", "handicap"):
            return _handicap_outcome(proj.margin, side, line)
        if kind in ("total", "game_total", "alt_total", "game_total_over_under"):
            return proj.total_pmf().line_outcome(line, over=over_under(side))
        if kind in ("team_total", "team_totals", "team_total_points"):
            return proj.team_pmf(_side_of_selection(side)).line_outcome(line, over=over_under(side))
        if kind in ("first_half_spread", "h1_spread"):
            return _handicap_outcome(proj.period_margin(FIRST_HALF_SHARE), side, line)
        if kind in ("first_half_total", "h1_total"):
            return proj.period_total(FIRST_HALF_SHARE).line_outcome(line, over=over_under(side))
        raise ValueError(f"american football cover_probability has no market {market!r}")

    def key_number_crossing_cents(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        side: str,
        line_from: float,
        line_to: float,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The value of moving a spread from ``line_from`` to ``line_to`` (e.g. +3.5 -> +2.5): both lines' cover
        outcomes, their fair American odds and the difference in cents (-100 and +100 coincide), plus the key
        numbers crossed and the margin mass between the two lines."""
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        s = _side_of_selection(side)
        pmf = proj.margin if s == "home" else proj.margin.negate()
        out_from, out_to = pmf.line_outcome(-line_from, over=True), pmf.line_outcome(-line_to, over=True)
        am_from, am_to = american_odds(out_from.fair_odds), american_odds(out_to.fair_odds)
        lo, hi = sorted((-line_from, -line_to))
        crossed = [int(v) for v in pmf.values if lo - 1e-9 <= v <= hi + 1e-9]
        return {
            "side": s,
            "line_from": line_from,
            "line_to": line_to,
            "cover_from": out_from.as_dict(),
            "cover_to": out_to.as_dict(),
            "probability_from": out_from.effective_probability,
            "probability_to": out_to.effective_probability,
            "fair_american_from": round(am_from, 1),
            "fair_american_to": round(am_to, 1),
            "cents": round(abs(american_cents(am_to) - american_cents(am_from)), 1),
            "key_numbers_crossed": sorted({abs(m) for m in crossed if abs(m) in KEY_NUMBERS}),
            "margin_mass_crossed": round(sum(pmf.prob_eq(m) for m in crossed), 6),
        }

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live win probability.

        ``live_state`` keys: ``quarter`` (1-4, 5 overtime), ``seconds_remaining`` (left in regulation, or in the
        overtime period) or ``seconds_remaining_in_quarter``, ``home_score``, ``away_score``, ``possession``
        ("home" / "away"), ``yardline_100`` (yards to the opponent's end zone), ``down``, ``distance``, optional
        ``status`` ("final" ends the game).

        The rest of the game is ``Normal(mu_pre f, 13.8 sqrt(f))`` (``f`` the remaining share of regulation)
        convolved with the current possession's points: 7 / 3 / 0 with the touchdown and field-goal chances that
        reproduce its expected points on the EP curve (field position, down, distance), cut by what the clock allows
        (``possession_outcomes``), signed for the home side; a level score at the end of
        regulation goes to overtime (~50/50 by strength, +4% for the receiving side, 4% regular-season tie)."""
        ctx = MatchContext.parse(context)
        proj = self.project(home_stats, away_stats, ctx)
        mu_pre, sigma = proj.mu, proj.sigma
        home_score = float(num(live_state, "home_score", "home_points", default=0.0))  # type: ignore[arg-type]
        away_score = float(num(live_state, "away_score", "away_points", default=0.0))  # type: ignore[arg-type]
        lead = round(home_score - away_score)
        quarter = int(num(live_state, "quarter", "period", default=1) or 1)
        status = norm(live_state.get("status"))
        possession = norm(live_state.get("possession"))
        playoff = bool(ctx.get("playoff", False))
        ot_tie = 0.0 if playoff else OT_TIE_PROBABILITY
        ot_share = OVERTIME_SECONDS / REGULATION_SECONDS
        q_ot = normal_cdf(mu_pre * ot_share / (sigma * math.sqrt(ot_share)))
        detail: dict[str, Any] = {"lead": lead, "quarter": quarter, "pregame_margin": round(mu_pre, 3)}

        in_ot = quarter >= 5
        remaining = remaining_seconds(live_state, quarter)
        f = remaining / REGULATION_SECONDS
        detail["remaining_fraction"] = round(f, 5)

        if status in FINISHED_STATUSES or remaining <= 0.0:
            if lead > 0:
                return LiveWinProbability(1.0, 0.0, 0.0, {**detail, "state": "final"})
            if lead < 0:
                return LiveWinProbability(0.0, 1.0, 0.0, {**detail, "state": "final"})
            if status in FINISHED_STATUSES or (in_ot and not playoff):
                return LiveWinProbability(0.0, 0.0, 1.0, {**detail, "state": "final_tie"})
            edge = OT_POSSESSION_EDGE if possession == "home" else -OT_POSSESSION_EDGE if possession == "away" else 0.0
            p_home = clamp(q_ot + edge, 0.01, 0.99)
            return LiveWinProbability((1.0 - ot_tie) * p_home, (1.0 - ot_tie) * (1.0 - p_home), ot_tie,
                                      {**detail, "state": "overtime_next"})

        ep, p_td, p_fg, residual = 0.0, 0.0, 0.0, 0.0
        if possession in ("home", "away"):
            y = float(num(live_state, "yardline_100", default=75.0))  # type: ignore[arg-type]
            down = int(num(live_state, "down", default=1) or 1)
            distance = float(num(live_state, "distance", default=10.0))  # type: ignore[arg-type]
            ep = expected_points(y, down, distance)
            p_td, p_fg, residual = possession_outcomes(y, down, distance, remaining)
        sign = -1.0 if possession == "away" else 1.0
        mu_rem = mu_pre * f + sign * residual
        sd_rem = max(sigma * math.sqrt(f), 0.5)
        drive = IntPMF.from_array([1.0 - p_td - p_fg, 0.0, 0.0, p_fg, 0.0, 0.0, 0.0, p_td], 0)
        if possession == "away":
            drive = drive.negate()
        rest = normal_pmf(mu_rem, sd_rem).convolve(drive).shift(lead)
        level = rest.prob_eq(0)
        if in_ot:
            home, away, draw = rest.prob_gt(0.0), rest.prob_lt(0.0), level * (0.0 if playoff else 1.0)
            if playoff:
                home, away = home + level * q_ot, away + level * (1.0 - q_ot)
        else:
            home = rest.prob_gt(0.0) + level * (1.0 - ot_tie) * q_ot
            away = rest.prob_lt(0.0) + level * (1.0 - ot_tie) * (1.0 - q_ot)
            draw = level * ot_tie
        total = home + away + draw
        return LiveWinProbability(home / total, away / total, draw / total,
                                  {**detail, "possession_expected_points": round(sign * ep, 3), "possession_p_touchdown": round(p_td, 4),
                                   "possession_p_field_goal": round(p_fg, 4), "remaining_margin_mean": round(mu_rem, 3),
                                   "remaining_margin_sd": round(sd_rem, 3), "p_level": round(level, 4)})

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        res = normalize_basic(match_result, home_keys=("home_score", "home_points"), away_keys=("away_score", "away_points"))
        for i in range(1, 5):
            h = num(match_result, f"q{i}_home", f"q{i}_home_score")
            a = num(match_result, f"q{i}_away", f"q{i}_away_score")
            if h is not None and a is not None:
                res.periods.setdefault(f"q{i}", (h, a))
        h1h, h1a = num(match_result, "h1_home_score", "ht_home_score", "h1_home"), num(match_result, "h1_away_score", "ht_away_score", "h1_away")
        if h1h is not None and h1a is not None:
            res.periods.setdefault("h1", (h1h, h1a))
        elif "q1" in res.periods and "q2" in res.periods:
            q1, q2 = res.periods["q1"], res.periods["q2"]
            res.periods.setdefault("h1", (q1[0] + q2[0], q1[1] + q2[1]))
        if "h1" in res.periods and res.home is not None and res.away is not None:
            res.periods.setdefault("h2", (res.home - res.periods["h1"][0], res.away - res.periods["h1"][1]))
        method = match_result.get("first_score_method")
        if method:
            res.extras["first_score_method"] = str(method)
            if match_result.get("first_score_team"):
                res.extras["first_score_team"] = str(match_result["first_score_team"])
        return res

    def first_score(self, result: NormalizedResult) -> tuple[str | None, str | None]:
        """(team, method) of the game's first score; (None, "none") for a scoreless game."""
        raw_method = result.extras.get("first_score_method")
        if raw_method:
            team, method = _parse_score_selection(str(raw_method), result)
            if team is None and result.extras.get("first_score_team"):
                team = side_token(norm(result.extras["first_score_team"]), result)
            if method is None:
                raise SettlementInputError(f"first score method {raw_method!r} is not a TD, FG or safety")
            return team, method
        scores = []
        for i, e in enumerate(result.events):
            kind = norm(e.get("type"))
            method_text = norm(e.get("method")) or kind
            if kind == "score" or kind in SCORE_METHODS:
                scores.append((float(e.get("t", e.get("minute", i))), i, e, method_text))
        if scores:
            scores.sort(key=lambda s: (s[0], s[1]))
            _, _, event, method_text = scores[0]
            method = SCORE_METHODS.get(method_text)
            if method is None:
                raise SettlementInputError(f"first score method {method_text!r} is not a TD, FG or safety")
            return side_token(norm(event.get("team")), result), method
        if result.home == 0 and result.away == 0:
            return None, "none"
        raise SettlementInputError("the result names no first score")

    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market == "first_score_method":
            team, method = self.first_score(result)
            if norm(selection) in ("none", "no_score", "no_scoring"):
                return LegStatus.WON if method == "none" else LegStatus.LOST
            want_team, want_method = _parse_score_selection(selection, result)
            if want_method is None:
                raise SettlementInputError(f"selection {selection!r} names no scoring method")
            if want_method != method:
                return LegStatus.LOST
            if want_team is not None and want_team != team:
                return LegStatus.LOST
            return LegStatus.WON
        return super().settle_special(market, result, raw, selection, line, params)


# ---------------------------------------------------------------- module helpers
def _handicap_outcome(margin: IntPMF, side: str, line: float) -> LineOutcome:
    pmf = margin if _side_of_selection(side) == "home" else margin.negate()
    return pmf.line_outcome(-line, over=True)


def _side_of_selection(selection: str, result: NormalizedResult | None = None) -> str:
    for token in [selection, *split_selection(selection)]:
        side = side_token(token, result)
        if side in ("home", "away"):
            return side
    raise SettlementInputError(f"selection {selection!r} names no side")


def _parse_score_selection(selection: str, result: NormalizedResult | None) -> tuple[str | None, str | None]:
    """``"home_td"``, ``"touchdown"``, ``"away field goal"``, ``"safety"`` -> (team or None, td / fg / safety)."""
    text = norm(selection)
    tokens = split_selection(selection)
    team = None
    for token in tokens:
        side = side_token(token, result)
        if side in ("home", "away"):
            team = side
            break
    if "safety" in tokens:
        return team, "safety"
    if "field_goal" in text or "fieldgoal" in text or "fg" in tokens:
        return team, "fg"
    if "touchdown" in text or "td" in tokens or text.endswith("_td"):
        return team, "td"
    return team, SCORE_METHODS.get(text)
