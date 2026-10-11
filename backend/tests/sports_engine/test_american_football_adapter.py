"""American football adapter tests (Group 79): EPA per play, success-rate drives, SRS / Elo, key-number margins,
half-point crossing value, context, in-play and settlement. Developed for Amit Ashok Kumar Patnaik."""

from __future__ import annotations

import time

import pytest

from app.domain.sports.american_football_adapter import (
    KEY_NUMBER_PROBABILITIES,
    TEASER_POINT_SHIFTS,
    TIE_PROBABILITY,
    AmericanFootballSportAdapter,
    american_cents,
    american_odds,
    drive_profile,
    expected_points,
    key_number_factors,
    key_number_margin,
    possession_outcomes,
)
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import IntPMF
from app.schemas.bet_types import LegStatus

UNIVERSAL_BET_TYPES = {
    "moneyline", "point_spread", "game_total", "team_total", "period_lines", "parlay", "teaser", "pleaser", "round_robin", "if_bet",
    "double_action", "futures", "player_props", "game_props", "live_next_event", "draw_no_bet", "btts", "correct_score",
    "asian_handicap", "clean_sheet", "method_of_victory", "round_betting", "map_winner", "total_maps", "first_5_innings",
    "inning_of_highest_score", "alternative_lines",
}
REF_HOME = {"pass_epa": 0.15, "rush_epa": 0.02}
REF_AWAY = {"pass_epa": -0.05, "rush_epa": -0.10}
HOME = {"pass_epa": 0.08, "rush_epa": -0.02, "plays_per_game": 64.0}
AWAY = {"pass_epa": 0.03, "rush_epa": -0.06, "plays_per_game": 63.0}
PICKEM = {"home_field_advantage": 0.0}


@pytest.fixture
def adapter() -> AmericanFootballSportAdapter:
    return AmericanFootballSportAdapter()


def total_points(res: object) -> float:
    m = res.sport_specific_metrics  # type: ignore[attr-defined]
    return m["home_projected_pts"] + m["away_projected_pts"]


# ---------------------------------------------------------------- identity
def test_identity_and_catalogue(adapter: AmericanFootballSportAdapter) -> None:
    assert adapter.sport_key == "american_football"
    assert adapter.display_name == "American Football"
    assert adapter.odds_api_family == "americanfootball"
    assert adapter.kelly_multiplier == 0.50
    assert adapter.has_draw is False
    assert adapter.model_family == "EPA & Key Numbers"
    assert adapter.BET_TYPES <= UNIVERSAL_BET_TYPES
    assert {"teaser", "pleaser", "game_props", "period_lines", "player_props", "alternative_lines"} <= adapter.BET_TYPES
    assert TEASER_POINT_SHIFTS == (6.0, 6.5, 7.0) == adapter.TEASER_POINT_SHIFTS
    assert {"point_spread", "first_half_spread", "team_total_points", "first_score_method"} <= set(adapter.supported_markets)
    for market in adapter.standard_markets:
        adapter.canonical_market(market)


# ---------------------------------------------------------------- the reference test (FILE 21)
def test_reference_epa_and_key_numbers(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=REF_HOME, away_stats=REF_AWAY)
    assert res.home_win_prob > 0.65
    assert res.sport_specific_metrics["key_number_cluster_3"] == 0.151
    assert res.sport_specific_metrics["key_number_cluster_7"] == 0.094
    assert adapter.resolve_market_winner("point_spread", {"home_score": 27, "away_score": 20}, "home", line=-6.5) is True
    assert adapter.resolve_market_winner("first_half_spread", {"h1_home_score": 14, "h1_away_score": 7}, "home", line=-3.5) is True


