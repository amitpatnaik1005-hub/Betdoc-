"""BETDOC SCORE PREDICTION: the flagship score view of every sport (Group 102, point b.16). Developed for Amit Ashok
Kumar Patnaik.

No new model and no new constant: every number comes from an engine BetDoc already prices with, so a predicted score
and the prices on the desk can never disagree.

* Football: a full scoreline matrix (0..10 goals a side, the tail folded into 10), from one of three sources:
  ``market`` (Dixon-Coles rates fitted to de-vigged 1X2 and totals: ``app.domain.oracle.match_model.fit_rates``),
  ``xg`` (Dixon-Coles on the caller's own expected goals) or ``stats`` (the Group 79 soccer adapter's mixed grid of
  Dixon-Coles, bivariate Poisson and xG Poisson). From it: the 0-5 heatmap (exact cells, not re-normalised; the mass
  outside it is reported), the most likely scorelines, and every market Ashoka prices (1X2, BTTS, totals and Asian
  handicaps on the quarter ball, double chance, draw no bet) settled cell by cell with pushes and half results, keyed
  exactly as the Oracle board keys its legs so a score-backed leg can be added to a parlay.
* Cricket (limited overs): the adapter's exact ball-by-ball Markov innings (dew, pitch wear, The Wire's weather,
  umpires and absences already inside): each innings' median and interval, its powerplay, its expected runs per
  phase (powerplay / middle / death overs), and the chase solved exactly against a stated target.
* Tennis, basketball, MMA: the adapters' own Markov (O'Malley-Barnett), Four Factors and finish-hazard outputs,
  regrouped as set scores / total games / tiebreaks, projected points / lines, and method / rounds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from app.domain.oracle.markets import SELECTIONS, LegResult, MarketKind, MarketRef
from app.domain.oracle.match_model import (
    DEFAULT_RHO,
    MAX_GOALS,
    MarketTargets,
    dixon_coles_matrix,
    fit_rates,
    one_x_two,
    outcome_distribution,
)
from app.domain.sports.basketball_adapter import BasketballSportAdapter
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.context import MatchContext
from app.domain.sports.cricket_adapter import PITCH_SIGMA, CricketSportAdapter, InningsDistribution, chase_probabilities, pitch_mixture
from app.domain.sports.quant import IntPMF, blend
from app.domain.sports.soccer_adapter import SoccerSportAdapter

FootballSource = Literal["market", "xg", "stats"]
GRID = MAX_GOALS + 1
TOTAL_LINES = tuple(x / 4 for x in range(2, 23))  # 0.5 .. 5.5 on the quarter ball
HANDICAP_LINES = tuple(x / 4 for x in range(-12, 13))  # the home side's handicap, -3 .. +3 on the quarter ball
_SIMPLE = (MarketKind.MATCH_ODDS, MarketKind.BTTS, MarketKind.DOUBLE_CHANCE, MarketKind.DRAW_NO_BET)


class PredictionInputError(ValueError):
    """The inputs cannot produce a prediction (an empty market, a format the engine does not project)."""


def _r(x: float, places: int = 6) -> float:
    return round(float(x), places)


# ================================================================ football
@dataclass(frozen=True, slots=True)
class PricedSelection:
    """One selection under the scoreline matrix: every settlement result and the break-even (fair) decimal price."""

    market: str  # MarketRef.key: the Oracle board's market key ("Totals 2.5", "Asian Handicap -0.25")
    kind: str
    line: float | None
    selection: str
    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float
    fair_odds: float | None  # where the expected return is exactly the stake; None when it can never win

    @property
    def win_probability(self) -> float:
        return self.win + self.half_win


def price_selection(matrix: np.ndarray, ref: MarketRef, selection: str) -> PricedSelection:
    d = outcome_distribution(matrix, ref, selection)
    won, half_won, void, half_lost, lost = (d[r] for r in (LegResult.WON, LegResult.HALF_WON, LegResult.VOID, LegResult.HALF_LOST, LegResult.LOST))
    up = won + half_won / 2.0
    fair = (1.0 - void - half_lost / 2.0 - half_won / 2.0) / up if up > 1e-12 else None
    return PricedSelection(ref.key, ref.kind.value, ref.line, selection, _r(won), _r(half_won), _r(void), _r(half_lost), _r(lost),
                           None if fair is None else round(fair, 4))


@dataclass(frozen=True, slots=True)
class Scoreline:
    home: int
    away: int
    probability: float

    @property
    def label(self) -> str:
        return f"{self.home}-{self.away}"


@dataclass(frozen=True, slots=True)
class FootballPrediction:
    source: FootballSource
    expected_goals: tuple[float, float]
    heatmap: list[list[float]]  # P(home = row, away = col) for 0..heatmap_goals, exact (not re-normalised)
    heatmap_mass: float  # the heatmap's share of all outcomes; the rest has a side above heatmap_goals
    top_scorelines: list[Scoreline]
    home_win: float
    draw: float
    away_win: float
    btts_yes: float
    over: dict[float, float]  # goal line -> P(over), for the x.5 lines
    markets: list[PricedSelection]
    rho: float | None
    fit_error: float | None = None  # market source: squared error of the fitted rates against the targets
    detail: dict[str, Any] = field(default_factory=dict)


def football_from_matrix(matrix: np.ndarray, *, source: FootballSource, heatmap_goals: int, top: int, rho: float | None,
                         fit_error: float | None = None, detail: Mapping[str, Any] | None = None) -> FootballPrediction:
    """Everything the score view shows, read off one 0..MAX_GOALS scoreline matrix."""
    m = np.asarray(matrix, dtype=float)
    if m.shape != (GRID, GRID) or not np.isfinite(m).all() or m.min() < 0 or m.sum() <= 0:
        raise PredictionInputError(f"a scoreline matrix is {GRID}x{GRID}, finite and non-negative")
    m = m / m.sum()
    n = max(1, min(heatmap_goals, MAX_GOALS - 1))
    cells = m[: n + 1, : n + 1]
    exact = m[:MAX_GOALS, :MAX_GOALS]  # row/col MAX_GOALS hold the folded tail: never a named scoreline
    order = np.argsort(exact.ravel())[::-1][: max(1, top)]
    tops = [Scoreline(int(i // MAX_GOALS), int(i % MAX_GOALS), _r(exact.ravel()[i])) for i in order]
    home, draw, away = one_x_two(m)
    goals = np.arange(GRID)
    total = np.add.outer(goals, goals)
    markets = [price_selection(m, MarketRef(kind), s) for kind in _SIMPLE for s in SELECTIONS[kind]]
    markets += [price_selection(m, MarketRef(MarketKind.TOTALS, line), s) for line in TOTAL_LINES for s in ("OVER", "UNDER")]
    markets += [price_selection(m, MarketRef(MarketKind.ASIAN_HANDICAP, line), s) for line in HANDICAP_LINES for s in ("HOME", "AWAY")]
    return FootballPrediction(
        source=source,
        expected_goals=(_r(float(m.sum(axis=1) @ goals), 4), _r(float(m.sum(axis=0) @ goals), 4)),
        heatmap=[[_r(v) for v in row] for row in cells],
        heatmap_mass=_r(cells.sum()),
        top_scorelines=tops,
        home_win=_r(home), draw=_r(draw), away_win=_r(away),
        btts_yes=_r(m[1:, 1:].sum()),
        over={line: _r(m[total > line].sum()) for line in (0.5, 1.5, 2.5, 3.5, 4.5)},
        markets=markets,
        rho=rho,
        fit_error=fit_error,
        detail=dict(detail or {}),
    )


def football_from_market(targets: MarketTargets, *, heatmap_goals: int, top: int, rho: float = DEFAULT_RHO) -> FootballPrediction:
    """Dixon-Coles rates fitted to the de-vigged market: the scorelines the prices imply."""
    p = (targets.home, targets.draw, targets.away)
    if any(not 0.0 < x < 1.0 for x in p):
        raise PredictionInputError("a market source needs home, draw and away probabilities strictly between 0 and 1")
    s = sum(p)
    norm = MarketTargets(p[0] / s, p[1] / s, p[2] / s, {float(k): float(v) for k, v in targets.totals.items() if 0.0 < v < 1.0})
    lam, mu, err = fit_rates(norm, rho)
    return football_from_matrix(dixon_coles_matrix(lam, mu, rho), source="market", heatmap_goals=heatmap_goals, top=top, rho=rho,
                                fit_error=_r(err, 8), detail={"fitted_rates": [_r(lam, 4), _r(mu, 4)], "targets": {
                                    "home": _r(norm.home), "draw": _r(norm.draw), "away": _r(norm.away),
                                    "totals_over": {f"{k:g}": _r(v) for k, v in sorted(norm.totals.items())}}})


def football_from_xg(home_xg: float, away_xg: float, *, heatmap_goals: int, top: int, rho: float = DEFAULT_RHO) -> FootballPrediction:
    if not (0.0 < home_xg <= 8.0 and 0.0 < away_xg <= 8.0):
        raise PredictionInputError("expected goals must be above 0 and at most 8")
    return football_from_matrix(dixon_coles_matrix(home_xg, away_xg, rho), source="xg", heatmap_goals=heatmap_goals, top=top, rho=rho)


def football_from_stats(adapter: SoccerSportAdapter, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                        context: Mapping[str, Any] | None, *, heatmap_goals: int, top: int) -> FootballPrediction:
    """The soccer adapter's mixed scoreline grid (Dixon-Coles, bivariate Poisson, xG Poisson) and its consensus."""
    ctx = MatchContext.parse(dict(context or {}))
    estimates, grid, metrics = adapter.model(dict(home_stats), dict(away_stats), ctx)
    consensus = blend(estimates, dict(ctx.model_weights))
    return football_from_matrix(grid.matrix, source="stats", heatmap_goals=heatmap_goals, top=top, rho=metrics.get("dixon_coles_rho"),
                                detail={"model_rates": [metrics.get("home_xg"), metrics.get("away_xg")], "consensus": consensus.as_dict(),
                                        "model_family": adapter.model_family})


