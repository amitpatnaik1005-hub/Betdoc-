"""
Cricket adapter tests (Group 79): DLS standard edition, the ball-by-ball Markov chain, evening dew, markets,
in-play and settlement.
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
import statistics
import time
from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from app.domain.sports.bet_catalog import BetType, markets_for
from app.domain.sports.cricket_adapter import (
    BALL_BASE,
    DEW_SPIN_WICKET_DROP,
    EXTRAS_RATE,
    CricketSportAdapter,
    InningsConditions,
    ball_matrix,
    balls_from_overs,
    chase_probabilities,
    extras_kernel,
    line_outcome,
    pitch_mixture,
    prob_exceeds,
    simulate_limited_innings,
    simulate_test_innings,
)
from app.domain.sports.quant import IntPMF
from app.domain.sports.settlement import SettlementInputError
from app.schemas.bet_types import LegStatus

ADAPTER = CricketSportAdapter()
R0 = {0: 100.0, 1: 93.4, 2: 85.1, 3: 74.9, 4: 62.7, 5: 49.0, 6: 34.9, 7: 22.0, 8: 11.9, 9: 4.7}
B = {0: 0.0350, 1: 0.0360, 2: 0.0375, 3: 0.0400, 4: 0.0430, 5: 0.0465, 6: 0.0520, 7: 0.0600, 8: 0.0750, 9: 0.1000}


def hand_resource(u: float, w: int) -> float:
    """The spec's curve normalised so R(50, w) = R0(w), written out independently of the adapter."""
    return R0[w] * (1.0 - math.exp(-B[w] * u)) / (1.0 - math.exp(-B[w] * 50.0))


def neutral(fmt: str, **kw: Any) -> InningsConditions:
    return InningsConditions(fmt, extras_rate=EXTRAS_RATE[fmt], **kw)


def chase_prob(res: Any, chasing: str) -> float:
    return res.home_win_prob if chasing == "home" else res.away_win_prob


# ================================================================ identity
def test_identity_and_class_attributes() -> None:
    assert ADAPTER.sport_key == "cricket"
    assert ADAPTER.display_name == "Cricket"
    assert ADAPTER.odds_api_family == "cricket"
    assert ADAPTER.kelly_multiplier == 0.25
    assert ADAPTER.has_draw is False
    assert ADAPTER.model_family == "Ball-by-ball Markov & DLS"
    assert "match_winner" in ADAPTER.standard_markets
    assert "teaser" not in ADAPTER.BET_TYPES and "pleaser" not in ADAPTER.BET_TYPES


def test_every_catalogue_market_for_offered_bet_types_is_settleable() -> None:
    supported = set(ADAPTER.supported_markets)
    for bet_type in ADAPTER.BET_TYPES:
        for market in markets_for(BetType(bet_type), "cricket"):
            assert market in supported, (bet_type, market)


# ================================================================ DLS
def test_dls_reference_values() -> None:
    assert ADAPTER.calculate_dls_resource(50.0, 0) == pytest.approx(100.0)
    r_20_w5 = ADAPTER.calculate_dls_resource(20.0, 5)
    assert 25.0 <= r_20_w5 <= 35.0
    assert r_20_w5 == pytest.approx(hand_resource(20.0, 5), abs=1e-9)
    assert r_20_w5 == pytest.approx(49.0 * (1 - math.exp(-0.93)) / (1 - math.exp(-2.325)), abs=1e-9)


def test_dls_full_innings_matches_r0_column() -> None:
    for w in range(10):
        assert ADAPTER.calculate_dls_resource(50.0, w) == pytest.approx(min(R0[k] for k in range(w + 1)), abs=1e-9)
    assert ADAPTER.calculate_dls_resource(30.0, 10) == 0.0
    assert ADAPTER.calculate_dls_resource(0.0, 0) == 0.0


def test_dls_monotone_in_wickets_and_overs_and_capped() -> None:
    for u in np.arange(0.0, 50.5, 0.5):
        values = [ADAPTER.calculate_dls_resource(float(u), w) for w in range(11)]
        assert all(a >= b - 1e-12 for a, b in pairwise(values))
    for w in range(10):
        values = [ADAPTER.calculate_dls_resource(float(u), w) for u in range(51)]
        assert all(a <= b + 1e-12 for a, b in pairwise(values))
    assert ADAPTER.calculate_dls_resource(80.0, 0) == pytest.approx(100.0)
    assert ADAPTER.calculate_dls_resource(-5.0, 0) == 0.0


def test_dls_t20_rescales_to_twenty_overs() -> None:
    assert ADAPTER.calculate_dls_resource(20.0, 0, 20.0) == pytest.approx(100.0)
    expected = 100.0 * hand_resource(10.0, 0) / hand_resource(20.0, 0)
    assert ADAPTER.calculate_dls_resource(10.0, 0, 20.0) == pytest.approx(expected, abs=1e-9)
    assert expected == pytest.approx(100.0 * (1 - math.exp(-0.35)) / (1 - math.exp(-0.7)), abs=1e-9)


