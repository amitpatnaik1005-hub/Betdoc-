"""
Esports Quantitative Adapter: HLTV Rating, Exact Round Markov & Map Veto Simulation (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

"home" is team A (the higher seed: it starts the veto and picks the side of game 1), "away" is team B. Titles via
``ctx.game``: ``cs2`` (default), ``valorant``, ``lol``, ``dota2``; series via ``ctx.series_format`` bo1 / bo3 (default) / bo5.

**Round model (CS2 / Valorant).** ``P(round) = logistic(k (Rating_A - Rating_B) +/- logit(c_map))`` with HLTV 2.0 /
VLR ``team_rating`` (map-specific ``map_ratings`` when given), ``k = 1.5`` per rating point (``ctx.round_k``; a 0.20
rating gap is a 57.4% round edge, about 70% per map) and the map's CT (defence) round share ``c_map`` added when A
defends, subtracted when A attacks. CS2 CT shares: nuke 0.56, ancient 0.55, train 0.55, overpass 0.53, inferno 0.52,
mirage 0.51, vertigo 0.50, dust2 0.49, anubis 0.46 (T-sided: the spec's 54% T bias), extras cache 0.50 and cobblestone
0.51; active duty pool ancient, anubis, dust2, inferno, mirage, nuke, train. Valorant defence shares: split 0.53,
ascent 0.52, pearl 0.52, bind 0.51, haven / breeze / sunset / corrode 0.50, abyss / icebox / lotus 0.49, fracture 0.47;
pool abyss, ascent, bind, haven, icebox, lotus, sunset. ``ctx.map_pool`` overrides a pool.

**Exact map Markov.** MR12: first to 13, sides switch after 12 rounds. CS2 at 12-12: MR3 overtime periods (first to 4
of 6, sides switch after 3; a period starts on the side the previous one ended on), the 3-3 repetition solved as a
geometric series ``V_even = (w_e + t_e w_o) / (1 - t_e t_o)``. Valorant at 12-12: win by two, sides alternating every
round, ``V = q_1 q_2 / (q_1 q_2 + (1 - q_1)(1 - q_2))``.

**MOBAs.** ``P(game) = logistic(4.0 (Rating_A - Rating_B) +/- logit(side))`` (``ctx.game_k``) with the LoL blue-side
53.8% and the Dota 2 Radiant 52% edge; the higher seed takes the better side in game 1, the loser of each game chooses
(the better side) for the next, so the series is an exact DP over (wins, chooser).

**Map veto (CS2 / Valorant, 7-map pool).** BO3 ``A ban, B ban, A pick, B pick, A ban, B ban, decider``; BO1 six bans
then the decider; BO5 ``ban, ban, pick, pick, pick, pick, decider``. Greedy and documented: each team bans the map with
its opponent's largest advantage and picks its own largest advantage (side-neutral map win probability). On a team's
pick the opponent chooses the starting side; the decider's side goes to a knife round (the mean of both starts). The
series probability is exact over that ordered, non-identical map sequence.

Four sub-models vote (fortress pillar 1):

1. **rating_round_markov** (0.30): every map side-neutral from ``team_rating`` alone, maps iid.
2. **map_veto_simulation** (0.40): the ordered veto sequence with map ratings and side bias (MOBA: the side DP).
3. **ranking_elo_logistic** (0.15): ``logistic(dElo ln10 / 400)`` per map from ``elo``; else Bradley-Terry on
   ``world_ranking_points`` (``dElo = 400 log10(points_A / points_B)``); else ``dElo = 1150 dRating`` (the slope at
   which a 0.20 rating gap gives the round Markov's 79% map, so the derived Elo is a consistent, independent link).
4. **recent_form** (0.15): log5 of ``recent_map_win_pct`` shrunk toward the rating model by ``n / (n + 30)``
   (``recent_maps``, default 20); without form data the rating model regressed 25% toward 0.5 (form is noisy).

Markets (series score distribution calibrated onto the consensus by scaling each team's scores): series winner, correct
series score, map handicaps -1.5 / +1.5 (and -2.5 / +2.5 in BO5), total maps 2.5 (BO3) / 3.5, 4.5 (BO5), the winner
of each map (given it is played), the map-1 pistol round (``logistic(k / 3 dRating +/- logit(c_map) / 2)``, blended 50/50
with a log5 of ``pistol_win_pct``) and first blood (LoL / Dota 2 / Valorant: ``logistic(0.45 x game logit)``, blended
50/50 with a log5 of ``first_blood_rate``).

Context: absences (pillar 4) are stand-ins: each missing starter costs 0.035 rating (status-weighted; ``0.10 x impact``
when The Wire supplies it) on every map; ``ctx.online`` (no LAN) scales ``k`` by 0.90. Weather and officials do not
apply.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult, LiveWinProbability
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import IntPMF, SubModelEstimate, clamp, logistic
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
    side_token,
)
from app.schemas.bet_types import LegStatus

CS2_CT_SHARE: dict[str, float] = {
    "ancient": 0.55, "anubis": 0.46, "dust2": 0.49, "inferno": 0.52, "mirage": 0.51, "nuke": 0.56, "train": 0.55,
    "overpass": 0.53, "vertigo": 0.50, "cache": 0.50, "cobblestone": 0.51,
}
CS2_ACTIVE_DUTY: tuple[str, ...] = ("ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train")
VALORANT_DEFENCE_SHARE: dict[str, float] = {
    "abyss": 0.49, "ascent": 0.52, "bind": 0.51, "breeze": 0.50, "corrode": 0.50, "fracture": 0.47, "haven": 0.50,
    "icebox": 0.49, "lotus": 0.49, "pearl": 0.52, "split": 0.53, "sunset": 0.50,
}
VALORANT_POOL: tuple[str, ...] = ("abyss", "ascent", "bind", "haven", "icebox", "lotus", "sunset")
SIDE_EDGE: dict[str, float] = {"lol": 0.538, "dota2": 0.52}  # blue side / Radiant game win share

ROUND_K = 1.5  # round log-odds per HLTV rating point
GAME_K = 4.0  # MOBA game log-odds per rating point
PISTOL_K_SHARE = 1.0 / 3.0  # pistol rounds are closer to a coin flip than gun rounds
FIRST_BLOOD_SHARE = 0.45
ELO_PER_RATING = 1150.0  # matches the round Markov map probability at a 0.20 rating gap (79%)
FORM_REGRESSION = 0.75
FORM_PRIOR_MAPS = 30.0
DEFAULT_RECENT_MAPS = 20.0
STANDIN_PENALTY = 0.035
ONLINE_K_SCALE = 0.90
DEFAULT_RATING = 1.0
REG_HALF = 12
REG_TARGET = 13
OT_HALF = 3
OT_TARGET = 4
MOBA_GOLD_BETA = {"lol": 0.30, "dota2": 0.18}  # log-odds per 1000 gold at minute 20
MOBA_PRIOR_DECAY_MIN = 15.0

FORMATS: dict[str, int] = {"bo1": 1, "bo2": 2, "bo3": 3, "bo5": 5, "bo7": 7}
MAP_HANDICAP_LINES: dict[int, tuple[float, ...]] = {1: (), 3: (-1.5, 1.5), 5: (-2.5, -1.5, 1.5, 2.5), 7: (-3.5, -2.5, -1.5, 1.5, 2.5, 3.5)}
TOTAL_MAP_LINES: dict[int, tuple[float, ...]] = {1: (), 3: (2.5,), 5: (3.5, 4.5), 7: (4.5, 5.5, 6.5)}

MAP_1_WINNER = "map_1_winner"
MAP_WINNER = "map_winner"
PISTOL_ROUND = "pistol_round_winner"
FIRST_BLOOD = "first_blood"


def _logit(p: float) -> float:
    p = clamp(p, 1e-9, 1.0 - 1e-9)
    return math.log(p / (1.0 - p))


def _other(side: str) -> str:
    return "t" if side == "ct" else "ct"


def log5(p_a: float, p_b: float) -> float:
    """Bill James' log5: P(A beats B) from each one's win rate against the field."""
    p_a, p_b = clamp(p_a, 0.01, 0.99), clamp(p_b, 0.01, 0.99)
    return p_a * (1.0 - p_b) / (p_a * (1.0 - p_b) + p_b * (1.0 - p_a))


def race(qs: Sequence[float], target_a: int, target_b: int, a: int = 0, b: int = 0) -> tuple[float, float, float]:
    """A first-to race over a fixed sequence of rounds (``qs[i]`` = P(A wins round i)). Returns P(A reaches
    ``target_a`` first), P(B reaches ``target_b`` first) and P(neither when the sequence runs out)."""
    if a >= target_a:
        return 1.0, 0.0, 0.0
    if b >= target_b:
        return 0.0, 1.0, 0.0
    states: dict[tuple[int, int], float] = {(a, b): 1.0}
    win = lose = 0.0
    for q in qs:
        nxt: dict[tuple[int, int], float] = {}
        for (x, y), p in states.items():
            if x + 1 >= target_a:
                win += p * q
            else:
                nxt[(x + 1, y)] = nxt.get((x + 1, y), 0.0) + p * q
            if y + 1 >= target_b:
                lose += p * (1.0 - q)
            else:
                nxt[(x, y + 1)] = nxt.get((x, y + 1), 0.0) + p * (1.0 - q)
        states = nxt
        if not states:
            break
    return win, lose, sum(states.values())


@dataclass(frozen=True, slots=True)
class RoundMap:
    """One CS2 / Valorant map: P(A wins a round) on each side, and the exact Markov map win probability."""

    game: str
    p_ct: float  # A defending (CT)
    p_t: float  # A attacking (T)

    def q(self, side: str) -> float:
        return self.p_ct if side == "ct" else self.p_t

    def side_at(self, n: int, start_side: str) -> str:
        """A's side in round index ``n`` (0-based) when A started on ``start_side``."""
        s1, s2 = start_side, _other(start_side)
        if n < 2 * REG_HALF:
            return s1 if n < REG_HALF else s2
        if self.game == "valorant":
            return s1 if (n - 2 * REG_HALF) % 2 == 0 else s2
        period, within = divmod(n - 2 * REG_HALF, 2 * OT_HALF)
        first = s2 if period % 2 == 0 else s1
        return first if within < OT_HALF else _other(first)

    def _cs2_ot_values(self, start_side: str) -> tuple[float, float]:
        """P(A wins from the start of an even / odd overtime period): MR3 periods repeat at 3-3, solved exactly."""
        s1, s2 = start_side, _other(start_side)
        even = [self.q(s2)] * OT_HALF + [self.q(s1)] * OT_HALF
        odd = [self.q(s1)] * OT_HALF + [self.q(s2)] * OT_HALF
        we, _, te = race(even, OT_TARGET, OT_TARGET)
        wo, _, to = race(odd, OT_TARGET, OT_TARGET)
        denom = 1.0 - te * to
        return (we + te * wo) / denom, (wo + to * we) / denom

    def _valorant_ot_value(self, start_side: str) -> float:
        q1, q2 = self.q(start_side), self.q(_other(start_side))
        win, lose = q1 * q2, (1.0 - q1) * (1.0 - q2)
        return win / (win + lose)

    def win_prob(self, start_side: str, a: int = 0, b: int = 0) -> float:
        """Exact P(A wins the map) from score ``a``-``b`` (A started the map on ``start_side``)."""
        n = a + b
        reg = 2 * REG_HALF
        if n <= reg and (a >= REG_TARGET or b >= REG_TARGET):
            return 1.0 if a > b else 0.0
        if n < reg:
            qs = [self.q(self.side_at(i, start_side)) for i in range(n, reg)]
            win, _, tie = race(qs, REG_TARGET, REG_TARGET, a, b)
            return win + tie * self._ot_from_start(start_side)
        if self.game == "valorant":
            lead = a - b
            if abs(lead) >= 2:
                return 1.0 if lead > 0 else 0.0
            v = self._valorant_ot_value(start_side)
            if lead == 0:
                return v
            q2 = self.q(self.side_at(n, start_side))
            return q2 + (1.0 - q2) * v if lead > 0 else q2 * v
        period, within = divmod(n - reg, 2 * OT_HALF)
        base = REG_HALF + OT_HALF * period
        rel_a, rel_b = a - base, b - base
        qs = [self.q(self.side_at(n + i, start_side)) for i in range(2 * OT_HALF - within)]
        win, _, tie = race(qs, OT_TARGET, OT_TARGET, rel_a, rel_b)
        v_even, v_odd = self._cs2_ot_values(start_side)
        return win + tie * (v_odd if period % 2 == 0 else v_even)

    def _ot_from_start(self, start_side: str) -> float:
        if self.game == "valorant":
            return self._valorant_ot_value(start_side)
        return self._cs2_ot_values(start_side)[0]

    @property
    def neutral(self) -> float:
        return 0.5 * (self.win_prob("ct") + self.win_prob("t"))