# ================================================================ cricket
@dataclass(frozen=True, slots=True)
class InningsProjection:
    side: Literal["home", "away"]
    median: int
    interval: tuple[int, int]
    mean: float
    powerplay_median: int | None
    powerplay_interval: tuple[int, int] | None
    phase_expected_runs: dict[str, float]  # powerplay / middle / death
    expected_sixes: float


@dataclass(frozen=True, slots=True)
class ChaseProjection:
    target: int | None  # None: the chase against the model's whole first-innings distribution
    source: Literal["stated", "model"]
    win: float  # the target reached
    tie: float  # level when the chase ends
    lose: float
    win_with_super_over: float | None  # T20: a tie goes to a super over, priced as a coin flip as the adapter does


@dataclass(frozen=True, slots=True)
class CricketPrediction:
    format: str
    overs: int
    batting_first: str
    first_innings: InningsProjection
    second_innings: InningsProjection  # the chasing side's full innings, as if it batted all its overs
    chase: ChaseProjection
    match_winner: dict[str, float]  # the adapter's sub-model consensus: home / away / tie
    coverage: float
    conditions: dict[str, Any]


def _bounds(pmf: IntPMF, coverage: float) -> tuple[int, int]:
    tail = (1.0 - coverage) / 2.0
    return pmf.quantile(tail), pmf.quantile(1.0 - tail)