# ---------------------------------------------------------------- the sub-models
def test_epa_sub_model_hand_computed(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    home = (0.58 * 0.15 + 0.42 * 0.02) * 65
    away = (0.58 * -0.05 + 0.42 * -0.10) * 65
    assert res.sport_specific_metrics["sub_model_margins"]["epa_per_play"] == pytest.approx(home - away + 1.8, abs=1e-3)
    assert res.sport_specific_metrics["home_epa_per_play_matchup"] == pytest.approx(0.0954)
    # the opposing defence's EPA allowed is additive against a league-zero baseline
    leaky = adapter.calculate_fair_odds(REF_HOME, {**REF_AWAY, "def_pass_epa_allowed": 0.10, "def_rush_epa_allowed": 0.0})
    assert leaky.sport_specific_metrics["home_epa_per_play_matchup"] == pytest.approx(0.0954 + 0.058)
    direct = adapter.calculate_fair_odds({"off_epa": 0.1}, {"def_epa_allowed": -0.05}, PICKEM)
    assert direct.sport_specific_metrics["home_epa_per_play_matchup"] == pytest.approx(0.05)


def test_drive_model_is_calibrated_to_the_league() -> None:
    league = drive_profile(0.45)
    assert league.series_conversion == pytest.approx(1 - 0.55 ** 2)
    assert league.touchdown == pytest.approx(0.22, abs=0.005)
    assert league.field_goal == pytest.approx(0.16, abs=0.005)
    assert league.points_per_drive * 11.2 == pytest.approx(22.5, abs=0.3)
    assert drive_profile(0.50).points_per_drive > league.points_per_drive > drive_profile(0.40).points_per_drive


def test_success_rate_inputs_drive_the_second_sub_model(adapter: AmericanFootballSportAdapter) -> None:
    neutral = adapter.calculate_fair_odds({}, {}, PICKEM)
    assert neutral.sport_specific_metrics["sub_model_margins"]["success_rate_drive"] == pytest.approx(0.0, abs=1e-9)
    sr = adapter.calculate_fair_odds({"success_rate": 0.50}, {"def_success_rate_allowed": 47.0}, PICKEM)
    assert sr.sport_specific_metrics["home_drive"]["success_rate"] == pytest.approx(0.52)
    assert sr.sport_specific_metrics["sub_model_margins"]["success_rate_drive"] > 0
    # with no success rate the proxy SR = 0.45 + 0.5 EPA is used
    proxy = adapter.calculate_fair_odds({"pass_epa": 0.1, "rush_epa": 0.1}, {}, PICKEM)
    assert proxy.sport_specific_metrics["home_drive"]["success_rate"] == pytest.approx(0.50)


def test_srs_elo_sub_model(adapter: AmericanFootballSportAdapter) -> None:
    srs = adapter.calculate_fair_odds({"srs": 6.0}, {"srs": -2.0})
    assert srs.sport_specific_metrics["sub_model_margins"]["srs_elo"] == pytest.approx(8.0 + 1.8, abs=1e-3)
    pts = adapter.calculate_fair_odds({"points_for": 27.0, "points_against": 20.0}, {"points_for": 21.0, "points_against": 23.0})
    assert pts.sport_specific_metrics["sub_model_margins"]["srs_elo"] == pytest.approx(9.0 + 1.8, abs=1e-3)
    elo = adapter.calculate_fair_odds({"elo": 1650}, {"elo": 1500}, PICKEM)
    assert elo.sport_specific_metrics["sub_model_margins"]["srs_elo"] == pytest.approx(6.0, abs=1e-3)
    both = adapter.calculate_fair_odds({"srs": 4.0, "elo": 1600}, {"srs": 0.0, "elo": 1500}, PICKEM)
    assert both.sport_specific_metrics["sub_model_margins"]["srs_elo"] == pytest.approx((4.0 + 4.0) / 2, abs=1e-3)
    fallback = adapter.calculate_fair_odds(REF_HOME, REF_AWAY, PICKEM)
    assert fallback.sport_specific_metrics["srs_source"] == "epa_regressed"
    assert fallback.sport_specific_metrics["sub_model_margins"]["srs_elo"] == pytest.approx(
        2 / 3 * fallback.sport_specific_metrics["sub_model_margins"]["epa_per_play"], abs=1e-3)


def test_three_distinct_sub_models_and_weights(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(HOME, AWAY)
    assert [s.name for s in res.sub_models] == ["epa_per_play", "success_rate_drive", "srs_elo"]
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    assert res.consensus is not None and res.consensus.model_count == 3
    with pytest.raises(ValueError):
        adapter.calculate_fair_odds(HOME, AWAY, {"model_weights": {"srs_elo": 0.0}})
    tilted = adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"model_weights": {"epa_per_play": 1.0, "success_rate_drive": 0.01,
                                                                                "srs_elo": 0.01}})
    assert tilted.home_win_prob > adapter.calculate_fair_odds(REF_HOME, REF_AWAY).home_win_prob


