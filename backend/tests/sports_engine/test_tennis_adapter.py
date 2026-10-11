"""
Tennis adapter tests (Group 79): the exact point-to-match Markov chain, surfaces, weather, markets, in-play and
settlement.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import statistics
import time
from functools import cache
from typing import Any

import pytest

from app.domain.sports.bet_catalog import BetType, markets_for
from app.domain.sports.settlement import SettlementInputError
from app.domain.sports.tennis_adapter import (
    SURFACE_SPEEDS,
    TennisSportAdapter,
    game_probability_from,
    hold_probability,
    match_distribution,
    set_outcomes,
    surface_tour_serve_probability,
    tiebreak_probability_from,
    tiebreak_server,
)
from app.schemas.bet_types import LegStatus

ADAPTER = TennisSportAdapter()
STATS_A = {"serve_points_won_pct": 0.67, "return_points_won_pct": 0.39}
STATS_B = {"serve_points_won_pct": 0.64, "return_points_won_pct": 0.36}


def brute_tiebreak(p_a: float, p_b: float, first: str, target: int) -> float:
    """Independent check: play the tiebreak point by point (server by the 1-2-2 rule) up to a long horizon."""

    @cache
    def win(i: int, j: int) -> float:
        if i >= target and i - j >= 2:
            return 1.0
        if j >= target and j - i >= 2:
            return 0.0
        if i + j > 240:
            return 0.5  # reaching 240 points carries mass below 1e-30
        n = i + j
        server = first if n == 0 or ((n - 1) // 2) % 2 == 1 else ("b" if first == "a" else "a")
        point = p_a if server == "a" else 1.0 - p_b
        return point * win(i + 1, j) + (1.0 - point) * win(i, j + 1)

    return win(0, 0)


# ================================================================ identity
def test_identity_and_class_attributes() -> None:
    assert ADAPTER.sport_key == "tennis"
    assert ADAPTER.display_name == "Tennis"
    assert ADAPTER.odds_api_family == "tennis"
    assert ADAPTER.kelly_multiplier == 0.30
    assert ADAPTER.has_draw is False
    assert ADAPTER.model_family == "O'Malley-Barnett Markov"
    assert "set_betting_correct_score" in ADAPTER.standard_markets


def test_every_catalogue_market_for_offered_bet_types_is_settleable() -> None:
    supported = set(ADAPTER.supported_markets)
    for bet_type in ADAPTER.BET_TYPES:
        for market in markets_for(BetType(bet_type), "tennis"):
            assert market in supported, (bet_type, market)


# ================================================================ game
def test_hold_probability_closed_form() -> None:
    assert ADAPTER.hold_probability(0.75) > 0.80
    assert ADAPTER.hold_probability(0.75) == pytest.approx(0.94921875, abs=1e-12)
    assert hold_probability(0.5) == pytest.approx(0.5)
    assert hold_probability(0.0) == 0.0 and hold_probability(1.0) == 1.0
    for p in (0.3, 0.55, 0.62, 0.7, 0.9):
        q = 1.0 - p
        spec = p ** 4 + 4 * p ** 4 * q + 10 * p ** 4 * q ** 2 + 20 * p ** 5 * q ** 3 / (1 - 2 * p * q)
        assert hold_probability(p) == pytest.approx(spec, abs=1e-12)
        assert hold_probability(p) + hold_probability(q) == pytest.approx(1.0, abs=1e-12)


def test_game_from_point_scores() -> None:
    p, q = 0.63, 0.37
    deuce = p * p / (p * p + q * q)
    assert game_probability_from(p, 0, 0) == pytest.approx(hold_probability(p), abs=1e-12)
    assert game_probability_from(p, 3, 3) == pytest.approx(deuce)
    assert game_probability_from(p, 5, 5) == pytest.approx(deuce)
    assert game_probability_from(p, 4, 3) == pytest.approx(p + q * deuce)
    assert game_probability_from(p, 3, 4) == pytest.approx(p * deuce)
    assert game_probability_from(p, 4, 2) == 1.0 and game_probability_from(p, 1, 4) == 0.0
    assert game_probability_from(p, 3, 0) > game_probability_from(p, 0, 0) > game_probability_from(p, 0, 3)


# ================================================================ tiebreak
def test_tiebreak_serve_rotation() -> None:
    assert [tiebreak_server("a", n) for n in range(7)] == ["a", "b", "b", "a", "a", "b", "b"]


@pytest.mark.parametrize(("p_a", "p_b"), [(0.65, 0.62), (0.7, 0.55), (0.6, 0.6), (0.52, 0.71)])
@pytest.mark.parametrize("target", [7, 10])
def test_tiebreak_exact_against_point_by_point(p_a: float, p_b: float, target: int) -> None:
    for first in ("a", "b"):
        assert tiebreak_probability_from(p_a, p_b, first, target=target) == pytest.approx(brute_tiebreak(p_a, p_b, first, target), abs=1e-12)


def test_tiebreak_properties() -> None:
    assert ADAPTER.tiebreak_probability(0.64, 0.64) == pytest.approx(0.5)
    # Newton-Keller: who serves first does not change the winner's probability
    assert ADAPTER.tiebreak_probability(0.7, 0.6, "a") == pytest.approx(ADAPTER.tiebreak_probability(0.7, 0.6, "b"), abs=1e-12)
    # the longer match tiebreak favours the better player more
    assert ADAPTER.tiebreak_probability(0.68, 0.62, target=10) > ADAPTER.tiebreak_probability(0.68, 0.62, target=7) > 0.5
    a_two, b_two = 0.7 * 0.4, 0.3 * 0.6
    assert tiebreak_probability_from(0.7, 0.6, "a", points_a=6, points_b=6) == pytest.approx(a_two / (a_two + b_two))
    assert tiebreak_probability_from(0.7, 0.6, "a", points_a=7, points_b=5) == 1.0


# ================================================================ set and match
def test_set_outcomes_law() -> None:
    outs = set_outcomes(0.66, 0.6, "a")
    assert sum(o.prob for o in outs) == pytest.approx(1.0, abs=1e-12)
    legal = {(6, g) for g in range(5)} | {(7, 5), (7, 6)}
    for o in outs:
        score = (o.games_a, o.games_b) if o.winner == "a" else (o.games_b, o.games_a)
        assert score in legal
        assert o.tiebreak == (score == (7, 6))
        games = o.games_a + o.games_b
        assert o.next_server == ("a" if games % 2 == 0 else "b")
    p6_0 = next(o.prob for o in outs if (o.winner, o.games_a, o.games_b) == ("a", 6, 0))
    assert p6_0 == pytest.approx((hold_probability(0.66) * (1 - hold_probability(0.6))) ** 3)
    assert ADAPTER.set_probability(0.66, 0.6, "a") == pytest.approx(ADAPTER.set_probability(0.66, 0.6, "b"), abs=1e-12)
    assert ADAPTER.set_probability(0.62, 0.62) == pytest.approx(0.5)


def test_set_from_mid_set_state() -> None:
    outs = set_outcomes(0.66, 0.6, "b", games_a=5, games_b=5)
    assert sum(o.prob for o in outs) == pytest.approx(1.0)
    assert {(o.games_a, o.games_b) for o in outs} <= {(7, 5), (5, 7), (7, 6), (6, 7)}
    tb = set_outcomes(0.66, 0.6, "a", tiebreak_points=(6, 6))
    assert tb[0].prob == pytest.approx(tiebreak_probability_from(0.66, 0.6, "a", points_a=6, points_b=6))


@pytest.mark.parametrize(("p_a", "p_b"), [(0.66, 0.6), (0.6, 0.64), (0.7, 0.7)])
def test_match_probability_equals_set_formula(p_a: float, p_b: float) -> None:
    s = ADAPTER.set_probability(p_a, p_b)
    bo3 = match_distribution(p_a, p_b, best_of=3, first_server="a", final_set_tiebreak=7)
    assert bo3.win_a == pytest.approx(s * s * (3.0 - 2.0 * s), abs=1e-12)
    bo5 = match_distribution(p_a, p_b, best_of=5, first_server="b", final_set_tiebreak=7)
    q = 1.0 - s
    assert bo5.win_a == pytest.approx(s ** 3 * (1 + 3 * q + 6 * q * q), abs=1e-12)
    assert sum(bo3.set_scores.values()) == pytest.approx(1.0)
    assert bo3.games.sum() == pytest.approx(1.0)
    assert bo3.set_scores[(2, 0)] == pytest.approx(s * s, abs=1e-12)


def test_final_set_match_tiebreak_changes_only_the_decider() -> None:
    seven = match_distribution(0.68, 0.62, best_of=5, first_server="a", final_set_tiebreak=7)
    ten = match_distribution(0.68, 0.62, best_of=5, first_server="a", final_set_tiebreak=10)
    assert ten.win_a > seven.win_a
    assert ten.set_scores[(3, 0)] == pytest.approx(seven.set_scores[(3, 0)])


def test_games_distribution_is_symmetric_for_equal_players() -> None:
    res = ADAPTER.calculate_fair_odds({}, {}, {"best_of": 3})
    m = res.market_probabilities
    assert res.home_win_prob == pytest.approx(0.5, abs=1e-4)
    assert m["game_handicap_player_a_+1.5"] == pytest.approx(m["game_handicap_player_b_+1.5"], abs=2e-4)
    assert m["set_betting_2-0"] == pytest.approx(m["set_betting_0-2"], abs=1e-4)


# ================================================================ serve probabilities and surfaces
def test_surface_calibrates_tour_hold_rates() -> None:
    expected = {"grass": 0.84, "fast_hard": 0.79, "medium_hard": 0.75, "slow_hard": 0.71, "clay": 0.66, "indoor_hard": 0.805}
    for surface, hold in expected.items():
        assert hold_probability(surface_tour_serve_probability(SURFACE_SPEEDS[surface])) == pytest.approx(hold, abs=1e-6)
        res = ADAPTER.calculate_fair_odds({}, {}, {"surface": surface})
        assert res.sport_specific_metrics["player_a_hold_prob"] == pytest.approx(hold, abs=1e-3)
        assert res.sport_specific_metrics["surface_speed_index"] == SURFACE_SPEEDS[surface]
    grass = ADAPTER.calculate_fair_odds({}, {}, {"surface": "grass"}).market_probabilities["tiebreak_in_match_yes"]
    clay = ADAPTER.calculate_fair_odds({}, {}, {"surface": "clay"}).market_probabilities["tiebreak_in_match_yes"]
    assert grass > clay
    assert ADAPTER.calculate_fair_odds({}, {}, {"surface": "hard"}).sport_specific_metrics["surface"] == "medium_hard"


def test_reference_assertions() -> None:
    res = ADAPTER.calculate_fair_odds(
        home_stats={"first_serve_win_pct": 0.76},
        away_stats={"first_serve_win_pct": 0.65},
        context={"surface": "grass", "best_of": 3},
    )
    assert res.home_win_prob > 0.60
    assert res.fair_home_odds < res.fair_away_odds
    m = res.sport_specific_metrics
    assert m["player_a_hold_prob"] > m["player_b_hold_prob"]
    assert m["serve_prob_source"] == "first_serve_components"
    assert m["player_a_raw_serve_prob"] == pytest.approx(0.62 * 0.76 + 0.38 * 0.50, abs=1e-4)
    assert ADAPTER.resolve_market_winner("match_winner", {"winner": "alcaraz"}, "alcaraz") is True
    assert ADAPTER.resolve_market_winner("set_betting_correct_score", {"set_score": "2-0"}, "2-0") is True
    assert ADAPTER.resolve_market_winner("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "player_a", line=-3.5) is True
    assert ADAPTER.resolve_market_winner("first_set_winner", {"first_set_winner": "sinner"}, "sinner") is True


def test_barnett_clarke_combination() -> None:
    res = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"surface": "medium_hard"})
    m = res.sport_specific_metrics
    assert m["serve_prob_source"] == "barnett_clarke"
    tour_return = 1.0 - (0.62 * 0.72 + 0.38 * 0.50)
    assert m["player_a_raw_serve_prob"] == pytest.approx(0.67 - (0.36 - tour_return), abs=1e-4)
    assert m["player_b_raw_serve_prob"] == pytest.approx(0.64 - (0.39 - tour_return), abs=1e-4)
    assert res.home_win_prob > 0.75
    better_return = ADAPTER.calculate_fair_odds({**STATS_A, "return_points_won_pct": 0.42}, STATS_B, {"surface": "medium_hard"})
    assert better_return.home_win_prob > res.home_win_prob


def test_monotone_in_serve_quality() -> None:
    probs = [ADAPTER.calculate_fair_odds({"first_serve_win_pct": x}, {}, {}).home_win_prob for x in (0.64, 0.70, 0.74, 0.80)]
    assert probs == sorted(probs) and probs[0] < 0.5 < probs[-1]


def test_best_of_five_favours_the_stronger_player() -> None:
    bo3 = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"best_of": 3})
    bo5 = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"best_of": 5})
    assert bo5.home_win_prob > bo3.home_win_prob
    assert bo5.sport_specific_metrics["final_set_tiebreak"] == 10
    assert bo3.sport_specific_metrics["final_set_tiebreak"] == 7
    assert bo5.expected_total_score > bo3.expected_total_score


def test_weather_moves_the_serve() -> None:
    def hold(weather: dict[str, Any]) -> float:
        return ADAPTER.calculate_fair_odds({}, {}, {"weather": weather}).sport_specific_metrics["player_a_hold_prob"]

    base = ADAPTER.calculate_fair_odds({}, {}, {}).sport_specific_metrics
    assert "weather_serve_logit" not in base
    assert hold({"temperature_c": 36.0}) > base["player_a_hold_prob"]
    assert hold({"altitude_m": 2600.0}) > base["player_a_hold_prob"]
    assert hold({"wind_kph": 40.0}) < base["player_a_hold_prob"]
    assert hold({"wind_kph": 40.0, "indoor": True}) == base["player_a_hold_prob"]
    assert hold({"friction": 1.2, "wind_kph": 60.0}) > base["player_a_hold_prob"]  # The Wire's factor replaces our terms


def test_sub_models() -> None:
    res = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {})
    assert {e.name for e in res.sub_models} == {"omalley_barnett_markov", "surface_elo", "dominance_ratio_logistic"}
    assert sum(e.weight for e in res.sub_models) == pytest.approx(1.0)
    m = res.sport_specific_metrics
    assert m["elo_source"] == "points_dominance" and m["dominance_source"] == "season_stats"
    assert m["dominance_ratio_a"] == pytest.approx(0.39 / 0.33, abs=1e-4)
    rated = ADAPTER.calculate_fair_odds({**STATS_A, "elo": 1900, "surface_elo": 2000}, {**STATS_B, "elo": 2100, "surface_elo": 2100}, {})
    elo = next(e for e in rated.sub_models if e.name == "surface_elo")
    assert rated.sport_specific_metrics["elo_diff"] == -150.0
    assert elo.home_win_prob == pytest.approx(1.0 / (1.0 + 10 ** (150 / 400)))
    reweighted = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"model_weights": {"surface_elo": 0.8}})
    assert reweighted.home_win_prob != res.home_win_prob


def test_benching_below_three_sub_models_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"model_weights": {"surface_elo": 0.0}})


# ================================================================ markets
@pytest.mark.parametrize("best_of", [3, 5])
def test_markets_are_coherent(best_of: int) -> None:
    res = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"best_of": best_of, "surface": "fast_hard"})
    m = res.market_probabilities
    assert res.home_win_prob + res.away_win_prob == pytest.approx(1.0, abs=1e-3)
    assert all(0.0 <= v <= 1.0 for v in m.values())
    sets = {k: v for k, v in m.items() if k.startswith("set_betting_")}
    assert len(sets) == (4 if best_of == 3 else 6)
    assert sum(sets.values()) == pytest.approx(1.0, abs=5e-4)
    need = best_of // 2 + 1
    assert m["straight_sets_player_a"] == m[f"set_betting_{need}-0"]
    a_sets = sum(v for k, v in sets.items() if int(k.split("_")[-1].split("-")[0]) == need)
    markov = next(e for e in res.sub_models if e.name == "omalley_barnett_markov")
    assert a_sets == pytest.approx(markov.home_win_prob, abs=5e-4)
    for key, value in m.items():
        if "_over_" in key:
            assert value + m[key.replace("_over_", "_under_")] == pytest.approx(1.0, abs=2e-4)
    for h in (1.5, 2.5, 3.5):
        assert m[f"game_handicap_player_a_-{h:g}"] + m[f"game_handicap_player_b_+{h:g}"] == pytest.approx(1.0, abs=2e-4)
    assert m["first_set_player_a"] + m["first_set_player_b"] == pytest.approx(1.0)
    assert m["tiebreak_in_match_yes"] + m["tiebreak_in_match_no"] == pytest.approx(1.0)
    assert m["game_handicap_player_a_-3.5"] < m["game_handicap_player_a_-1.5"] < m["game_handicap_player_a_+1.5"]
    lines = (2.5,) if best_of == 3 else (3.5, 4.5)
    for line in lines:
        assert f"total_sets_over_{line:g}" in m
    assert any(k.startswith("total_games_over_") for k in m)


def test_pricing_is_fast() -> None:
    for best_of in (3, 5):
        times = []
        for _ in range(7):
            start = time.perf_counter()
            ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"best_of": best_of})
            times.append(time.perf_counter() - start)
        assert statistics.median(times) < 0.025


# ================================================================ in-play
def test_in_play_start_equals_pre_match_with_known_server() -> None:
    pre = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {"first_server": "b"})
    markov = next(e for e in pre.sub_models if e.name == "omalley_barnett_markov")
    live = ADAPTER.in_play_win_probability(STATS_A, STATS_B, {"server": "b"}, {})
    assert live.home_win_prob == pytest.approx(markov.home_win_prob, abs=1e-9)


def test_in_play_terminal_and_monotone() -> None:
    def live(state: dict[str, Any], ctx: dict[str, Any] | None = None) -> float:
        r = ADAPTER.in_play_win_probability(STATS_A, STATS_B, state, ctx)
        assert r.home_win_prob + r.away_win_prob == pytest.approx(1.0)
        return r.home_win_prob

    assert live({"sets_a": 2, "sets_b": 1}) == 1.0
    assert live({"sets_a": 0, "sets_b": 2}) == 0.0
    assert live({"sets_a": 2, "sets_b": 2}, {"best_of": 5}) < 1.0
    start = live({})
    assert live({"sets_a": 1}) > start > live({"sets_b": 1})
    assert live({"games_a": 5, "games_b": 2, "server": "a"}) > live({"games_a": 2, "games_b": 5, "server": "a"})
    assert live({"points_a": 40, "points_b": 0, "server": "b"}) > live({"points_a": 0, "points_b": 40, "server": "b"})
    assert live({"points_a": "AD", "points_b": 40, "server": "a"}) == pytest.approx(live({"points_a": 4, "points_b": 3, "server": "a"}))
    tb = live({"sets_a": 1, "sets_b": 1, "games_a": 6, "games_b": 6, "points_a": 6, "points_b": 2, "server": "b"})
    assert tb > live({"sets_a": 1, "sets_b": 1, "games_a": 6, "games_b": 6, "points_a": 2, "points_b": 6, "server": "b"})
    long_tb = live({"sets_a": 1, "sets_b": 1, "games_a": 6, "games_b": 6, "points_a": 15, "points_b": 14, "in_tiebreak": True, "server": "a"})
    assert 0.5 < long_tb < 1.0  # 15 is a tiebreak point count, not a game call


def test_in_play_exact_set_point_value() -> None:
    """5-4, A serving at 40-0: A wins the set by holding from 40-0, else the set continues at 5-5."""
    p_a = ADAPTER.calculate_fair_odds(STATS_A, STATS_B, {}).sport_specific_metrics["player_a_serve_prob"]
    hold = game_probability_from(p_a, 3, 0)
    live = ADAPTER.in_play_win_probability(STATS_A, STATS_B, {"sets_a": 1, "games_a": 5, "games_b": 4, "points_a": 40, "server": "a"}, {})
    assert live.detail["set_win_prob_a"] > hold
    assert live.home_win_prob > live.detail["set_win_prob_a"]
    with pytest.raises(ValueError, match="server"):
        ADAPTER.in_play_win_probability(STATS_A, STATS_B, {"server": "c"}, {})


# ================================================================ settlement
FULL = {"sets": [[6, 4], [6, 7], [7, 6]], "winner": "a"}


@pytest.mark.parametrize(("market", "result", "selection", "line", "expected"), [
    ("match_winner", {"winner": "alcaraz"}, "alcaraz", None, LegStatus.WON),
    ("match_winner", {"winner": "alcaraz"}, "sinner", None, LegStatus.LOST),
    ("match_winner", FULL, "player_a", None, LegStatus.WON),
    ("moneyline", {"sets": [[4, 6], [3, 6]]}, "b", None, LegStatus.WON),
    ("set_betting_correct_score", {"set_score": "2-0"}, "2-0", None, LegStatus.WON),
    ("set_betting_correct_score", {"set_score": "2-1"}, "2-0", None, LegStatus.LOST),
    ("set_betting", FULL, "2:1", None, LegStatus.WON),
    ("correct_score", FULL, "2-1", None, LegStatus.WON),
    ("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "player_a", -3.5, LegStatus.WON),
    ("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "a", -5.0, LegStatus.VOID),
    ("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "b", 5.25, LegStatus.HALF_WON),
    ("game_handicap", FULL, "b", 1.5, LegStatus.LOST),
    ("games_handicap", FULL, "player_b", 2.5, LegStatus.WON),
    ("total_games_over_under", {"total_games": 22}, "over", 21.5, LegStatus.WON),
    ("total_games_over_under", FULL, "over", 36.0, LegStatus.VOID),
    ("total_games", FULL, "over", 36.25, LegStatus.HALF_LOST),
    ("first_set_winner", {"first_set_winner": "sinner"}, "sinner", None, LegStatus.WON),
    ("first_set_winner", {"first_set_winner": "sinner"}, "alcaraz", None, LegStatus.LOST),
    ("first_set_winner", FULL, "player_a", None, LegStatus.WON),
    ("first_set_winner", FULL, "b", None, LegStatus.LOST),
    ("tiebreak_in_match", FULL, "yes", None, LegStatus.WON),
    ("tiebreak_in_match", {"sets": [[6, 4], [6, 3]]}, "no", None, LegStatus.WON),
    ("tiebreak_yes_no", {"tiebreaks": 0}, "yes", None, LegStatus.LOST),
    ("total_sets", FULL, "over", 2.5, LegStatus.WON),
    ("total_sets", {"set_score": "2-0"}, "under", 2.5, LegStatus.WON),
    ("total_sets_over_under", {"total_sets": 4}, "over", 3.5, LegStatus.WON),
    ("set_winner:set2", FULL, "b", None, LegStatus.WON),
    ("first_set_total_games", FULL, "over", 9.5, LegStatus.WON),
    ("set_handicap", FULL, "b", 1.5, LegStatus.WON),
    ("player_aces", {"sets": [[6, 4]], "player_stats": {"Isner": {"aces": 21}}}, "over", 19.5, LegStatus.WON),
])
def test_settlement(market: str, result: dict[str, Any], selection: str, line: float | None, expected: LegStatus) -> None:
    params = {"player": "isner"} if market == "player_aces" else None
    assert ADAPTER.settle_market(market, result, selection, line, params) == expected


def test_retirement_and_walkover_rules() -> None:
    retired = {"sets": [[6, 4], [2, 1]], "winner": "a", "status": "retired"}
    assert ADAPTER.settle_market("match_winner", retired, "a") == LegStatus.WON
    assert ADAPTER.settle_market("match_winner", retired, "b") == LegStatus.LOST
    for market, selection, line in (("total_games", "over", 10.5), ("game_handicap", "a", -1.5), ("set_betting", "1-0", None),
                                    ("first_set_winner", "a", None), ("tiebreak_in_match", "no", None)):
        assert ADAPTER.settle_market(market, retired, selection, line) == LegStatus.VOID
    walkover = {"winner": "a", "status": "walkover"}
    assert ADAPTER.settle_market("match_winner", walkover, "a") == LegStatus.VOID
    assert ADAPTER.normalize_result({"sets": [[6, 4], [2, 1]]}).home == 1.0  # the unfinished set is not counted


def test_settlement_errors() -> None:
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("game_handicap", {"winner": "a"}, "a", -1.5)
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("total_games", {"total_games": 20}, "over")
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("set_betting", {"set_score": "two-nil"}, "2-0")
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("most_aces_team", {"winner": "a"}, "a")
    with pytest.raises(SettlementInputError):
        ADAPTER.normalize_result({"sets": [[6, 4, 1]]})
    assert ADAPTER.resolve_market_winner("tiebreak_in_match", {"winner": "a"}, "yes") is False