def test_dls_par_score_reduced_resources_case() -> None:
    par = ADAPTER.calculate_dls_par_score(team1_runs=200, team1_overs=50.0, team2_overs_available=30.0, team2_wickets_lost=2, format_overs=50.0)
    r2 = min(hand_resource(30.0, w) for w in range(3))
    assert par == math.floor(200 * r2 / 100.0) + 1 == 136


def test_dls_par_score_extra_resources_uses_g() -> None:
    r1 = hand_resource(40.0, 0)
    target = ADAPTER.calculate_dls_par_score(team1_runs=250, team1_overs=40.0, team2_overs_available=50.0, team2_wickets_lost=0, format_overs=50.0)
    assert target == 250 + math.floor((100.0 - r1) / 100.0 * 245.0) + 1 == 272
    t20 = ADAPTER.calculate_dls_par_score(team1_runs=150, team1_overs=15.0, team2_overs_available=20.0, team2_wickets_lost=0, format_overs=20.0)
    r1_t20 = 100.0 * hand_resource(15.0, 0) / hand_resource(20.0, 0)
    assert t20 == 150 + math.floor((100.0 - r1_t20) / 100.0 * 170.0) + 1


def test_revised_target_both_cases() -> None:
    assert ADAPTER.revised_target(240, 100.0, 80.0, 50.0) == math.floor(240 * 0.8) + 1
    assert ADAPTER.revised_target(240, 90.0, 100.0, 50.0) == 240 + math.floor(0.10 * 245.0) + 1
    assert ADAPTER.revised_target(160, 90.0, 100.0, 20.0) == 160 + math.floor(0.10 * 170.0) + 1
    assert ADAPTER.revised_target(160, 0.0, 50.0, 20.0) == 1


def test_mid_innings_interruption_uses_resources_lost() -> None:
    lost = ADAPTER.resources_lost_to_interruption(20.0, 10.0, 3, 50.0)
    by_hand = min(hand_resource(20.0, w) for w in range(4)) - min(hand_resource(10.0, w) for w in range(4))
    assert lost == pytest.approx(by_hand, abs=1e-9)
    assert ADAPTER.resources_lost_to_interruption(20.0, 20.0, 3, 50.0) == 0.0
    target = ADAPTER.interrupted_chase_target(250, 100.0, team2_overs_at_start=50.0, overs_left_before=20.0, overs_left_after=10.0,
                                              wickets_lost=3, format_overs=50.0)
    assert target == math.floor(250 * (100.0 - lost) / 100.0) + 1


# ================================================================ the ball
@pytest.mark.parametrize(("fmt", "phase"), [(f, p) for f, phases in BALL_BASE.items() for p in phases])
def test_ball_matrix_rows_are_distributions(fmt: str, phase: str) -> None:
    m = ball_matrix(neutral(fmt, strength=1.3, wear=0.5, dew=1.0), phase)
    assert m.shape == (10, 7)
    assert np.all(m >= 0.0)
    assert np.allclose(m.sum(axis=1), 1.0)


def test_ball_matrix_strength_wear_and_wickets_in_hand() -> None:
    base = ball_matrix(neutral("t20"), "middle")
    strong = ball_matrix(neutral("t20", strength=1.2), "middle")
    runs = np.array([0, 1, 2, 3, 4, 6])
    assert (strong[:, :6] @ runs > base[:, :6] @ runs).all()
    assert (strong[:, 6] < base[:, 6]).all()
    assert base[8, 4] < base[0, 4] and base[8, 5] < base[0, 5]  # fewer wickets in hand: less boundary aggression
    assert base[8, 6] > base[0, 6]  # the tail gets out more
    spin_worn = ball_matrix(neutral("t20", wear=0.8, spin_share=0.8), "middle")
    pace_worn = ball_matrix(neutral("t20", wear=0.8, spin_share=0.1), "middle")
    assert spin_worn[0, 6] > pace_worn[0, 6] > base[0, 6]


def test_dew_cuts_spin_wickets_by_42_percent_of_spin_share() -> None:
    for intensity, sp in ((1.0, 0.4), (0.75, 0.6), (0.5, 1.0)):
        dry = ball_matrix(neutral("t20", spin_share=sp), "middle")
        wet = ball_matrix(neutral("t20", spin_share=sp, dew=intensity), "middle")
        assert np.allclose(wet[:, 6] / dry[:, 6], 1.0 - DEW_SPIN_WICKET_DROP * intensity * sp)
        assert (wet[:, 4:6] > dry[:, 4:6]).all()