# ---------------------------------------------------------------- key numbers
def test_pickem_key_number_shares_match_the_spec() -> None:
    pmf = key_number_margin(0.0)
    assert float(pmf.probs.sum()) == pytest.approx(1.0)
    for k, share in KEY_NUMBER_PROBABILITIES.items():
        assert pmf.prob_eq(k) + pmf.prob_eq(-k) == pytest.approx(share, abs=1e-12)
    assert pmf.prob_eq(0) == pytest.approx(TIE_PROBABILITY, abs=1e-12)
    # non-key margins keep their normal shape; key margins stand out of it
    plain = IntPMF.from_normal(0.0, 13.8, -75, 75)
    assert pmf.prob_eq(5) / pmf.prob_eq(8) == pytest.approx(plain.prob_eq(5) / plain.prob_eq(8))
    assert pmf.prob_eq(5) < pmf.prob_eq(3) and pmf.prob_eq(8) < pmf.prob_eq(7) and pmf.prob_eq(11) < pmf.prob_eq(10)


def test_key_factors_apply_multiplicatively_for_any_mean() -> None:
    factors, other = key_number_factors(13.8)
    pmf, plain = key_number_margin(5.0), IntPMF.from_normal(5.0, 13.8, -75, 75)
    ratio = {m: pmf.prob_eq(m) / plain.prob_eq(m) for m in (-3, 3, 5, 7, 11)}
    assert ratio[3] == pytest.approx(ratio[-3])
    assert ratio[3] / ratio[5] == pytest.approx(factors[3] / other)
    assert ratio[7] / ratio[11] == pytest.approx(factors[7] / other)
    assert pmf.prob_gt(0) > 0.6