@dataclass(frozen=True, slots=True)
class PlannedMap:
    name: str
    picked_by: str  # "home" | "away" | "decider"
    home_win_prob: float
    home_start_side: str | None  # "ct" | "t" | None (knife round)
    rating_gap: float
    side_share: float


@dataclass
class TeamProfile:
    rating: float
    map_ratings: dict[str, float] = field(default_factory=dict)
    elo: float | None = None
    ranking_points: float | None = None
    recent_map_win_pct: float | None = None
    recent_maps: float = DEFAULT_RECENT_MAPS
    pistol_win_pct: float | None = None
    first_blood_rate: float | None = None

    @classmethod
    def parse(cls, stats: Mapping[str, Any]) -> TeamProfile:
        raw_maps = stats.get("map_ratings") or {}
        maps = {norm(k): float(v) for k, v in raw_maps.items() if isinstance(v, (int, float)) and math.isfinite(float(v))} \
            if isinstance(raw_maps, Mapping) else {}
        form = num(stats, "recent_map_win_pct", "recent_win_pct")
        return cls(
            rating=num(stats, "team_rating", "rating", "hltv_rating", "vlr_rating", default=DEFAULT_RATING) or DEFAULT_RATING,
            map_ratings=maps,
            elo=num(stats, "elo"),
            ranking_points=num(stats, "world_ranking_points", "ranking_points"),
            recent_map_win_pct=(form / 100.0 if form is not None and form > 1.0 else form),
            recent_maps=num(stats, "recent_maps", "recent_maps_played", default=DEFAULT_RECENT_MAPS) or DEFAULT_RECENT_MAPS,
            pistol_win_pct=_rate(num(stats, "pistol_win_pct", "pistol_round_win_pct")),
            first_blood_rate=_rate(num(stats, "first_blood_rate", "first_blood_pct")),
        )

    def map_rating(self, name: str) -> float:
        return self.map_ratings.get(name, self.rating)

    def penalised(self, penalty: float) -> TeamProfile:
        if penalty <= 0:
            return self
        return TeamProfile(self.rating - penalty, {k: v - penalty for k, v in self.map_ratings.items()}, self.elo, self.ranking_points,
                           self.recent_map_win_pct, self.recent_maps, self.pistol_win_pct, self.first_blood_rate)