def test_extras_kernel_and_over_notation() -> None:
    k = extras_kernel(0.04)
    assert k.sum() == pytest.approx(1.0)
    assert k[0] == pytest.approx(0.96)
    assert balls_from_overs(12.3) == 75
    assert balls_from_overs(20.0) == 120
    assert balls_from_overs(0.5) == 5


# ================================================================ innings distributions
def test_neutral_innings_calibration() -> None:
    t20 = simulate_limited_innings(neutral("t20"), 20)
    odi = simulate_limited_innings(neutral("odi"), 50)
    assert 163.0 <= t20.mean <= 172.0
    assert 265.0 <= odi.mean <= 285.0
    assert t20.powerplay is not None and 40.0 <= t20.powerplay.mean <= 55.0
    assert t20.total.probs.sum() == pytest.approx(1.0)
    assert 5.0 <= t20.expected_sixes <= 10.0
    assert 115.0 <= t20.expected_balls <= 120.0
    test = simulate_test_innings(neutral("test", wear=0.1))
    assert 290.0 <= test.mean <= 360.0


def test_dft_propagation_matches_direct_chase_dp() -> None:
    cond = neutral("t20", strength=1.05, dew=0.75)
    full = simulate_limited_innings(cond, 20).total
    for need in (120, 165, 210):
        chase = chase_probabilities(cond, need=need, balls_left=120, wickets=0, start_ball=0, overs=20)
        assert chase.reached == pytest.approx(full.prob_gt(need - 1), abs=1e-9)
        assert chase.level == pytest.approx(full.prob_eq(need - 1), abs=1e-9)
        assert chase.reached + chase.level + chase.short_all_out + chase.short_alive == pytest.approx(1.0, abs=1e-9)


def test_test_innings_closed_form_matches_ball_by_ball_chain() -> None:
    cond = neutral("test", wear=0.3)
    closed = simulate_test_innings(cond)
    long_innings = simulate_limited_innings(cond, 400)  # all out long before 400 overs
    assert closed.mean == pytest.approx(long_innings.mean, abs=0.05)
    assert closed.total.std == pytest.approx(long_innings.total.std, abs=0.05)
    assert closed.expected_balls == pytest.approx(long_innings.expected_balls, rel=1e-3)
    assert closed.expected_sixes == pytest.approx(long_innings.expected_sixes, rel=1e-3)


def test_live_projection_shifts_by_runs_already_scored() -> None:
    cond = neutral("t20")
    later = simulate_limited_innings(cond, 20, start_ball=60, wickets=2, runs=80)
    assert later.total.offset >= 80
    assert later.powerplay is None
    assert 120.0 < later.mean < 200.0


# ================================================================ numeric helpers
def test_local_line_outcome_matches_foundation() -> None:
    rng = np.random.default_rng(7)
    pmf = IntPMF.from_array(rng.random(40), -12)
    for line in (-3.0, -2.75, -2.5, -0.25, 0.0, 0.25, 1.5, 4.75, 10.0):
        for over in (True, False):
            mine, ref = line_outcome(pmf, line, over=over), pmf.line_outcome(line, over=over)
            assert mine.as_dict() == pytest.approx(ref.as_dict(), abs=1e-12)
            assert mine.effective_probability == ref.effective_probability


def test_prob_exceeds_matches_brute_force() -> None:
    a = IntPMF.from_array([0.2, 0.5, 0.3], 10)
    b = IntPMF.from_array([0.1, 0.3, 0.4, 0.2], 9)
    gt = sum(pa * pb for i, pa in enumerate(a.probs) for j, pb in enumerate(b.probs) if 9 + j > 10 + i)
    eq = sum(pa * pb for i, pa in enumerate(a.probs) for j, pb in enumerate(b.probs) if 9 + j == 10 + i)
    assert prob_exceeds(b, a) == pytest.approx((gt, eq))


def test_pitch_mixture_preserves_mean_and_widens() -> None:
    pmf = simulate_limited_innings(neutral("t20"), 20).total
    mixed = pitch_mixture(pmf, 0.13)
    assert mixed.probs.sum() == pytest.approx(1.0)
    assert mixed.mean == pytest.approx(pmf.mean, abs=0.3)
    assert 26.0 <= mixed.std <= 36.0 and mixed.std > pmf.std
    assert pitch_mixture(pmf, 0.0) is pmf


