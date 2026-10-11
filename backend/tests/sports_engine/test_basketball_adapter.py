"""Basketball adapter tests (Group 79): Four Factors, pace, Morey-Pythagorean, overtime-aware margins, context,
in-play and settlement. Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

import math
import time

import pytest

from app.domain.sports.basketball_adapter import (
    FACTOR_WEIGHTS,
    LEAGUE_FACTORS,
    PYTHAGOREAN_EXPONENT,
    TEASER_POINT_SHIFTS,
    BasketballSportAdapter,
    final_margin,
    log5,
)
from app.domain.sports.quant import IntPMF, normal_cdf
from app.schemas.bet_types import LegStatus

UNIVERSAL_BET_TYPES = {
    "moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "teaser", "pleaser", "round_robin", "if_bet",
    "double_action", "futures", "player_props", "game_props", "live_next_event", "draw_no_bet", "btts", "correct_score",
    "asian_handicap", "clean_sheet", "method_of_victory", "round_betting", "map_winner", "total_maps", "first_5_innings",
    "inning_of_highest_score", "alternative_lines",
}
REF_HOME = {"efg_pct": 0.56, "tov_pct": 0.11, "orb_pct": 0.30, "ftr": 0.25, "pace": 102.0}
REF_AWAY = {"efg_pct": 0.50, "tov_pct": 0.14, "orb_pct": 0.24, "ftr": 0.19, "pace": 98.0}
EVEN_HOME = {"off_rating": 115.0, "def_rating": 113.0, "pace": 99.0}
EVEN_AWAY = {"off_rating": 114.0, "def_rating": 113.5, "pace": 100.0}
BOX = {"fgm": 42, "fga": 88, "fg3m": 13, "tov": 13, "fta": 22, "ftm": 17, "orb": 10, "opp_drb": 34}


@pytest.fixture
def adapter() -> BasketballSportAdapter:
    return BasketballSportAdapter()


# ---------------------------------------------------------------- identity
def test_identity_and_catalogue(adapter: BasketballSportAdapter) -> None:
    assert adapter.sport_key == "basketball"
    assert adapter.display_name == "Basketball"
    assert adapter.odds_api_family == "basketball"
    assert adapter.kelly_multiplier == 0.40
    assert adapter.has_draw is False
    assert adapter.model_family == "Dean Oliver Four Factors"
    assert adapter.BET_TYPES <= UNIVERSAL_BET_TYPES
    assert {"teaser", "pleaser", "point_spread", "period_lines", "player_props", "alternative_lines"} <= adapter.BET_TYPES
    assert {"draw_no_bet", "btts", "map_winner"}.isdisjoint(adapter.BET_TYPES)
    assert TEASER_POINT_SHIFTS == adapter.TEASER_POINT_SHIFTS
    assert {6.0, 6.5, 7.0} <= set(TEASER_POINT_SHIFTS)
    assert {"point_spread", "game_total_over_under", "team_totals", "quarter_winner"} <= set(adapter.supported_markets)
    for market in adapter.standard_markets:
        adapter.canonical_market(market)
    assert math.isclose(sum(FACTOR_WEIGHTS.values()), 1.0)
    assert FACTOR_WEIGHTS == {"efg": 0.40, "tov": 0.25, "orb": 0.20, "ftr": 0.15}


# ---------------------------------------------------------------- the reference test (FILE 21)
def test_reference_four_factors_and_altitude(adapter: BasketballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=REF_HOME, away_stats=REF_AWAY, context={"venue_city": "denver"})
    assert res.home_win_prob > res.away_win_prob
    assert res.expected_total_score > 200.0
    assert res.sport_specific_metrics["altitude_points"] == 2.8
    assert adapter.resolve_market_winner("moneyline", {"home_score": 115, "away_score": 108}, "home") is True
    assert adapter.resolve_market_winner("point_spread", {"home_score": 115, "away_score": 108}, "home", line=-4.5) is True
    assert adapter.resolve_market_winner("game_total_over_under", {"home_score": 115, "away_score": 108}, "over", line=220.5) is True
    assert adapter.resolve_market_winner("quarter_winner", {"quarter": 1, "q1_home": 32, "q1_away": 28}, "home") is True


# ---------------------------------------------------------------- formulas
def test_four_factors_from_box_score() -> None:
    f = BasketballSportAdapter.four_factors(BOX)
    assert f["efg"] == pytest.approx((42 + 0.5 * 13) / 88)
    assert f["tov"] == pytest.approx(13 / (88 + 0.44 * 22 + 13))
    assert f["orb"] == pytest.approx(10 / (10 + 34))
    assert f["ftr"] == pytest.approx(17 / 88)


def test_four_factors_fall_back_to_summary_fields_then_league() -> None:
    assert BasketballSportAdapter.four_factors({"efg_pct": 0.57, "tov_pct": 12.0}) == pytest.approx(
        {"efg": 0.57, "tov": 0.12, "orb": LEAGUE_FACTORS["orb"], "ftr": LEAGUE_FACTORS["ftr"]})


def test_four_factor_points_hand_computed() -> None:
    off = BasketballSportAdapter.four_factors(REF_HOME)
    # (0.4 * 0.02 / 0.020 + 0.25 * 0.02 / 0.010 + 0.2 * 0.05 / 0.025 + 0.15 * 0.05 / 0.030) * 9.0
    expected = (0.4 * 1.0 + 0.25 * 2.0 + 0.2 * 2.0 + 0.15 * 0.05 / 0.03) * 9.0
    assert BasketballSportAdapter.four_factor_points(off) == pytest.approx(expected)
    # an opponent that allows a league-average game changes nothing; a leaky defence adds
    league_def = BasketballSportAdapter.defensive_factors({})
    assert BasketballSportAdapter.four_factor_points(off, league_def) == pytest.approx(expected)
    leaky = BasketballSportAdapter.defensive_factors({"def_efg_pct": 0.56})
    assert BasketballSportAdapter.four_factor_points(off, leaky) == pytest.approx(expected + 0.4 * 1.0 * 9.0)


def test_pace_from_box_score(adapter: BasketballSportAdapter) -> None:
    poss = 88 + 0.44 * 22 - 10 + 13
    assert adapter.possessions(BOX) == pytest.approx(poss)
    assert adapter.team_pace(BOX, 99.5) == pytest.approx(48 * (2 * poss) / (2 * (240 / 5)))
    two_games = {**{k: 2 * v for k, v in BOX.items()}, "games": 2, "opp_fga": 180, "opp_fta": 40, "opp_orb": 22, "opp_tov": 28}
    opp_poss = 180 + 0.44 * 40 - 22 + 28
    assert adapter.team_pace(two_games, 99.5) == pytest.approx(48 * (2 * poss + opp_poss) / (2 * (480 / 5)))
    assert adapter.team_pace({"pace": 103.0}, 99.5) == 103.0
    assert adapter.team_pace({}, 99.5) == 99.5


def test_game_pace_and_expected_total(adapter: BasketballSportAdapter) -> None:
    res = adapter.calculate_fair_odds({"pace": 102.0}, {"pace": 98.0}, {"neutral_site": True})
    pace = 102.0 * 98.0 / 99.5
    assert res.sport_specific_metrics["projected_pace"] == pytest.approx(pace, abs=0.01)
    # league-average teams: E[total] = pace / 100 x (114 + 114), plus a small overtime mass
    m = res.sport_specific_metrics
    assert m["home_projected_pts"] + m["away_projected_pts"] == pytest.approx(pace / 100 * 228, abs=0.02)
    assert res.expected_total_score == pytest.approx(pace / 100 * 228 * (1 + m["overtime_probability"] * 5 / 48), abs=0.15)


def test_pythagorean_and_log5() -> None:
    p = BasketballSportAdapter.pythagorean({"points_for": 115.0, "points_against": 110.0}, 100.0, 100.0)
    assert p == pytest.approx(115 ** PYTHAGOREAN_EXPONENT / (115 ** PYTHAGOREAN_EXPONENT + 110 ** PYTHAGOREAN_EXPONENT))
    assert BasketballSportAdapter.pythagorean({}, 118.0, 112.0) == pytest.approx(1 / (1 + (112 / 118) ** 13.91))
    assert log5(0.6, 0.4) == pytest.approx(0.6 * 0.6 / (0.6 * 0.6 + 0.4 * 0.4))
    assert log5(0.5, 0.5) == pytest.approx(0.5)


def test_net_rating_margin_hand_computed(adapter: BasketballSportAdapter) -> None:
    home = {"off_rating": 118.0, "def_rating": 110.0, "pace": 99.5}
    away = {"off_rating": 112.0, "def_rating": 116.0, "pace": 99.5}
    res = adapter.calculate_fair_odds(home, away)
    ortg_h, ortg_a = 118.0 + (116.0 - 114.0), 112.0 + (110.0 - 114.0)
    assert res.sport_specific_metrics["home_ortg_adj"] == pytest.approx(ortg_h)
    assert res.sport_specific_metrics["sub_model_margins"]["net_rating_srs"] == pytest.approx(99.5 / 100 * (ortg_h - ortg_a) + 2.2, abs=1e-3)
    srs = adapter.calculate_fair_odds({**home, "srs": 6.0}, {**away, "srs": -1.0})
    assert srs.sport_specific_metrics["sub_model_margins"]["net_rating_srs"] == pytest.approx(7.0 + 2.2, abs=1e-3)


# ---------------------------------------------------------------- sub-models and probabilities
def test_at_least_three_distinct_sub_models(adapter: BasketballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    names = [s.name for s in res.sub_models]
    assert names == ["four_factors_efficiency", "morey_pythagorean", "net_rating_srs"]
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    elo = adapter.calculate_fair_odds({**EVEN_HOME, "elo": 1650}, {**EVEN_AWAY, "elo": 1500})
    assert [s.name for s in elo.sub_models][-1] == "elo"
    assert sum(s.weight for s in elo.sub_models) == pytest.approx(1.0)
    assert elo.sport_specific_metrics["sub_model_margins"]["elo"] == pytest.approx(150 / 28 + 2.2, abs=1e-3)
    assert elo.consensus is not None and elo.consensus.model_count == 4


def test_probabilities_sum_to_one_and_moneyline_matches_consensus(adapter: BasketballSportAdapter) -> None:
    for home, away in ((REF_HOME, REF_AWAY), (EVEN_HOME, EVEN_AWAY), ({}, {})):
        res = adapter.calculate_fair_odds(home, away)
        assert res.home_win_prob + res.away_win_prob + res.draw_prob == pytest.approx(1.0, abs=1e-3)
        assert res.draw_prob == 0.0 and res.fair_draw_odds is None
        assert res.market_probabilities["moneyline_home"] == pytest.approx(res.home_win_prob, abs=2e-4)
        assert all(0.0 <= p <= 1.0 for p in res.market_probabilities.values())


def test_no_stub_half_probabilities_and_complementary_sides(adapter: BasketballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    mp = res.market_probabilities
    spreads = [k for k in mp if k.startswith("spread_home_") and k.endswith(".5")]
    assert len(spreads) >= 6
    for key in spreads:
        line = float(key.removeprefix("spread_home_"))
        away_key = f"spread_away_{-line:+g}"
        assert mp[key] + mp[away_key] == pytest.approx(1.0, abs=2e-4)
    overs = [k for k in mp if k.startswith("total_over_") and k.endswith(".5")]
    for key in overs:
        assert mp[key] + mp[key.replace("over", "under")] == pytest.approx(1.0, abs=2e-4)
    # the ladder is monotone: laying more points covers less often
    home_lines = sorted((float(k.removeprefix("spread_home_")), v) for k, v in mp.items() if k.startswith("spread_home_"))
    probs = [v for _, v in home_lines]
    assert probs == sorted(probs)
    for key in ("team_total_home_over_", "h1_spread_home_", "h1_total_over_", "q1_total_over_", "alt_spread_home_", "alt_total_over_",
                "teaser_6_home_"):
        assert any(k.startswith(key) for k in mp), key
    assert mp["q1_home"] + mp["q1_tie"] + mp["q1_away"] == pytest.approx(1.0, abs=2e-4)
    assert mp["q1_tie"] > 0.03  # a quarter ties often enough to matter
    assert mp["q1_home_2way"] == pytest.approx(mp["q1_home"] / (mp["q1_home"] + mp["q1_away"]), abs=2e-4)


def test_monotonicity(adapter: BasketballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(REF_AWAY, REF_AWAY).home_win_prob
    better = adapter.calculate_fair_odds({**REF_AWAY, "efg_pct": 0.53}, REF_AWAY).home_win_prob
    assert better > base
    worse_tov = adapter.calculate_fair_odds({**REF_AWAY, "tov_pct": 0.16}, REF_AWAY).home_win_prob
    assert worse_tov < base
    strong = adapter.calculate_fair_odds({"off_rating": 120.0}, {}).home_win_prob
    assert strong > adapter.calculate_fair_odds({"off_rating": 116.0}, {}).home_win_prob


def test_home_advantage_sources(adapter: BasketballSportAdapter) -> None:
    def hfa(ctx: dict[str, object]) -> float:
        return adapter.calculate_fair_odds({}, {}, ctx).sport_specific_metrics["altitude_points"]

    assert hfa({"venue_city": "Denver"}) == 2.8
    assert hfa({"venue_city": "Salt Lake City"}) == 2.1
    assert hfa({"venue_city": "utah"}) == 2.1
    assert hfa({"venue_city": "boston"}) == 2.2
    assert hfa({}) == 2.2
    assert hfa({"home_advantage_points": 3.1}) == 3.1
    assert hfa({"neutral_site": True}) == 0.0
    neutral = adapter.calculate_fair_odds({}, {}, {"neutral_site": True})
    assert neutral.home_win_prob == pytest.approx(0.5, abs=1e-3)
    assert adapter.calculate_fair_odds({}, {}, {"venue_city": "denver"}).home_win_prob > adapter.calculate_fair_odds({}, {}).home_win_prob


# ---------------------------------------------------------------- distributions
def test_pmf_mixture_is_weighted() -> None:
    a, b = IntPMF.from_array([0.5, 0.5], 0), IntPMF.from_array([1.0], 3)
    mixed = IntPMF.mix([(a, 3.0), (b, 1.0)])
    assert mixed.offset == 0
    assert list(mixed.probs) == pytest.approx([0.375, 0.375, 0.0, 0.25])


def test_overtime_redistributes_the_regulation_tie() -> None:
    margin, p_tie = final_margin(0.0, 11.5)
    assert p_tie == pytest.approx(normal_cdf(0.5, 0, 11.5) - normal_cdf(-0.5, 0, 11.5), rel=1e-6)
    assert margin.prob_eq(0) == 0.0
    assert float(margin.probs.sum()) == pytest.approx(1.0)
    assert margin.prob_gt(0) == pytest.approx(0.5, abs=1e-9)
    # overtime margins are small: the extra mass sits within a few points
    plain = IntPMF.from_normal(0.0, 11.5, margin.offset, margin.offset + len(margin.probs) - 1)
    assert margin.prob_eq(2) - plain.prob_eq(2) > margin.prob_eq(15) - plain.prob_eq(15)
    favourite, _ = final_margin(6.0, 11.5)
    assert favourite.prob_gt(0) > normal_cdf(6.0 / 11.5)  # the favourite also wins most overtimes


def test_cover_probability_matches_markets_and_teasers(adapter: BasketballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    key = next(k for k in res.market_probabilities if k.startswith("spread_home_") and k.endswith(".5"))
    line = float(key.removeprefix("spread_home_"))
    out = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home", line)
    assert out.effective_probability == pytest.approx(res.market_probabilities[key], abs=1e-4)
    assert out.push == 0.0
    whole = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "away", 3.0)
    assert whole.push > 0.0
    assert whole.win + whole.push + whole.loss == pytest.approx(1.0)
    quarter = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home", -2.75)
    assert quarter.half_win > 0.0
    covers = [adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home", line + s).effective_probability for s in (0.0, *TEASER_POINT_SHIFTS)]
    assert covers == sorted(covers)
    over = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "over", 225.5, market="total")
    under = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "under", 225.5, market="total")
    assert over.win == pytest.approx(under.loss)
    team = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home_over", 110.5, market="team_total")
    assert 0.0 < team.win < 1.0
    h1 = adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home", -0.5, market="first_half_spread")
    assert h1.win == pytest.approx(res.market_probabilities["h1_home"], abs=1e-4)
    with pytest.raises(ValueError):
        adapter.cover_probability(EVEN_HOME, EVEN_AWAY, "home", -0.5, market="corners")


# ---------------------------------------------------------------- context
def test_absence_war_moves_the_margin_by_war_over_2_7(adapter: BasketballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    ctx = {"absences": [{"side": "home", "player": "star", "war": 5.4, "status": "out"}]}
    hit = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, ctx)
    assert hit.sport_specific_metrics["projected_spread"] - base.sport_specific_metrics["projected_spread"] == pytest.approx(2.0, abs=0.02)
    assert hit.home_win_prob < base.home_win_prob
    assert hit.sport_specific_metrics["home_absence_margin_points"] == pytest.approx(-2.0)
    doubtful = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, {"absences": [{"side": "away", "war": 5.4, "status": "doubtful"}]})
    assert doubtful.sport_specific_metrics["projected_spread"] - base.sport_specific_metrics["projected_spread"] == pytest.approx(-1.0, abs=0.02)


def test_absence_usage_lowers_offence(adapter: BasketballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    ctx = {"absences": [{"side": "home", "player": "creator", "war": 0.0, "usage_pct": 30.0}]}
    hit = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, ctx)
    assert hit.sport_specific_metrics["home_usage_ortg_delta"] == pytest.approx(-1.2)
    pace = base.sport_specific_metrics["projected_pace"]
    drop = base.sport_specific_metrics["home_projected_pts"] - hit.sport_specific_metrics["home_projected_pts"]
    assert drop == pytest.approx(pace / 100 * 1.2, abs=0.02)
    assert hit.home_win_prob < base.home_win_prob
    fraction = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, {"absences": [{"side": "home", "war": 0.0, "usage_pct": 0.30}]})
    assert fraction.sport_specific_metrics["home_usage_ortg_delta"] == pytest.approx(-1.2)


def test_officials_fouls_add_free_throw_points(adapter: BasketballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    tight = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, {"officials": {"name": "crew", "fouls_per_game": 43.5}})
    assert tight.sport_specific_metrics["officials_foul_points_delta"] == pytest.approx(0.85 * 4.0)
    m0, m1 = base.sport_specific_metrics, tight.sport_specific_metrics
    assert (m1["home_projected_pts"] + m1["away_projected_pts"]) - (m0["home_projected_pts"] + m0["away_projected_pts"]) == pytest.approx(3.4, abs=0.02)
    assert m1["projected_spread"] == pytest.approx(m0["projected_spread"], abs=1e-6)
    assert tight.evidence["officials"] is True


def test_no_context_means_no_adjustment_and_weather_is_ignored(adapter: BasketballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    assert not any(k.endswith(("_absence_margin_points", "_foul_points_delta")) for k in base.sport_specific_metrics)
    windy = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY, {"weather": {"wind_mph": 30, "precipitation_mm": 10, "temperature_c": -5}})
    assert windy.home_win_prob == base.home_win_prob
    assert windy.expected_total_score == base.expected_total_score


def test_calibrated_model_weights_are_honoured(adapter: BasketballSportAdapter) -> None:
    home = {"off_rating": 117.0, "def_rating": 111.0, "efg_pct": 0.53}
    base = adapter.calculate_fair_odds(home, {})
    benched = adapter.calculate_fair_odds({**home, "elo": 1500}, {"elo": 1500}, {"model_weights": {"elo": 0.0}})
    assert benched.consensus is not None and benched.consensus.model_count == 3
    same_weights = adapter.calculate_fair_odds(home, {}, {"model_weights": {"four_factors_efficiency": 0.35, "morey_pythagorean": 0.20,
                                                                            "net_rating_srs": 0.30}})
    assert benched.home_win_prob == pytest.approx(same_weights.home_win_prob, abs=1e-4)
    tilted = adapter.calculate_fair_odds(home, {}, {"model_weights": {"four_factors_efficiency": 1.0, "morey_pythagorean": 0.01,
                                                                      "net_rating_srs": 0.01}})
    assert tilted.home_win_prob != base.home_win_prob
    with pytest.raises(ValueError):
        adapter.calculate_fair_odds(home, {}, {"model_weights": {"four_factors_efficiency": 0.0}})


# ---------------------------------------------------------------- in-play
def test_in_play_game_over_states(adapter: BasketballSportAdapter) -> None:
    won = adapter.in_play_win_probability({}, {}, {"quarter": 4, "seconds_remaining_in_quarter": 0, "home_score": 101, "away_score": 99})
    assert (won.home_win_prob, won.away_win_prob) == (1.0, 0.0)
    lost = adapter.in_play_win_probability({}, {}, {"quarter": 5, "seconds_remaining_in_quarter": 0, "home_score": 110, "away_score": 112})
    assert (lost.home_win_prob, lost.away_win_prob) == (0.0, 1.0)
    final = adapter.in_play_win_probability({}, {}, {"status": "final", "quarter": 3, "home_score": 80, "away_score": 70})
    assert final.home_win_prob == 1.0


def test_in_play_level_at_the_horn_goes_to_overtime(adapter: BasketballSportAdapter) -> None:
    live = adapter.in_play_win_probability({"off_rating": 118.0}, {}, {"quarter": 4, "seconds_remaining_in_quarter": 0,
                                                                        "home_score": 100, "away_score": 100})
    assert live.detail["state"] == "overtime_next"
    assert 0.5 < live.home_win_prob < 0.75
    assert live.home_win_prob + live.away_win_prob == pytest.approx(1.0)


def test_in_play_tracks_the_game(adapter: BasketballSportAdapter) -> None:
    pre = adapter.calculate_fair_odds(EVEN_HOME, EVEN_AWAY)
    tip = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 1, "seconds_remaining_in_quarter": 720,
                                                                 "home_score": 0, "away_score": 0})
    assert tip.home_win_prob == pytest.approx(pre.home_win_prob, abs=0.01)
    leads = [adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 3, "seconds_remaining_in_quarter": 300,
                                                                    "home_score": 70 + d, "away_score": 70}).home_win_prob for d in (-8, 0, 8)]
    assert leads == sorted(leads)
    late = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 4, "seconds_remaining_in_quarter": 60,
                                                                  "home_score": 100, "away_score": 92})
    early = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 2, "seconds_remaining_in_quarter": 60,
                                                                   "home_score": 50, "away_score": 42})
    assert late.home_win_prob > early.home_win_prob > 0.5
    with_ball = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 4, "seconds_remaining_in_quarter": 20,
                                                                       "home_score": 99, "away_score": 100, "possession": "home"})
    without = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 4, "seconds_remaining_in_quarter": 20,
                                                                     "home_score": 99, "away_score": 100, "possession": "away"})
    assert with_ball.home_win_prob > without.home_win_prob
    via_game_clock = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 3, "seconds_remaining": 1020,
                                                                            "home_score": 70, "away_score": 70})
    via_quarter = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 3, "seconds_remaining_in_quarter": 300,
                                                                         "home_score": 70, "away_score": 70})
    assert via_game_clock.home_win_prob == pytest.approx(via_quarter.home_win_prob)
    ot = adapter.in_play_win_probability(EVEN_HOME, EVEN_AWAY, {"quarter": 5, "seconds_remaining_in_quarter": 150,
                                                                "home_score": 110, "away_score": 107})
    assert 0.7 < ot.home_win_prob < 1.0
    for state in (tip, late, with_ball, ot):
        assert state.home_win_prob + state.away_win_prob + state.draw_prob == pytest.approx(1.0)


# ---------------------------------------------------------------- settlement
RESULT = {"home_score": 112, "away_score": 105, "q1_home": 30, "q1_away": 25, "q2_home": 26, "q2_away": 26, "q3_home": 28,
          "q3_away": 30, "q4_home": 28, "q4_away": 24}


@pytest.mark.parametrize(("market", "selection", "line", "expected"), [
    ("moneyline", "home", None, LegStatus.WON),
    ("match_winner", "away", None, LegStatus.LOST),
    ("point_spread", "home", -6.5, LegStatus.WON),
    ("point_spread", "home", -7.0, LegStatus.VOID),
    ("point_spread", "home", -7.25, LegStatus.HALF_LOST),
    ("point_spread", "home", -6.75, LegStatus.HALF_WON),
    ("point_spread", "away", 6.5, LegStatus.LOST),
    ("alternative_spread", "away", 10.5, LegStatus.WON),
    ("game_total_over_under", "over", 216.5, LegStatus.WON),
    ("game_total_over_under", "under", 217.0, LegStatus.VOID),
    ("alternative_total", "under", 230.5, LegStatus.WON),
    ("team_totals", "home_over", 110.5, LegStatus.WON),
    ("team_totals", "away_over", 105.5, LegStatus.LOST),
    ("first_half_spread", "home", -4.5, LegStatus.WON),
    ("first_half_total", "over", 107.5, LegStatus.LOST),
    ("first_half_moneyline", "home", None, LegStatus.WON),
    ("first_quarter_spread", "away", 5.0, LegStatus.VOID),
    ("first_quarter_winner", "home", None, LegStatus.WON),
])
def test_settlement_markets(adapter: BasketballSportAdapter, market: str, selection: str, line: float | None, expected: LegStatus) -> None:
    assert adapter.settle_market(market, RESULT, selection, line) == expected


def test_quarter_winner_variants(adapter: BasketballSportAdapter) -> None:
    assert adapter.settle_market("quarter_winner", RESULT, "home", params={"quarter": 2}) == LegStatus.VOID  # tied quarter
    assert adapter.settle_market("quarter_winner", RESULT, "tie", params={"quarter": 2}) == LegStatus.WON
    assert adapter.settle_market("quarter_winner", RESULT, "tie", params={"quarter": 1}) == LegStatus.LOST
    assert adapter.settle_market("quarter_winner", RESULT, "away", params={"period": "q3"}) == LegStatus.WON
    assert adapter.settle_market("quarter_winner:q4", RESULT, "home") == LegStatus.WON
    assert adapter.settle_market("quarter_winner", {"quarter": "Q1", "q1_home": 20, "q1_away": 28}, "home") == LegStatus.LOST
    assert adapter.resolve_market_winner("quarter_winner", {"home_score": 100, "away_score": 90}, "home") is False  # no quarter


def test_normalize_result_variants(adapter: BasketballSportAdapter) -> None:
    res = adapter.normalize_result(RESULT)
    assert res.periods["h1"] == (56, 51)
    assert res.periods["h2"] == (56, 54)
    assert (res.regulation_home, res.regulation_away) == (112, 105)
    listed = adapter.normalize_result({"quarters": {"home": [25, 25, 25, 25, 10], "away": [30, 20, 25, 25, 8]}})
    assert (listed.home, listed.away) == (110, 108)
    assert (listed.regulation_home, listed.regulation_away) == (100, 100)
    assert listed.periods["ot1"] == (10, 8)
    assert listed.periods["h2"] == (60, 58)  # the second half carries the overtime
    pairs = adapter.normalize_result({"quarters": [[20, 22], [30, 18], [25, 25], [27, 21]]})
    assert (pairs.home, pairs.away) == (102, 86)
    raw_half = adapter.normalize_result({"home_score": 100, "away_score": 98, "h1_home_score": 49, "h1_away_score": 55})
    assert raw_half.periods["h1"] == (49, 55)


def test_overtime_counts_for_spread_and_moneyline(adapter: BasketballSportAdapter) -> None:
    ot_game = {"quarters": {"home": [25, 25, 25, 25, 10], "away": [30, 20, 25, 25, 8]}}
    assert adapter.settle_market("moneyline", ot_game, "home") == LegStatus.WON
    assert adapter.settle_market("point_spread", ot_game, "away", 1.5) == LegStatus.LOST
    assert adapter.settle_market("point_spread", ot_game, "away", 2.0) == LegStatus.VOID
    assert adapter.settle_market("game_total_over_under", ot_game, "over", 217.5) == LegStatus.WON


def test_player_props_and_scratch_rule(adapter: BasketballSportAdapter) -> None:
    box = {**RESULT, "player_stats": {"Star Guard": {"points": 31, "rebounds": 6, "assists": 9, "threes": 5}},
           "participants": ["star guard", "bench big"]}
    assert adapter.settle_market("player_points", box, "over", 28.5, {"player": "star guard"}) == LegStatus.WON
    assert adapter.settle_market("player_threes", box, "under", 4.5, {"player": "Star Guard"}) == LegStatus.LOST
    assert adapter.settle_market("player_assists", box, "over", 9.0, {"player": "star guard"}) == LegStatus.VOID
    assert adapter.settle_market("player_prop", box, "over", 5.5, {"player": "star guard", "stat": "rebounds"}) == LegStatus.WON
    assert adapter.settle_market("points_rebounds_assists", box, "over", 45.5, {"player": "star guard"}) == LegStatus.WON
    assert adapter.settle_market("points_rebounds_assists", box, "under", 46.0, {"player": "star guard"}) == LegStatus.VOID
    assert adapter.settle_market("player_points", box, "over", 10.5, {"player": "injured wing"}) == LegStatus.VOID
    assert adapter.settle_market("points_rebounds_assists", box, "over", 10.5, {"player": "injured wing"}) == LegStatus.VOID


def test_next_score_and_void_statuses(adapter: BasketballSportAdapter) -> None:
    events = {**RESULT, "events": [{"type": "score", "team": "away", "t": 14}, {"type": "score", "team": "home", "t": 40}]}
    assert adapter.settle_market("first_team_to_score", events, "away") == LegStatus.WON
    assert adapter.settle_market("next_score", events, "home", params={"after": 20}) == LegStatus.WON
    assert adapter.settle_market("moneyline", {**RESULT, "status": "postponed"}, "home") == LegStatus.VOID
    assert adapter.resolve_market_winner("corner_kicks", RESULT, "over", 5.5) is False


# ---------------------------------------------------------------- latency
def test_pricing_latency(adapter: BasketballSportAdapter) -> None:
    home = {**BOX, "off_rating": 117.0, "def_rating": 112.0, "points_for": 116.0, "points_against": 111.0, "elo": 1600}
    away = {"efg_pct": 0.53, "pace": 98.0, "off_rating": 113.0, "def_rating": 114.0, "points_for": 112.0, "points_against": 113.0,
            "elo": 1540}
    ctx = {"venue_city": "denver", "absences": [{"side": "home", "war": 2.0, "usage_pct": 25.0}], "officials": {"fouls_per_game": 41.0}}
    adapter.calculate_fair_odds(home, away, ctx)
    start = time.perf_counter()
    for _ in range(10):
        adapter.calculate_fair_odds(home, away, ctx)
    assert (time.perf_counter() - start) / 10 < 0.025