def _innings(side: Literal["home", "away"], inn: InningsDistribution, sigma: float, coverage: float) -> InningsProjection:
    total = pitch_mixture(inn.total, sigma)  # the shared pitch factor: the spread seen in the field, as the adapter prices
    pp = pitch_mixture(inn.powerplay, sigma) if inn.powerplay is not None else None
    return InningsProjection(side, total.quantile(0.5), _bounds(total, coverage), _r(total.mean, 2),
                             None if pp is None else pp.quantile(0.5), None if pp is None else _bounds(pp, coverage),
                             {k: _r(v, 2) for k, v in inn.phase_runs.items()}, _r(inn.expected_sixes, 3))


def predict_cricket(adapter: CricketSportAdapter, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                    context: Mapping[str, Any] | None, *, coverage: float, chase_target: int | None = None) -> CricketPrediction:
    ctx = MatchContext.parse(dict(context or {}))
    setup = adapter.match_setup(home_stats, away_stats, ctx)
    if setup.fmt == "test":
        raise PredictionInputError("innings projections cover limited-overs cricket (T20, ODI); price a Test with /sports/calculate-odds")
    built = adapter.model(home_stats, away_stats, ctx)
    first_side, chase_side = setup.batting_first, setup.chasing
    sigma = PITCH_SIGMA[setup.fmt]
    innings: dict[str, InningsDistribution] = built["innings"]
    consensus = blend(built["estimates"], dict(ctx.model_weights))
    if chase_target is not None:
        if not 1 <= chase_target <= 1000:
            raise PredictionInputError("a chase target is between 1 and 1000 runs")
        outcome = chase_probabilities(setup.conditions(chase_side, 2), need=chase_target, balls_left=setup.overs * 6, wickets=0,
                                      start_ball=0, overs=setup.overs)
        win, tie, source, target = outcome.reached, outcome.level, "stated", chase_target
    else:
        h, t, a = built["grid"].outcome()
        win, tie, source, target = (a if first_side == "home" else h), t, "model", None
    lose = max(0.0, 1.0 - win - tie)
    metrics = built["metrics"]
    conditions = {k: metrics[k] for k in ("dew_impact_applied", "dew_intensity", "dew_scale", "dew_runs_shift", "dew_chase_win_shift",
                                          "pitch_wear", "home_strength", "away_strength", "dls_par_score") if k in metrics}
    conditions.update({k: v for k, v in metrics.items() if k.startswith(("weather_", "umpire_")) or k.endswith("_absence_rating_delta")})
    return CricketPrediction(
        format=setup.fmt, overs=setup.overs, batting_first=first_side,
        first_innings=_innings(first_side, innings[first_side], sigma, coverage),  # type: ignore[arg-type]
        second_innings=_innings(chase_side, innings[chase_side], sigma, coverage),  # type: ignore[arg-type]
        chase=ChaseProjection(target, source, _r(win), _r(tie), _r(lose), _r(win + tie / 2.0) if setup.fmt == "t20" else None),
        match_winner={"home": _r(consensus.home_win_prob), "away": _r(consensus.away_win_prob), "tie": _r(consensus.draw_prob)},
        coverage=coverage, conditions=conditions,
    )