def test_key_number_metrics_and_tie(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.calculate_fair_odds({}, {}, PICKEM)
    m = res.sport_specific_metrics
    assert m["model_p_margin_3"] == pytest.approx(0.151, abs=1e-4)
    assert m["model_p_margin_7"] == pytest.approx(0.094, abs=1e-4)
    assert res.draw_prob == pytest.approx(0.003, abs=1e-4)
    assert res.fair_draw_odds is not None and res.fair_draw_odds > 300
    assert res.home_win_prob == pytest.approx(res.away_win_prob, abs=1e-4)
    assert res.market_probabilities["margin_exactly_3"] == pytest.approx(0.151, abs=1e-4)


def test_half_point_crossing_three_is_worth_at_least_25_cents(adapter: AmericanFootballSportAdapter) -> None:
    out = adapter.key_number_crossing_cents({}, {}, "home", 3.5, 2.5, PICKEM)
    assert out["cents"] >= 25.0
    assert out["key_numbers_crossed"] == [3]
    assert out["margin_mass_crossed"] == pytest.approx(0.151 / 2, abs=1e-6)
    assert out["probability_from"] - out["probability_to"] == pytest.approx(0.151 / 2, abs=1e-5)
    assert out["fair_american_from"] < out["fair_american_to"] < 0  # +3.5 is the dearer price
    seven = adapter.key_number_crossing_cents({}, {}, "away", 7.5, 6.5, PICKEM)
    five = adapter.key_number_crossing_cents({}, {}, "home", 5.5, 4.5, PICKEM)
    assert five["key_numbers_crossed"] == []
    assert out["cents"] > five["cents"]
    assert seven["key_numbers_crossed"] == [7]
    fav = adapter.key_number_crossing_cents({}, {}, "home", -2.5, -3.5, PICKEM)
    assert fav["probability_from"] > fav["probability_to"]


def test_american_odds_and_cents() -> None:
    assert american_odds(2.5) == pytest.approx(150.0)
    assert american_odds(1.5) == pytest.approx(-200.0)
    assert american_cents(-120.0) - american_cents(110.0) == pytest.approx(-30.0)
    assert american_cents(-100.0) == american_cents(100.0)


# ---------------------------------------------------------------- probabilities and markets
def test_probabilities_and_market_consistency(adapter: AmericanFootballSportAdapter) -> None:
    for home, away in ((REF_HOME, REF_AWAY), (HOME, AWAY), ({}, {})):
        res = adapter.calculate_fair_odds(home, away)
        mp = res.market_probabilities
        assert res.home_win_prob + res.away_win_prob + res.draw_prob == pytest.approx(1.0, abs=1e-3)
        assert mp["moneyline_home"] == pytest.approx(res.home_win_prob / (res.home_win_prob + res.away_win_prob), abs=3e-4)
        assert mp["moneyline_home"] + mp["moneyline_away"] == pytest.approx(1.0, abs=2e-4)
        assert all(0.0 <= p <= 1.0 for p in mp.values())
        for key in [k for k in mp if k.startswith("spread_home_") and k.endswith(".5")]:
            line = float(key.removeprefix("spread_home_"))
            assert mp[key] + mp[f"spread_away_{-line:+g}"] == pytest.approx(1.0, abs=2e-4)
        for line in ("-3", "+3", "-7", "+7", "-2.5", "-3.5", "-6.5", "-7.5", "-10", "+14"):
            assert f"spread_home_{line}" in mp
        for prefix in ("total_over_", "team_total_home_over_", "team_total_away_under_", "h1_spread_home_", "h1_total_over_",
                       "alt_spread_home_", "teaser_6.5_home_", "first_score_"):
            assert any(k.startswith(prefix) for k in mp), prefix


def test_whole_number_spread_on_a_key_number_pushes_often(adapter: AmericanFootballSportAdapter) -> None:
    three = adapter.cover_probability({}, {}, "home", -3.0, PICKEM)
    four = adapter.cover_probability({}, {}, "home", -4.0, PICKEM)
    assert three.push == pytest.approx(0.151 / 2, abs=1e-9)
    assert four.push == pytest.approx(0.038 / 2, abs=1e-9)
    quarter = adapter.cover_probability({}, {}, "home", -2.75, PICKEM)
    assert quarter.half_win == pytest.approx(three.push, abs=1e-9)


def test_cover_probability_and_teasers(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(HOME, AWAY)
    for key in ("spread_home_-2.5", "spread_away_+7.5", "spread_home_+3"):
        side, line = key.split("_")[1], float(key.split("_")[2])
        assert adapter.cover_probability(HOME, AWAY, side, line).effective_probability == pytest.approx(res.market_probabilities[key], abs=1e-4)
    covers = [adapter.cover_probability(HOME, AWAY, "away", 1.5 + s).win for s in (0.0, *TEASER_POINT_SHIFTS)]
    assert covers == sorted(covers)
    # the classic Wong teaser: +1.5 -> +7.5 crosses both 3 and 7
    assert covers[2] - covers[0] > 0.2
    over = adapter.cover_probability(HOME, AWAY, "over", 44.5, market="total")
    under = adapter.cover_probability(HOME, AWAY, "under", 44.5, market="total")
    assert over.win == pytest.approx(under.loss)
    team = adapter.cover_probability(HOME, AWAY, "away_under", 20.5, market="team_total")
    assert 0.0 < team.win < 1.0
    h1 = adapter.cover_probability(HOME, AWAY, "home", -0.5, market="first_half_spread")
    assert h1.win == pytest.approx(res.market_probabilities["h1_home"], abs=1e-4)
    h1t = adapter.cover_probability(HOME, AWAY, "over", 21.5, market="first_half_total")
    assert 0.0 < h1t.win < 1.0
    with pytest.raises(ValueError):
        adapter.cover_probability(HOME, AWAY, "home", 1.5, market="corners")


def test_first_score_distribution_sums_to_one(adapter: AmericanFootballSportAdapter) -> None:
    ctx = MatchContext.parse(None)
    proj = adapter.project(REF_HOME, REF_AWAY, ctx)
    dist = adapter.first_score_distribution(proj, ctx)
    assert dist["td"] + dist["fg"] + dist["safety"] + dist["none"] == pytest.approx(1.0)
    assert dist["home"] + dist["away"] + dist["none"] == pytest.approx(1.0)
    assert dist["home"] > dist["away"]
    assert dist["td"] > dist["fg"] > dist["safety"] > 0
    receives = adapter.first_score_distribution(proj, MatchContext.parse({"home_receives_first": True}))
    assert receives["home"] > dist["home"]


def test_monotonicity(adapter: AmericanFootballSportAdapter) -> None:
    probs = [adapter.calculate_fair_odds({"pass_epa": e}, {}).home_win_prob for e in (-0.1, 0.0, 0.1, 0.2)]
    assert probs == sorted(probs)
    assert adapter.calculate_fair_odds({}, {"def_pass_epa_allowed": 0.1}).home_win_prob > adapter.calculate_fair_odds({}, {}).home_win_prob
    assert adapter.calculate_fair_odds({}, {}, {"home_field_advantage": 3.0}).home_win_prob > adapter.calculate_fair_odds({}, {}).home_win_prob


# ---------------------------------------------------------------- context
def test_weather_lowers_the_total(adapter: AmericanFootballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(HOME, AWAY)
    windy = adapter.calculate_fair_odds(HOME, AWAY, {"weather": {"wind_mph": 25.0}})
    assert total_points(base) - total_points(windy) == pytest.approx(3.0, abs=0.02)
    assert windy.sport_specific_metrics["weather_wind_points"] == pytest.approx(-3.0, abs=1e-3)
    assert windy.expected_total_score < base.expected_total_score
    wet_cold = adapter.calculate_fair_odds(HOME, AWAY, {"weather": {"precipitation_mm": 4.0, "temperature_c": -3.0, "wind_mph": 10.0}})
    assert total_points(base) - total_points(wet_cold) == pytest.approx(2.5, abs=0.02)
    calm = adapter.calculate_fair_odds(HOME, AWAY, {"weather": {"wind_mph": 12.0, "temperature_c": 10.0, "precipitation_mm": 1.0}})
    assert total_points(calm) == pytest.approx(total_points(base))
    dome = adapter.calculate_fair_odds(HOME, AWAY, {"weather": {"wind_mph": 30.0, "indoor": True}})
    assert dome.expected_total_score == base.expected_total_score
    roof = adapter.calculate_fair_odds(HOME, AWAY, {"roof": "dome", "weather": {"wind_mph": 30.0}})
    assert roof.expected_total_score == base.expected_total_score
    friction = adapter.calculate_fair_odds(HOME, AWAY, {"weather": {"friction": 0.9, "wind_mph": 30.0}})
    assert total_points(friction) == pytest.approx(0.9 * total_points(base), abs=0.02)
    assert friction.sport_specific_metrics["weather_friction_multiplier"] == 0.9
    assert "weather_wind_points" not in friction.sport_specific_metrics


def test_absences_cost_2_1_points_per_war(adapter: AmericanFootballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(HOME, AWAY)
    qb = adapter.calculate_fair_odds(HOME, AWAY, {"absences": [{"side": "home", "player": "qb1", "war": 1.0, "position": "QB"}]})
    assert qb.sport_specific_metrics["projected_spread"] - base.sport_specific_metrics["projected_spread"] == pytest.approx(2.1, abs=0.01)
    assert total_points(base) - total_points(qb) == pytest.approx(2.1, abs=0.02)
    assert qb.home_win_prob < base.home_win_prob
    cb = adapter.calculate_fair_odds(HOME, AWAY, {"absences": [{"side": "home", "war": 1.0, "position": "cb"}]})
    assert cb.sport_specific_metrics["away_projected_pts"] - base.sport_specific_metrics["away_projected_pts"] == pytest.approx(2.1, abs=0.02)
    unknown = adapter.calculate_fair_odds(HOME, AWAY, {"absences": [{"side": "away", "war": 2.0, "status": "questionable"}]})
    assert unknown.sport_specific_metrics["projected_spread"] - base.sport_specific_metrics["projected_spread"] == pytest.approx(-1.05, abs=0.01)
    assert unknown.sport_specific_metrics["away_absence_margin_points"] == pytest.approx(-1.05)


def test_officials_flags_and_no_context(adapter: AmericanFootballSportAdapter) -> None:
    base = adapter.calculate_fair_odds(HOME, AWAY)
    assert not any(k.startswith(("weather_", "officials_")) or k.endswith("_absence_margin_points") for k in base.sport_specific_metrics)
    flags = adapter.calculate_fair_odds(HOME, AWAY, {"officials": {"name": "crew", "flags_per_game": 17.0}})
    assert flags.sport_specific_metrics["officials_flag_points_delta"] == pytest.approx(1.2)
    assert flags.sport_specific_metrics["officials_flags_per_game"] == 17.0
    assert total_points(flags) - total_points(base) == pytest.approx(1.2, abs=0.02)
    assert flags.evidence["officials"] is True


# ---------------------------------------------------------------- in-play
def test_expected_points_curve() -> None:
    assert expected_points(1) > expected_points(20) > expected_points(50) > expected_points(80) > expected_points(99)
    assert expected_points(75) == pytest.approx(0.9, abs=0.01)
    assert expected_points(50, 1, 10) > expected_points(50, 3, 10) > expected_points(50, 4, 10)
    assert expected_points(50, 3, 2) > expected_points(50, 3, 12)


def test_possession_outcomes_reproduce_expected_points() -> None:
    for y, down, dist in ((15, 1, 10), (50, 2, 6), (75, 1, 10), (30, 3, 4)):
        p_td, p_fg, residual = possession_outcomes(y, down, dist, 1800.0)
        assert residual == 0.0
        assert 6.95 * p_td + 3.0 * p_fg == pytest.approx(expected_points(y, down, dist), abs=1e-9)
        assert 0.0 < p_fg and 0.0 < p_td and p_td + p_fg < 1.0
    near_td, near_fg, _ = possession_outcomes(5, 1, 5, 1800.0)
    far_td, far_fg, _ = possession_outcomes(80, 1, 10, 1800.0)
    assert near_td > far_td and near_td / near_fg > far_td / far_fg
    hurried = possession_outcomes(80, 1, 10, 20.0)
    assert hurried[0] < far_td and hurried[1] < far_fg
    assert possession_outcomes(99, 3, 15, 600.0)[2] < 0.0  # backed up: the opponent likelier to score next


def test_in_play_final_states(adapter: AmericanFootballSportAdapter) -> None:
    won = adapter.in_play_win_probability({}, {}, {"quarter": 4, "seconds_remaining": 0, "home_score": 24, "away_score": 21})
    assert (won.home_win_prob, won.away_win_prob, won.draw_prob) == (1.0, 0.0, 0.0)
    lost = adapter.in_play_win_probability({}, {}, {"status": "final", "home_score": 10, "away_score": 13})
    assert (lost.home_win_prob, lost.away_win_prob) == (0.0, 1.0)
    tie = adapter.in_play_win_probability({}, {}, {"quarter": 5, "seconds_remaining": 0, "home_score": 20, "away_score": 20})
    assert tie.draw_prob == 1.0
    playoff = adapter.in_play_win_probability({}, {}, {"quarter": 5, "seconds_remaining": 0, "home_score": 20, "away_score": 20},
                                              {"playoff": True})
    assert playoff.draw_prob == 0.0 and playoff.home_win_prob + playoff.away_win_prob == pytest.approx(1.0)


def test_in_play_overtime_after_a_level_regulation(adapter: AmericanFootballSportAdapter) -> None:
    receive_home = adapter.in_play_win_probability({}, {}, {"quarter": 4, "seconds_remaining": 0, "home_score": 17, "away_score": 17,
                                                            "possession": "home"})
    receive_away = adapter.in_play_win_probability({}, {}, {"quarter": 4, "seconds_remaining": 0, "home_score": 17, "away_score": 17,
                                                            "possession": "away"})
    assert receive_home.detail["state"] == "overtime_next"
    assert receive_home.home_win_prob > receive_away.home_win_prob
    assert receive_home.draw_prob == pytest.approx(0.04)
    for state in (receive_home, receive_away):
        assert state.home_win_prob + state.away_win_prob + state.draw_prob == pytest.approx(1.0)


def test_in_play_tracks_the_game(adapter: AmericanFootballSportAdapter) -> None:
    pre = adapter.calculate_fair_odds(HOME, AWAY)
    kickoff = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 1, "seconds_remaining": 3600, "home_score": 0, "away_score": 0})
    assert kickoff.home_win_prob == pytest.approx(pre.home_win_prob, abs=0.03)
    leads = [adapter.in_play_win_probability(HOME, AWAY, {"quarter": 3, "seconds_remaining": 1200, "home_score": 14 + d,
                                                          "away_score": 14}).home_win_prob for d in (-7, 0, 7)]
    assert leads == sorted(leads)
    red_zone = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 4, "seconds_remaining": 30, "home_score": 20, "away_score": 21,
                                                            "possession": "home", "yardline_100": 15, "down": 1, "distance": 10})
    own_end = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 4, "seconds_remaining": 30, "home_score": 20, "away_score": 21,
                                                           "possession": "home", "yardline_100": 80, "down": 1, "distance": 10})
    assert red_zone.home_win_prob > 0.6 > own_end.home_win_prob
    quarter_clock = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 3, "seconds_remaining_in_quarter": 300, "home_score": 14,
                                                                 "away_score": 10})
    game_clock = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 3, "seconds_remaining": 1200, "home_score": 14, "away_score": 10})
    assert quarter_clock.home_win_prob == pytest.approx(game_clock.home_win_prob)
    ot = adapter.in_play_win_probability(HOME, AWAY, {"quarter": 5, "seconds_remaining": 120, "home_score": 23, "away_score": 20,
                                                      "possession": "away", "yardline_100": 70})
    assert 0.6 < ot.home_win_prob < 1.0
    for state in (kickoff, red_zone, own_end, ot):
        assert state.home_win_prob + state.away_win_prob + state.draw_prob == pytest.approx(1.0)