# ================================================================ pricing
@pytest.mark.parametrize("fmt", ["t20", "odi", "test"])
@pytest.mark.parametrize("dew", [None, "heavy"])
def test_probabilities_and_markets_are_coherent(fmt: str, dew: str | None) -> None:
    ctx: dict[str, Any] = {"format": fmt}
    if dew:
        ctx.update(is_night_match=True, dew_intensity=dew)
    res = ADAPTER.calculate_fair_odds({"batting_rating": 104.0}, {"bowling_rating": 98.0}, ctx)
    assert res.home_win_prob + res.away_win_prob + res.draw_prob == pytest.approx(1.0, abs=1e-3)
    m = res.market_probabilities
    assert all(0.0 <= v <= 1.0 for v in m.values())
    for key, value in m.items():
        if "_over_" in key:
            assert value + m[key.replace("_over_", "_under_")] == pytest.approx(1.0, abs=2e-4)
    assert m["most_sixes_home"] + m["most_sixes_tie"] + m["most_sixes_away"] == pytest.approx(1.0, abs=2e-4)
    assert "first_6_overs_over_48_5" in m
    assert any(k.startswith("total_match_runs_over_") for k in m)
    if fmt == "test":
        assert res.draw_prob > 0.02 and "match_draw" in m
    elif fmt == "t20":
        assert res.draw_prob == 0.0
    else:
        assert 0.0 < res.draw_prob < 0.03  # an ODI tie


def test_reference_assertions() -> None:
    res = ADAPTER.calculate_fair_odds(
        home_stats={"batting_rating": 110.0, "bowling_rating": 95.0},
        away_stats={"batting_rating": 95.0, "bowling_rating": 105.0},
        context={"format": "t20", "is_night_match": True, "dew_intensity": "heavy", "chasing_team": "away"},
    )
    assert 0.0 < res.home_win_prob < 1.0
    assert 0.0 < res.away_win_prob < 1.0
    assert abs((res.home_win_prob + res.away_win_prob + res.draw_prob) - 1.0) < 0.01
    assert res.sport_specific_metrics["dew_impact_applied"] is True
    assert res.sport_specific_metrics["format"] == "t20"
    assert res.sport_specific_metrics["home_expected_runs"] > 0
    assert res.sport_specific_metrics["batting_first"] == "home"
    assert ADAPTER.resolve_market_winner("match_winner", {"winner": "india"}, "india") is True
    assert ADAPTER.resolve_market_winner("innings_total_runs", {"innings_runs": 185}, "over", line=175.5) is True
    assert ADAPTER.resolve_market_winner("first_6_overs_runs", {"powerplay_runs": 52}, "over", line=48.5) is True
    assert ADAPTER.resolve_market_winner("most_sixes", {"home_sixes": 8, "away_sixes": 5}, "home") is True


def test_better_batting_raises_win_probability() -> None:
    probs = [ADAPTER.calculate_fair_odds({"batting_rating": r}, {}, {"format": "t20"}).home_win_prob for r in (85.0, 95.0, 105.0, 120.0)]
    assert probs == sorted(probs) and probs[0] < 0.5 < probs[-1]
    bowl = [ADAPTER.calculate_fair_odds({}, {"bowling_rating": r}, {"format": "odi"}).home_win_prob for r in (85.0, 100.0, 115.0)]
    assert bowl == sorted(bowl, reverse=True)


def test_equal_sides_are_even_without_dew() -> None:
    res = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20"})
    assert res.home_win_prob == pytest.approx(0.5, abs=1e-3)
    assert res.sport_specific_metrics["dew_impact_applied"] is False


@pytest.mark.parametrize("fmt", ["t20", "odi"])
def test_heavy_dew_calibration_and_intensity_order(fmt: str) -> None:
    chase = []
    for intensity in ("light", "moderate", "heavy"):
        res = ADAPTER.calculate_fair_odds({}, {}, {"format": fmt, "is_night_match": True, "dew_intensity": intensity})
        chase.append(res.away_win_prob)  # home bats first by default, away chases in the dew
        assert res.sport_specific_metrics["dew_impact_applied"] is True
        assert res.sport_specific_metrics["dew_chase_win_shift"] > 0
    assert chase == sorted(chase)
    assert 0.60 <= chase[-1] <= 0.68
    markov = next(e for e in res.sub_models if e.name == "markov_ball_by_ball")
    assert 0.60 <= markov.away_win_prob / (markov.home_win_prob + markov.away_win_prob + markov.draw_prob) <= 0.70


def test_dew_needs_night_and_moisture_and_follows_the_chasing_side() -> None:
    day = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "dew_intensity": "heavy"})
    assert day.sport_specific_metrics["dew_impact_applied"] is False
    humid_night = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "weather": {"local_hour": 20.0, "humidity_pct": 88.0}})
    assert humid_night.sport_specific_metrics["dew_intensity"] == "moderate"
    assert humid_night.away_win_prob > 0.55
    dry_night = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "weather": {"local_hour": 20.0, "humidity_pct": 60.0}})
    assert dry_night.sport_specific_metrics["dew_impact_applied"] is False
    flipped = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "is_night_match": True, "dew_intensity": "heavy", "batting_first": "away"})
    assert chase_prob(flipped, "home") > 0.6
    spin_heavy = ADAPTER.calculate_fair_odds({"spin_share": 0.8}, {}, {"format": "t20", "is_night_match": True, "dew_intensity": "heavy"})
    assert spin_heavy.away_win_prob > flipped.home_win_prob  # the home attack bowls second in the dew and spins more
    test = ADAPTER.calculate_fair_odds({}, {}, {"format": "test", "is_night_match": True, "dew_intensity": "heavy"})
    assert test.sport_specific_metrics["dew_impact_applied"] is False