# ================================================================ tennis, basketball, MMA
def _prefixed(markets: Mapping[str, float], prefix: str) -> dict[str, float]:
    return {k[len(prefix):]: _r(v) for k, v in markets.items() if k.startswith(prefix)}


def predict_tennis(adapter: BaseSportAdapter, a_stats: Mapping[str, Any], b_stats: Mapping[str, Any], context: Mapping[str, Any] | None) -> dict[str, Any]:
    res = adapter.calculate_fair_odds(dict(a_stats), dict(b_stats), dict(context or {}))
    mk, mt = res.market_probabilities, res.sport_specific_metrics
    return {
        "player_a_win": res.home_win_prob, "player_b_win": res.away_win_prob, "best_of_sets": mt.get("best_of_sets"),
        "set_scores": _prefixed(mk, "set_betting_"), "expected_total_games": mt.get("expected_total_games"),
        "total_games_over": _prefixed(mk, "total_games_over_"), "tiebreak_in_match": mk.get("tiebreak_in_match_yes"),
        "first_set": {"player_a": mk.get("first_set_player_a"), "player_b": mk.get("first_set_player_b")},
        "serve": {k: mt.get(k) for k in ("player_a_serve_prob", "player_b_serve_prob", "player_a_hold_prob", "player_b_hold_prob", "surface")},
        "model_family": res.model_family,
    }