# ---------------------------------------------------------------- settlement
RESULT = {"home_score": 27, "away_score": 24, "q1_home": 7, "q1_away": 3, "q2_home": 10, "q2_away": 7, "q3_home": 3, "q3_away": 7,
          "q4_home": 7, "q4_away": 7}


@pytest.mark.parametrize(("market", "selection", "line", "expected"), [
    ("moneyline", "home", None, LegStatus.WON),
    ("match_winner", "away", None, LegStatus.LOST),
    ("point_spread", "home", -3.0, LegStatus.VOID),
    ("point_spread", "home", -2.5, LegStatus.WON),
    ("point_spread", "home", -3.5, LegStatus.LOST),
    ("point_spread", "home", -2.75, LegStatus.HALF_WON),
    ("point_spread", "home", -3.25, LegStatus.HALF_LOST),
    ("point_spread", "away", 3.5, LegStatus.WON),
    ("alternative_spread", "away", 9.5, LegStatus.WON),
    ("game_total_over_under", "over", 50.5, LegStatus.WON),
    ("game_total_over_under", "under", 51.0, LegStatus.VOID),
    ("alternative_total", "under", 44.5, LegStatus.LOST),
    ("team_total_points", "home_over", 26.5, LegStatus.WON),
    ("team_total_points", "away_under", 23.5, LegStatus.LOST),
    ("first_half_spread", "home", -6.5, LegStatus.WON),
    ("first_half_spread", "home", -7.0, LegStatus.VOID),
    ("first_half_total", "over", 27.5, LegStatus.LOST),
    ("first_half_moneyline", "away", None, LegStatus.LOST),
    ("first_quarter_spread", "away", 3.5, LegStatus.LOST),
])
def test_settlement_markets(adapter: AmericanFootballSportAdapter, market: str, selection: str, line: float | None,
                            expected: LegStatus) -> None:
    assert adapter.settle_market(market, RESULT, selection, line) == expected