def _rate(value: float | None) -> float | None:
    if value is None:
        return None
    return clamp(value / 100.0 if value > 1.0 else value, 0.0, 1.0)


def series_distribution(ps: Sequence[float], need: int, a0: int = 0, b0: int = 0) -> dict[tuple[int, int], float]:
    """Final series scores over an ordered map sequence (``ps[i]`` = P(A wins the i-th remaining map)), from ``a0``-``b0``."""
    if a0 >= need or b0 >= need:
        return {(a0, b0): 1.0}
    states: dict[tuple[int, int], float] = {(a0, b0): 1.0}
    final: dict[tuple[int, int], float] = {}
    for p in ps:
        nxt: dict[tuple[int, int], float] = {}
        for (a, b), pr in states.items():
            for (x, y), w in (((a + 1, b), p), ((a, b + 1), 1.0 - p)):
                if x >= need or y >= need:
                    final[(x, y)] = final.get((x, y), 0.0) + pr * w
                else:
                    nxt[(x, y)] = nxt.get((x, y), 0.0) + pr * w
        states = nxt
        if not states:
            break
    for key, pr in states.items():  # an even format (bo2) can end level
        final[key] = final.get(key, 0.0) + pr
    return final


def moba_series(p_blue: float, p_red: float, need: int, max_games: int, *, first_chooser: str, a0: int = 0, b0: int = 0
                ) -> tuple[dict[tuple[int, int], float], list[tuple[float, float]]]:
    """Exact MOBA series with side choice: the chooser takes the better side (blue / Radiant), the loser of each game
    chooses next. Returns the final-score distribution and, per game index, (P(game played), P(game played and A wins))."""
    states: dict[tuple[int, int, str], float] = {(a0, b0, first_chooser): 1.0}
    final: dict[tuple[int, int], float] = {}
    per_game: list[tuple[float, float]] = []
    for _ in range(max_games - (a0 + b0)):
        if not states:
            break
        nxt: dict[tuple[int, int, str], float] = {}
        played = won = 0.0
        for (a, b, chooser), pr in states.items():
            p = p_blue if chooser == "home" else p_red
            played += pr
            won += pr * p
            for (x, y, loser), w in (((a + 1, b, "away"), p), ((a, b + 1, "home"), 1.0 - p)):
                if x >= need or y >= need:
                    final[(x, y)] = final.get((x, y), 0.0) + pr * w
                else:
                    nxt[(x, y, loser)] = nxt.get((x, y, loser), 0.0) + pr * w
        per_game.append((played, won))
        states = nxt
    for (a, b, _), pr in states.items():
        final[(a, b)] = final.get((a, b), 0.0) + pr
    return final, per_game


def _win_share(dist: Mapping[tuple[int, int], float]) -> tuple[float, float, float]:
    home = sum(p for (a, b), p in dist.items() if a > b)
    away = sum(p for (a, b), p in dist.items() if b > a)
    level = sum(p for (a, b), p in dist.items() if a == b)
    return home, away, level


