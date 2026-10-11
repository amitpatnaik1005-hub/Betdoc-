"""
Ice hockey adapter tests: shot-level xG, Corsi/Fenwick possession, empty-net late game, overtime (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from app.domain.sports.ice_hockey_adapter import (
    EMPTY_NET_RATE,
    SIX_ON_FIVE_RATE,
    IceHockeySportAdapter,
    expected_goals_from_shots,
    inflate_ties,
    late_game,
    overtime_home_win,
    resolve_overtime,
    shot_goal_probability,
)
from app.domain.sports.settlement import SettlementInputError
from app.schemas.bet_types import LegStatus

REF_HOME = {"expected_goals_per_60": 3.4, "corsi_for_pct": 54.0}
REF_AWAY = {"expected_goals_per_60": 2.6, "corsi_for_pct": 46.0}


@pytest.fixture(scope="module")
def adapter() -> IceHockeySportAdapter:
    return IceHockeySportAdapter()


# ================================================================ identity
def test_identity_and_class_attributes(adapter: IceHockeySportAdapter) -> None:
    assert adapter.sport_key == "ice_hockey"
    assert adapter.display_name == "Ice Hockey"
    assert adapter.odds_api_family == "icehockey"
    assert adapter.kelly_multiplier == 0.30
    assert adapter.has_draw is False
    assert adapter.model_family == "xG Poisson, Corsi & Empty Net"
    assert {"moneyline", "point_spread", "game_props", "btts"} <= adapter.BET_TYPES
    assert "teaser" not in adapter.BET_TYPES and "first_5_innings" not in adapter.BET_TYPES
    for market in adapter.standard_markets:
        adapter.canonical_market(market)


# ================================================================ shot-level xG
def test_shot_xg_calibration() -> None:
    average = shot_goal_probability(35.0, 30.0)
    assert 0.07 <= average <= 0.09
    assert average == pytest.approx(1.0 / (1.0 + math.exp(-(-1.258 - 0.0252 * 35 - 0.010 * 30))), abs=1e-12)
    assert shot_goal_probability(12.0, 10.0, rebound=True) >= 0.25
    assert shot_goal_probability(12.0, 10.0, rush=True) > shot_goal_probability(12.0, 10.0)
    assert shot_goal_probability(60.0, 45.0) < shot_goal_probability(30.0, 45.0) < shot_goal_probability(10.0, 45.0)
    assert shot_goal_probability(20.0, -40.0) == shot_goal_probability(20.0, 40.0)


def test_expected_goals_from_shots_sums() -> None:
    shots = [{"distance_ft": 35, "angle_deg": 30}, {"distance": 12, "angle": 10, "rebound": True}, {"distance_ft": 60, "angle_deg": 45}]
    expected = shot_goal_probability(35, 30) + shot_goal_probability(12, 10, rebound=True) + shot_goal_probability(60, 45)
    assert expected_goals_from_shots(shots) == pytest.approx(expected)
    assert expected_goals_from_shots([]) == 0.0


def test_shot_sample_feeds_team_rate(adapter: IceHockeySportAdapter) -> None:
    shots = [{"distance_ft": 15, "angle_deg": 10, "rebound": True}] * 20
    res = adapter.calculate_fair_odds({"shots": shots, "games_in_sample": 2}, {})
    sample_rate = expected_goals_from_shots(shots) / 2
    assert res.sport_specific_metrics["projected_home_xg"] == pytest.approx(sample_rate + 0.11, abs=1e-3)


# ================================================================ building blocks
def test_late_game_transformation_single_cells() -> None:
    m = np.zeros((6, 6))
    m[2, 1] = 1.0  # home leads by one: the away coach pulls the goalie
    out, pulled = late_game(m, 0.1, 0.1, EMPTY_NET_RATE, SIX_ON_FIVE_RATE)
    assert pulled == pytest.approx(1.0)
    assert out[3, 1] == pytest.approx(0.224)
    assert out[2, 2] == pytest.approx(0.08)
    assert out[2, 1] == pytest.approx(1.0 - 0.224 - 0.08)
    m = np.zeros((6, 6))
    m[1, 1] = 1.0  # level: the window is played at the normal rates
    out, pulled = late_game(m, 0.1, 0.05, EMPTY_NET_RATE, SIX_ON_FIVE_RATE)
    assert pulled == 0.0
    assert out[1, 1] == pytest.approx(math.exp(-0.15))
    assert out[2, 1] == pytest.approx(0.1 * math.exp(-0.15))
    m = np.zeros((6, 6))
    m[0, 3] = 1.0  # a three-goal margin: no pull
    out, pulled = late_game(m, 0.0, 0.0, EMPTY_NET_RATE, SIX_ON_FIVE_RATE)
    assert pulled == 0.0 and out[0, 3] == pytest.approx(1.0)


def test_resolve_overtime_adds_one_goal_to_the_winner() -> None:
    reg = np.zeros((4, 4))
    reg[2, 2], reg[3, 1] = 0.25, 0.75
    out = resolve_overtime(reg, 0.6)
    assert out[3, 2] == pytest.approx(0.25 * 0.6)
    assert out[2, 3] == pytest.approx(0.25 * 0.4)
    assert out[2, 2] == 0.0 and out[3, 1] == pytest.approx(0.75)
    assert out.sum() == pytest.approx(1.0)


def test_overtime_probability() -> None:
    neutral = overtime_home_win(3.16, 2.94)  # league rates with home ice
    assert 0.51 < neutral < 0.54
    assert overtime_home_win(3.0, 3.0, playoff=True) == pytest.approx(0.5)
    assert overtime_home_win(4.0, 2.0, playoff=True) == pytest.approx(4.0 / 6.0)
    assert overtime_home_win(3.0, 3.0, 0.0) == pytest.approx(0.51)  # straight to the shootout


def test_inflate_ties() -> None:
    m = np.full((3, 3), 1.0 / 9.0)
    assert np.allclose(inflate_ties(m, 0.0), m)
    out = inflate_ties(m, 0.5)
    assert out.sum() == pytest.approx(1.0)
    assert np.trace(out) == pytest.approx(1.5 * 3 / (9 + 1.5))


# ================================================================ pricing
def test_reference_assertions(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=REF_HOME, away_stats=REF_AWAY)
    assert res.home_win_prob > 0.55
    assert res.sport_specific_metrics["empty_net_probability"] == 0.224
    assert adapter.resolve_market_winner("regulation_3_way", {"reg_home_score": 3, "reg_away_score": 3}, "draw") is True
    assert adapter.resolve_market_winner("empty_net_goal_yes_no", {"empty_net_goal_scored": True}, "yes") is True


def test_probabilities_and_draw_semantics(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    m = res.market_probabilities
    # the headline market is the moneyline including overtime: a coherent 2-way partition
    assert res.home_win_prob + res.away_win_prob + res.draw_prob == pytest.approx(1.0, abs=1e-3)
    assert res.draw_prob == 0.0 and res.fair_draw_odds is None
    assert m["regulation_draw"] == pytest.approx(res.sport_specific_metrics["regulation_draw_prob"], abs=1e-4)  # the 3-way X
    assert m["regulation_home"] + m["regulation_draw"] + m["regulation_away"] == pytest.approx(1.0, abs=1e-3)
    assert m["moneyline_home"] == pytest.approx(m["regulation_home"] + m["regulation_draw"] * res.sport_specific_metrics["ot_home_win_prob"], abs=1e-3)
    assert m["puck_line_home_-1.5"] + m["puck_line_away_+1.5"] == pytest.approx(1.0, abs=1e-3)
    assert m["total_over_5.5"] + m["total_under_5.5"] == pytest.approx(1.0, abs=1e-3)
    assert m["empty_net_goal_yes"] + m["empty_net_goal_no"] == pytest.approx(1.0, abs=1e-3)
    assert m["empty_net_goal_yes"] == pytest.approx(res.sport_specific_metrics["goalie_pull_probability"] * 0.224, abs=1e-3)
    assert m["btts_yes"] + m["btts_no"] == pytest.approx(1.0, abs=1e-3)
    assert m["p1_home"] + m["p1_draw"] + m["p1_away"] == pytest.approx(1.0, abs=1e-3)
    assert m["puck_line_home_-1.5"] < m["regulation_home"]
    for value in m.values():
        assert 0.0 <= value <= 1.0
    json.dumps(res.sport_specific_metrics)  # the API serialises the metrics


def test_neutral_game_is_calibrated(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds({}, {})
    assert 0.52 < res.home_win_prob < 0.56  # NHL home teams win about 53-54%
    assert 0.19 < res.sport_specific_metrics["regulation_draw_prob"] < 0.24  # about 22% of NHL games reach overtime
    assert 5.8 < res.expected_total_score < 6.8
    assert 0.25 < res.market_probabilities["puck_line_home_-1.5"] < 0.36


def test_sub_models(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    assert [s.name for s in res.sub_models] == ["xg_poisson", "corsi_fenwick_possession", "goal_differential_pythagorean"]
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    assert all(s.home_win_prob > 0.55 for s in res.sub_models)
    with_elo = adapter.calculate_fair_odds({**REF_HOME, "elo": 1550}, {**REF_AWAY, "elo": 1500})
    assert with_elo.sub_models[-1].name == "elo"
    benched = adapter.calculate_fair_odds({**REF_HOME, "elo": 1550}, {**REF_AWAY, "elo": 1500}, {"model_weights": {"corsi_fenwick_possession": 0.0}})
    assert benched.consensus is not None and benched.consensus.model_count == 3
    with pytest.raises(ValueError, match="at least 3"):  # pillar 1 refuses a two-model consensus
        adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"model_weights": {"corsi_fenwick_possession": 0.0}})


def test_possession_regression_hand_computed(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    gh, ga = 0.5 + 0.7 * 0.04, 0.5 - 0.7 * 0.04
    odds = gh / (1 - gh) * (1 - ga) / ga
    share = odds / (1 + odds) + 0.11 / (3.51 + 2.49)
    expected = share ** 2.05 / (share ** 2.05 + (1 - share) ** 2.05)
    poss = next(s for s in res.sub_models if s.name == "corsi_fenwick_possession")
    assert poss.home_win_prob == pytest.approx(expected, abs=1e-9)
    fenwick = adapter.calculate_fair_odds({**REF_HOME, "fenwick_for_pct": 0.58}, REF_AWAY)
    assert fenwick.sport_specific_metrics["home_attempt_share"] == pytest.approx(0.4 * 0.54 + 0.6 * 0.58, abs=1e-4)


def test_goal_differential_pythagorean_hand_computed(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds({"goals_for_per_game": 3.5, "goals_against_per_game": 2.7}, {"goals_for_per_game": 2.9, "goals_against_per_game": 3.2})
    pyth = next(s for s in res.sub_models if s.name == "goal_differential_pythagorean")
    home = 3.5 ** 2.05 / (3.5 ** 2.05 + 2.7 ** 2.05)
    away = 2.9 ** 2.05 / (2.9 ** 2.05 + 3.2 ** 2.05)
    odds = home / (1 - home) * (1 - away) / away * 0.535 / 0.465
    assert pyth.home_win_prob == pytest.approx(odds / (1 + odds), abs=1e-9)


def test_monotonicity(adapter: IceHockeySportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {})
    better_xg = adapter.calculate_fair_odds({"expected_goals_per_60": 3.6}, {})
    assert better_xg.home_win_prob > base.home_win_prob
    better_corsi = adapter.calculate_fair_odds({"corsi_for_pct": 56.0}, {})
    assert better_corsi.home_win_prob > base.home_win_prob
    hot_goalie = adapter.calculate_fair_odds({"gsax_per_60": 0.4}, {})
    assert hot_goalie.sport_specific_metrics["projected_away_xg"] == pytest.approx(base.sport_specific_metrics["projected_away_xg"] - 0.4, abs=1e-3)
    assert hot_goalie.home_win_prob > base.home_win_prob
    leaky = adapter.calculate_fair_odds({}, {"expected_goals_against_per_60": 3.6})
    assert leaky.sport_specific_metrics["projected_home_xg"] > base.sport_specific_metrics["projected_home_xg"]


# ================================================================ context
def test_referee_power_plays(adapter: IceHockeySportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {})
    whistle = adapter.calculate_fair_odds({}, {}, {"officials": {"referee": "Busy", "penalties_per_game": 8.6}})
    assert whistle.sport_specific_metrics["referee_power_play_goal_delta"] == pytest.approx(2.0 * 0.18)
    assert whistle.sport_specific_metrics["projected_home_xg"] == pytest.approx(base.sport_specific_metrics["projected_home_xg"] + 0.18, abs=1e-3)
    assert whistle.expected_total_score > base.expected_total_score


def test_absences(adapter: IceHockeySportAdapter) -> None:
    base = adapter.calculate_fair_odds({}, {})
    skater = adapter.calculate_fair_odds({}, {}, {"absences": [{"side": "home", "player": "Star C", "war": 4.1, "position": "c"}]})
    assert skater.sport_specific_metrics["projected_home_xg"] == pytest.approx(base.sport_specific_metrics["projected_home_xg"] - 4.1 * 6 / 82, abs=1e-3)
    assert skater.home_win_prob < base.home_win_prob
    goalie = adapter.calculate_fair_odds({}, {}, {"absences": [{"side": "away", "player": "Starter", "war": 2.0, "position": "g"}]})
    assert goalie.sport_specific_metrics["projected_home_xg"] == pytest.approx(base.sport_specific_metrics["projected_home_xg"] + 2.0 * 6 / 82, abs=1e-3)
    assert goalie.home_win_prob > base.home_win_prob


def test_weather_is_ignored_indoors(adapter: IceHockeySportAdapter) -> None:
    base = adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    snowy = adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"weather": {"temperature_c": -10, "wind_kph": 40, "friction": 0.7}})
    assert snowy.home_win_prob == base.home_win_prob and snowy.expected_total_score == base.expected_total_score
    assert snowy.evidence["weather"] is True and snowy.sport_specific_metrics["weather_applied"] is False


def test_ot_override_and_playoff(adapter: IceHockeySportAdapter) -> None:
    res = adapter.calculate_fair_odds({}, {}, {"ot_home_win_prob": 0.7})
    assert res.sport_specific_metrics["ot_home_win_prob"] == 0.7
    playoff = adapter.calculate_fair_odds({}, {}, {"playoff": True})
    m = playoff.sport_specific_metrics
    assert m["ot_home_win_prob"] == pytest.approx(m["projected_home_xg"] / (m["projected_home_xg"] + m["projected_away_xg"]), abs=1e-3)


# ================================================================ in-play
def _live(adapter: IceHockeySportAdapter, **state: object) -> float:
    w = adapter.in_play_win_probability({}, {}, dict(state))
    assert w.home_win_prob + w.away_win_prob + w.draw_prob == pytest.approx(1.0)
    return w.home_win_prob


def test_in_play_finished_states(adapter: IceHockeySportAdapter) -> None:
    assert _live(adapter, period=3, seconds_remaining_in_period=0, home_score=3, away_score=2) == 1.0
    assert _live(adapter, period=4, seconds_remaining_in_period=120, home_score=2, away_score=3) == 0.0
    assert _live(adapter, period=2, seconds_remaining_in_period=500, home_score=1, away_score=4, status="final") == 0.0


def test_in_play_dynamics(adapter: IceHockeySportAdapter) -> None:
    start = _live(adapter, period=1, seconds_remaining_in_period=1200, home_score=0, away_score=0)
    assert start == pytest.approx(adapter.calculate_fair_odds({}, {}).home_win_prob, abs=0.01)
    late_lead = _live(adapter, period=3, seconds_remaining_in_period=120, home_score=3, away_score=2)
    early_lead = _live(adapter, period=1, seconds_remaining_in_period=600, home_score=1, away_score=0)
    assert late_lead > early_lead > start
    # a one-goal lead with the opponent's goalie pulled: only the 6-on-5 equaliser (then overtime) can beat the leader
    pulled = _live(adapter, period=3, seconds_remaining_in_period=90, home_score=3, away_score=2, goalie_pulled="away")
    assert pulled == pytest.approx(1.0 - 0.08 * 90 / 150 * (1.0 - adapter.calculate_fair_odds({}, {}).sport_specific_metrics["ot_home_win_prob"]), abs=2e-3)
    power_play = _live(adapter, period=2, seconds_remaining_in_period=600, home_score=1, away_score=1, home_skaters=5, away_skaters=4)
    even = _live(adapter, period=2, seconds_remaining_in_period=600, home_score=1, away_score=1)
    short = _live(adapter, period=2, seconds_remaining_in_period=600, home_score=1, away_score=1, home_skaters=4, away_skaters=5)
    assert power_play > even > short
    tied_end = _live(adapter, period=3, seconds_remaining_in_period=0, home_score=2, away_score=2)
    assert tied_end == pytest.approx(adapter.calculate_fair_odds({}, {}).sport_specific_metrics["ot_home_win_prob"], abs=1e-3)
    ot = adapter.in_play_win_probability({}, {}, {"period": 4, "seconds_remaining_in_period": 30, "home_score": 2, "away_score": 2})
    assert ot.detail["overtime"] is True and 0.5 < ot.home_win_prob < 0.53


# ================================================================ settlement
SETTLEMENT_CASES = [
    ("moneyline_incl_ot", {"home_score": 4, "away_score": 3}, "home", None, None, LegStatus.WON),
    ("match_winner", {"home_score": 2, "away_score": 3}, "home", None, None, LegStatus.LOST),
    ("puck_line_handicap", {"home_score": 4, "away_score": 2}, "home", -1.5, None, LegStatus.WON),
    ("puck_line", {"home_score": 3, "away_score": 2}, "home", -1.5, None, LegStatus.LOST),
    ("puck_line", {"home_score": 3, "away_score": 2}, "away", 1.5, None, LegStatus.WON),
    ("alt_puck_line", {"home_score": 3, "away_score": 2}, "home", -1.0, None, LegStatus.VOID),
    ("regulation_3_way", {"reg_home_score": 3, "reg_away_score": 3}, "draw", None, None, LegStatus.WON),
    ("regulation_3_way", {"home_score": 4, "away_score": 3, "decided_in": "OT"}, "home", None, None, LegStatus.LOST),
    ("regulation_3_way", {"home_score": 4, "away_score": 3, "decided_in": "so"}, "draw", None, None, LegStatus.WON),
    ("60_minute_line", {"home_score": 4, "away_score": 3, "period_scores": [[1, 1], [2, 1], [0, 1], [1, 0]]}, "draw", None, None, LegStatus.WON),
    ("regulation_3_way", {"home_score": 4, "away_score": 2, "decided_in": "regulation"}, "home", None, None, LegStatus.WON),
    ("game_total_goals", {"home_score": 4, "away_score": 2}, "over", 5.5, None, LegStatus.WON),
    ("game_total_goals", {"home_score": 4, "away_score": 2}, "under", 6.0, None, LegStatus.VOID),
    ("alt_total_goals", {"home_score": 4, "away_score": 2}, "over", 5.75, None, LegStatus.HALF_WON),
    ("team_total_goals", {"home_score": 4, "away_score": 2}, "home_over", 3.5, None, LegStatus.WON),
    ("empty_net_goal_yes_no", {"empty_net_goal_scored": True}, "yes", None, None, LegStatus.WON),
    ("empty_net_goal_yes_no", {"empty_net_goal_scored": False}, "yes", None, None, LegStatus.LOST),
    ("empty_net_goal_yes_no", {"empty_net_goals": 0}, "no", None, None, LegStatus.WON),
    ("empty_net_goal_yes_no", {"events": [{"type": "goal", "team": "home", "t": 3550, "empty_net": True}]}, "yes", None, None, LegStatus.WON),
    ("empty_net_goal_yes_no", {"events": [{"type": "goal", "team": "home", "t": 300}]}, "no", None, None, LegStatus.WON),
    ("first_period_winner", {"periods": {"p1": [1, 1]}}, "draw", None, None, LegStatus.WON),
    ("first_period_total", {"period_scores": [[2, 1], [0, 0], [1, 1]]}, "over", 2.5, None, LegStatus.WON),
    ("first_period_puck_line", {"periods": {"p1": [2, 0]}}, "home", -1.5, None, LegStatus.WON),
    ("second_period_winner", {"period_scores": [[2, 1], [0, 1], [1, 1]]}, "away", None, None, LegStatus.WON),
    ("third_period_total", {"period_scores": [[2, 1], [0, 1], [1, 1]]}, "under", 2.0, None, LegStatus.VOID),
    ("both_teams_to_score", {"home_score": 3, "away_score": 0}, "yes", None, None, LegStatus.LOST),
    ("next_goal", {"events": [{"type": "goal", "team": "away", "t": 1300}, {"type": "goal", "team": "home", "t": 2000}]}, "home",
     None, {"after": 1500}, LegStatus.WON),
    ("player_shots_on_goal", {"player_stats": {"McDavid": {"shots_on_goal": 5}}}, "over", 3.5, {"player": "McDavid"}, LegStatus.WON),
    ("goalie_saves", {"player_stats": {"Shesterkin": {"saves": 28}}}, "over", 29.5, {"player": "Shesterkin"}, LegStatus.LOST),
    ("player_points", {"player_stats": {}, "participants": ["Other"]}, "over", 0.5, {"player": "Scratched"}, LegStatus.VOID),
    ("moneyline_incl_ot", {"home_score": 4, "away_score": 3, "status": "postponed"}, "home", None, None, LegStatus.VOID),
]


@pytest.mark.parametrize("case", SETTLEMENT_CASES, ids=[f"{c[0]}-{c[2]}-{i}" for i, c in enumerate(SETTLEMENT_CASES)])
def test_settlement(adapter: IceHockeySportAdapter, case: tuple) -> None:
    market, result, selection, line, params, expected = case
    assert adapter.settle_market(market, result, selection, line, params) == expected


def test_settlement_errors(adapter: IceHockeySportAdapter) -> None:
    with pytest.raises(SettlementInputError):
        adapter.settle_market("empty_net_goal_yes_no", {"home_score": 3, "away_score": 1}, "yes")
    assert adapter.resolve_market_winner("empty_net_goal_yes_no", {"home_score": 3, "away_score": 1}, "yes") is False
    with pytest.raises(SettlementInputError):
        adapter.settle_market("empty_net_goal_yes_no", {"empty_net_goal_scored": True}, "maybe")
    with pytest.raises(SettlementInputError):
        adapter.settle_market("inning_of_highest_score", {"home_score": 1, "away_score": 0}, "1")
    assert adapter.settle_market("moneyline", {"home_score": 2, "away_score": 2}, "home") == LegStatus.VOID


def test_supported_markets(adapter: IceHockeySportAdapter) -> None:
    markets = adapter.supported_markets
    for key in ("moneyline_incl_ot", "puck_line_handicap", "regulation_3_way", "game_total_goals", "empty_net_goal_yes_no", "empty_net_goal"):
        assert key in markets


# ================================================================ performance
def test_pricing_is_fast(adapter: IceHockeySportAdapter) -> None:
    adapter.calculate_fair_odds(REF_HOME, REF_AWAY)
    timings = []
    for _ in range(10):
        start = time.perf_counter()
        adapter.calculate_fair_odds(REF_HOME, REF_AWAY, {"officials": {"penalties_per_game": 7.4}})
        timings.append(time.perf_counter() - start)
    assert min(timings) < 0.025