def test_sub_models_and_weights() -> None:
    res = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20"})
    names = {e.name for e in res.sub_models}
    assert names == {"markov_ball_by_ball", "dls_resource_par", "rating_logistic"}
    assert sum(e.weight for e in res.sub_models) == pytest.approx(1.0)
    elo = ADAPTER.calculate_fair_odds({"elo": 1700}, {"elo": 1500}, {"format": "t20"})
    assert {e.name for e in elo.sub_models} == names | {"elo_logistic"}
    assert sum(e.weight for e in elo.sub_models) == pytest.approx(1.0)
    assert elo.home_win_prob > res.home_win_prob
    benched = ADAPTER.calculate_fair_odds({"elo": 1700}, {"elo": 1500}, {"format": "t20", "model_weights": {"elo_logistic": 0.0}})
    assert benched.consensus is not None and benched.consensus.model_count == 3
    assert benched.home_win_prob == pytest.approx(0.5, abs=0.01)
    with pytest.raises(ValueError, match="at least 3"):
        ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "model_weights": {"rating_logistic": 0.0}})


def test_absences_cost_rating_points() -> None:
    base = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20"})
    ctx = {"format": "t20", "absences": [{"side": "home", "player": "opener", "war": 2.0, "position": "batter"},
                                         {"side": "home", "player": "spinner", "war": 2.0, "position": "bowler", "status": "doubtful"}]}
    res = ADAPTER.calculate_fair_odds({}, {}, ctx)
    m = res.sport_specific_metrics
    assert m["home_absence_rating_delta"] == {"batting": -8.0, "bowling": -4.0}
    assert m["home_expected_runs"] < base.sport_specific_metrics["home_expected_runs"]
    assert m["away_expected_runs"] > base.sport_specific_metrics["away_expected_runs"]
    assert res.home_win_prob < base.home_win_prob
    assert "home_absence_rating_delta" not in base.sport_specific_metrics