class EsportsSportAdapter(BaseSportAdapter):
    """
    Quantitative Esports Engine (CS2, LoL, Dota 2, Valorant) implementing
    rating differentials, 7-map veto sequence simulation, and asymmetrical side biases.
    """

    MAP_SIDE_BIASES: ClassVar[dict[str, float]] = CS2_CT_SHARE

    kelly_multiplier = 0.25
    model_family = "HLTV Rating & Map Veto Markov"
    has_draw = False
    odds_api_family = "esports"
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "match_winner_series": (MONEYLINE, {}),
        "series_winner": (MONEYLINE, {}),
        "match_winner": (MONEYLINE, {}),
        "moneyline": (MONEYLINE, {}),
        "map_handicap": (SPREAD, {}),
        "alt_map_handicap": (ALT_SPREAD, {}),
        "map_total_over_under": (TOTAL, {}),
        "total_maps": (TOTAL, {}),
        "alt_total_maps": (ALT_TOTAL, {}),
        "correct_series_score": (CORRECT_SCORE, {}),
        "series_correct_score": (CORRECT_SCORE, {}),
        "correct_score": (CORRECT_SCORE, {}),
        "map_1_winner": (MAP_1_WINNER, {}),
        "map_winner": (MAP_WINNER, {}),
        "map_round_handicap": (PERIOD_SPREAD, {}),
        "map_total_rounds": (PERIOD_TOTAL, {}),
        "map_moneyline": (PERIOD_MONEYLINE, {"level_is_void": True}),
        "pistol_round_winner": (PISTOL_ROUND, {}),
        "first_blood": (FIRST_BLOOD, {}),
        "player_kills": (PLAYER_PROP, {"stat": "kills"}),
        "player_headshots": (PLAYER_PROP, {"stat": "headshots"}),
    }
    GENERIC_SUPPORTED = frozenset({MONEYLINE, SPREAD, ALT_SPREAD, TOTAL, ALT_TOTAL, CORRECT_SCORE, PERIOD_MONEYLINE, PERIOD_SPREAD,
                                   PERIOD_TOTAL, PLAYER_PROP})
    SPECIAL_MARKETS = frozenset({MAP_1_WINNER, MAP_WINNER, PISTOL_ROUND, FIRST_BLOOD})
    BET_TYPES = frozenset({"moneyline", "point_spread", "game_total", "period_lines", "parlay", "round_robin", "if_bet", "double_action",
                           "futures", "player_props", "game_props", "correct_score", "map_winner", "total_maps", "alternative_lines"})

    @property
    def sport_key(self) -> str:
        return "esports"

    @property
    def display_name(self) -> str:
        return "Esports"

    @property
    def standard_markets(self) -> list[str]:
        return [
            "match_winner_series",
            "map_handicap",
            "map_total_over_under",
            "map_1_winner",
            "pistol_round_winner",
            "correct_series_score",
            "first_blood",
        ]

    # ------------------------------------------------------------------ context
    @staticmethod
    def game(ctx: MatchContext) -> str:
        g = norm(ctx.text("game", "cs2"))
        aliases = {"csgo": "cs2", "cs": "cs2", "counter_strike": "cs2", "counter_strike_2": "cs2", "val": "valorant", "vct": "valorant",
                   "league_of_legends": "lol", "league": "lol", "dota": "dota2", "dota_2": "dota2"}
        g = aliases.get(g, g)
        return g if g in ("cs2", "valorant", "lol", "dota2") else "cs2"

    @staticmethod
    def best_of(ctx: MatchContext) -> int:
        fmt = norm(ctx.text("series_format", "bo3"))
        if fmt in FORMATS:
            return FORMATS[fmt]
        digits = "".join(c for c in fmt if c.isdigit())
        return int(digits) if digits and int(digits) in FORMATS.values() else 3

    @staticmethod
    def side_shares(game: str) -> dict[str, float]:
        return VALORANT_DEFENCE_SHARE if game == "valorant" else CS2_CT_SHARE

    def map_pool(self, ctx: MatchContext, game: str) -> list[str]:
        raw = ctx.get("map_pool")
        if isinstance(raw, (list, tuple)) and raw:
            return [norm(m) for m in raw]
        return list(VALORANT_POOL if game == "valorant" else CS2_ACTIVE_DUTY)

    def teams(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext
              ) -> tuple[TeamProfile, TeamProfile, dict[str, Any]]:
        a, b = TeamProfile.parse(home_stats), TeamProfile.parse(away_stats)
        adjustments: dict[str, Any] = {}
        for side in ("home", "away"):
            penalty = sum(x.weight * (0.10 * x.impact if x.impact is not None else STANDIN_PENALTY) for x in ctx.absences if x.side == side)
            if penalty > 0:
                adjustments[f"{side}_standin_rating_penalty"] = round(penalty, 4)
                if side == "home":
                    a = a.penalised(penalty)
                else:
                    b = b.penalised(penalty)
        return a, b, adjustments

    # ------------------------------------------------------------------ map and game models
    def round_map(self, game: str, name: str, a: TeamProfile, b: TeamProfile, k: float) -> RoundMap:
        gap = a.map_rating(name) - b.map_rating(name)
        bias = _logit(self.side_shares(game).get(name, 0.5))
        return RoundMap(game, logistic(k * gap + bias), logistic(k * gap - bias))

    def neutral_round_map(self, game: str, a: TeamProfile, b: TeamProfile, k: float) -> RoundMap:
        p = logistic(k * (a.rating - b.rating))
        return RoundMap(game, p, p)

    def veto(self, game: str, a: TeamProfile, b: TeamProfile, ctx: MatchContext, *, k: float, best_of: int) -> tuple[list[PlannedMap], list[dict[str, str]]]:
        """The greedy veto (A starts) and the played sequence with each map's side-choice-aware win probability."""
        maps_given = ctx.get("maps")
        maps_given = list(maps_given) if isinstance(maps_given, (list, tuple)) else []
        pool = self.map_pool(ctx, game)
        models = {m: self.round_map(game, m, a, b, k) for m in dict.fromkeys(pool + [norm(x) for x in maps_given])}
        if len(maps_given) >= best_of:
            order = [(norm(m), "decider" if i == best_of - 1 else ("home" if i % 2 == 0 else "away")) for i, m in enumerate(maps_given[:best_of])]
            steps: list[dict[str, str]] = [{"team": "given", "action": "play", "map": m} for m, _ in order]
        else:
            order, steps = self._greedy_veto(pool, {m: models[m].neutral for m in pool}, best_of)
        planned = []
        shares = self.side_shares(game)
        for name, picker in order:
            model = models[name]
            ct_start, t_start = model.win_prob("ct"), model.win_prob("t")
            if picker == "home":  # B chooses the starting side against A's pick
                p, start = (ct_start, "ct") if ct_start <= t_start else (t_start, "t")
            elif picker == "away":
                p, start = (ct_start, "ct") if ct_start >= t_start else (t_start, "t")
            else:
                p, start = 0.5 * (ct_start + t_start), None
            planned.append(PlannedMap(name, picker, p, start, a.map_rating(name) - b.map_rating(name), shares.get(name, 0.5)))
        return planned, steps

    @staticmethod
    def _greedy_veto(pool: list[str], value: Mapping[str, float], best_of: int) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
        if best_of == 1:
            sequence = ["ban"] * 6
        elif best_of == 5:
            sequence = ["ban", "ban", "pick", "pick", "pick", "pick"]
        else:
            sequence = ["ban", "ban", "pick", "pick", "ban", "ban"]
        remaining = list(pool)
        picks: list[tuple[str, str]] = []
        steps: list[dict[str, str]] = []
        picks_left = sequence.count("pick")
        for i, action in enumerate(sequence):
            team = "home" if i % 2 == 0 else "away"
            if action == "ban" and len(remaining) - 1 < picks_left + 1:
                continue  # a short pool: skip bans that would leave too few maps
            if action == "pick" and len(remaining) <= 1:
                break
            if action == "pick":
                picks_left -= 1
            # A: ban its worst, pick its best; B: ban A's best, pick A's worst (ties broken by pool order)
            want_high = (team == "home") == (action == "pick")
            chosen = max(remaining, key=lambda m: value[m]) if want_high else min(remaining, key=lambda m: value[m])
            remaining.remove(chosen)
            steps.append({"team": team, "action": action, "map": chosen})
            if action == "pick":
                picks.append((chosen, team))
        while len(picks) < best_of and remaining:
            decider = remaining.pop(0)
            steps.append({"team": "decider", "action": "decider", "map": decider})
            picks.append((decider, "decider"))
        while len(picks) < best_of:  # a pool smaller than the series: replay the strongest neutral map
            picks.append((pool[0], "decider"))
        return picks, steps

    def game_probs(self, game: str, a: TeamProfile, b: TeamProfile, k_game: float) -> tuple[float, float, float]:
        """MOBA: P(A wins a game) on the better side (blue / Radiant), on the other side, and side-neutral."""
        gap = a.rating - b.rating
        edge = _logit(SIDE_EDGE[game])
        return logistic(k_game * gap + edge), logistic(k_game * gap - edge), logistic(k_game * gap)

    # ------------------------------------------------------------------ the model
    def model(self, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any], ctx: MatchContext) -> dict[str, Any]:
        game, best_of = self.game(ctx), self.best_of(ctx)
        need = best_of // 2 + 1
        a, b, adjustments = self.teams(home_stats, away_stats, ctx)
        online = bool(ctx.get("online", False))
        k = ctx.number("round_k", ROUND_K) * (ONLINE_K_SCALE if online else 1.0)
        k_game = ctx.number("game_k", GAME_K) * (ONLINE_K_SCALE if online else 1.0)
        if online:
            adjustments["online_k_scale"] = ONLINE_K_SCALE
        first_chooser = "away" if norm(ctx.text("higher_seed", ctx.text("veto_first", "home"))) in ("away", "b", "team_b") else "home"
        out: dict[str, Any] = {"game": game, "best_of": best_of, "need": need, "a": a, "b": b, "k": k, "k_game": k_game,
                               "first_chooser": first_chooser, "adjustments": adjustments}
        if game in SIDE_EDGE:
            p_blue, p_red, p_neutral = self.game_probs(game, a, b, k_game)
            veto_dist, per_game = moba_series(p_blue, p_red, need, best_of, first_chooser=first_chooser)
            map_neutral = p_neutral
            out.update({"p_blue": p_blue, "p_red": p_red, "per_game": per_game, "planned": [], "veto_steps": [],
                        "round_win_prob": None, "map_probs": [won / played if played > 0 else p_neutral for played, won in per_game]})
        else:
            planned, steps = self.veto(game, a, b, ctx, k=k, best_of=best_of)
            ps = [m.home_win_prob for m in planned]
            veto_dist = series_distribution(ps, need)
            neutral = self.neutral_round_map(game, a, b, k)
            map_neutral = neutral.win_prob("ct")
            out.update({"planned": planned, "veto_steps": steps, "round_win_prob": neutral.p_ct, "map_probs": ps,
                        "per_game": [(1.0, p) for p in ps]})
        # sub-model per-map probabilities
        elo_map, elo_source = self._elo_map_prob(a, b)
        form_map, form_source = self._form_map_prob(a, b, map_neutral)
        iid = {name: series_distribution([p] * best_of, need) for name, p in
               (("rating_round_markov", map_neutral), ("ranking_elo_logistic", elo_map), ("recent_form", form_map))}
        out.update({"veto_dist": veto_dist, "iid": iid, "map_neutral": map_neutral, "elo_map": elo_map, "elo_source": elo_source,
                    "form_map": form_map, "form_source": form_source})
        return out

    def _elo_map_prob(self, a: TeamProfile, b: TeamProfile) -> tuple[float, str]:
        if a.elo is not None and b.elo is not None:
            diff, source = a.elo - b.elo, "elo"
        elif a.ranking_points and b.ranking_points and a.ranking_points > 0 and b.ranking_points > 0:
            diff, source = 400.0 * math.log10(a.ranking_points / b.ranking_points), "world_ranking_points"
        else:
            diff, source = ELO_PER_RATING * (a.rating - b.rating), "rating"
        return logistic(diff * math.log(10.0) / 400.0), source

    @staticmethod
    def _form_map_prob(a: TeamProfile, b: TeamProfile, rating_map: float) -> tuple[float, str]:
        if a.recent_map_win_pct is not None and b.recent_map_win_pct is not None:
            weight = min(a.recent_maps, b.recent_maps) / (min(a.recent_maps, b.recent_maps) + FORM_PRIOR_MAPS)
            return weight * log5(a.recent_map_win_pct, b.recent_map_win_pct) + (1.0 - weight) * rating_map, "recent_map_win_pct"
        return 0.5 + FORM_REGRESSION * (rating_map - 0.5), "regressed_rating"

    @staticmethod
    def estimates(mdl: Mapping[str, Any]) -> list[SubModelEstimate]:
        """The four sub-models' series verdicts (default weights 0.30 / 0.40 / 0.15 / 0.15)."""
        out = []
        for name, dist, weight in (("rating_round_markov", mdl["iid"]["rating_round_markov"], 0.30), ("map_veto_simulation", mdl["veto_dist"], 0.40),
                                   ("ranking_elo_logistic", mdl["iid"]["ranking_elo_logistic"], 0.15), ("recent_form", mdl["iid"]["recent_form"], 0.15)):
            home, away, level = _win_share(dist)
            out.append(SubModelEstimate(name, home, away, level, _expected_maps(dist), weight))
        return out

    # ------------------------------------------------------------------ pricing
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        ctx = MatchContext.parse(context)
        mdl = self.model(home_stats, away_stats, ctx)
        consensus = self._consensus(self.estimates(mdl), ctx)
        dist = _calibrate(mdl["veto_dist"], consensus.home_win_prob, consensus.away_win_prob, consensus.draw_prob)
        markets = self.price_markets(mdl, dist)
        a, b = mdl["a"], mdl["b"]
        metrics: dict[str, Any] = {
            "game": mdl["game"],
            "series_format": f"bo{mdl['best_of']}",
            "rating_differential": round(a.rating - b.rating, 4),
            "single_map_prob_team_a": round(mdl["map_neutral"], 4),
            "round_win_prob_team_a": None if mdl["round_win_prob"] is None else round(mdl["round_win_prob"], 4),
            "round_k": mdl["k"],
            "game_k": mdl["k_game"],
            "map_probabilities": [round(p, 4) for p in mdl["map_probs"]],
            "series_score_distribution": {f"{x}-{y}": round(p, 4) for (x, y), p in sorted(dist.items())},
            "expected_maps": round(_expected_maps(dist), 4),
            "elo_map_prob": round(mdl["elo_map"], 4),
            "elo_source": mdl["elo_source"],
            "form_map_prob": round(mdl["form_map"], 4),
            "form_source": mdl["form_source"],
            **mdl["adjustments"],
        }
        if mdl["planned"]:
            metrics["map_sequence"] = [{"map": m.name, "picked_by": m.picked_by, "home_win_prob": round(m.home_win_prob, 4),
                                        "home_start_side": m.home_start_side, "rating_gap": round(m.rating_gap, 4),
                                        "ct_round_share": m.side_share} for m in mdl["planned"]]
            metrics["veto"] = mdl["veto_steps"]
        else:
            metrics["side_edge"] = SIDE_EDGE[mdl["game"]]
            metrics["game_win_prob_better_side"] = round(mdl["p_blue"], 4)
            metrics["game_win_prob_other_side"] = round(mdl["p_red"], 4)
        return self._assemble(consensus, ctx, expected_total=_expected_maps(dist), metrics=metrics, markets=markets)

    def price_markets(self, mdl: Mapping[str, Any], dist: Mapping[tuple[int, int], float]) -> dict[str, float]:
        best_of, game = int(mdl["best_of"]), str(mdl["game"])
        markets: dict[str, float] = {}
        home, away, level = _win_share(dist)
        markets["series_home"], markets["series_away"] = home, away
        if level > 0:
            markets["series_draw"] = level
        for (x, y), p in sorted(dist.items()):
            markets[f"correct_score_{x}-{y}"] = p
        margin, total = _margin_total_pmfs(dist)
        for line in MAP_HANDICAP_LINES.get(best_of, ()):
            markets[f"map_handicap_home_{line:+g}"] = margin.line_outcome(-line, over=True).effective_probability
            markets[f"map_handicap_away_{line:+g}"] = margin.negate().line_outcome(-line, over=True).effective_probability
        for line in TOTAL_MAP_LINES.get(best_of, ()):
            markets[f"total_maps_over_{line:g}"] = total.line_outcome(line, over=True).effective_probability
            markets[f"total_maps_under_{line:g}"] = total.line_outcome(line, over=False).effective_probability
        played = _maps_played(dist, best_of)
        for i, p_map in enumerate(mdl["map_probs"]):
            markets[f"map_{i + 1}_home"] = p_map
            markets[f"map_{i + 1}_away"] = 1.0 - p_map
            markets[f"map_{i + 1}_played"] = played[i]
        a, b, k = mdl["a"], mdl["b"], float(mdl["k"])
        if game in ("cs2", "valorant"):
            first: PlannedMap = mdl["planned"][0]
            p_pistol = self.pistol_probability(first, a, b, k)
            markets["pistol_round_home"], markets["pistol_round_away"] = p_pistol, 1.0 - p_pistol
        if game in ("lol", "dota2", "valorant"):
            if game == "valorant":
                base_logit = k * (a.rating - b.rating)
            else:
                base_logit = _logit(float(mdl["map_probs"][0]))
            p_fb = logistic(FIRST_BLOOD_SHARE * base_logit)
            if a.first_blood_rate is not None and b.first_blood_rate is not None:
                p_fb = 0.5 * p_fb + 0.5 * log5(a.first_blood_rate, b.first_blood_rate)
            markets["first_blood_home"], markets["first_blood_away"] = p_fb, 1.0 - p_fb
        return markets

    @staticmethod
    def pistol_probability(first: PlannedMap, a: TeamProfile, b: TeamProfile, k: float) -> float:
        """Map 1's first pistol: ``logistic(k / 3 x rating gap +/- logit(c_map) / 2)``; the knife-round decider averages
        both sides; a log5 of the teams' ``pistol_win_pct`` is blended in 50/50 when both carry it."""
        gap_logit = k * PISTOL_K_SHARE * first.rating_gap
        bias = 0.5 * _logit(first.side_share)
        if first.home_start_side == "ct":
            p = logistic(gap_logit + bias)
        elif first.home_start_side == "t":
            p = logistic(gap_logit - bias)
        else:
            p = 0.5 * (logistic(gap_logit + bias) + logistic(gap_logit - bias))
        if a.pistol_win_pct is not None and b.pistol_win_pct is not None:
            p = 0.5 * p + 0.5 * log5(a.pistol_win_pct, b.pistol_win_pct)
        return p

    # ------------------------------------------------------------------ in-play
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """Live series win probability. CS2 / Valorant ``live_state``: ``maps_a``, ``maps_b`` (maps won),
        ``current_map_rounds_a``, ``current_map_rounds_b``, ``current_side_a`` ("ct" / "t", or "defense" / "attack")
        and ``map_name``: the exact round Markov over the rest of the current map, then the ordered remaining maps.
        LoL / Dota 2: ``maps_a``, ``maps_b``, ``gold_diff`` (team A minus team B), ``minute`` and optional ``side_a``
        ("blue" / "red"): ``logit P(game) = prior x e^{-minute / 15} + beta(minute) x gold_diff / 1000`` with
        ``beta = 0.30`` (LoL; Dota 2 0.18) ``x sqrt(20 / max(minute, 5))``: an early lead is worth more per gold.
        ``finished`` ({"winner": ...}) or a decided score returns 1 / 0. The consensus edge over the veto model is
        carried in log-odds in proportion to the series still to play."""
        ctx = MatchContext.parse(context)
        mdl = self.model(home_stats, away_stats, ctx)
        need, best_of, game = int(mdl["need"]), int(mdl["best_of"]), str(mdl["game"])
        finished = live_state.get("finished") or ({"winner": live_state["winner"]} if live_state.get("winner") else None)
        if finished:
            winner = side_token(norm(finished.get("winner") if isinstance(finished, Mapping) else finished)) or ""
            if winner in ("home", "away"):
                return LiveWinProbability(float(winner == "home"), float(winner == "away"), 0.0, {"finished": True, "winner": winner})
        maps_a, maps_b = int(live_state.get("maps_a", 0) or 0), int(live_state.get("maps_b", 0) or 0)
        if maps_a >= need or maps_b >= need:
            return LiveWinProbability(float(maps_a > maps_b), float(maps_b > maps_a), 0.0, {"finished": True, "score": f"{maps_a}-{maps_b}"})
        index = maps_a + maps_b
        detail: dict[str, Any] = {"maps": f"{maps_a}-{maps_b}", "map_index": index + 1}
        if game in SIDE_EDGE:
            p_now, progress = self._moba_live(mdl, live_state, detail)
            # the loser of the current game chooses the side of the next one
            win_next, _ = moba_series(mdl["p_blue"], mdl["p_red"], need, best_of, first_chooser="away", a0=maps_a + 1, b0=maps_b)
            lose_next, _ = moba_series(mdl["p_blue"], mdl["p_red"], need, best_of, first_chooser="home", a0=maps_a, b0=maps_b + 1)
        else:
            p_now, progress = self._round_live(mdl, live_state, index, detail)
            rest = [m.home_win_prob for m in mdl["planned"][index + 1:]]
            win_next = series_distribution(rest, need, maps_a + 1, maps_b)
            lose_next = series_distribution(rest, need, maps_a, maps_b + 1)
        home = p_now * _win_share(win_next)[0] + (1.0 - p_now) * _win_share(lose_next)[0]
        away = p_now * _win_share(win_next)[1] + (1.0 - p_now) * _win_share(lose_next)[1]
        level = max(0.0, 1.0 - home - away)
        # carry the pre-match consensus edge over the veto model, fading as the series is played
        veto_home, veto_away, _ = _win_share(mdl["veto_dist"])
        consensus = self._consensus(self.estimates(mdl), ctx)
        remaining = clamp(1.0 - (index + progress) / max(_expected_maps(mdl["veto_dist"]), 1.0), 0.0, 1.0)
        offset = (_logit(consensus.home_win_prob / (consensus.home_win_prob + consensus.away_win_prob))
                  - _logit(veto_home / (veto_home + veto_away))) * remaining
        decided = home + away
        if decided > 0 and 0.0 < home < decided:
            share = logistic(_logit(home / decided) + offset)
            home, away = decided * share, decided * (1.0 - share)
        detail.update({"current_map_win_prob": round(p_now, 4), "consensus_offset_logit": round(offset, 4)})
        return LiveWinProbability(home, away, level, detail)

    def _round_live(self, mdl: Mapping[str, Any], live_state: Mapping[str, Any], index: int, detail: dict[str, Any]) -> tuple[float, float]:
        planned: list[PlannedMap] = mdl["planned"]
        name = norm(live_state.get("map_name")) or (planned[index].name if index < len(planned) else "")
        model = self.round_map(str(mdl["game"]), name, mdl["a"], mdl["b"], float(mdl["k"]))
        ra, rb = int(live_state.get("current_map_rounds_a", 0) or 0), int(live_state.get("current_map_rounds_b", 0) or 0)
        side_raw = norm(live_state.get("current_side_a"))
        side_now = "t" if side_raw in ("t", "terrorist", "attack", "attacker", "attacking") else "ct" if side_raw else None
        n = ra + rb
        if side_now is None:
            p_now = 0.5 * (model.win_prob("ct", ra, rb) + model.win_prob("t", ra, rb))
        else:
            start = side_now if model.side_at(n, "ct") == "ct" else _other(side_now)
            p_now = model.win_prob(start, ra, rb)
        detail.update({"map_name": name, "rounds": f"{ra}-{rb}", "side_a": side_now})
        return p_now, clamp(n / 22.0, 0.0, 1.0)

    @staticmethod
    def _moba_live(mdl: Mapping[str, Any], live_state: Mapping[str, Any], detail: dict[str, Any]) -> tuple[float, float]:
        game = str(mdl["game"])
        minute = max(0.0, float(live_state.get("minute", 0.0) or 0.0))
        gold = float(live_state.get("gold_diff", 0.0) or 0.0)
        side = norm(live_state.get("side_a"))
        if side in ("blue", "radiant"):
            prior = _logit(float(mdl["p_blue"]))
        elif side in ("red", "dire"):
            prior = _logit(float(mdl["p_red"]))
        else:
            prior = _logit(float(mdl["map_neutral"]))
        beta = MOBA_GOLD_BETA[game] * math.sqrt(20.0 / max(minute, 5.0))
        logit = prior * math.exp(-minute / MOBA_PRIOR_DECAY_MIN) + beta * gold / 1000.0
        detail.update({"minute": minute, "gold_diff": gold, "gold_beta_per_1000": round(beta, 4)})
        return logistic(logit), clamp(minute / 35.0, 0.0, 1.0)

    # ------------------------------------------------------------------ settlement
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """``home`` / ``away`` are maps won by team A / B: ``team_a_maps`` / ``team_b_maps`` (or ``maps_a`` / ``home_maps``),
        else counted from ``maps`` (winners ``["a", "b", ...]`` or dicts with ``winner`` / ``score``) or ``map_scores``
        (``[[13, 9], [10, 13]]``). Per-map round scores become periods ``map1``, ``map2`` ... (``map_1`` too)."""
        res = normalize_basic(match_result, home_keys=("team_a_maps", "maps_a", "home_maps", "home_score"),
                              away_keys=("team_b_maps", "maps_b", "away_maps", "away_score"))
        res.home_name = res.home_name or norm(match_result.get("team_a") or match_result.get("team_a_name"))
        res.away_name = res.away_name or norm(match_result.get("team_b") or match_result.get("team_b_name"))
        if res.winner is None and res.winner_name:
            if res.home_name and res.winner_name == res.home_name:
                res.winner, res.winner_name = "home", ""
            elif res.away_name and res.winner_name == res.away_name:
                res.winner, res.winner_name = "away", ""
        winners = self._map_records(match_result, res)
        by_index: dict[int, tuple[float, float]] = {}
        for key, pair in list(res.periods.items()):  # periods: "map_1" <-> "map1"
            digits = "".join(c for c in key if c.isdigit())
            if key.startswith("map") and digits:
                res.periods.setdefault(f"map{int(digits)}", pair)
                res.periods.setdefault(f"map_{int(digits)}", pair)
                by_index[int(digits)] = pair
        if not winners and by_index and sorted(by_index) == list(range(1, len(by_index) + 1)):
            winners = ["home" if h > a else "away" if a > h else "draw" for h, a in (by_index[i] for i in sorted(by_index))]
        if res.home is None and res.away is None and winners:
            res.home = float(sum(1 for w in winners if w == "home"))
            res.away = float(sum(1 for w in winners if w == "away"))
        if res.home is not None and res.away is None and num(match_result, "total_maps") is not None:
            res.away = float(num(match_result, "total_maps") or 0.0) - res.home
        res.extras["map_winners"] = winners
        res.extras["total_maps"] = num(match_result, "total_maps")
        return res

    def _map_records(self, match_result: Mapping[str, Any], res: NormalizedResult) -> list[str]:
        """Per-map winners (and round-score periods) from ``map_scores`` and ``maps`` / ``map_winners``."""
        winners: list[str] = []
        scores = match_result.get("map_scores")
        if isinstance(scores, list):
            for i, pair in enumerate(scores):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    h, a = float(pair[0]), float(pair[1])
                    res.periods[f"map{i + 1}"] = (h, a)
                    winners.append("home" if h > a else "away" if a > h else "draw")
        maps = match_result.get("maps") or match_result.get("map_winners")
        if not isinstance(maps, list) or not maps:
            return winners
        winners = []
        for i, entry in enumerate(maps):
            if not isinstance(entry, Mapping):
                winners.append(self._party(entry, res))
                continue
            score = entry.get("score")
            pair = (float(score[0]), float(score[1])) if isinstance(score, (list, tuple)) and len(score) == 2 else None
            if pair is not None:
                res.periods[f"map{i + 1}"] = pair
            who = entry.get("winner")
            if who is None and pair is not None:
                who = "home" if pair[0] > pair[1] else "away"
            winners.append(self._party(who, res))
        return winners

    @staticmethod
    def _party(value: Any, res: NormalizedResult) -> str:
        """A side ("home" / "away") when the value names one, else the normalised name."""
        token = norm(value)
        return side_token(token, res) or token

    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,  # noqa: PLR0917 - the base interface
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        if market in (MAP_1_WINNER, MAP_WINNER):
            index = 1 if market == MAP_1_WINNER else int(params.get("map", params.get("map_number", 1)) or 1)
            actual = self._map_winner(result, raw, index)
            if actual is None:
                if result.home is not None and result.away is not None and index > result.home + result.away:
                    return LegStatus.VOID  # the map was never played
                raise SettlementInputError(f"the result has no winner for map {index}")
            return self._same_party(selection, actual, result)
        if market == PISTOL_ROUND:
            pistols = raw.get("pistol_winners")
            n = int(params.get("pistol", 1) or 1)
            actual_raw = pistols[n - 1] if isinstance(pistols, list) and len(pistols) >= n else (
                raw.get("pistol_winner") or raw.get("pistol_round_winner") or raw.get("first_pistol_winner"))
            if actual_raw is None:
                raise SettlementInputError("the result has no pistol round winner")
            return self._same_party(selection, self._party(actual_raw, result), result)
        if market == FIRST_BLOOD:
            bloods = raw.get("first_bloods")
            n = int(params.get("map", 1) or 1)
            actual_raw = bloods[n - 1] if isinstance(bloods, list) and len(bloods) >= n else (raw.get("first_blood") or raw.get("first_blood_team"))
            if actual_raw is None:
                raise SettlementInputError("the result has no first blood")
            return self._same_party(selection, self._party(actual_raw, result), result)
        raise SettlementInputError(f"{self.display_name} has no market {market!r}")

    def _map_winner(self, result: NormalizedResult, raw: Mapping[str, Any], index: int) -> str | None:
        explicit = raw.get(f"map_{index}_winner") or raw.get(f"map{index}_winner")
        if explicit is not None:
            return self._party(explicit, result)
        winners: list[str] = result.extras.get("map_winners") or []
        if len(winners) >= index:
            return winners[index - 1]
        pair = result.periods.get(f"map{index}")
        if pair is not None:
            return "home" if pair[0] > pair[1] else "away" if pair[1] > pair[0] else "draw"
        return None

    @staticmethod
    def _same_party(selection: str, actual: str, result: NormalizedResult) -> LegStatus:
        pick = side_token(norm(selection), result) or norm(selection)
        if pick not in ("home", "away") and actual in ("home", "away"):
            names = {"home": result.home_name, "away": result.away_name}
            if not names[actual]:
                raise SettlementInputError(f"cannot match {selection!r} to a side")
            return LegStatus.WON if pick == names[actual] else LegStatus.LOST
        return LegStatus.WON if pick == actual else LegStatus.LOST


