"""
Baseball adapter tests: BaseRuns, pitch-count decay, negative-binomial game grid, NRFI Markov chain (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from app.domain.sports.baseball_adapter import (
    INNING_DISPERSION,
    MANFRED_RUNNER_STATE,
    PARK_FACTORS,
    RE24,
    SCORE_PROB,
    BaseballSportAdapter,
    _advance,
    batting_line,
    compose_game,
    extras_joint,
    first_half_inning_markov,
    innings_from_notation,
    nb_innings,
    pitching_profile,
    split_hits,
    state_runs_pmf,
    top_three_traffic,
    trimmed,
)
from app.domain.sports.context import MatchContext
from app.domain.sports.quant import ScoreGrid, negative_binomial_pmf
from app.domain.sports.settlement import SettlementInputError
from app.schemas.bet_types import LegStatus

REF_HOME = {"hits_per_9": 8.0, "walks_per_9": 3.0, "home_runs_per_9": 1.2, "total_bases_per_9": 14.0, "stolen_bases_per_9": 0.5}
REF_AWAY = {"hits_per_9": 7.5, "walks_per_9": 2.5, "home_runs_per_9": 0.9, "total_bases_per_9": 12.0, "stolen_bases_per_9": 0.8}


@pytest.fixture(scope="module")
def adapter() -> BaseballSportAdapter:
    return BaseballSportAdapter()


# ================================================================ identity
def test_identity_and_class_attributes(adapter: BaseballSportAdapter) -> None:
    assert adapter.sport_key == "baseball"
    assert adapter.display_name == "Baseball"
    assert adapter.odds_api_family == "baseball"
    assert adapter.kelly_multiplier == 0.35
    assert adapter.has_draw is False
    assert adapter.model_family == "BaseRuns BsR & NRFI Markov"
    assert {"first_5_innings", "inning_of_highest_score", "game_props"} <= adapter.BET_TYPES
    assert "teaser" not in adapter.BET_TYPES and "pleaser" not in adapter.BET_TYPES
    for market in adapter.standard_markets:
        adapter.canonical_market(market)  # every standard market resolves


# ================================================================ BaseRuns
def test_baseruns_detailed_line_matches_hand_computation(adapter: BaseballSportAdapter) -> None:
    stats = {"singles": 5.0, "doubles": 2.0, "triples": 0.2, "home_runs": 1.0, "walks": 3.0, "hbp": 0.5, "ibb": 0.2, "sb": 1.0,
             "cs": 0.3, "outs": 27.0}
    a = (5.0 + 2.0 + 0.2 + 1.0) + 3.0 + 0.5 - 1.0 - 0.5 * 0.2
    b = 0.78 * 5.0 + 1.15 * 2.0 + 1.58 * 0.2 + 1.85 * 1.0 + 0.16 * 1.0 - 0.22 * 0.3
    assert adapter.calculate_baseruns(stats) == pytest.approx(a * b / (b + 27.0) + 1.0, abs=1e-9)


def test_baseruns_uses_given_outs(adapter: BaseballSportAdapter) -> None:
    base = {"singles": 5.0, "doubles": 2.0, "triples": 0.0, "home_runs": 1.0, "walks": 3.0}
    assert adapter.calculate_baseruns({**base, "outs": 24.0}) > adapter.calculate_baseruns({**base, "outs": 27.0})


def test_per9_split_attributes_extra_bases_to_doubles() -> None:
    singles, doubles, triples = split_hits(8.0, 1.2, 14.0)  # extra = 14 - 8 - 3.6 = 2.4
    assert (singles, doubles, triples) == pytest.approx((4.4, 2.4, 0.0))
    singles, doubles, triples = split_hits(3.0, 0.0, 9.0)  # extra 6 > non-HR hits 3: triples absorb the rest
    assert singles + doubles + triples == pytest.approx(3.0)
    assert singles + 2 * doubles + 3 * triples == pytest.approx(9.0)


def test_baseruns_reference_per9_line(adapter: BaseballSportAdapter) -> None:
    # league HBP 0.40, IBB 0.08 and CS 0.19 fill the per-9 line
    a = 8.0 + 3.0 + 0.40 - 1.2 - 0.5 * 0.08
    b = 0.78 * 4.4 + 1.15 * 2.4 + 1.85 * 1.2 + 0.16 * 0.5 - 0.22 * 0.19
    assert adapter.calculate_baseruns(REF_HOME) == pytest.approx(a * b / (b + 27.0) + 1.2, abs=1e-9)
    line = batting_line(REF_HOME)
    assert line.total_bases == pytest.approx(14.0)
    assert line.hits == pytest.approx(8.0)


def test_better_offence_scores_more(adapter: BaseballSportAdapter) -> None:
    assert adapter.calculate_baseruns({"hits_per_9": 9.5, "home_runs_per_9": 1.6, "total_bases_per_9": 16.0}) > adapter.calculate_baseruns({})


# ================================================================ pitching
def test_innings_notation() -> None:
    assert innings_from_notation(5.2) == pytest.approx(5 + 2 / 3)
    assert innings_from_notation(6.1) == pytest.approx(6 + 1 / 3)
    assert innings_from_notation(6.0) == 6.0
    assert innings_from_notation(5.5) == 5.5


def test_pitch_count_decay_and_bullpen_weighting_hand_computed() -> None:
    profile = pitching_profile({"sp_fip": 3.0, "bullpen_era": 5.0, "sp_expected_innings": 5.2, "sp_pitch_count_per_inning": 16})
    assert profile.sp_outs == 17 and profile.hook_reason == "expected_innings"
    # outs 14, 15, 16 are past pitch 75: +0.12 FIP per 15 pitches
    decayed = [3.0 + 0.12 * ((k + 0.5) * 16 / 3 - 75.0) / 15.0 for k in (14, 15, 16)]
    expected = (14 * 3.0 + sum(decayed) + 10 * 5.0) / 27.0
    assert profile.game_era == pytest.approx(expected, abs=1e-9)


def test_fatigue_hook_when_decayed_fip_exceeds_bullpen() -> None:
    profile = pitching_profile({"sp_fip": 4.2, "bullpen_era": 4.10})
    assert profile.hook_reason == "fatigue"
    assert profile.sp_outs == 14  # pitch 75 falls in the 15th out at 16 pitches an inning
    heavy = pitching_profile({"sp_fip": 4.2, "bullpen_era": 4.10, "sp_pitch_count_per_inning": 20})
    assert heavy.sp_outs < profile.sp_outs  # a high pitch count reaches the decay sooner


def test_better_starter_lowers_opponent_runs(adapter: BaseballSportAdapter) -> None:
    ace = adapter.calculate_fair_odds({"sp_fip": 2.6}, {})
    poor = adapter.calculate_fair_odds({"sp_fip": 5.6}, {})
    assert ace.sport_specific_metrics["away_expected_runs"] < poor.sport_specific_metrics["away_expected_runs"]
    assert ace.home_win_prob > poor.home_win_prob


# ================================================================ distributions
def test_innings_add_exactly() -> None:
    one = nb_innings(4.5, 1, 40)
    nine = one
    for _ in range(8):
        nine = np.convolve(nine, one)[:40]
    assert np.allclose(nine[:20], nb_innings(4.5, 9, 40)[:20], atol=1e-9)
    assert np.allclose(nb_innings(4.5, 9, 26), negative_binomial_pmf(4.5, 4.5, 26))


def test_state_runs_pmf_reproduces_re24() -> None:
    for bases, outs in ((0, 0), (MANFRED_RUNNER_STATE, 0), (7, 2), (5, 1)):
        pmf = state_runs_pmf(outs, bases, 1.0, 30)
        assert pmf.sum() == pytest.approx(1.0)
        assert 1.0 - pmf[0] == pytest.approx(SCORE_PROB[bases][outs])
        assert float(np.dot(np.arange(30), pmf)) == pytest.approx(RE24[bases][outs], rel=1e-3)
    assert state_runs_pmf(3, 7, 1.0)[0] == 1.0


def test_extras_joint_is_a_distribution_without_ties() -> None:
    dist = state_runs_pmf(0, MANFRED_RUNNER_STATE, 1.0)
    joint, q = extras_joint(dist, dist, dist, dist)
    assert joint.sum() == pytest.approx(1.0)
    assert np.trace(joint) == pytest.approx(0.0, abs=1e-12)
    assert q == pytest.approx(0.5, abs=0.03)  # equal teams: the walk-off edge is added afterwards
    home_wins = np.tril(joint, -1)
    rows, cols = np.nonzero(home_wins > 1e-12)
    assert set((rows - cols).tolist()) == {1}  # a home extra-inning win is a walk-off by one run


def test_extras_home_edge_is_52_percent(adapter: BaseballSportAdapter) -> None:
    joint = adapter._extras(1.0, 1.0, MatchContext.parse(None))
    assert float(np.tril(joint, -1).sum()) == pytest.approx(0.52, abs=1e-6)
    joint = adapter._extras(1.0, 1.0, MatchContext.parse({"extra_innings_home_win_prob": 0.6}))
    assert float(np.tril(joint, -1).sum()) == pytest.approx(0.6, abs=1e-6)


def test_compose_game_walk_off_and_skip() -> None:
    # away scores exactly 2, home has 2 before its last half: a run in the last half is a 3-2 walk-off
    a_total = np.array([0.0, 0.0, 1.0])
    h_pre = np.array([0.0, 0.0, 1.0])
    h_last = np.array([0.6, 0.3, 0.1])
    extras = np.zeros((16, 16))
    extras[0, 1] = 1.0  # the away side wins extras by one run
    final, p_extra, p_skip = compose_game(h_pre, a_total, h_last, extras)
    assert final[3, 2] == pytest.approx(0.4)  # 0.3 + 0.1: a walk-off ends the game by one run
    assert final[2, 3] == pytest.approx(0.6)  # the tie at 2-2 resolved in extras
    assert p_extra == pytest.approx(0.6) and p_skip == 0.0
    # home ahead after the top of the 9th: the bottom half is not played
    final, _, p_skip = compose_game(np.array([0.0, 0.0, 0.0, 1.0]), a_total, h_last, extras)
    assert p_skip == pytest.approx(1.0) and final[3, 2] == pytest.approx(1.0)


def test_trimmed_pmf_prices_lines_exactly() -> None:
    grid = ScoreGrid.independent(negative_binomial_pmf(4.6, 4.5, 26), negative_binomial_pmf(4.2, 4.5, 26))
    full, cut = grid.total_pmf(), trimmed(grid.total_pmf())
    assert len(cut.probs) < len(full.probs) and cut.probs.sum() == pytest.approx(1.0)
    for line in (6.5, 8.0, 8.5, 11.5):
        assert cut.line_outcome(line, over=True).effective_probability == pytest.approx(full.line_outcome(line, over=True).effective_probability, abs=1e-6)
    margin = grid.margin_pmf()
    expected = margin.line_outcome(1.5, over=True).effective_probability
    assert trimmed(margin).line_outcome(1.5, over=True).effective_probability == pytest.approx(expected, abs=1e-6)


# ================================================================ pricing
def test_reference_assertions(adapter: BaseballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=REF_HOME, away_stats=REF_AWAY, context={"park": "coors_field"})
    assert 0.0 < res.sport_specific_metrics["nrfi_probability"] < 1.0
    assert res.expected_total_score > 5.0
    assert res.sport_specific_metrics["park_factor"] == 1.34
    assert adapter.resolve_market_winner("moneyline", {"home_runs": 6, "away_runs": 4}, "home") is True
    assert adapter.resolve_market_winner("run_line_handicap", {"home_runs": 6, "away_runs": 4}, "home", line=-1.5) is True
    assert adapter.resolve_market_winner("nrfi_yrfi_first_inning", {"first_inning_runs": 0}, "nrfi") is True
    assert adapter.resolve_market_winner("first_5_innings_winner", {"f5_home_runs": 3, "f5_away_runs": 1}, "home") is True


def test_probabilities_are_distributions(adapter: BaseballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"park": "fenway_park"})
    assert res.home_win_prob + res.away_win_prob == pytest.approx(1.0, abs=1e-3)
    assert res.draw_prob == 0.0 and res.fair_draw_odds is None
    m = res.market_probabilities
    assert m["moneyline_home"] + m["moneyline_away"] == pytest.approx(1.0, abs=1e-3)
    assert m["run_line_home_-1.5"] + m["run_line_away_+1.5"] == pytest.approx(1.0, abs=1e-3)
    assert m["total_over_8.5"] + m["total_under_8.5"] == pytest.approx(1.0, abs=1e-3)
    assert m["nrfi"] + m["yrfi"] == pytest.approx(1.0, abs=1e-3)
    assert m["f5_home"] + m["f5_tie"] + m["f5_away"] == pytest.approx(1.0, abs=1e-3)
    assert m["f5_home_2way"] + m["f5_away_2way"] == pytest.approx(1.0, abs=1e-3)
    assert m["f5_run_line_home_-0.5"] == pytest.approx(m["f5_home"], abs=1e-3)
    assert sum(m[f"inning_highest_{k}"] for k in range(1, 10)) + m["inning_highest_tie"] == pytest.approx(1.0, abs=2e-3)
    assert m["inning_highest_1"] > m["inning_highest_2"]  # the top of the order bats in the 1st
    assert m["run_line_home_-1.5"] < m["moneyline_home"] < m["run_line_home_+1.5"]
    for value in m.values():
        assert 0.0 <= value <= 1.0
    json.dumps(res.sport_specific_metrics)  # the API serialises the metrics


def test_neutral_game_is_calibrated(adapter: BaseballSportAdapter) -> None:
    res = adapter.calculate_fair_odds({}, {})
    assert 0.52 < res.home_win_prob < 0.56  # MLB home teams win about 53-54%
    assert 8.3 < res.expected_total_score < 9.5
    assert 0.07 < res.sport_specific_metrics["extra_innings_probability"] < 0.13
    assert 0.44 < res.sport_specific_metrics["yrfi_probability"] < 0.56


def test_sub_models(adapter: BaseballSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    names = [s.name for s in res.sub_models]
    assert names == ["baseruns_negbin", "pythagenpat", "pitcher_fip_matchup"]
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    with_elo = adapter.calculate_fair_odds({**REF_HOME, "elo": 1600}, {**REF_AWAY, "elo": 1450})
    assert [s.name for s in with_elo.sub_models][-1] == "elo"
    assert sum(s.weight for s in with_elo.sub_models) == pytest.approx(1.0)
    benched = adapter.calculate_fair_odds({**REF_HOME, "elo": 1600}, {**REF_AWAY, "elo": 1450}, {"model_weights": {"elo": 0.0}})
    assert benched.consensus is not None and benched.consensus.model_count == 3


def test_pythagenpat_uses_season_runs(adapter: BaseballSportAdapter) -> None:
    res = adapter.calculate_fair_odds({"runs_scored_per_game": 5.6, "runs_allowed_per_game": 3.6}, {})
    pyth = next(s for s in res.sub_models if s.name == "pythagenpat")

    def pythagenpat(rs: float, ra: float) -> float:
        x = (rs + ra) ** 0.287
        return rs ** x / (rs ** x + ra ** x)

    home = pythagenpat(5.6, 3.6)
    away = pythagenpat(4.5, pitching_profile({}).game_era * 4.5 / 4.2)  # league offence, default staff
    odds = home / (1 - home) * (1 - away) / away * 0.54 / 0.46
    assert pyth.home_win_prob == pytest.approx(odds / (1 + odds), abs=1e-9)


def test_monotonic_in_offence(adapter: BaseballSportAdapter) -> None:
    weak = adapter.calculate_fair_odds({"hits_per_9": 7.0, "total_bases_per_9": 11.0}, {})
    strong = adapter.calculate_fair_odds({"hits_per_9": 9.5, "total_bases_per_9": 16.0, "home_runs_per_9": 1.5}, {})
    assert strong.home_win_prob > weak.home_win_prob
    assert strong.expected_total_score > weak.expected_total_score


def test_park_factors(adapter: BaseballSportAdapter) -> None:
    assert len(PARK_FACTORS) >= 27
    coors = adapter.calculate_fair_odds({}, {}, {"park": "coors_field"})
    petco = adapter.calculate_fair_odds({}, {}, {"park": "petco_park"})
    assert coors.expected_total_score > petco.expected_total_score
    explicit = adapter.calculate_fair_odds({}, {}, {"park_factor": 1.2})
    assert explicit.sport_specific_metrics["park_factor"] == 1.2
    assert adapter.calculate_fair_odds({}, {}, {"park": "unknown_field"}).sport_specific_metrics["park_factor"] == 1.0


# ================================================================ context
def test_weather_temperature_and_wind(adapter: BaseballSportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {}, {"park": "wrigley_field"})
    hot_out = adapter.calculate_fair_odds({}, {}, {"park": "wrigley_field", "weather": {"temperature_f": 90, "wind_mph": 15, "wind_direction": "out"}})
    cold_in = adapter.calculate_fair_odds({}, {}, {"park": "wrigley_field", "weather": {"temperature_f": 45, "wind_mph": 15, "wind_direction": "in"}})
    assert hot_out.expected_total_score > base.expected_total_score > cold_in.expected_total_score
    expected = (1 + 0.015 * 2.0) * (1 + 0.03 * 3.0)
    assert hot_out.sport_specific_metrics["weather_run_multiplier"] == pytest.approx(expected, abs=1e-3)
    cross = adapter.calculate_fair_odds({}, {}, {"park": "wrigley_field", "weather": {"temperature_f": 70, "wind_mph": 15, "wind_direction": "cross"}})
    assert cross.sport_specific_metrics["weather_run_multiplier"] == pytest.approx(1.0)


def test_weather_ignored_indoors_and_friction_replaces(adapter: BaseballSportAdapter) -> None:
    hot = {"temperature_f": 95, "wind_mph": 20, "wind_direction": "out"}
    dome = adapter.calculate_fair_odds({}, {}, {"park": "tropicana_field", "weather": hot})
    assert dome.sport_specific_metrics["weather_run_multiplier"] == 1.0
    closed = adapter.calculate_fair_odds({}, {}, {"park": "chase_field", "weather": hot})
    assert closed.sport_specific_metrics["weather_run_multiplier"] == 1.0
    opened = adapter.calculate_fair_odds({}, {}, {"park": "chase_field", "roof": "open", "weather": hot})
    assert opened.sport_specific_metrics["weather_run_multiplier"] > 1.0
    friction = adapter.calculate_fair_odds({}, {}, {"park": "wrigley_field", "weather": {**hot, "friction": 0.9}})
    assert friction.sport_specific_metrics["weather_run_multiplier"] == pytest.approx(0.9)


def test_umpire_adjustments(adapter: BaseballSportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {})
    tight = adapter.calculate_fair_odds({}, {}, {"officials": {"umpire": "Small Zone", "strike_zone_run_factor": 1.06}})
    assert tight.expected_total_score > base.expected_total_score
    assert tight.sport_specific_metrics["umpire_run_factor"] == 1.06
    delta = adapter.calculate_fair_odds({}, {}, {"officials": {"runs_per_game_delta": -0.6}})
    assert delta.sport_specific_metrics["home_expected_runs"] == pytest.approx(base.sport_specific_metrics["home_expected_runs"] - 0.3, abs=1e-3)


def test_absences_war(adapter: BaseballSportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {})
    hitter = adapter.calculate_fair_odds({}, {}, {"absences": [{"side": "home", "player": "Slugger", "war": 6.0, "position": "rf"}]})
    loss = 6.0 * 10.0 / 162.0
    assert hitter.sport_specific_metrics["home_expected_runs"] == pytest.approx(base.sport_specific_metrics["home_expected_runs"] - loss, abs=1e-3)
    assert hitter.home_win_prob < base.home_win_prob
    pitcher = adapter.calculate_fair_odds({}, {}, {"absences": [{"side": "home", "player": "Ace", "war": 5.0, "position": "sp"}]})
    assert pitcher.sport_specific_metrics["away_expected_runs"] == pytest.approx(base.sport_specific_metrics["away_expected_runs"] + 5.0 * 10 / 162, abs=1e-3)
    doubtful = adapter.calculate_fair_odds({}, {}, {"absences": [{"side": "home", "player": "Slugger", "war": 6.0, "status": "doubtful"}]})
    assert doubtful.sport_specific_metrics["home_absence_runs_lost"] == pytest.approx(loss / 2, abs=1e-4)
    assert base.evidence == {"weather": False, "absences": False, "officials": False, "model_weights": False}


# ================================================================ NRFI / YRFI
def test_advance_rules() -> None:
    assert _advance(0, "walk") == (1, False)
    assert _advance(1, "walk") == (3, False)
    assert _advance(3, "walk") == (7, False)
    assert _advance(7, "walk") == (7, True)
    assert _advance(5, "walk") == (7, False)
    assert _advance(1, "single") == (3, False)
    assert _advance(2, "single") == (1, True)
    assert _advance(1, "double") == (6, False)
    assert _advance(0, "triple") == (4, False)
    assert _advance(1, "triple") == (4, True)
    assert _advance(0, "home_run") == (0, True)


def test_traffic_formula_hand_computed() -> None:
    assert top_three_traffic([0.3, 0.3, 0.3], [0.03, 0.03, 0.03]) == pytest.approx(1 - 0.7 ** 3 * 0.97 ** 3)


def test_markov_first_inning() -> None:
    league = first_half_inning_markov([0.315] * 9, [0.03] * 9, [1.0] * 9, 1.0)
    assert 0.22 < league < 0.33  # MLB: about 27% of half-innings from the leadoff produce a run
    better = first_half_inning_markov([0.380] * 9, [0.05] * 9, [1.2] * 9, 1.0)
    assert better > league
    assert first_half_inning_markov([0.0] * 9, [0.0] * 9, [1.0] * 9, 1.0) < 1e-3  # on-base floored at 1%


def test_nrfi_responds_to_lineup_and_first_inning_era(adapter: BaseballSportAdapter) -> None:
    base = adapter.calculate_nrfi_yrfi({}, {})
    stacked = adapter.calculate_nrfi_yrfi({"top_order_obp": [0.400, 0.390, 0.410, 0.380], "top_order_hr_rate": [0.04, 0.05, 0.07, 0.06]}, {})
    assert stacked > base
    shaky = adapter.calculate_nrfi_yrfi({"first_inning_era": 7.5}, {})  # the home starter pitches the top of the 1st
    assert shaky > base
    res = adapter.calculate_fair_odds({}, {})
    assert res.sport_specific_metrics["yrfi_probability"] == pytest.approx(base, abs=1e-3)
    parts = res.sport_specific_metrics["first_inning_away_score_prob"]
    blended = 0.5 * parts["markov"] + 0.2 * parts["traffic"] + 0.3 * parts["first_inning_era"]
    assert parts["blended"] == pytest.approx(blended, abs=1e-3)
    era = 4.2 * 1.12 / 9.0
    assert parts["first_inning_era"] == pytest.approx(1 - (INNING_DISPERSION / (INNING_DISPERSION + era)) ** INNING_DISPERSION, abs=1e-3)


# ================================================================ in-play
def _live(adapter: BaseballSportAdapter, **state: object) -> tuple[float, float]:
    w = adapter.in_play_win_probability({}, {}, dict(state))
    assert w.home_win_prob + w.away_win_prob + w.draw_prob == pytest.approx(1.0)
    return w.home_win_prob, w.away_win_prob


def test_in_play_game_over_states(adapter: BaseballSportAdapter) -> None:
    assert _live(adapter, inning=9, half="bottom", outs=0, home_runs=5, away_runs=4) == (1.0, 0.0)  # home leads: no bottom 9th
    assert _live(adapter, inning=9, half="bottom", outs=3, home_runs=3, away_runs=4) == (0.0, 1.0)
    assert _live(adapter, inning=11, half="top", outs=0, home_runs=3, away_runs=4) == (0.0, 1.0)
    assert _live(adapter, inning=6, half="top", home_runs=1, away_runs=4, status="final") == (0.0, 1.0)


def test_in_play_dynamics(adapter: BaseballSportAdapter) -> None:
    start, _ = _live(adapter, inning=1, half="top", outs=0, home_runs=0, away_runs=0)
    pregame = adapter.calculate_fair_odds({}, {}).sport_specific_metrics["consensus"]
    assert start == pytest.approx(pregame["home_win_prob"], abs=0.02)
    ahead, _ = _live(adapter, inning=9, half="top", outs=2, home_runs=3, away_runs=2)
    assert ahead > 0.93
    tied, _ = _live(adapter, inning=9, half="bottom", outs=0, home_runs=2, away_runs=2)
    assert 0.6 < tied < 0.75  # walk-off chances plus the extra-inning edge
    loaded, _ = _live(adapter, inning=9, half="bottom", outs=2, bases=[1, 2, 3], home_runs=2, away_runs=3)
    empty, _ = _live(adapter, inning=9, half="bottom", outs=2, bases=[], home_runs=2, away_runs=3)
    assert loaded > empty
    extras, _ = _live(adapter, inning=10, half="bottom", outs=0, bases=[2], home_runs=4, away_runs=5)
    assert 0.3 < extras < 0.6
    w = adapter.in_play_win_probability({}, {}, {"inning": 3, "half": "bottom", "outs": 1, "bases": [1], "home_runs": 1, "away_runs": 1})
    assert 0.0 < w.detail["current_half_score_prob"] < 1.0


# ================================================================ settlement
SETTLEMENT_CASES = [
    ("moneyline", {"home_runs": 6, "away_runs": 4}, "home", None, LegStatus.WON),
    ("match_winner", {"home_runs": 2, "away_runs": 4}, "home", None, LegStatus.LOST),
    ("moneyline", {"home_score": 3, "away_score": 3}, "away", None, LegStatus.VOID),
    ("run_line_handicap", {"home_runs": 6, "away_runs": 4}, "home", -1.5, LegStatus.WON),
    ("run_line", {"home_runs": 5, "away_runs": 4}, "home", -1.5, LegStatus.LOST),
    ("run_line", {"home_runs": 5, "away_runs": 4}, "away", 1.5, LegStatus.WON),
    ("alt_run_line", {"home_runs": 6, "away_runs": 4}, "home", -2.0, LegStatus.VOID),
    ("game_total_runs", {"home_runs": 5, "away_runs": 4}, "over", 8.5, LegStatus.WON),
    ("total_runs", {"home_runs": 5, "away_runs": 3}, "over", 8.0, LegStatus.VOID),
    ("alt_total_runs", {"home_runs": 5, "away_runs": 3}, "over", 8.25, LegStatus.HALF_LOST),
    ("alt_total_runs", {"home_runs": 5, "away_runs": 3}, "under", 8.25, LegStatus.HALF_WON),
    ("team_total_runs", {"home_runs": 5, "away_runs": 3}, "away_under", 3.5, LegStatus.WON),
    ("nrfi_yrfi_first_inning", {"first_inning_runs": 0}, "nrfi", None, LegStatus.WON),
    ("nrfi_yrfi_first_inning", {"first_inning_runs": 2}, "nrfi", None, LegStatus.LOST),
    ("yrfi", {"first_inning_runs": 1}, "yrfi", None, LegStatus.WON),
    ("nrfi", {"periods": {"i1": [0, 1]}}, "yes", None, LegStatus.WON),
    ("nrfi", {"innings": [[0, 0], [1, 0]]}, "no", None, LegStatus.WON),
    ("first_inning_winner", {"innings": [[0, 2], [1, 0]]}, "home", None, LegStatus.WON),
    ("first_inning_total", {"innings": [[0, 2], [1, 0]]}, "over", 1.5, LegStatus.WON),
    ("first_5_innings_winner", {"f5_home_runs": 3, "f5_away_runs": 1}, "home", None, LegStatus.WON),
    ("first_5_innings_winner", {"f5_home_runs": 2, "f5_away_runs": 2}, "home", None, LegStatus.VOID),
    ("first_5_innings_3_way", {"f5_home_runs": 2, "f5_away_runs": 2}, "home", None, LegStatus.LOST),
    ("first_5_innings_3_way", {"f5_home_runs": 2, "f5_away_runs": 2}, "tie", None, LegStatus.WON),
    ("first_5_innings_run_line", {"periods": {"f5": [3, 1]}}, "home", -1.5, LegStatus.WON),
    ("first_5_innings_total", {"periods": {"f5": [3, 1]}}, "under", 4.0, LegStatus.VOID),
    ("next_team_to_score", {"events": [{"type": "run", "team": "away", "t": 5}]}, "away", None, LegStatus.WON),
    ("player_hits", {"player_stats": {"Judge": {"hits": 2}}}, "over", 1.5, LegStatus.WON),
    ("pitcher_strikeouts", {"player_stats": {"Cole": {"strikeouts": 6}}, "participants": ["Cole"]}, "over", 6.5, LegStatus.LOST),
    ("player_total_bases", {"player_stats": {}, "participants": ["Other"]}, "over", 1.5, LegStatus.VOID),
    ("moneyline", {"home_runs": 6, "away_runs": 4, "status": "postponed"}, "home", None, LegStatus.VOID),
]


@pytest.mark.parametrize("case", SETTLEMENT_CASES, ids=[f"{c[0]}-{c[2]}-{i}" for i, c in enumerate(SETTLEMENT_CASES)])
def test_settlement(adapter: BaseballSportAdapter, case: tuple) -> None:
    market, result, selection, line, expected = case
    params = None
    if market.startswith(("player", "pitcher")):
        params = {"player": next(iter(result["player_stats"]), "nobody")}
    elif market.startswith("next"):
        params = {"after": 0}
    assert adapter.settle_market(market, result, selection, line, params) == expected


INNINGS = [[0, 1], [0, 0], [3, 0], [0, 0], [0, 1], [0, 0], [1, 0], [0, 0], [0, "x"]]


def test_inning_of_highest_score_settlement(adapter: BaseballSportAdapter) -> None:
    assert adapter.settle_market("inning_of_highest_score", {"innings": INNINGS}, "3") == LegStatus.WON
    assert adapter.settle_market("highest_scoring_inning", {"innings": INNINGS}, "inning_1") == LegStatus.LOST
    assert adapter.settle_market("inning_of_highest_score", {"inning_runs": [1, 0, 2, 0, 2, 0, 0, 0, 0]}, "tie") == LegStatus.WON
    assert adapter.settle_market("inning_of_highest_score", {"inning_runs": [1, 0, 2, 0, 2, 0, 0, 0, 0]}, "3rd") == LegStatus.LOST
    with pytest.raises(SettlementInputError):
        adapter.settle_market("inning_of_highest_score", {"home_runs": 3, "away_runs": 1}, "1")
    with pytest.raises(SettlementInputError):
        adapter.settle_market("inning_of_highest_score", {"innings": INNINGS}, "10")


def test_innings_build_the_result(adapter: BaseballSportAdapter) -> None:
    res = adapter.normalize_result({"innings": INNINGS})
    assert (res.home, res.away) == (2.0, 4.0)
    assert res.periods["f5"] == (2.0, 3.0)
    assert res.extras["first_inning_runs"] == 1.0
    assert adapter.settle_market("first_5_innings_total", {"innings": INNINGS}, "over", 4.5) == LegStatus.WON


def test_settlement_errors(adapter: BaseballSportAdapter) -> None:
    assert adapter.resolve_market_winner("nrfi_yrfi_first_inning", {"home_runs": 3}, "nrfi") is False
    with pytest.raises(SettlementInputError):
        adapter.settle_market("nrfi", {"first_inning_runs": 0}, "maybe")
    with pytest.raises(SettlementInputError):
        adapter.settle_market("cricket_sixes", {"home_runs": 1}, "home")


def test_supported_markets(adapter: BaseballSportAdapter) -> None:
    markets = adapter.supported_markets
    for key in ("nrfi_yrfi_first_inning", "run_line_handicap", "first_5_innings_winner", "inning_of_highest_score", "nrfi_yrfi"):
        assert key in markets


# ================================================================ performance
def test_pricing_is_fast(adapter: BaseballSportAdapter) -> None:
    adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"park": "coors_field"})
    timings = []
    for _ in range(10):
        start = time.perf_counter()
        adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"park": "coors_field", "weather": {"temperature_f": 80}})
        timings.append(time.perf_counter() - start)
    assert min(timings) < 0.025