def test_tie_voids_the_moneyline(adapter: AmericanFootballSportAdapter) -> None:
    tie = {"home_score": 20, "away_score": 20, "h1_home_score": 10, "h1_away_score": 10}
    assert adapter.settle_market("moneyline", tie, "home") == LegStatus.VOID
    assert adapter.settle_market("first_half_moneyline", tie, "away") == LegStatus.VOID
    assert adapter.settle_market("point_spread", tie, "away", 0.5) == LegStatus.WON


def test_normalize_result(adapter: AmericanFootballSportAdapter) -> None:
    res = adapter.normalize_result(RESULT)
    assert res.periods["h1"] == (17, 10)
    assert res.periods["h2"] == (10, 14)
    raw = adapter.normalize_result({"home_score": 30, "away_score": 20, "h1_home_score": 14, "h1_away_score": 7})
    assert raw.periods["h1"] == (14, 7)


def test_first_score_method(adapter: AmericanFootballSportAdapter) -> None:
    events = {**RESULT, "events": [{"type": "score", "team": "away", "method": "fg", "t": 300},
                                   {"type": "score", "team": "home", "method": "td", "t": 120}]}
    for selection, expected in (("touchdown", LegStatus.WON), ("td", LegStatus.WON), ("home_td", LegStatus.WON),
                                ("home touchdown", LegStatus.WON), ("away_td", LegStatus.LOST), ("field_goal", LegStatus.LOST),
                                ("safety", LegStatus.LOST), ("none", LegStatus.LOST)):
        assert adapter.settle_market("first_score_method", events, selection) == expected, selection
    assert adapter.settle_market("first_score", events, "td") == LegStatus.WON
    typed = {**RESULT, "events": [{"type": "safety", "team": "home", "t": 50}, {"type": "touchdown", "team": "away", "t": 10}]}
    assert adapter.settle_market("first_score_method", typed, "away_td") == LegStatus.WON
    assert adapter.settle_market("first_score_method", {**RESULT, "first_score_method": "field_goal"}, "fg") == LegStatus.WON
    assert adapter.settle_market("first_score_method", {**RESULT, "first_score_method": "home_safety"}, "safety") == LegStatus.WON
    assert adapter.settle_market("first_score_method", {**RESULT, "first_score_method": "home_safety"}, "away_safety") == LegStatus.LOST
    assert adapter.settle_market("first_score_method", {**RESULT, "first_score_method": "td", "first_score_team": "away"},
                                 "home_td") == LegStatus.LOST
    scoreless = {"home_score": 0, "away_score": 0}
    assert adapter.settle_market("first_score_method", scoreless, "no_score") == LegStatus.WON
    assert adapter.settle_market("first_score_method", scoreless, "td") == LegStatus.LOST
    assert adapter.resolve_market_winner("first_score_method", RESULT, "td") is False  # no first-score information
    assert adapter.resolve_market_winner("first_score_method", events, "two_point_conversion") is False