def _expected_maps(dist: Mapping[tuple[int, int], float]) -> float:
    return sum((a + b) * p for (a, b), p in dist.items())


def _calibrate(dist: Mapping[tuple[int, int], float], home: float, away: float, level: float) -> dict[tuple[int, int], float]:
    """Each team's series scores rescaled to the consensus win probability (shapes kept)."""
    h, a, lv = _win_share(dist)
    out: dict[tuple[int, int], float] = {}
    for (x, y), p in dist.items():
        if x > y:
            out[(x, y)] = p * (home / h if h > 0 else 0.0)
        elif y > x:
            out[(x, y)] = p * (away / a if a > 0 else 0.0)
        else:
            out[(x, y)] = p * (level / lv if lv > 0 else 0.0)
    total = sum(out.values())
    return {key: p / total for key, p in out.items()}


def _margin_total_pmfs(dist: Mapping[tuple[int, int], float]) -> tuple[IntPMF, IntPMF]:
    top = max(max(a, b) for a, b in dist)
    margin = [0.0] * (2 * top + 1)
    total = [0.0] * (2 * top + 1)
    for (a, b), p in dist.items():
        margin[a - b + top] += p
        total[a + b] += p
    return IntPMF.from_array(margin, -top), IntPMF.from_array(total, 0)


def _maps_played(dist: Mapping[tuple[int, int], float], best_of: int) -> list[float]:
    """P(map i is played) = P(the series lasts at least i maps)."""
    return [sum(p for (a, b), p in dist.items() if a + b >= i) for i in range(1, best_of + 1)]
