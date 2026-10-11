"""
Combat (MMA / boxing) adapter tests (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
import time
from typing import Any

import pytest

from app.domain.sports.bet_catalog import BetType
from app.domain.sports.combat_adapter import (
    DRAW_GIVEN_DECISION,
    CombatSportAdapter,
    FighterProfile,
    HazardParams,
    classify_method,
    project_fight,
    rounds_won_pmf,
    solve_base_hazard,
)
from app.domain.sports.quant import logistic
from app.domain.sports.settlement import SettlementInputError
from app.schemas.bet_types import LegStatus

REF_A = {"slpm": 5.5, "sapm": 2.8, "td_avg_per_15": 2.5, "td_def_pct": 0.80, "finish_rate": 0.70}
REF_B = {"slpm": 3.2, "sapm": 4.1, "td_avg_per_15": 0.5, "td_def_pct": 0.50, "finish_rate": 0.40}
EVEN = {"slpm": 4.0, "sapm": 3.6, "td_avg_per_15": 1.5, "td_def_pct": 0.65, "finish_rate": 0.55}


@pytest.fixture
def adapter() -> CombatSportAdapter:
    return CombatSportAdapter()


def _sum3(res: Any) -> float:
    return res.home_win_prob + res.away_win_prob + res.draw_prob


# ================================================================ reference test (GROUP_79_PROMPT FILE 21)
def test_reference_strike_differential(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=REF_A, away_stats=REF_B, context={"rounds": 5})
    assert res.home_win_prob > 0.70
    assert "distance_probability" in res.sport_specific_metrics
    assert adapter.resolve_market_winner("method_of_victory", {"method": "ko_tko"}, "ko_tko") is True
    assert adapter.resolve_market_winner("fight_goes_the_distance", {"went_distance": False}, "no") is True


def test_identity_and_class_attributes(adapter: CombatSportAdapter) -> None:
    assert adapter.sport_key == "combat"
    assert adapter.display_name == "MMA / Combat Sports"
    assert adapter.kelly_multiplier == 0.20
    assert adapter.has_draw is False
    assert adapter.odds_api_family == "mma"
    assert adapter.model_family == "Strike Differential & Finish Decay"
    universal = {b.value for b in BetType}
    assert adapter.BET_TYPES <= universal
    assert {"method_of_victory", "round_betting", "moneyline"} <= adapter.BET_TYPES
    assert "teaser" not in adapter.BET_TYPES and "pleaser" not in adapter.BET_TYPES
    for market in adapter.standard_markets:
        adapter.canonical_market(market)


# ================================================================ formulas against hand-computed values
def test_matchup_spec_quantities(adapter: CombatSportAdapter) -> None:
    m = adapter.matchup(FighterProfile.parse(REF_A), FighterProfile.parse(REF_B))
    assert m.strike_diff_a == pytest.approx(5.5 - 2.8)
    assert m.strike_diff_b == pytest.approx(3.2 - 4.1)
    assert m.land_a == pytest.approx((5.5 + 4.1) / 2)
    assert m.land_b == pytest.approx((3.2 + 2.8) / 2)
    # no accuracy data: the projected differential is half the gap of the per-fighter StrikeDiffs
    assert m.projected_diff == pytest.approx((m.strike_diff_a - m.strike_diff_b) / 2)
    assert m.td_a == pytest.approx(2.5 * (1 - 0.50))
    assert m.td_b == pytest.approx(0.5 * (1 - 0.80))


def test_accuracy_and_takedown_accuracy_factors(adapter: CombatSportAdapter) -> None:
    a = FighterProfile.parse({**REF_A, "str_acc": 0.50, "td_acc": 0.60})
    b = FighterProfile.parse({**REF_B, "str_def": 0.60})
    m = adapter.matchup(a, b)
    f_a = math.sqrt((0.50 / 0.45) * ((1 - 0.60) / 0.45))
    assert m.land_a == pytest.approx((5.5 + 4.1) / 2 * f_a)
    assert m.td_a == pytest.approx(2.5 * 0.5 * 0.60 / 0.40)
    # percentages are accepted too
    assert FighterProfile.parse({"str_acc": 50}).str_acc == pytest.approx(0.50)


def test_strike_submodel_is_the_spec_logistic(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_A, REF_B, {"rounds": 5})
    strike = next(s for s in res.sub_models if s.name == "strike_differential_logistic")
    expected = logistic(0.84 * 1.8 + 0.30 * (1.25 - 0.10))
    assert strike.home_win_prob / (strike.home_win_prob + strike.away_win_prob) == pytest.approx(expected, abs=1e-9)


def test_solve_base_hazard_matches_target() -> None:
    for threat in (0.1, 0.35, 0.8):
        for gamma in (0.0, 0.25, 0.6):
            h0 = solve_base_hazard(threat, gamma)
            survival = math.prod(1 - h0 * math.exp(-gamma * r) for r in range(3))
            assert 1 - survival == pytest.approx(threat, abs=1e-9)
    assert solve_base_hazard(0.3, 0.0) == pytest.approx(1 - 0.7 ** (1 / 3), abs=1e-9)
    assert solve_base_hazard(0.0, 0.25) == 0.0


def test_rounds_won_pmf_is_binomial() -> None:
    pmf = rounds_won_pmf([0.6, 0.6, 0.6])
    for k in range(4):
        assert pmf[k] == pytest.approx(math.comb(3, k) * 0.6**k * 0.4 ** (3 - k))


def test_project_fight_hand_computed() -> None:
    hz = HazardParams(h0_a=0.2, h0_b=0.1, ko_share_a=0.75, ko_share_b=0.5, threat_a=0.0, threat_b=0.0)
    dist = project_fight(hz, gamma=0.0, rounds=3, p_round=0.5)
    h = 1 - 0.8 * 0.9
    assert dist.ko_a[0] == pytest.approx(h * (2 / 3) * 0.75)
    assert dist.sub_b[1] == pytest.approx((1 - h) * h * (1 / 3) * 0.5)
    survive = (1 - h) ** 3
    assert dist.dec_a == pytest.approx(survive * 0.5 * (1 - DRAW_GIVEN_DECISION))
    assert dist.draw == pytest.approx(survive * DRAW_GIVEN_DECISION)
    assert dist.total() == pytest.approx(1.0)
    assert dist.distance == pytest.approx(survive)
    # the spec: P(decision) = 1 - sum of per-round finish probabilities
    assert dist.distance == pytest.approx(1 - dist.finish_by_round.sum())


def test_decay_shapes_finish_rounds() -> None:
    hz = HazardParams(0.3, 0.2, 0.7, 0.7, 0.0, 0.0)
    flat = project_fight(hz, gamma=0.0, rounds=3, p_round=0.5)
    decaying = project_fight(hz, gamma=0.5, rounds=3, p_round=0.5)
    assert decaying.finish_by_round[0] == pytest.approx(flat.finish_by_round[0])
    assert decaying.finish_by_round[2] < flat.finish_by_round[2]
    assert decaying.distance > flat.distance


def test_classify_method() -> None:
    assert classify_method("KO/TKO") == "ko_tko"
    assert classify_method("TKO (punches)") == "ko_tko"
    assert classify_method("DQ") == "ko_tko"  # documented: a disqualification settles as KO/TKO
    assert classify_method("Technical Submission") == "submission"
    assert classify_method("split_decision") == "decision"
    assert classify_method("Unanimous") == "decision"
    assert classify_method("Majority Draw") == "draw"
    assert classify_method("No Contest") == "void"
    assert classify_method("") is None


# ================================================================ distribution, markets, metrics
def test_probabilities_and_market_coherence(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_A, REF_B, {"rounds": 5})
    mk, mt = res.market_probabilities, res.sport_specific_metrics
    assert _sum3(res) == pytest.approx(1.0, abs=2e-4)
    assert mk["moneyline_home"] + mk["moneyline_away"] == pytest.approx(1.0, abs=2e-4)
    methods = [mk[f"method_{s}_{m}"] for s in ("home", "away") for m in ("ko_tko", "submission", "decision")] + [mk["method_draw"]]
    assert sum(methods) == pytest.approx(1.0, abs=1e-3)
    rounds = [mk[f"round_{s}_r{r}"] for s in ("home", "away") for r in range(1, 6)] + [mk["round_home_decision"], mk["round_away_decision"], mk["draw"]]
    assert sum(rounds) == pytest.approx(1.0, abs=1e-3)
    assert mk["goes_distance_yes"] + mk["goes_distance_no"] == pytest.approx(1.0, abs=2e-4)
    assert mk["goes_distance_yes"] == pytest.approx(mt["distance_probability"], abs=1e-4)
    home_total = sum(mk[f"method_home_{m}"] for m in ("ko_tko", "submission", "decision"))
    assert home_total == pytest.approx(res.home_win_prob, abs=1e-3)
    assert mk["home_inside_distance"] == pytest.approx(mk["method_home_ko_tko"] + mk["method_home_submission"], abs=2e-4)
    assert mk["method_ko_tko"] == pytest.approx(mk["method_home_ko_tko"] + mk["method_away_ko_tko"], abs=2e-4)
    assert 0.01 > res.draw_prob > 0.0
    assert res.fair_draw_odds is not None


def test_round_totals_use_uniform_timing(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_A, REF_B, {"rounds": 5})
    f = res.sport_specific_metrics["finish_by_round"]
    mk = res.market_probabilities
    assert mk["rounds_under_0.5"] == pytest.approx(0.5 * f[0], abs=2e-4)
    # "over 1.5 rounds": the fight lasts past 2:30 of round 2
    assert mk["rounds_over_1.5"] == pytest.approx(1 - (f[0] + 0.5 * f[1]), abs=2e-4)
    assert mk["rounds_over_4.5"] == pytest.approx(1 - (sum(f[:4]) + 0.5 * f[4]), abs=2e-4)
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        assert mk[f"rounds_over_{line:g}"] + mk[f"rounds_under_{line:g}"] == pytest.approx(1.0, abs=2e-4)
    assert "rounds_over_5.5" not in mk


def test_expected_duration_minutes(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_A, REF_B, {"rounds": 5})
    f = res.sport_specific_metrics["finish_by_round"]
    distance = res.sport_specific_metrics["distance_probability"]
    expected = 5.0 * (sum(p * (r + 0.5) for r, p in enumerate(f)) + distance * 5)
    assert res.expected_total_score == pytest.approx(expected, abs=0.02)
    assert 0 < res.expected_total_score < 25.0


def test_league_average_fight_is_realistic(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(EVEN, EVEN)
    assert res.home_win_prob == pytest.approx(res.away_win_prob, abs=1e-4)
    distance = res.sport_specific_metrics["distance_probability"]
    assert 0.40 < distance < 0.65  # roughly half of UFC bouts go the distance
    f = res.sport_specific_metrics["finish_by_round"]
    assert f[0] > f[1] > f[2]
    assert 0.010 <= res.draw_prob <= 0.016


def test_at_least_three_submodels_with_weights(adapter: CombatSportAdapter) -> None:
    res = adapter.calculate_fair_odds(REF_A, REF_B)
    names = {s.name for s in res.sub_models}
    assert names == {"strike_differential_logistic", "grappling_matrix", "finish_hazard"}
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    with_elo = adapter.calculate_fair_odds({**REF_A, "elo": 1700}, {**REF_B, "elo": 1500})
    assert {s.name for s in with_elo.sub_models} == names | {"elo"}
    assert sum(s.weight for s in with_elo.sub_models) == pytest.approx(1.0)
    weighted = sum(s.home_win_prob * s.weight for s in res.sub_models)
    assert res.home_win_prob == pytest.approx(weighted, abs=1e-4)


def test_model_weights_override(adapter: CombatSportAdapter) -> None:
    base = adapter.calculate_fair_odds(REF_A, REF_B)
    only_hazard = adapter.calculate_fair_odds(REF_A, REF_B, {"model_weights": {"strike_differential_logistic": 0.0001, "grappling_matrix": 0.0001,
                                                                               "finish_hazard": 10.0}})
    hazard = next(s for s in base.sub_models if s.name == "finish_hazard")
    assert only_hazard.home_win_prob == pytest.approx(hazard.home_win_prob, abs=1e-3)


def test_timing(adapter: CombatSportAdapter) -> None:
    start = time.perf_counter()
    for _ in range(20):
        adapter.calculate_fair_odds({**REF_A, "elo": 1600}, REF_B, {"rounds": 5, "weight_class": "heavyweight",
                                                                    "officials": {"judge_variance": 0.4}})
    assert (time.perf_counter() - start) / 20 < 0.025


# ================================================================ monotonicity and symmetry
def test_better_striking_raises_win_probability(adapter: CombatSportAdapter) -> None:
    probs = [adapter.calculate_fair_odds({**EVEN, "slpm": slpm}, EVEN).home_win_prob for slpm in (3.0, 4.0, 5.0, 6.0)]
    assert probs == sorted(probs)
    absorbs_less = adapter.calculate_fair_odds({**EVEN, "sapm": 2.5}, EVEN).home_win_prob
    assert absorbs_less > adapter.calculate_fair_odds(EVEN, EVEN).home_win_prob


def test_takedown_defence_and_wrestling(adapter: CombatSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN, EVEN).home_win_prob
    wrestler = adapter.calculate_fair_odds({**EVEN, "td_avg_per_15": 4.0}, EVEN)
    assert wrestler.home_win_prob > base
    stuffed = adapter.calculate_fair_odds({**EVEN, "td_avg_per_15": 4.0}, {**EVEN, "td_def_pct": 0.95})
    assert stuffed.home_win_prob < wrestler.home_win_prob


def test_symmetry_when_corners_swap(adapter: CombatSportAdapter) -> None:
    ab = adapter.calculate_fair_odds(REF_A, REF_B)
    ba = adapter.calculate_fair_odds(REF_B, REF_A)
    assert ab.home_win_prob == pytest.approx(ba.away_win_prob, abs=2e-4)
    assert ab.market_probabilities["method_home_ko_tko"] == pytest.approx(ba.market_probabilities["method_away_ko_tko"], abs=2e-4)


def test_finish_rate_and_rounds_move_distance(adapter: CombatSportAdapter) -> None:
    low = adapter.calculate_fair_odds({**EVEN, "finish_rate": 0.3}, {**EVEN, "finish_rate": 0.3})
    high = adapter.calculate_fair_odds({**EVEN, "finish_rate": 0.9}, {**EVEN, "finish_rate": 0.9})
    assert high.sport_specific_metrics["distance_probability"] < low.sport_specific_metrics["distance_probability"]
    three = adapter.calculate_fair_odds(EVEN, EVEN, {"rounds": 3})
    five = adapter.calculate_fair_odds(EVEN, EVEN, {"rounds": 5})
    assert five.sport_specific_metrics["distance_probability"] < three.sport_specific_metrics["distance_probability"]
    assert five.expected_total_score > three.expected_total_score


def test_ko_and_sub_splits_and_durability(adapter: CombatSportAdapter) -> None:
    striker = adapter.calculate_fair_odds({**EVEN, "ko_rate": 0.6, "sub_rate": 0.1}, EVEN)
    grappler = adapter.calculate_fair_odds({**EVEN, "ko_rate": 0.1, "sub_rate": 0.6}, EVEN)
    assert striker.market_probabilities["method_home_ko_tko"] > grappler.market_probabilities["method_home_ko_tko"]
    assert grappler.market_probabilities["method_home_submission"] > striker.market_probabilities["method_home_submission"]
    glass = adapter.calculate_fair_odds(EVEN, {**EVEN, "ko_absorbed_rate": 0.30})
    iron = adapter.calculate_fair_odds(EVEN, {**EVEN, "chin": 0.95})
    base = adapter.calculate_fair_odds(EVEN, EVEN)
    assert glass.market_probabilities["method_home_ko_tko"] > base.market_probabilities["method_home_ko_tko"]
    assert iron.market_probabilities["method_home_ko_tko"] < base.market_probabilities["method_home_ko_tko"]


def test_reach_and_age_are_small_effects(adapter: CombatSportAdapter) -> None:
    base = adapter.calculate_fair_odds(EVEN, EVEN).home_win_prob
    reach = adapter.calculate_fair_odds({**EVEN, "reach_cm": 200}, {**EVEN, "reach_cm": 185}).home_win_prob
    old = adapter.calculate_fair_odds({**EVEN, "age": 39}, {**EVEN, "age": 29}).home_win_prob
    assert base < reach < base + 0.05
    assert base - 0.10 < old < base


# ================================================================ context
def test_weight_class_moves_finishes(adapter: CombatSportAdapter) -> None:
    heavy = adapter.calculate_fair_odds(EVEN, EVEN, {"weight_class": "Heavyweight"})
    fly = adapter.calculate_fair_odds(EVEN, EVEN, {"weight_class": "flyweight"})
    womens = adapter.calculate_fair_odds(EVEN, EVEN, {"weight_class": "womens_flyweight"})
    assert heavy.sport_specific_metrics["distance_probability"] < fly.sport_specific_metrics["distance_probability"]
    assert heavy.market_probabilities["method_ko_tko"] > fly.market_probabilities["method_ko_tko"]
    assert womens.market_probabilities["method_ko_tko"] < fly.market_probabilities["method_ko_tko"]
    assert heavy.sport_specific_metrics["weight_class"] == "heavyweight"
    assert heavy.sport_specific_metrics["weight_class_ko_multiplier"] == 1.45
    assert "weight_class" not in adapter.calculate_fair_odds(EVEN, EVEN).sport_specific_metrics


def test_judge_variance_shrinks_decision_and_raises_draw(adapter: CombatSportAdapter) -> None:
    base = adapter.calculate_fair_odds(REF_A, REF_B)
    noisy = adapter.calculate_fair_odds(REF_A, REF_B, {"officials": {"judge_variance": 1.0}})
    assert "judge_variance" not in base.sport_specific_metrics
    assert noisy.sport_specific_metrics["judge_variance"] == 1.0
    assert noisy.draw_prob > base.draw_prob

    def dec_share(r: Any) -> float:
        mk = r.market_probabilities
        return mk["method_home_decision"] / (mk["method_home_decision"] + mk["method_away_decision"])

    assert 0.5 < dec_share(noisy) < dec_share(base)
    split = adapter.calculate_fair_odds(REF_A, REF_B, {"officials": {"split_decision_rate": 0.325}})
    assert split.sport_specific_metrics["judge_variance"] == pytest.approx(0.5)
    league = adapter.calculate_fair_odds(REF_A, REF_B, {"officials": {"split_decision_rate": 0.15}})
    assert league.draw_prob == pytest.approx(base.draw_prob, abs=1e-4)


def test_weather_and_absences_do_not_apply(adapter: CombatSportAdapter) -> None:
    base = adapter.calculate_fair_odds(REF_A, REF_B)
    ctx = {"weather": {"temperature_c": 35, "wind_kph": 40}, "absences": [{"side": "home", "player": "coach", "war": 2.0}]}
    other = adapter.calculate_fair_odds(REF_A, REF_B, ctx)
    assert other.home_win_prob == base.home_win_prob


def test_boxing_round_length(adapter: CombatSportAdapter) -> None:
    boxing = adapter.calculate_fair_odds(EVEN, EVEN, {"rounds": 12, "round_minutes": 3})
    assert boxing.sport_specific_metrics["scheduled_rounds"] == 12
    assert len(boxing.sport_specific_metrics["finish_by_round"]) == 12
    assert "round_home_r12" in boxing.market_probabilities
    assert boxing.expected_total_score <= 36.0
    assert _sum3(boxing) == pytest.approx(1.0, abs=2e-4)


# ================================================================ in-play
def test_in_play_start_matches_prefight(adapter: CombatSportAdapter) -> None:
    pre = adapter.calculate_fair_odds(REF_A, REF_B, {"rounds": 5})
    live = adapter.in_play_win_probability(REF_A, REF_B, {"round": 1, "seconds_elapsed_in_round": 0}, {"rounds": 5})
    assert live.home_win_prob == pytest.approx(pre.home_win_prob, abs=2e-3)
    assert live.home_win_prob + live.away_win_prob + live.draw_prob == pytest.approx(1.0)


def test_in_play_finished_and_final_bell(adapter: CombatSportAdapter) -> None:
    done = adapter.in_play_win_probability(REF_A, REF_B, {"finished": {"winner": "b"}})
    assert (done.home_win_prob, done.away_win_prob, done.draw_prob) == (0.0, 1.0, 0.0)
    drawn = adapter.in_play_win_probability(REF_A, REF_B, {"finished": {"winner": "draw"}})
    assert drawn.draw_prob == 1.0
    bell = adapter.in_play_win_probability(REF_A, REF_B, {"round": 3, "seconds_elapsed_in_round": 300, "scorecard_rounds_a": 2,
                                                          "scorecard_rounds_b": 1})
    assert bell.away_win_prob == pytest.approx(0.0, abs=1e-9)
    assert bell.home_win_prob > 0.95
    assert bell.home_win_prob + bell.draw_prob == pytest.approx(1.0)


def test_in_play_scorecards_and_knockdowns(adapter: CombatSportAdapter) -> None:
    ahead = adapter.in_play_win_probability(EVEN, EVEN, {"round": 3, "seconds_elapsed_in_round": 60, "scorecard_rounds_a": 2,
                                                         "scorecard_rounds_b": 0})
    behind = adapter.in_play_win_probability(EVEN, EVEN, {"round": 3, "seconds_elapsed_in_round": 60, "scorecard_rounds_a": 0,
                                                          "scorecard_rounds_b": 2})
    assert ahead.home_win_prob > 0.75
    assert behind.home_win_prob < 0.25
    hurt = adapter.in_play_win_probability(EVEN, EVEN, {"round": 1, "seconds_elapsed_in_round": 120, "knockdowns_b": 2})
    calm = adapter.in_play_win_probability(EVEN, EVEN, {"round": 1, "seconds_elapsed_in_round": 120})
    assert hurt.away_win_prob > calm.away_win_prob
    outlanded = adapter.in_play_win_probability(EVEN, EVEN, {"round": 2, "seconds_elapsed_in_round": 0, "sig_strikes_a": 10, "sig_strikes_b": 50})
    assert outlanded.away_win_prob > calm.away_win_prob
    for r in (ahead, behind, hurt, calm, outlanded):
        assert r.home_win_prob + r.away_win_prob + r.draw_prob == pytest.approx(1.0)


def test_in_play_time_running_out_favours_decision_leader(adapter: CombatSportAdapter) -> None:
    early = adapter.in_play_win_probability(EVEN, EVEN, {"round": 3, "seconds_elapsed_in_round": 10, "scorecard_rounds_a": 2, "scorecard_rounds_b": 0})
    late = adapter.in_play_win_probability(EVEN, EVEN, {"round": 3, "seconds_elapsed_in_round": 290, "scorecard_rounds_a": 2, "scorecard_rounds_b": 0})
    assert late.home_win_prob > early.home_win_prob


# ================================================================ settlement
def test_moneyline_aliases(adapter: CombatSportAdapter) -> None:
    for market in ("moneyline", "match_winner", "fight_winner"):
        assert adapter.settle_market(market, {"winner": "a"}, "home") == LegStatus.WON
        assert adapter.settle_market(market, {"winner": "a"}, "away") == LegStatus.LOST
    assert adapter.settle_market("moneyline", {"winner": "jones"}, "jones") == LegStatus.WON
    assert adapter.settle_market("moneyline", {"winner": "jones", "fighter_a": "Jones", "fighter_b": "Aspinall"}, "home") == LegStatus.WON
    assert adapter.settle_market("moneyline", {"winner": "red"}, "a") == LegStatus.WON
    assert adapter.settle_market("moneyline", {"winner": "draw"}, "home") == LegStatus.VOID
    assert adapter.settle_market("moneyline", {"method": "majority draw"}, "away") == LegStatus.VOID
    assert adapter.settle_market("moneyline", {"winner": "a", "method": "no contest"}, "home") == LegStatus.VOID
    assert adapter.settle_market("moneyline", {"winner": "a", "status": "cancelled"}, "home") == LegStatus.VOID


def test_method_of_victory(adapter: CombatSportAdapter) -> None:
    ko_a = {"winner": "a", "method": "KO/TKO", "round": 2}
    assert adapter.settle_market("method_of_victory", ko_a, "ko_tko") == LegStatus.WON
    assert adapter.settle_market("method_of_victory", ko_a, "home_ko_tko") == LegStatus.WON
    assert adapter.settle_market("method_of_victory", ko_a, "away_ko_tko") == LegStatus.LOST
    assert adapter.settle_market("method_of_victory", ko_a, "a:submission") == LegStatus.LOST
    assert adapter.settle_market("method_of_victory", ko_a, "home_inside_distance") == LegStatus.WON
    sub_b = {"winner": "b", "method": "Technical Submission", "round": 1}
    assert adapter.settle_market("method_of_victory", sub_b, "b:submission") == LegStatus.WON
    assert adapter.settle_market("method_of_victory", sub_b, "submission") == LegStatus.WON
    dec_b = {"winner": "b", "method": "split_decision"}
    assert adapter.settle_market("method_of_victory", dec_b, "b_decision") == LegStatus.WON
    assert adapter.settle_market("method_of_victory", dec_b, "home_decision") == LegStatus.LOST
    assert adapter.settle_market("method", {"winner": "a", "method": "DQ"}, "home_ko_tko") == LegStatus.WON
    named = {"winner": "jones", "fighter_a": "Jon Jones", "fighter_b": "Stipe", "method": "ko"}
    assert adapter.settle_market("method_of_victory", {**named, "winner": "Jon Jones"}, "jon_jones_ko_tko") == LegStatus.WON
    draw = {"winner": "draw", "method": "split draw"}
    assert adapter.settle_market("method_of_victory", draw, "draw") == LegStatus.WON
    assert adapter.settle_market("method_of_victory", draw, "home_decision") == LegStatus.LOST
    with pytest.raises(SettlementInputError):
        adapter.settle_market("method_of_victory", {"winner": "a"}, "home_ko_tko")
    assert adapter.resolve_market_winner("method_of_victory", {"winner": "a"}, "ko_tko") is False


def test_round_betting(adapter: CombatSportAdapter) -> None:
    r2 = {"winner": "a", "method": "ko_tko", "round": 2, "time": "3:12"}
    assert adapter.settle_market("round_betting", r2, "home_r2") == LegStatus.WON
    assert adapter.settle_market("round_betting", r2, "a:round_2") == LegStatus.WON
    assert adapter.settle_market("round_betting", r2, "a:round_3") == LegStatus.LOST
    assert adapter.settle_market("round_betting", r2, "away_r2") == LegStatus.LOST
    assert adapter.settle_market("round_betting", r2, "home_ko_r2") == LegStatus.WON
    assert adapter.settle_market("round_betting", r2, "home_sub_r2") == LegStatus.LOST
    assert adapter.settle_market("round_betting", r2, "decision") == LegStatus.LOST
    dec = {"winner": "b", "method": "unanimous decision"}
    assert adapter.settle_market("round_betting", dec, "decision") == LegStatus.WON
    assert adapter.settle_market("round_betting", dec, "away_decision") == LegStatus.WON
    assert adapter.settle_market("round_betting", dec, "home_decision") == LegStatus.LOST
    assert adapter.settle_market("round_betting", dec, "b:round_3") == LegStatus.LOST
    assert adapter.settle_market("round_betting", {"winner": "draw"}, "draw") == LegStatus.WON
    with pytest.raises(SettlementInputError):
        adapter.settle_market("round_betting", r2, "home")


def test_goes_the_distance(adapter: CombatSportAdapter) -> None:
    assert adapter.settle_market("fight_goes_the_distance", {"went_distance": True}, "yes") == LegStatus.WON
    assert adapter.settle_market("goes_the_distance", {"went_distance": True}, "no") == LegStatus.LOST
    assert adapter.settle_market("fight_goes_the_distance", {"method": "decision"}, "yes") == LegStatus.WON
    assert adapter.settle_market("fight_goes_the_distance", {"method": "submission"}, "no") == LegStatus.WON
    assert adapter.settle_market("fight_goes_the_distance", {"rounds_completed": 3, "scheduled_rounds": 3}, "yes") == LegStatus.WON
    with pytest.raises(SettlementInputError):
        adapter.settle_market("fight_goes_the_distance", {"winner": "a"}, "yes")


def test_round_totals(adapter: CombatSportAdapter) -> None:
    assert adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.7}, "over", line=2.5) == LegStatus.WON
    assert adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.7}, "under", line=2.5) == LegStatus.LOST
    assert adapter.resolve_market_winner("round_totals_over_under", {"rounds_completed": 1.2}, "under", line=1.5) is True
    # round + time: round 2 at 2:30 is exactly 1.5 rounds elapsed -> a push on 1.5
    assert adapter.settle_market("total_rounds", {"round": 2, "time": "2:30"}, "over", line=1.5) == LegStatus.VOID
    assert adapter.settle_market("total_rounds", {"round": 2, "time": "2:31"}, "over", line=1.5) == LegStatus.WON
    assert adapter.settle_market("round_totals", {"method": "decision", "scheduled_rounds": 3}, "over", line=2.5) == LegStatus.WON
    # quarter lines split
    assert adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.0}, "over", line=2.25) == LegStatus.HALF_LOST
    assert adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.5}, "over", line=2.25) == LegStatus.HALF_WON
    assert adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.0}, "over", line=2.0) == LegStatus.VOID
    with pytest.raises(SettlementInputError):
        adapter.settle_market("round_totals_over_under", {"rounds_completed": 2.0}, "over")
    with pytest.raises(SettlementInputError):
        adapter.settle_market("round_totals_over_under", {"winner": "a"}, "over", line=1.5)


def test_draw_market_and_props(adapter: CombatSportAdapter) -> None:
    assert adapter.settle_market("draw", {"winner": "draw"}, "yes") == LegStatus.WON
    assert adapter.settle_market("draw", {"method": "majority draw"}, "draw") == LegStatus.WON
    assert adapter.settle_market("fight_draw", {"winner": "a", "method": "ko"}, "yes") == LegStatus.LOST
    assert adapter.settle_market("fight_draw", {"winner": "a", "method": "ko"}, "no") == LegStatus.WON
    stats = {"winner": "a", "player_stats": {"jones": {"sig_strikes": 88}}}
    assert adapter.settle_market("fighter_significant_strikes", stats, "over", line=75.5, params={"player": "jones"}) == LegStatus.WON


def test_normalize_result_extras(adapter: CombatSportAdapter) -> None:
    res = adapter.normalize_result({"winner": "Blue", "method": "Submission", "round": 3, "time": "1:30", "scheduled_rounds": 5})
    assert res.winner == "away"
    assert res.extras["method"] == "submission"
    assert res.extras["elapsed_rounds"] == pytest.approx(2.3)
    assert res.extras["went_distance"] is False
    assert adapter.normalize_result({"method": "No Contest"}).void is True