def test_player_props_next_score_and_void(adapter: AmericanFootballSportAdapter) -> None:
    box = {**RESULT, "player_stats": {"QB One": {"passing_yards": 287, "passing_touchdowns": 2}, "WR One": {"receptions": 7}},
           "participants": ["qb one", "wr one"]}
    assert adapter.settle_market("passing_yards", box, "over", 265.5, {"player": "qb one"}) == LegStatus.WON
    assert adapter.settle_market("passing_touchdowns", box, "over", 2.0, {"player": "qb one"}) == LegStatus.VOID
    assert adapter.settle_market("receptions", box, "under", 6.5, {"player": "wr one"}) == LegStatus.LOST
    assert adapter.settle_market("rushing_yards", box, "over", 40.5, {"player": "rb scratched"}) == LegStatus.VOID
    events = {**RESULT, "events": [{"type": "score", "team": "home", "t": 120}, {"type": "score", "team": "away", "t": 300}]}
    assert adapter.settle_market("first_team_to_score", events, "home") == LegStatus.WON
    assert adapter.settle_market("next_score", events, "away", params={"after": 200}) == LegStatus.WON
    assert adapter.settle_market("point_spread", {**RESULT, "status": "cancelled"}, "home", -3.5) == LegStatus.VOID


# ---------------------------------------------------------------- latency and helpers
def test_pricing_latency(adapter: AmericanFootballSportAdapter) -> None:
    home = {**HOME, "success_rate": 0.48, "points_for": 26.0, "points_against": 20.0, "elo": 1600, "srs": 4.0}
    away = {**AWAY, "def_pass_epa_allowed": 0.03, "points_for": 21.0, "points_against": 22.0, "elo": 1520, "srs": -1.0}
    ctx = {"weather": {"wind_mph": 20.0}, "absences": [{"side": "home", "war": 1.0, "position": "qb"}], "officials": {"flags_per_game": 14}}
    adapter.calculate_fair_odds(home, away, ctx)
    start = time.perf_counter()
    for _ in range(10):
        adapter.calculate_fair_odds(home, away, ctx)
    assert (time.perf_counter() - start) / 10 < 0.025