def test_weather_and_umpire_adjustments_move_runs_the_right_way() -> None:
    base = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20"}).sport_specific_metrics["home_expected_runs"]

    def runs(ctx: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        metrics = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", **ctx}).sport_specific_metrics
        return metrics["home_expected_runs"], metrics

    friction, m = runs({"weather": {"friction": 1.1, "precipitation_mm": 5.0}})
    assert friction > base and m["weather_friction_multiplier"] == 1.1 and "weather_run_multiplier" not in m
    wet, m = runs({"weather": {"precipitation_mm": 3.0}})
    assert wet < base and m["weather_run_multiplier"] < 1.0
    high, m = runs({"weather": {"altitude_m": 1800.0}})
    assert high > base and m["weather_six_multiplier"] > 1.0
    swing, m = runs({"weather": {"humidity_pct": 90.0, "local_hour": 14.0}})
    assert swing < base and m["weather_swing_wicket_multiplier"] > 1.0
    indoor, m = runs({"weather": {"indoor": True, "precipitation_mm": 9.0}})
    assert indoor == base
    wides, m = runs({"officials": {"name": "Umpire", "wides_per_innings": 14.0}})
    assert wides > base and m["umpire_extras_factor"] == 2.0
    lbw, m = runs({"officials": {"lbw_factor": 1.15}})
    assert lbw < base and m["umpire_wicket_factor"] == 1.15
    worn, m = runs({"pitch_wear": 0.7})
    assert worn < base
    worn_spin = ADAPTER.calculate_fair_odds({}, {"spin_share": 0.9}, {"format": "t20", "pitch_wear": 0.7}).sport_specific_metrics["home_expected_runs"]
    assert worn_spin < worn


def test_reduced_overs_match_scales_resources() -> None:
    res = ADAPTER.calculate_fair_odds({}, {}, {"format": "t20", "overs_per_side": 10})
    m = res.sport_specific_metrics
    assert m["overs_per_side"] == 10
    assert m["dls_resource_pct"] == pytest.approx(ADAPTER.calculate_dls_resource(10, 0, 20), abs=1e-3)
    assert 80.0 < m["home_expected_runs"] < 120.0


def test_test_match_draw_model() -> None:
    base = ADAPTER.calculate_fair_odds({}, {}, {"format": "test"})
    assert 0.12 <= base.draw_prob <= 0.30
    rain = ADAPTER.calculate_fair_odds({}, {}, {"format": "test", "weather": {"precipitation_mm": 4.0}})
    assert rain.draw_prob > base.draw_prob
    assert rain.sport_specific_metrics["overs_lost_to_rain"] == 100.0
    flat = ADAPTER.calculate_fair_odds({}, {}, {"format": "test", "pitch_flatness": 0.9})
    green = ADAPTER.calculate_fair_odds({}, {}, {"format": "test", "pitch_flatness": 0.1})
    assert green.draw_prob < base.draw_prob < flat.draw_prob
    m = base.sport_specific_metrics
    assert len(m["innings_expected_runs"]) == 4
    assert m["innings_expected_runs"] == sorted(m["innings_expected_runs"], reverse=True)  # the pitch wears
    strong = ADAPTER.calculate_fair_odds({"batting_rating": 115.0, "bowling_rating": 110.0}, {}, {"format": "test"})
    assert strong.home_win_prob > base.home_win_prob


def test_pricing_is_fast() -> None:
    for fmt in ("t20", "odi", "test"):
        times = []
        for _ in range(7):
            start = time.perf_counter()
            ADAPTER.calculate_fair_odds({"elo": 1550}, {"elo": 1500}, {"format": fmt, "is_night_match": True, "dew_intensity": "heavy"})
            times.append(time.perf_counter() - start)
        assert statistics.median(times) < 0.06, (fmt, statistics.median(times))


# ================================================================ in-play
def test_in_play_start_equals_pre_match_markov() -> None:
    ctx = {"format": "t20", "is_night_match": True, "dew_intensity": "moderate"}
    pre = ADAPTER.calculate_fair_odds({"batting_rating": 105.0}, {}, ctx)
    markov = next(e for e in pre.sub_models if e.name == "markov_ball_by_ball")
    live = ADAPTER.in_play_win_probability({"batting_rating": 105.0}, {}, {"innings": 1, "runs": 0, "wickets": 0, "balls_bowled": 0}, ctx)
    assert live.home_win_prob == pytest.approx(markov.home_win_prob, abs=1e-6)


def test_in_play_chase_terminal_states() -> None:
    def live(state: dict[str, Any], fmt: str = "t20") -> Any:
        return ADAPTER.in_play_win_probability({}, {}, state, {"format": fmt})

    won = live({"innings": 2, "runs": 171, "wickets": 4, "overs": 18.2, "target": 171})
    assert won.away_win_prob == 1.0
    all_out = live({"innings": 2, "runs": 150, "wickets": 10, "overs": 18.2, "target": 171})
    assert all_out.home_win_prob == 1.0
    tie = live({"innings": 2, "runs": 170, "wickets": 6, "overs": 20.0, "target": 171})
    assert tie.home_win_prob == pytest.approx(0.5) and tie.draw_prob == 0.0  # super over
    odi_tie = live({"innings": 2, "runs": 270, "wickets": 10, "overs": 49.4, "target": 271}, "odi")
    assert odi_tie.draw_prob == pytest.approx(1.0)
    last_ball = live({"innings": 2, "runs": 165, "wickets": 9, "overs": 19.5, "target": 171})
    # by hand: j extras (each one run) then the last legal ball from the nine-down death-overs row
    row = ball_matrix(neutral("t20"), "death")[9]
    legal = dict(zip((0, 1, 2, 3, 4, 6), row[:6], strict=True))
    ext = extras_kernel(EXTRAS_RATE["t20"])
    win = sum(ext[j] * sum(p for r, p in legal.items() if r >= 6 - j) for j in range(len(ext)))
    level = sum(ext[j] * legal.get(5 - j, 0.0) for j in range(len(ext)))
    assert last_ball.away_win_prob == pytest.approx(win + level / 2.0, abs=1e-12)


def test_in_play_chase_monotonic_and_mapped() -> None:
    def chase_win(runs: int, wickets: int, overs: float) -> float:
        return ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": runs, "wickets": wickets, "overs": overs, "target": 171},
                                               {"format": "t20"}).away_win_prob

    assert chase_win(120, 2, 14.0) > chase_win(100, 2, 14.0) > chase_win(80, 2, 14.0)
    assert chase_win(100, 2, 14.0) > chase_win(100, 5, 14.0) > chase_win(100, 8, 14.0)
    assert chase_win(100, 2, 12.0) > chase_win(100, 2, 14.0)
    by_notation = ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": 90, "wickets": 3, "overs": 12.3, "target": 171}, {"format": "t20"})
    by_balls = ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": 90, "wickets": 3, "balls_bowled": 75, "first_innings_runs": 170},
                                               {"format": "t20"})
    assert by_notation.away_win_prob == pytest.approx(by_balls.away_win_prob)
    flipped = ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": 90, "wickets": 3, "balls_bowled": 75, "target": 171},
                                              {"format": "t20", "batting_first": "away"})
    assert flipped.home_win_prob == pytest.approx(by_balls.away_win_prob)
    with pytest.raises(ValueError, match="target"):
        ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": 90}, {"format": "t20"})


