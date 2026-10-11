"""
Esports adapter tests (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
import time

import pytest

from app.domain.sports.bet_catalog import BetType
from app.domain.sports.context import MatchContext
from app.domain.sports.esports_adapter import (
    CS2_ACTIVE_DUTY,
    CS2_CT_SHARE,
    EsportsSportAdapter,
    RoundMap,
    log5,
    moba_series,
    race,
    series_distribution,
)
from app.domain.sports.quant import logistic
from app.domain.sports.settlement import SettlementInputError
from app.schemas.bet_types import LegStatus

STRONG = {"team_rating": 1.15}
WEAK = {"team_rating": 0.95}


@pytest.fixture
def adapter() -> EsportsSportAdapter:
    return EsportsSportAdapter()


# ================================================================ reference test (GROUP_79_PROMPT FILE 21)
def test_reference_map_veto(adapter: EsportsSportAdapter) -> None:
    res = adapter.calculate_fair_odds(home_stats=STRONG, away_stats=WEAK, context={"series_format": "bo3"})
    assert res.home_win_prob > 0.70
    assert 2.0 <= res.expected_total_score <= 3.0
    assert adapter.resolve_market_winner("map_handicap", {"team_a_maps": 2, "team_b_maps": 0}, "team_a", line=-1.5) is True
    assert adapter.resolve_market_winner("pistol_round_winner", {"pistol_winner": "faze"}, "faze") is True


def test_identity_and_class_attributes(adapter: EsportsSportAdapter) -> None:
    assert adapter.sport_key == "esports"
    assert adapter.display_name == "Esports"
    assert adapter.kelly_multiplier == 0.25
    assert adapter.has_draw is False
    assert adapter.odds_api_family == "esports"
    assert adapter.model_family == "HLTV Rating & Map Veto Markov"
    assert adapter.BET_TYPES <= {b.value for b in BetType}
    assert {"map_winner", "total_maps", "correct_score", "point_spread"} <= adapter.BET_TYPES
    assert "teaser" not in adapter.BET_TYPES
    for market in adapter.standard_markets:
        adapter.canonical_market(market)


# ================================================================ the exact Markov chains
def test_race_basics() -> None:
    assert race([1.0] * 13, 13, 13) == (1.0, 0.0, 0.0)
    w, lose, t = race([0.5] * 24, 13, 13)
    assert w == pytest.approx(lose)
    assert t == pytest.approx(math.comb(24, 12) * 0.5**24)
    assert race([0.5], 4, 4, a=4) == (1.0, 0.0, 0.0)


def _closed_form_cs2(q: float) -> float:
    """Constant round probability: P(13-k) sums, the 12-12 tie, then MR3 periods with V = w / (1 - t)."""
    reg = sum(math.comb(12 + k, k) * q**13 * (1 - q) ** k for k in range(12))
    tie = math.comb(24, 12) * q**12 * (1 - q) ** 12
    w = sum(math.comb(3 + k, k) * q**4 * (1 - q) ** k for k in range(3))
    t = math.comb(6, 3) * q**3 * (1 - q) ** 3
    return reg + tie * w / (1 - t)


@pytest.mark.parametrize("q", [0.45, 0.5, 0.55, 0.6])
def test_cs2_map_matches_closed_form(q: float) -> None:
    assert RoundMap("cs2", q, q).win_prob("ct") == pytest.approx(_closed_form_cs2(q), abs=1e-12)


def test_cs2_overtime_geometric_matches_truncated_series() -> None:
    m = RoundMap("cs2", 0.6, 0.45)
    # A started CT: OT period 0 opens on A's second-half side (T), then switches; periods alternate
    even = [0.45] * 3 + [0.6] * 3
    odd = [0.6] * 3 + [0.45] * 3
    we, _, te = race(even, 4, 4)
    wo, _, to = race(odd, 4, 4)
    total, reach = 0.0, 1.0
    for period in range(200):
        w, t = (we, te) if period % 2 == 0 else (wo, to)
        total += reach * w
        reach *= t
    assert m.win_prob("ct", 12, 12) == pytest.approx(total, abs=1e-12)


def test_valorant_overtime_win_by_two() -> None:
    m = RoundMap("valorant", 0.6, 0.45)
    v = 0.6 * 0.45 / (0.6 * 0.45 + 0.4 * 0.55)
    assert m.win_prob("ct", 12, 12) == pytest.approx(v)
    # one up after the first OT round (index 25: A on its second side for that round)
    q2 = m.q(m.side_at(25, "ct"))
    assert m.win_prob("ct", 13, 12) == pytest.approx(q2 + (1 - q2) * v)
    assert m.win_prob("ct", 14, 12) == 1.0
    assert RoundMap("valorant", 0.5, 0.5).win_prob("t") == pytest.approx(0.5)


def test_map_terminal_states_and_live_rounds() -> None:
    m = RoundMap("cs2", 0.55, 0.5)
    assert m.win_prob("ct", 13, 7) == 1.0
    assert m.win_prob("ct", 11, 13) == 0.0
    assert m.win_prob("ct", 12, 0) > m.win_prob("ct", 6, 6) > m.win_prob("ct", 0, 12)
    assert m.side_at(0, "ct") == "ct" and m.side_at(12, "ct") == "t"
    assert m.side_at(24, "ct") == "t" and m.side_at(27, "ct") == "ct" and m.side_at(30, "ct") == "ct"


def test_side_bias_on_maps(adapter: EsportsSportAdapter) -> None:
    a = b = adapter.teams({"team_rating": 1.0}, {"team_rating": 1.0}, adapter_ctx())[0]
    nuke = adapter.round_map("cs2", "nuke", a, b, 1.5)
    anubis = adapter.round_map("cs2", "anubis", a, b, 1.5)
    assert nuke.p_ct == pytest.approx(0.56)
    assert anubis.p_t == pytest.approx(0.54)  # the spec's T bias on Anubis
    assert set(CS2_ACTIVE_DUTY) <= set(CS2_CT_SHARE)
    assert {"vertigo", "overpass"} <= set(CS2_CT_SHARE)


def adapter_ctx() -> MatchContext:
    return MatchContext.parse({})


def test_series_distribution_iid_and_ordered() -> None:
    p = 0.7
    iid = series_distribution([p] * 3, 2)
    assert iid[(2, 0)] + iid[(2, 1)] == pytest.approx(p * p * (3 - 2 * p))
    p1, p2, p3 = 0.8, 0.4, 0.6
    ordered = series_distribution([p1, p2, p3], 2)
    assert ordered[(2, 0)] == pytest.approx(p1 * p2)
    assert ordered[(2, 1)] == pytest.approx(p1 * (1 - p2) * p3 + (1 - p1) * p2 * p3)
    assert sum(ordered.values()) == pytest.approx(1.0)
    bo5 = series_distribution([0.6] * 5, 3)
    assert bo5[(3, 0)] + bo5[(3, 1)] + bo5[(3, 2)] == pytest.approx(0.6**3 * (1 + 3 * 0.4 + 6 * 0.4**2))


def test_moba_side_dp() -> None:
    final, per_game = moba_series(0.6, 0.5, 2, 3, first_chooser="home")
    # game 1: A on blue (0.6); the loser picks blue for game 2
    assert per_game[0] == pytest.approx((1.0, 0.6))
    p_2_0 = 0.6 * 0.5
    assert final[(2, 0)] == pytest.approx(p_2_0)
    assert final[(0, 2)] == pytest.approx(0.4 * (1 - 0.6))
    assert sum(final.values()) == pytest.approx(1.0)


def test_log5() -> None:
    assert log5(0.5, 0.5) == pytest.approx(0.5)
    assert log5(0.7, 0.4) == pytest.approx(0.7 * 0.6 / (0.7 * 0.6 + 0.4 * 0.3))


# ================================================================ pricing
def test_markets_coherent_bo3(adapter: EsportsSportAdapter) -> None:
    res = adapter.calculate_fair_odds(STRONG, WEAK)
    mk = res.market_probabilities
    scores = {k: v for k, v in mk.items() if k.startswith("correct_score_")}
    assert set(scores) == {"correct_score_2-0", "correct_score_2-1", "correct_score_1-2", "correct_score_0-2"}
    assert sum(scores.values()) == pytest.approx(1.0, abs=2e-4)
    assert mk["series_home"] == pytest.approx(res.home_win_prob, abs=2e-4)
    assert mk["map_handicap_home_-1.5"] == pytest.approx(mk["correct_score_2-0"], abs=2e-4)
    assert mk["map_handicap_away_+1.5"] == pytest.approx(1 - mk["correct_score_2-0"], abs=2e-4)
    assert mk["map_handicap_home_+1.5"] == pytest.approx(1 - mk["correct_score_0-2"], abs=2e-4)
    assert mk["total_maps_over_2.5"] == pytest.approx(mk["correct_score_2-1"] + mk["correct_score_1-2"], abs=2e-4)
    assert mk["map_3_played"] == pytest.approx(mk["total_maps_over_2.5"], abs=2e-4)
    assert res.expected_total_score == pytest.approx(2 + mk["total_maps_over_2.5"], abs=0.01)
    assert mk["map_1_home"] + mk["map_1_away"] == pytest.approx(1.0)
    assert 0.5 < mk["pistol_round_home"] < mk["map_1_home"]
    assert "first_blood_home" not in mk  # CS2 prices pistols, not first blood


def test_bo5_and_bo1_lines(adapter: EsportsSportAdapter) -> None:
    bo5 = adapter.calculate_fair_odds(STRONG, WEAK, {"series_format": "bo5"})
    mk = bo5.market_probabilities
    for key in ("map_handicap_home_-2.5", "map_handicap_home_-1.5", "map_handicap_away_+2.5", "total_maps_over_3.5", "total_maps_over_4.5"):
        assert key in mk
    assert mk["map_handicap_home_-2.5"] == pytest.approx(mk["correct_score_3-0"], abs=2e-4)
    assert mk["total_maps_over_4.5"] == pytest.approx(mk["correct_score_3-2"] + mk["correct_score_2-3"], abs=2e-4)
    assert 3.0 <= bo5.expected_total_score <= 5.0
    assert bo5.home_win_prob > adapter.calculate_fair_odds(STRONG, WEAK).home_win_prob  # longer series favour the better team
    bo1 = adapter.calculate_fair_odds(STRONG, WEAK, {"series_format": "bo1"})
    assert bo1.expected_total_score == 1.0
    assert not any(k.startswith("map_handicap") for k in bo1.market_probabilities)
    assert len(bo1.sport_specific_metrics["map_sequence"]) == 1


def test_four_submodels(adapter: EsportsSportAdapter) -> None:
    res = adapter.calculate_fair_odds(STRONG, WEAK)
    assert {s.name for s in res.sub_models} == {"rating_round_markov", "map_veto_simulation", "ranking_elo_logistic", "recent_form"}
    assert sum(s.weight for s in res.sub_models) == pytest.approx(1.0)
    assert res.home_win_prob == pytest.approx(sum(s.home_win_prob * s.weight for s in res.sub_models), abs=1e-4)
    # the rating sub-model is the exact Markov map, iid over the series
    rating = next(s for s in res.sub_models if s.name == "rating_round_markov")
    p_round = logistic(1.5 * 0.20)
    p_map = _closed_form_cs2(p_round)
    assert res.sport_specific_metrics["single_map_prob_team_a"] == pytest.approx(p_map, abs=1e-4)
    assert rating.home_win_prob == pytest.approx(p_map**2 * (3 - 2 * p_map), abs=1e-9)
    assert res.sport_specific_metrics["round_win_prob_team_a"] == pytest.approx(p_round, abs=1e-4)


def test_elo_ranking_and_form_sources(adapter: EsportsSportAdapter) -> None:
    derived = adapter.calculate_fair_odds(STRONG, WEAK).sport_specific_metrics
    assert derived["elo_source"] == "rating" and derived["form_source"] == "regressed_rating"
    elo = adapter.calculate_fair_odds({**STRONG, "elo": 1700}, {**WEAK, "elo": 1500}).sport_specific_metrics
    assert elo["elo_source"] == "elo"
    assert elo["elo_map_prob"] == pytest.approx(1 / (1 + 10 ** (-200 / 400)), abs=1e-4)
    pts = adapter.calculate_fair_odds({**STRONG, "world_ranking_points": 900}, {**WEAK, "world_ranking_points": 300}).sport_specific_metrics
    assert pts["elo_source"] == "world_ranking_points"
    assert pts["elo_map_prob"] == pytest.approx(0.75, abs=1e-4)  # Bradley-Terry: 900 / (900 + 300)
    form = adapter.calculate_fair_odds({**STRONG, "recent_map_win_pct": 0.40}, {**WEAK, "recent_map_win_pct": 0.70})
    assert form.sport_specific_metrics["form_source"] == "recent_map_win_pct"
    assert form.sport_specific_metrics["form_map_prob"] < derived["form_map_prob"]
    # the regressed form model: 0.5 + 0.75 (p - 0.5)
    assert derived["form_map_prob"] == pytest.approx(0.5 + 0.75 * (derived["single_map_prob_team_a"] - 0.5), abs=2e-4)


def test_veto_sequence_rules(adapter: EsportsSportAdapter) -> None:
    home = {"team_rating": 1.05, "map_ratings": {"nuke": 1.30, "mirage": 0.85}}
    away = {"team_rating": 1.00, "map_ratings": {"inferno": 1.25}}
    res = adapter.calculate_fair_odds(home, away)
    veto = res.sport_specific_metrics["veto"]
    assert [(s["team"], s["action"]) for s in veto] == [("home", "ban"), ("away", "ban"), ("home", "pick"), ("away", "pick"),
                                                        ("home", "ban"), ("away", "ban"), ("decider", "decider")]
    assert veto[0]["map"] in ("mirage", "inferno")  # A bans the map with B's largest advantage
    assert veto[1]["map"] == "nuke"  # B bans A's best map
    maps = [s["map"] for s in veto]
    assert len(set(maps)) == 7
    sequence = res.sport_specific_metrics["map_sequence"]
    assert [m["picked_by"] for m in sequence] == ["home", "away", "decider"]
    bo1 = adapter.calculate_fair_odds(home, away, {"series_format": "bo1"}).sport_specific_metrics["veto"]
    assert [s["action"] for s in bo1] == ["ban"] * 6 + ["decider"]
    bo5 = adapter.calculate_fair_odds(home, away, {"series_format": "bo5"}).sport_specific_metrics["veto"]
    assert [s["action"] for s in bo5] == ["ban", "ban", "pick", "pick", "pick", "pick", "decider"]


def test_map_ratings_change_the_veto_model(adapter: EsportsSportAdapter) -> None:
    flat = adapter.calculate_fair_odds({"team_rating": 1.0}, {"team_rating": 1.0})
    specialist = adapter.calculate_fair_odds({"team_rating": 1.0, "map_ratings": dict.fromkeys(CS2_ACTIVE_DUTY, 1.15)}, {"team_rating": 1.0})
    veto_flat = next(s for s in flat.sub_models if s.name == "map_veto_simulation")
    veto_spec = next(s for s in specialist.sub_models if s.name == "map_veto_simulation")
    assert veto_spec.home_win_prob > veto_flat.home_win_prob + 0.2
    assert abs(veto_flat.home_win_prob - 0.5) < 0.02


def test_given_maps_override_veto(adapter: EsportsSportAdapter) -> None:
    res = adapter.calculate_fair_odds(STRONG, WEAK, {"maps": ["nuke", "anubis", "mirage"]})
    assert [m["map"] for m in res.sport_specific_metrics["map_sequence"]] == ["nuke", "anubis", "mirage"]


def test_monotone_in_rating_and_symmetric(adapter: EsportsSportAdapter) -> None:
    probs = [adapter.calculate_fair_odds({"team_rating": r}, {"team_rating": 1.0}).home_win_prob for r in (0.9, 1.0, 1.1, 1.2)]
    assert probs == sorted(probs)
    ab = adapter.calculate_fair_odds(STRONG, WEAK)
    ba = adapter.calculate_fair_odds(WEAK, STRONG)
    assert ab.home_win_prob == pytest.approx(ba.away_win_prob, abs=0.02)
    assert ab.home_win_prob + ab.away_win_prob == pytest.approx(1.0, abs=2e-4)


def test_valorant_and_mobas(adapter: EsportsSportAdapter) -> None:
    val = adapter.calculate_fair_odds(STRONG, WEAK, {"game": "valorant"})
    assert val.sport_specific_metrics["map_sequence"][0]["map"] in ("abyss", "ascent", "bind", "haven", "icebox", "lotus", "sunset")
    assert "pistol_round_home" in val.market_probabilities and "first_blood_home" in val.market_probabilities
    lol = adapter.calculate_fair_odds({"team_rating": 1.0}, {"team_rating": 1.0}, {"game": "lol"})
    # the higher seed takes blue side in game 1 (53.8%): a small edge for equal teams
    assert 0.5 < lol.home_win_prob < 0.53
    assert lol.sport_specific_metrics["side_edge"] == 0.538
    assert lol.sport_specific_metrics["game_win_prob_better_side"] == pytest.approx(0.538, abs=1e-4)
    assert "first_blood_home" in lol.market_probabilities and "pistol_round_home" not in lol.market_probabilities
    dota = adapter.calculate_fair_odds(STRONG, WEAK, {"game": "dota2", "series_format": "bo5"})
    assert dota.sport_specific_metrics["side_edge"] == 0.52
    assert 3.0 <= dota.expected_total_score <= 5.0
    assert sum(v for k, v in dota.market_probabilities.items() if k.startswith("correct_score_")) == pytest.approx(1.0, abs=2e-4)


def test_first_blood_and_pistol_rates(adapter: EsportsSportAdapter) -> None:
    base = adapter.calculate_fair_odds(STRONG, WEAK, {"game": "lol"}).market_probabilities["first_blood_home"]
    rated = adapter.calculate_fair_odds({**STRONG, "first_blood_rate": 0.30}, {**WEAK, "first_blood_rate": 0.70},
                                        {"game": "lol"}).market_probabilities["first_blood_home"]
    assert 0.5 < base and rated < base
    pistol = adapter.calculate_fair_odds({**STRONG, "pistol_win_pct": 0.40}, {**WEAK, "pistol_win_pct": 0.60}).market_probabilities
    assert pistol["pistol_round_home"] < adapter.calculate_fair_odds(STRONG, WEAK).market_probabilities["pistol_round_home"]


def test_context_standins_and_online(adapter: EsportsSportAdapter) -> None:
    base = adapter.calculate_fair_odds(STRONG, WEAK)
    standin = adapter.calculate_fair_odds(STRONG, WEAK, {"absences": [{"side": "home", "player": "s1mple", "status": "out"}]})
    assert standin.home_win_prob < base.home_win_prob
    assert standin.sport_specific_metrics["home_standin_rating_penalty"] == pytest.approx(0.035)
    assert "home_standin_rating_penalty" not in base.sport_specific_metrics
    online = adapter.calculate_fair_odds(STRONG, WEAK, {"online": True})
    assert 0.5 < online.home_win_prob < base.home_win_prob
    assert online.sport_specific_metrics["online_k_scale"] == 0.9


def test_timing(adapter: EsportsSportAdapter) -> None:
    home = {"team_rating": 1.1, "map_ratings": {"nuke": 1.2}, "elo": 1700, "recent_map_win_pct": 0.6}
    start = time.perf_counter()
    for _ in range(10):
        adapter.calculate_fair_odds(home, WEAK, {"series_format": "bo5"})
    assert (time.perf_counter() - start) / 10 < 0.025


# ================================================================ in-play
def test_in_play_start_matches_prematch(adapter: EsportsSportAdapter) -> None:
    pre = adapter.calculate_fair_odds(STRONG, WEAK)
    live = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 0, "maps_b": 0, "current_map_rounds_a": 0, "current_map_rounds_b": 0})
    assert live.home_win_prob == pytest.approx(pre.home_win_prob, abs=2e-3)
    assert live.home_win_prob + live.away_win_prob + live.draw_prob == pytest.approx(1.0)


def test_in_play_finished_states(adapter: EsportsSportAdapter) -> None:
    done = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 0, "maps_b": 2})
    assert (done.home_win_prob, done.away_win_prob) == (0.0, 1.0)
    flagged = adapter.in_play_win_probability(STRONG, WEAK, {"finished": {"winner": "a"}})
    assert flagged.home_win_prob == 1.0


def test_in_play_rounds_and_sides(adapter: EsportsSportAdapter) -> None:
    lead = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "current_map_rounds_a": 12, "current_map_rounds_b": 3,
                                                          "current_side_a": "ct", "map_name": "inferno"})
    trail = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "current_map_rounds_a": 3, "current_map_rounds_b": 12,
                                                           "current_side_a": "ct", "map_name": "inferno"})
    assert lead.home_win_prob > 0.97 and trail.home_win_prob < 0.15
    # the decider is the whole series: the live probability is the exact current-map Markov
    m = adapter.round_map("cs2", "inferno", *adapter.teams(STRONG, WEAK, adapter_ctx())[:2], 1.5)
    assert lead.home_win_prob == pytest.approx(m.win_prob("t", 12, 3), abs=1e-9)  # 15 rounds in: A started on T
    up_map = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 0, "current_map_rounds_a": 0, "current_map_rounds_b": 0})
    down_map = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 0, "maps_b": 1, "current_map_rounds_a": 0, "current_map_rounds_b": 0})
    assert up_map.home_win_prob > down_map.home_win_prob


def test_in_play_moba_gold(adapter: EsportsSportAdapter) -> None:
    ctx = {"game": "lol"}
    ahead = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "gold_diff": 6000, "minute": 25}, ctx)
    even = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "gold_diff": 0, "minute": 25}, ctx)
    behind = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "gold_diff": -6000, "minute": 25}, ctx)
    assert ahead.home_win_prob > even.home_win_prob > behind.home_win_prob
    assert ahead.home_win_prob > 0.8
    early = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "gold_diff": 3000, "minute": 8}, ctx)
    late = adapter.in_play_win_probability(STRONG, WEAK, {"maps_a": 1, "maps_b": 1, "gold_diff": 3000, "minute": 35}, ctx)
    assert early.home_win_prob > late.home_win_prob  # an early lead is worth more per gold
    for r in (ahead, even, behind):
        assert r.home_win_prob + r.away_win_prob + r.draw_prob == pytest.approx(1.0)


# ================================================================ settlement
def test_series_winner_and_correct_score(adapter: EsportsSportAdapter) -> None:
    result = {"team_a_maps": 2, "team_b_maps": 1}
    for market in ("match_winner_series", "series_winner", "moneyline"):
        assert adapter.settle_market(market, result, "team_a") == LegStatus.WON
        assert adapter.settle_market(market, result, "team_b") == LegStatus.LOST
    assert adapter.settle_market("match_winner_series", {"winner": "faze"}, "faze") == LegStatus.WON
    assert adapter.settle_market("correct_series_score", result, "2-1") == LegStatus.WON
    assert adapter.settle_market("correct_score", result, "2-0") == LegStatus.LOST
    assert adapter.settle_market("match_winner_series", {**result, "status": "cancelled"}, "team_a") == LegStatus.VOID


def test_map_handicap(adapter: EsportsSportAdapter) -> None:
    assert adapter.settle_market("map_handicap", {"team_a_maps": 2, "team_b_maps": 1}, "team_a", line=-1.5) == LegStatus.LOST
    assert adapter.settle_market("map_handicap", {"team_a_maps": 2, "team_b_maps": 1}, "team_b", line=1.5) == LegStatus.WON
    assert adapter.settle_market("map_handicap", {"team_a_maps": 2, "team_b_maps": 1}, "team_a", line=-1.0) == LegStatus.VOID
    assert adapter.settle_market("map_handicap", {"team_a_maps": 2, "team_b_maps": 1}, "team_a", line=-1.25) == LegStatus.HALF_LOST
    assert adapter.settle_market("map_handicap", {"team_a_maps": 2, "team_b_maps": 1}, "team_a", line=-0.75) == LegStatus.HALF_WON
    assert adapter.settle_market("map_handicap", {"maps": ["a", "b", "a"]}, "home", line=-1.5) == LegStatus.LOST


def test_total_maps(adapter: EsportsSportAdapter) -> None:
    assert adapter.settle_market("map_total_over_under", {"team_a_maps": 2, "team_b_maps": 1}, "over", line=2.5) == LegStatus.WON
    assert adapter.settle_market("total_maps", {"maps": ["a", "a"]}, "under", line=2.5) == LegStatus.WON
    assert adapter.settle_market("map_total_over_under", {"team_a_maps": 2, "total_maps": 3}, "over", line=2.5) == LegStatus.WON
    assert adapter.settle_market("map_total_over_under", {"team_a_maps": 3, "team_b_maps": 1}, "over", line=4.0) == LegStatus.VOID


def test_map_winners(adapter: EsportsSportAdapter) -> None:
    assert adapter.settle_market("map_1_winner", {"maps": ["b", "a", "a"]}, "team_b") == LegStatus.WON
    assert adapter.settle_market("map_1_winner", {"map_1_winner": "navi"}, "navi") == LegStatus.WON
    assert adapter.settle_market("map_1_winner", {"map_scores": [[13, 9], [7, 13]]}, "home") == LegStatus.WON
    assert adapter.settle_market("map_1_winner", {"periods": {"map_1": [10, 13]}}, "away") == LegStatus.WON
    assert adapter.settle_market("map_winner", {"maps": ["b", "a", "a"]}, "a", params={"map": 3}) == LegStatus.WON
    assert adapter.settle_market("map_winner", {"maps": ["a", "a"], "team_a_maps": 2, "team_b_maps": 0}, "a", params={"map": 3}) == LegStatus.VOID
    named = {"team_a": "Vitality", "team_b": "Spirit", "maps": [{"winner": "Spirit", "score": [9, 13]}, {"score": [13, 4]}]}
    assert adapter.settle_market("map_1_winner", named, "spirit") == LegStatus.WON
    assert adapter.settle_market("map_winner", named, "vitality", params={"map": 2}) == LegStatus.WON
    with pytest.raises(SettlementInputError):
        adapter.settle_market("map_1_winner", {"winner": "a"}, "a")


def test_period_round_markets(adapter: EsportsSportAdapter) -> None:
    result = {"map_scores": [[13, 9], [11, 13]]}
    assert adapter.settle_market("map_round_handicap", result, "home", line=-3.5, params={"period": "map1"}) == LegStatus.WON
    assert adapter.settle_market("map_total_rounds", result, "over", line=21.5, params={"period": "map2"}) == LegStatus.WON
    assert adapter.settle_market("period_moneyline:map2", result, "away") == LegStatus.WON


def test_pistol_and_first_blood(adapter: EsportsSportAdapter) -> None:
    assert adapter.settle_market("pistol_round_winner", {"pistol_winner": "faze"}, "navi") == LegStatus.LOST
    assert adapter.settle_market("pistol_round_winner", {"pistol_winner": "a"}, "team_a") == LegStatus.WON
    assert adapter.settle_market("pistol_round_winner", {"pistol_winners": ["a", "b"]}, "b", params={"pistol": 2}) == LegStatus.WON
    assert adapter.settle_market("pistol_round_winner", {"team_a": "FaZe", "pistol_winner": "a"}, "faze") == LegStatus.WON
    assert adapter.settle_market("first_blood", {"first_blood": "t1"}, "t1") == LegStatus.WON
    assert adapter.settle_market("first_blood", {"first_blood": "blue"}, "t1") == LegStatus.LOST
    assert adapter.settle_market("first_blood", {"first_bloods": ["a", "b"]}, "away", params={"map": 2}) == LegStatus.WON
    with pytest.raises(SettlementInputError):
        adapter.settle_market("first_blood", {"winner": "a"}, "a")


def test_normalize_result(adapter: EsportsSportAdapter) -> None:
    res = adapter.normalize_result({"maps": ["a", "b", "a"]})
    assert (res.home, res.away) == (2.0, 1.0)
    assert res.side_winner() == "home"
    res2 = adapter.normalize_result({"map_scores": [[13, 9], [7, 13], [16, 14]]})
    assert (res2.home, res2.away) == (2.0, 1.0)
    assert res2.periods["map3"] == (16.0, 14.0) and res2.periods["map_3"] == (16.0, 14.0)
    assert adapter.normalize_result({"team_a_maps": 0, "team_b_maps": 2}).side_winner() == "away"
    props = {"team_a_maps": 2, "team_b_maps": 0, "player_stats": {"zywoo": {"kills": 45}}}
    assert adapter.settle_market("player_kills", props, "over", line=39.5, params={"player": "zywoo"}) == LegStatus.WON