def predict_basketball(adapter: BasketballSportAdapter, home_stats: Mapping[str, Any], away_stats: Mapping[str, Any],
                       context: Mapping[str, Any] | None, *, coverage: float, spread_line: float | None = None,
                       total_line: float | None = None) -> dict[str, Any]:
    """Projected points from the adapter's blended margin and total; a stated line priced off the same distributions."""
    ctx = MatchContext.parse(dict(context or {}))
    proj = adapter.project(dict(home_stats), dict(away_stats), ctx)
    consensus = blend(proj.estimates, dict(ctx.model_weights))
    total, home_pts, away_pts = proj.total_pmf(), proj.team_pmf("home"), proj.team_pmf("away")
    out: dict[str, Any] = {
        "projected_home_points": _r(home_pts.mean, 1), "projected_away_points": _r(away_pts.mean, 1),
        "projected_total": _r(total.mean, 1), "total_interval": list(_bounds(total, coverage)),
        "projected_margin": _r(proj.margin.mean, 1), "margin_interval": list(_bounds(proj.margin, coverage)),
        "home_win": _r(consensus.home_win_prob), "away_win": _r(consensus.away_win_prob), "overtime": _r(proj.p_overtime),
        "pace": _r(proj.pace, 2), "coverage": coverage, "model_family": adapter.model_family,
    }
    if spread_line is not None:  # the home side's handicap: home covers when margin + line > 0
        o = proj.margin.line_outcome(-spread_line, over=True)
        out["spread"] = {"home_line": spread_line, "home_cover": _r(o.win + o.half_win), "push": _r(o.push), "away_cover": _r(o.loss + o.half_loss)}
    if total_line is not None:
        o = total.line_outcome(total_line, over=True)
        out["total"] = {"line": total_line, "over": _r(o.win + o.half_win), "push": _r(o.push), "under": _r(o.loss + o.half_loss)}
    return out


def predict_mma(adapter: BaseSportAdapter, a_stats: Mapping[str, Any], b_stats: Mapping[str, Any], context: Mapping[str, Any] | None) -> dict[str, Any]:
    res = adapter.calculate_fair_odds(dict(a_stats), dict(b_stats), dict(context or {}))
    mk, mt = res.market_probabilities, res.sport_specific_metrics
    return {
        "fighter_a_win": res.home_win_prob, "fighter_b_win": res.away_win_prob, "draw": res.draw_prob,
        "method": {"ko_tko": mk.get("method_ko_tko"), "submission": mk.get("method_submission"), "decision": mk.get("method_decision"),
                   "draw": mk.get("method_draw")},
        "method_by_fighter": {side: {m: mk.get(f"method_{side}_{m}") for m in ("ko_tko", "submission", "decision")} for side in ("home", "away")},
        "rounds_over": _prefixed(mk, "rounds_over_"), "finish_in_round": _prefixed(mk, "finish_in_round_"),
        "goes_distance": mk.get("goes_distance_yes"), "scheduled_rounds": mt.get("scheduled_rounds"),
        "expected_fight_minutes": mt.get("expected_fight_minutes"), "model_family": res.model_family,
    }


def de_vig_totals(books: Mapping[str, Mapping[str, Any]]) -> dict[float, float]:
    """P(over) per goal line from stored Odds API totals books (``{book: {"outcomes": [{name, price, point}]}}``):
    each book with both sides of a line de-vigged proportionally, then averaged; quarter lines are skipped (a push
    has no single implied probability)."""
    per_line: dict[float, list[float]] = {}
    for book in books.values():
        sides: dict[float, dict[str, float]] = {}
        for o in book.get("outcomes") or []:
            point, price, name = o.get("point"), o.get("price"), str(o.get("name") or "").casefold()
            if point is None or not isinstance(price, (int, float)) or price <= 1.0 or name not in ("over", "under"):
                continue
            sides.setdefault(float(point), {})[name] = float(price)
        for line, pair in sides.items():
            if "over" in pair and "under" in pair and abs(line % 1 - 0.5) < 1e-9:
                over, under = 1.0 / pair["over"], 1.0 / pair["under"]
                per_line.setdefault(line, []).append(over / (over + under))
    return {line: sum(v) / len(v) for line, v in sorted(per_line.items())}


def market_targets_from_store(consensus: Mapping[str, Any], markets: Mapping[str, Any]) -> MarketTargets:
    """The pre-match store's h2h consensus and totals as fitting targets (Group 101 rows)."""
    outcomes = (consensus or {}).get("outcomes") or {}
    if not all(k in outcomes for k in ("home", "draw", "away")):
        raise PredictionInputError("the stored board has no complete three-way (1X2) consensus to fit")
    totals = de_vig_totals(((markets or {}).get("totals") or {}).get("books") or {})
    return MarketTargets(*(float(outcomes[k]["probability"]) for k in ("home", "draw", "away")), totals=totals)