def test_in_play_first_innings_projection() -> None:
    fast = ADAPTER.in_play_win_probability({}, {}, {"innings": 1, "runs": 70, "wickets": 0, "overs": 6.0}, {"format": "t20"})
    slow = ADAPTER.in_play_win_probability({}, {}, {"innings": 1, "runs": 30, "wickets": 3, "overs": 6.0}, {"format": "t20"})
    assert fast.home_win_prob > 0.5 > slow.home_win_prob
    done = ADAPTER.in_play_win_probability({}, {}, {"innings": 1, "runs": 240, "wickets": 4, "overs": 20.0}, {"format": "t20"})
    assert done.home_win_prob > 0.99
    for state in ({"innings": 1, "runs": 120, "wickets": 4, "overs": 15.0}, {"innings": 1, "runs": 160, "wickets": 6, "overs": 35.0}):
        for fmt in ("t20", "odi"):
            r = ADAPTER.in_play_win_probability({}, {}, state, {"format": fmt})
            assert r.home_win_prob + r.away_win_prob + r.draw_prob == pytest.approx(1.0)


def test_in_play_test_match() -> None:
    ctx = {"format": "test"}
    chase = ADAPTER.in_play_win_probability({}, {}, {"innings": 4, "runs": 150, "wickets": 3, "completed_innings": [350, 300, 250],
                                                     "overs_left_in_match": 80}, ctx)
    assert chase.home_win_prob + chase.away_win_prob + chase.draw_prob == pytest.approx(1.0)
    assert chase.detail["target"] == 301 and chase.detail["runs_needed"] == 151
    reached = ADAPTER.in_play_win_probability({}, {}, {"innings": 4, "runs": 301, "wickets": 3, "completed_innings": [350, 300, 250]}, ctx)
    assert reached.away_win_prob == 1.0
    out_of_time = ADAPTER.in_play_win_probability({}, {}, {"innings": 4, "runs": 200, "wickets": 3, "completed_innings": [350, 300, 250],
                                                           "overs_left_in_match": 0}, ctx)
    assert out_of_time.draw_prob == 1.0
    more_time = ADAPTER.in_play_win_probability({}, {}, {"innings": 4, "runs": 150, "wickets": 3, "completed_innings": [350, 300, 250],
                                                         "overs_left_in_match": 200}, ctx)
    assert more_time.draw_prob < chase.draw_prob
    second = ADAPTER.in_play_win_probability({}, {}, {"innings": 2, "runs": 100, "wickets": 2, "completed_innings": [350],
                                                      "overs_left_in_match": 330}, ctx)
    assert second.home_win_prob + second.away_win_prob + second.draw_prob == pytest.approx(1.0)
    assert second.home_win_prob > second.away_win_prob
    with pytest.raises(ValueError, match="completed_innings"):
        ADAPTER.in_play_win_probability({}, {}, {"innings": 3, "runs": 10}, ctx)


# ================================================================ settlement
@pytest.mark.parametrize(("market", "result", "selection", "line", "expected"), [
    ("innings_total_runs", {"innings_runs": 185}, "over", 175.5, LegStatus.WON),
    ("innings_total_runs", {"innings_runs": 185}, "under", 175.5, LegStatus.LOST),
    ("innings_total_runs", {"innings_runs": 185}, "over", 185.0, LegStatus.VOID),
    ("innings_total_runs", {"innings_runs": 185}, "over", 185.25, LegStatus.HALF_LOST),
    ("innings_total_runs", {"innings_runs": 185}, "over", 184.75, LegStatus.HALF_WON),
    ("innings_total_runs", {"home_runs": 160, "away_runs": 190}, "away_over", 180.5, LegStatus.WON),
    ("innings_runs", {"innings_runs": 150}, "under", 160.5, LegStatus.WON),
    ("first_6_overs_runs", {"powerplay_runs": 52}, "over", 48.5, LegStatus.WON),
    ("first_6_overs_runs", {"powerplay_runs": 48}, "over", 48.0, LegStatus.VOID),
    ("powerplay_runs", {"home_powerplay_runs": 40, "away_powerplay_runs": 61}, "away_over", 55.5, LegStatus.WON),
    ("most_sixes", {"home_sixes": 8, "away_sixes": 5}, "home", None, LegStatus.WON),
    ("most_sixes", {"home_sixes": 8, "away_sixes": 5}, "away", None, LegStatus.LOST),
    ("most_sixes", {"home_sixes": 6, "away_sixes": 6}, "tie", None, LegStatus.WON),
    ("most_sixes", {"home_sixes": 6, "away_sixes": 6}, "home", None, LegStatus.LOST),
    ("top_batsman_runs", {"player_runs": 72}, "over", 49.5, LegStatus.WON),
    ("top_batsman_runs", {"player_runs": 50}, "over", 50.0, LegStatus.VOID),
    ("inning_of_highest_score", {"innings": [320, 280, 150, 210]}, "1st", None, LegStatus.WON),
    ("inning_of_highest_score", {"innings": [320, 280, 150, 210]}, "2", None, LegStatus.LOST),
    ("inning_of_highest_score", {"innings": [320, 280, 350, 210]}, "third", None, LegStatus.WON),
    ("inning_of_highest_score", {"innings": [320, 280, 320, 210]}, "1", None, LegStatus.LOST),
    ("inning_of_highest_score", {"innings": [320, 280, 320, 210]}, "tie", None, LegStatus.WON),
    ("inning_of_highest_score", {"innings": [320, 280, 150, 210]}, "tie", None, LegStatus.LOST),
    ("innings_of_highest_score", {"innings": [{"runs": 180}, {"runs": 181}]}, "2nd", None, LegStatus.WON),
    ("match_winner", {"winner": "india"}, "india", None, LegStatus.WON),
    ("match_winner", {"winner": "india"}, "australia", None, LegStatus.LOST),
    ("match_winner", {"home_runs": 180, "away_runs": 150}, "home", None, LegStatus.WON),
    ("match_winner", {"home_runs": 450, "away_runs": 380, "result": "draw"}, "home", None, LegStatus.VOID),
    ("match_winner", {"home_runs": 170, "away_runs": 170, "winner": "away"}, "away", None, LegStatus.WON),
    ("match_winner", {"home_runs": 170, "away_runs": 120, "status": "no_result"}, "home", None, LegStatus.VOID),
    ("match_winner", {"status": "abandoned"}, "home", None, LegStatus.VOID),
    ("test_match_result", {"home_runs": 450, "away_runs": 380, "result": "draw"}, "draw", None, LegStatus.WON),
    ("test_match_result", {"home_runs": 450, "away_runs": 380, "result": "draw"}, "home", None, LegStatus.LOST),
    ("match_result_3way", {"home_runs": 450, "away_runs": 380}, "home", None, LegStatus.WON),
    ("total_match_runs", {"home_runs": 180, "away_runs": 150}, "over", 330.25, LegStatus.HALF_LOST),
    ("total_match_runs", {"home_runs": 180, "away_runs": 150}, "under", 330.5, LegStatus.WON),
    ("team_total_runs", {"home_runs": 180, "away_runs": 150}, "away_under", 155.5, LegStatus.WON),
    ("runs_handicap", {"home_runs": 180, "away_runs": 150}, "away", 30.0, LegStatus.VOID),
    ("runs_handicap", {"home_runs": 180, "away_runs": 150}, "home", -25.5, LegStatus.WON),
])
def test_settlement(market: str, result: dict[str, Any], selection: str, line: float | None, expected: LegStatus) -> None:
    assert ADAPTER.settle_market(market, result, selection, line) == expected


def test_settlement_with_params_and_players() -> None:
    result = {"innings": [310, 290, 180, 175], "player_stats": {"Virat Kohli": {"runs": 82, "sixes": 3}},
              "participants": ["Virat Kohli", "Rohit Sharma"]}
    assert ADAPTER.settle_market("innings_total_runs", result, "over", 300.5, {"innings": 1}) == LegStatus.WON
    assert ADAPTER.settle_market("innings_total_runs", result, "over", 300.5, {"innings": "2nd"}) == LegStatus.LOST
    assert ADAPTER.settle_market("top_batsman_runs", result, "over", 75.5, {"player": "virat kohli"}) == LegStatus.WON
    assert ADAPTER.settle_market("top_batsman_runs", result, "over", 75.5, {"player": "steve smith"}) == LegStatus.VOID
    assert ADAPTER.settle_market("player_runs", result, "over", 75.5, {"player": "virat kohli"}) == LegStatus.WON
    assert ADAPTER.settle_market("player_sixes", result, "under", 3.5, {"player": "virat kohli"}) == LegStatus.WON
    period = {"home_runs": 520, "away_runs": 400, "periods": {"first_innings": {"home": 320, "away": 340}}}
    assert ADAPTER.settle_market("first_innings_lead", period, "away") == LegStatus.WON
    assert ADAPTER.settle_market("first_innings_total_runs", period, "over", 650.5) == LegStatus.WON


def test_settlement_errors() -> None:
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("most_sixes", {"home_sixes": 3}, "home")
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("inning_of_highest_score", {"innings": [100, 200]}, "fifth")
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("innings_total_runs", {"innings_runs": 100}, "over")
    with pytest.raises(SettlementInputError):
        ADAPTER.settle_market("hat_trick_yes_no", {"home_runs": 1}, "yes")
    assert ADAPTER.resolve_market_winner("first_6_overs_runs", {}, "over", line=40.5) is False
    normalized = ADAPTER.normalize_result({"home_score": 115, "away_score": 108})
    assert normalized.home == 115 and normalized.away == 108
