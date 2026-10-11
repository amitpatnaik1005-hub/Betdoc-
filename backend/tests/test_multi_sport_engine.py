"""
Multi-Sport Engine Test Suite (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

The brief's reference assertions, then the engine end to end on the real code paths:

* the nine adapters (each sport's deep suite lives in ``tests/sports_engine``): registry, >= 3 sub-models, sane
  probabilities, every seeded market settleable, live probabilities from a game state;
* the numeric toolkit (pushes and quarter balls), leg settlement, all 27 universal bet types, and the structures
  (parlay, teaser, pleaser, round robin, if-bet, action reverse, double action, futures with dead heats);
* the twin bridge: pillar 1's consensus verdict, pillar 9's Shin benchmark (Betfair net of commission), the 0-100
  confidence index, FA-4's sport Kelly through KUMBHA's sizing, FA-8's OmniRoute, FA-2's discipline read;
* the omni-provider service: vault-first credentials, Odds API normalisation inside FA-3's 12 ms, the paced poll and
  the cross-provider screen on Redis, model line shopping;
* the API through ``httpx.ASGITransport(app=app)`` on SQLite and PostgreSQL (``TEST_POSTGRES_URL``): catalogue,
  configuration (admin only), leagues, pricing, settlement, the twin pipeline, in-play ticks published on Redis,
  discipline XP paid once.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_user, get_db
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.security_vault import VaultCrypto
from app.domain.cfo import growth_math as gm
from app.domain.sports import discipline as dsc
from app.domain.sports.american_football_adapter import AmericanFootballSportAdapter
from app.domain.sports.baseball_adapter import BaseballSportAdapter
from app.domain.sports.basketball_adapter import BasketballSportAdapter
from app.domain.sports.bet_catalog import CATALOG, STRUCTURE_TYPES, BetType, markets_for
from app.domain.sports.combat_adapter import CombatSportAdapter
from app.domain.sports.cricket_adapter import CricketSportAdapter
from app.domain.sports.errors import SportNotFoundError
from app.domain.sports.esports_adapter import EsportsSportAdapter
from app.domain.sports.ice_hockey_adapter import IceHockeySportAdapter
from app.domain.sports.quant import IntPMF, ScoreGrid, SubModelEstimate, blend, poisson_pmf
from app.domain.sports.registry import SPORT_KEYS, SportAdapterRegistry, sport_for_odds_key
from app.domain.sports.seed import LEAGUE_SEEDS, SPORT_SEEDS
from app.domain.sports.settlement import settle_handicap, settle_threshold
from app.domain.sports.soccer_adapter import SoccerSportAdapter
from app.domain.sports.structures import (
    LegSpec,
    StructureError,
    american_to_decimal,
    settle_future,
    settle_if_bet,
    settle_parlay,
    settle_pleaser,
    settle_round_robin,
    settle_teaser,
    teaser_expected_value,
)
from app.domain.sports.tennis_adapter import TennisSportAdapter
from app.domain.sports.twin_bridge import (
    FAIL,
    PASS,
    UNVERIFIED,
    confidence_index,
    consensus_verdict,
    route_best_price,
    sharp_benchmark,
    sport_kelly_sizing,
)
from app.main import app
from app.models import User
from app.models.never_forget import UserXPProfile, XPAuditLog
from app.models.omni_vault import VaultProviderCredential
from app.models.sports_config import LeagueRegistry, SportConfiguration
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.schemas.bet_types import LegStatus
from app.services.multi_sport_odds_sync import (
    MultiSportOddsSyncService,
    normalize_event,
    screen_event,
)
from app.services.vault.markdown_importer import provider_context

DEVELOPER = "Amit Ashok Kumar Patnaik"
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-sports-engine"
W, L, V, HW, HL, P = LegStatus.WON, LegStatus.LOST, LegStatus.VOID, LegStatus.HALF_WON, LegStatus.HALF_LOST, LegStatus.PENDING


# ================================================================ the brief's reference tests
def test_registry_initialization():
    """Verifies that all 9 sport domain adapters are correctly registered."""
    SportAdapterRegistry.initialize()
    sports = SportAdapterRegistry.list_supported_sports()
    assert len(sports) == 9
    keys = {s["sport_key"] for s in sports}
    expected = {
        "soccer", "cricket", "basketball", "tennis",
        "baseball", "american_football", "ice_hockey", "combat", "esports"
    }
    assert keys == expected
    assert all(len(s["bet_types"]) >= 10 and s["model_family"] for s in sports)


def test_cricket_adapter_dls_and_dew():
    """Tests Cricket DLS resource calculation, par score and dew effect."""
    adapter = CricketSportAdapter()
    r_50 = adapter.calculate_dls_resource(50.0, 0)
    assert 99.0 <= r_50 <= 100.0
    r_20_w5 = adapter.calculate_dls_resource(20.0, 5)
    assert 25.0 <= r_20_w5 <= 35.0

    par = adapter.calculate_dls_par_score(
        team1_runs=200,
        team1_overs=50.0,
        team2_overs_available=30.0,
        team2_wickets_lost=2,
        format_overs=50.0,
    )
    assert par > 0

    res = adapter.calculate_fair_odds(
        home_stats={"batting_rating": 110.0, "bowling_rating": 95.0},
        away_stats={"batting_rating": 95.0, "bowling_rating": 105.0},
        context={"format": "t20", "is_night_match": True, "dew_intensity": "heavy", "chasing_team": "away"},
    )
    assert 0.0 < res.home_win_prob < 1.0
    assert 0.0 < res.away_win_prob < 1.0
    assert abs((res.home_win_prob + res.away_win_prob + res.draw_prob) - 1.0) < 0.01

    # Resolvers
    assert adapter.resolve_market_winner("match_winner", {"winner": "india"}, "india") is True
    assert adapter.resolve_market_winner("innings_total_runs", {"innings_runs": 185}, "over", line=175.5) is True
    assert adapter.resolve_market_winner("first_6_overs_runs", {"powerplay_runs": 52}, "over", line=48.5) is True
    assert adapter.resolve_market_winner("most_sixes", {"home_sixes": 8, "away_sixes": 5}, "home") is True


def test_basketball_four_factors_and_altitude():
    """Tests Basketball Four Factors regression, altitude, and pace-adjusted point spread."""
    adapter = BasketballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"efg_pct": 0.56, "tov_pct": 0.11, "orb_pct": 0.30, "ftr": 0.25, "pace": 102.0},
        away_stats={"efg_pct": 0.50, "tov_pct": 0.14, "orb_pct": 0.24, "ftr": 0.19, "pace": 98.0},
        context={"venue_city": "denver"},
    )
    assert res.home_win_prob > res.away_win_prob
    assert res.expected_total_score > 200.0
    assert res.sport_specific_metrics["altitude_points"] == 2.8

    # Resolvers
    assert adapter.resolve_market_winner("moneyline", {"home_score": 115, "away_score": 108}, "home") is True
    assert adapter.resolve_market_winner("point_spread", {"home_score": 115, "away_score": 108}, "home", line=-4.5) is True
    assert adapter.resolve_market_winner("game_total_over_under", {"home_score": 115, "away_score": 108}, "over", line=220.5) is True
    assert adapter.resolve_market_winner("quarter_winner", {"quarter": 1, "q1_home": 32, "q1_away": 28}, "home") is True


def test_tennis_markov_hold_and_sets():
    """Tests Tennis analytical hold probability and surface speed index."""
    adapter = TennisSportAdapter()
    hold_grass = adapter.hold_probability(0.75)
    assert hold_grass > 0.80

    res = adapter.calculate_fair_odds(
        home_stats={"first_serve_win_pct": 0.76},
        away_stats={"first_serve_win_pct": 0.65},
        context={"surface": "grass", "best_of": 3},
    )
    assert res.home_win_prob > 0.60
    assert res.fair_home_odds < res.fair_away_odds

    # Resolvers
    assert adapter.resolve_market_winner("match_winner", {"winner": "alcaraz"}, "alcaraz") is True
    assert adapter.resolve_market_winner("set_betting_correct_score", {"set_score": "2-0"}, "2-0") is True
    assert adapter.resolve_market_winner("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "player_a", line=-3.5) is True
    assert adapter.resolve_market_winner("first_set_winner", {"first_set_winner": "sinner"}, "sinner") is True


def test_baseball_baseruns_and_nrfi():
    """Tests Baseball BaseRuns run expectancy and NRFI probability."""
    adapter = BaseballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"hits_per_9": 8.0, "walks_per_9": 3.0, "home_runs_per_9": 1.2, "total_bases_per_9": 14.0, "stolen_bases_per_9": 0.5},
        away_stats={"hits_per_9": 7.5, "walks_per_9": 2.5, "home_runs_per_9": 0.9, "total_bases_per_9": 12.0, "stolen_bases_per_9": 0.8},
        context={"park": "coors_field"},
    )
    assert 0.0 < res.sport_specific_metrics["nrfi_probability"] < 1.0
    assert res.expected_total_score > 5.0
    assert res.sport_specific_metrics["park_factor"] == 1.34

    # Resolvers
    assert adapter.resolve_market_winner("moneyline", {"home_runs": 6, "away_runs": 4}, "home") is True
    assert adapter.resolve_market_winner("run_line_handicap", {"home_runs": 6, "away_runs": 4}, "home", line=-1.5) is True
    assert adapter.resolve_market_winner("nrfi_yrfi_first_inning", {"first_inning_runs": 0}, "nrfi") is True
    assert adapter.resolve_market_winner("first_5_innings_winner", {"f5_home_runs": 3, "f5_away_runs": 1}, "home") is True


def test_nfl_epa_and_key_numbers():
    """Tests NFL EPA per play and margin mass clustering."""
    adapter = AmericanFootballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"pass_epa": 0.15, "rush_epa": 0.02},
        away_stats={"pass_epa": -0.05, "rush_epa": -0.10},
    )
    assert res.home_win_prob > 0.65
    assert res.sport_specific_metrics["key_number_cluster_3"] == 0.151

    # Resolvers
    assert adapter.resolve_market_winner("point_spread", {"home_score": 27, "away_score": 20}, "home", line=-6.5) is True
    assert adapter.resolve_market_winner("first_half_spread", {"h1_home_score": 14, "h1_away_score": 7}, "home", line=-3.5) is True


def test_hockey_xg_and_empty_net():
    """Tests NHL xG, Corsi For, and empty-net probabilities."""
    adapter = IceHockeySportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"expected_goals_per_60": 3.4, "corsi_for_pct": 54.0},
        away_stats={"expected_goals_per_60": 2.6, "corsi_for_pct": 46.0},
    )
    assert res.home_win_prob > 0.55
    assert res.sport_specific_metrics["empty_net_probability"] == 0.224

    # Resolvers
    assert adapter.resolve_market_winner("regulation_3_way", {"reg_home_score": 3, "reg_away_score": 3}, "draw") is True
    assert adapter.resolve_market_winner("empty_net_goal_yes_no", {"empty_net_goal_scored": True}, "yes") is True


def test_combat_strike_differential():
    """Tests MMA significant strike differential and distance probability."""
    adapter = CombatSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"slpm": 5.5, "sapm": 2.8, "td_avg_per_15": 2.5, "td_def_pct": 0.80, "finish_rate": 0.70},
        away_stats={"slpm": 3.2, "sapm": 4.1, "td_avg_per_15": 0.5, "td_def_pct": 0.50, "finish_rate": 0.40},
        context={"rounds": 5},
    )
    assert res.home_win_prob > 0.70
    assert "distance_probability" in res.sport_specific_metrics

    # Resolvers
    assert adapter.resolve_market_winner("method_of_victory", {"method": "ko_tko"}, "ko_tko") is True
    assert adapter.resolve_market_winner("fight_goes_the_distance", {"went_distance": False}, "no") is True


def test_esports_map_veto():
    """Tests Esports HLTV rating and BO3 series progression."""
    adapter = EsportsSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"team_rating": 1.15},
        away_stats={"team_rating": 0.95},
        context={"series_format": "bo3"},
    )
    assert res.home_win_prob > 0.70
    assert 2.0 <= res.expected_total_score <= 3.0

    # Resolvers
    assert adapter.resolve_market_winner("map_handicap", {"team_a_maps": 2, "team_b_maps": 0}, "team_a", line=-1.5) is True
    assert adapter.resolve_market_winner("pistol_round_winner", {"pistol_winner": "faze"}, "faze") is True


def test_soccer_dixon_coles():
    """Tests Soccer Dixon-Coles bivariate Poisson with low-score correlation."""
    adapter = SoccerSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"attack_rating": 1.6, "defense_rating": 0.9},
        away_stats={"attack_rating": 1.0, "defense_rating": 1.4},
    )
    assert res.home_win_prob > res.away_win_prob
    assert res.draw_prob > 0.15
    assert "dixon_coles_rho" in res.sport_specific_metrics

    # Resolvers
    assert adapter.resolve_market_winner("1x2_match_odds", {"home_goals": 2, "away_goals": 1}, "home") is True
    assert adapter.resolve_market_winner("over_under_2_5_goals", {"home_goals": 2, "away_goals": 1}, "over") is True
    assert adapter.resolve_market_winner("both_teams_to_score", {"home_goals": 2, "away_goals": 1}, "yes") is True
    assert adapter.resolve_market_winner("correct_score", {"home_goals": 2, "away_goals": 1}, "2-1") is True


async def test_multi_provider_line_shopping_and_edges():
    """Tests multi-provider line shopping, positive EV edges, and arbitrage detection."""
    service = MultiSportOddsSyncService()
    venue_odds = {
        "pinnacle": {"home": 2.15, "away": 1.78},
        "betfair": {"home": 2.18, "away": 1.80},
        "the_odds_api": {"home": 2.10, "away": 1.75},
        "parimatch": {"home": 2.22, "away": 1.72},
        "stake": {"home": 2.25, "away": 1.70},
    }
    edges = await service.screen_multi_provider_edges(
        sport_key="soccer",
        match_id="m123",
        home_team="Arsenal",
        away_team="Chelsea",
        venue_odds=venue_odds,
        home_stats={"attack_rating": 1.8, "defense_rating": 0.8},
        away_stats={"attack_rating": 1.1, "defense_rating": 1.3},
    )
    assert len(edges) > 0
    assert any(e["developer_credit"] == "Amit Ashok Kumar Patnaik" for e in edges)
    home_edges = [e for e in edges if e.get("selection") == "Arsenal"]
    assert home_edges, "the model makes Arsenal a clear value at 2.25"
    assert home_edges[0]["best_venue"] == "stake"  # 2.25 was highest price
    assert home_edges[0]["bookmaker_odds"] == 2.25
    assert home_edges[0]["pillar_1"]["voting"] >= 3
    assert 0 <= home_edges[0]["confidence"]["score"] <= 100


async def test_provider_key_resolution():
    """Tests provider API key and endpoint configuration resolution."""
    service = MultiSportOddsSyncService()
    pinnacle_creds = await service.get_provider_credentials(None, "pinnacle")
    assert pinnacle_creds is not None
    assert "base_url" in pinnacle_creds

    odds_api_creds = await service.get_provider_credentials(None, "the_odds_api")
    assert odds_api_creds is not None
    assert "api_key" not in odds_api_creds  # the public view never carries a key
    assert await service.get_provider_credentials(None, "no_such_provider") is None


# ================================================================ adapters: shared invariants
STATS: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {
    "soccer": ({"attack_rating": 1.5, "defense_rating": 1.0}, {"attack_rating": 1.2, "defense_rating": 1.1}, {}),
    "cricket": ({"batting_rating": 104.0, "bowling_rating": 101.0}, {"batting_rating": 99.0, "bowling_rating": 100.0}, {"format": "t20"}),
    "basketball": ({"efg_pct": 0.55, "tov_pct": 0.12, "orb_pct": 0.27, "ftr": 0.22, "pace": 100.0},
                   {"efg_pct": 0.53, "tov_pct": 0.13, "orb_pct": 0.25, "ftr": 0.20, "pace": 99.0}, {}),
    "tennis": ({"first_serve_win_pct": 0.74}, {"first_serve_win_pct": 0.70}, {"surface": "fast_hard", "best_of": 3}),
    "baseball": ({"hits_per_9": 8.4, "walks_per_9": 3.2, "home_runs_per_9": 1.2, "total_bases_per_9": 13.9, "stolen_bases_per_9": 0.6},
                 {"hits_per_9": 8.0, "walks_per_9": 3.0, "home_runs_per_9": 1.0, "total_bases_per_9": 13.0, "stolen_bases_per_9": 0.6}, {}),
    "american_football": ({"pass_epa": 0.08, "rush_epa": -0.02}, {"pass_epa": 0.03, "rush_epa": -0.05}, {}),
    "ice_hockey": ({"expected_goals_per_60": 3.2, "corsi_for_pct": 52.0}, {"expected_goals_per_60": 2.9, "corsi_for_pct": 48.0}, {}),
    "combat": ({"slpm": 4.6, "sapm": 3.0, "td_avg_per_15": 1.8, "td_def_pct": 0.72, "finish_rate": 0.6},
               {"slpm": 4.0, "sapm": 3.5, "td_avg_per_15": 1.2, "td_def_pct": 0.65, "finish_rate": 0.5}, {"rounds": 3}),
    "esports": ({"team_rating": 1.08}, {"team_rating": 1.00}, {"series_format": "bo3"}),
}


@pytest.mark.parametrize("sport", SPORT_KEYS)
def test_every_adapter_prices_from_a_consensus(sport: str) -> None:
    adapter = SportAdapterRegistry.get_adapter(sport)
    home, away, ctx = STATS[sport]
    res = adapter.calculate_fair_odds(home, away, ctx)
    assert res.consensus is not None and res.consensus.model_count >= 3
    assert len({m.name for m in res.sub_models}) == len(res.sub_models) >= 3
    assert abs(res.home_win_prob + res.away_win_prob + res.draw_prob - 1.0) < 0.002
    assert 0 < res.home_win_prob < 1 and 0 < res.away_win_prob < 1
    assert res.fair_home_odds == pytest.approx(1.0 / res.home_win_prob, rel=2e-3)
    assert res.expected_total_score > 0
    assert res.market_probabilities and all(0.0 <= p <= 1.0 for p in res.market_probabilities.values())
    assert len(set(res.market_probabilities.values())) > 1, "market probabilities are computed, not a constant stub"
    assert res.kelly_multiplier == adapter.kelly_multiplier
    # a stronger home side never prices worse
    flipped = adapter.calculate_fair_odds(away, home, ctx)
    assert res.home_win_prob > flipped.home_win_prob


@pytest.mark.parametrize("sport", SPORT_KEYS)
def test_calibrated_weights_bench_a_sub_model(sport: str) -> None:
    adapter = SportAdapterRegistry.get_adapter(sport)
    home, away, ctx = STATS[sport]
    base = adapter.calculate_fair_odds(home, away, ctx)
    names = [m.name for m in base.sub_models]
    if len(names) < 4:
        weights = {names[0]: 3.0}
    else:
        weights = {names[-1]: 0.0}
    tuned = adapter.calculate_fair_odds(home, away, {**ctx, "model_weights": weights})
    assert tuned.evidence["model_weights"] is True
    assert tuned.consensus is not None and tuned.consensus.model_count >= 3


@pytest.mark.parametrize("sport", SPORT_KEYS)
def test_every_seeded_market_is_settleable(sport: str) -> None:
    adapter = SportAdapterRegistry.get_adapter(sport)
    seed = next(s for s in SPORT_SEEDS if s[0] == sport)
    for market in (*seed[3], *adapter.standard_markets):
        adapter.canonical_market(market)  # raises when the adapter cannot settle it


def test_unknown_sport_never_falls_back_to_football() -> None:
    with pytest.raises(SportNotFoundError):
        SportAdapterRegistry.get_adapter("curling")
    assert SportAdapterRegistry.get_adapter("basketball_nba").sport_key == "basketball"
    assert sport_for_odds_key("americanfootball_nfl") == "american_football"
    assert sport_for_odds_key("icehockey_nhl") == "ice_hockey"
    assert sport_for_odds_key("mma_mixed_martial_arts") == "combat"
    assert sport_for_odds_key("boxing_boxing") == "combat"
    assert sport_for_odds_key("golf_masters") is None


# ================================================================ the numeric toolkit and leg settlement
def test_quarter_and_whole_lines_carry_exact_half_and_push_mass() -> None:
    grid = ScoreGrid.independent(poisson_pmf(1.6, 11), poisson_pmf(1.1, 11))
    h, d, a = grid.outcome()
    margin = grid.margin_pmf()
    level = margin.line_outcome(0.0, over=True)  # draw no bet on home
    assert level.push == pytest.approx(d) and level.win == pytest.approx(h) and level.loss == pytest.approx(a)
    q = margin.line_outcome(0.75, over=True)  # home -0.75: a one-goal win is half won
    assert q.half_win == pytest.approx(margin.prob_eq(1)) and q.win == pytest.approx(margin.prob_gt(1.0))
    assert q.fair_odds == pytest.approx(1 + (q.loss + q.half_loss / 2) / (q.win + q.half_win / 2))
    total = grid.total_pmf()
    assert total.mean == pytest.approx(2.7, abs=1e-3)
    assert total.line_outcome(2.5, over=True).win + total.line_outcome(2.5, over=False).win == pytest.approx(1.0)
    pmf = IntPMF.from_normal(3.0, 13.8, -70, 70)
    assert pmf.mean == pytest.approx(3.0, abs=0.01) and pmf.std == pytest.approx(13.8, abs=0.05)


def test_consensus_needs_three_voting_sub_models() -> None:
    estimates = [SubModelEstimate("a", 0.6, 0.4), SubModelEstimate("b", 0.5, 0.5), SubModelEstimate("c", 0.55, 0.45)]
    c = blend(estimates)
    assert c.home_win_prob == pytest.approx(0.55) and c.model_count == 3 and 0 < c.agreement < 1
    with pytest.raises(ValueError):
        blend(estimates, {"a": 0.0})


@pytest.mark.parametrize(("value", "line", "over", "expected"), [
    (3, 2.5, True, W), (3, 3.0, True, V), (3, 3.25, True, HL), (3, 2.75, True, HW), (2, 2.25, False, HW), (3, 3.5, False, W),
])
def test_threshold_settlement(value: float, line: float, over: bool, expected: LegStatus) -> None:
    assert settle_threshold(value, line, over=over) == expected


@pytest.mark.parametrize(("team", "opp", "hcp", "expected"), [
    (1, 0, -0.75, HW), (1, 0, -1.0, V), (1, 0, -1.25, HL), (2, 0, -1.25, W), (0, 0, -0.25, HL), (0, 0, 0.25, HW), (0, 1, 0.5, L), (1, 1, 0.0, V),
])
def test_asian_handicap_settlement(team: int, opp: int, hcp: float, expected: LegStatus) -> None:
    assert settle_handicap(team, opp, hcp) == expected


# ================================================================ the 27 universal bet types
def test_catalogue_holds_27_bet_types_each_offered_somewhere() -> None:
    assert len(CATALOG) == 27 and len(BetType) == 27
    offered = {bt for a in SportAdapterRegistry.all_adapters() for bt in a.BET_TYPES}
    assert offered == {b.value for b in BetType}
    for adapter in SportAdapterRegistry.all_adapters():
        assert adapter.BET_TYPES <= {b.value for b in BetType}, adapter.sport_key
        for bt in adapter.BET_TYPES:
            if BetType(bt) in STRUCTURE_TYPES:
                continue
            reachable = [m for m in markets_for(BetType(bt), adapter.sport_key) if _settleable(adapter, m)]
            assert reachable, f"{adapter.sport_key} offers {bt} but no market implements it"


def _settleable(adapter: Any, market: str) -> bool:
    try:
        adapter.canonical_market(market)
    except ValueError:
        return False
    return True


MARKET_CASES: list[tuple[str, str, str, dict[str, Any], str, float | None, dict[str, Any], LegStatus]] = [
    ("moneyline", "basketball", "moneyline", {"home_score": 101, "away_score": 99}, "home", None, {}, W),
    ("moneyline", "american_football", "moneyline", {"home_score": 20, "away_score": 20}, "home", None, {}, V),
    ("moneyline", "soccer", "1x2_match_odds", {"home_goals": 1, "away_goals": 1}, "home", None, {}, L),
    ("point_spread", "american_football", "point_spread", {"home_score": 24, "away_score": 21}, "home", -3.0, {}, V),
    ("point_spread", "baseball", "run_line_handicap", {"home_runs": 3, "away_runs": 2}, "away", 1.5, {}, W),
    ("point_spread", "ice_hockey", "puck_line_handicap", {"home_score": 4, "away_score": 2}, "home", -1.5, {}, W),
    ("game_total", "basketball", "game_total_over_under", {"home_score": 110, "away_score": 110}, "under", 220.0, {}, V),
    ("team_total", "american_football", "team_total_points", {"home_score": 27, "away_score": 13}, "home_over", 24.5, {}, W),
    ("period_lines", "soccer", "period_total:h1", {"home_goals": 2, "away_goals": 1, "periods": {"h1": {"home": 1, "away": 0}}}, "over", 0.5, {}, W),
    ("period_lines", "basketball", "period_spread:q1", {"home_score": 99, "away_score": 90, "periods": {"q1": {"home": 25, "away": 28}}},
     "home", 2.5, {}, L),
    ("player_props", "basketball", "player_prop", {"home_score": 1, "away_score": 0, "player_stats": {"Jokic": {"rebounds": 13}}}, "over", 11.5,
     {"player": "Jokic", "stat": "rebounds"}, W),
    ("player_props", "baseball", "player_prop", {"home_runs": 1, "away_runs": 0, "participants": ["ohtani"], "player_stats": {}}, "over", 0.5,
     {"player": "judge", "stat": "hits"}, V),
    ("game_props", "baseball", "nrfi_yrfi_first_inning", {"first_inning_runs": 2}, "yrfi", None, {}, W),
    ("game_props", "cricket", "most_sixes", {"home_sixes": 7, "away_sixes": 7}, "tie", None, {}, W),
    ("live_next_event", "soccer", "next_goal", {"home_goals": 2, "away_goals": 1, "events": [{"type": "goal", "team": "home", "t": 10},
                                                                                              {"type": "goal", "team": "away", "t": 70}]},
     "away", None, {"after": 30}, W),
    ("draw_no_bet", "soccer", "draw_no_bet", {"home_goals": 2, "away_goals": 2}, "away", None, {}, V),
    ("btts", "soccer", "both_teams_to_score", {"home_goals": 3, "away_goals": 0}, "no", None, {}, W),
    ("correct_score", "soccer", "correct_score", {"home_goals": 3, "away_goals": 0}, "3-0", None, {}, W),
    ("asian_handicap", "soccer", "asian_handicap", {"home_goals": 2, "away_goals": 1}, "away", 1.25, {}, HW),
    ("clean_sheet", "soccer", "clean_sheet", {"home_goals": 1, "away_goals": 0}, "home", None, {}, W),
    ("method_of_victory", "combat", "method_of_victory", {"winner": "a", "method": "submission"}, "submission", None, {}, W),
    ("total_maps", "esports", "map_total_over_under", {"team_a_maps": 2, "team_b_maps": 1}, "over", 2.5, {}, W),
    ("first_5_innings", "baseball", "first_5_innings_winner", {"f5_home_runs": 2, "f5_away_runs": 2}, "home", None, {}, V),
    ("alternative_lines", "basketball", "alt_spread", {"home_score": 120, "away_score": 100}, "home", -15.5, {}, W),
]


@pytest.mark.parametrize(("bet_type", "sport", "market", "result", "selection", "line", "params", "expected"), MARKET_CASES,
                         ids=[f"{c[0]}-{c[1]}-{c[2]}" for c in MARKET_CASES])
def test_universal_market_bet_types_settle(bet_type: str, sport: str, market: str, result: dict[str, Any], selection: str, line: float | None,
                                           params: dict[str, Any], expected: LegStatus) -> None:
    adapter = SportAdapterRegistry.get_adapter(sport)
    assert bet_type in adapter.BET_TYPES
    assert adapter.settle_market(market, result, selection, line, params) == expected


def test_market_bet_types_with_sport_shaped_results() -> None:
    combat = SportAdapterRegistry.get_adapter("combat")
    assert combat.settle_market("round_betting", {"winner": "b", "method": "ko_tko", "round": 2, "time": "1:12"}, "away_r2") == W
    assert combat.settle_market("round_betting", {"winner": "b", "method": "ko_tko", "round": 2, "time": "1:12"}, "home_r2") == L
    esports = SportAdapterRegistry.get_adapter("esports")
    assert esports.settle_market("map_winner", {"team_a_maps": 2, "team_b_maps": 1, "maps": ["a", "b", "a"]}, "team_b", params={"map": 2}) == W
    baseball = SportAdapterRegistry.get_adapter("baseball")
    innings = [[0, 1], [0, 0], [3, 0], [0, 0], [0, 0], [1, 0], [0, 0], [0, 1], [0, 0]]
    assert baseball.settle_market("inning_of_highest_score", {"home_runs": 2, "away_runs": 4, "innings": innings}, "3") == W
    tennis = SportAdapterRegistry.get_adapter("tennis")
    assert tennis.settle_market("set_betting_correct_score", {"set_score": "2-1"}, "2-0") == L


# ================================================================ structures
def _leg(leg_id: str, odds: float, status: LegStatus) -> LegSpec:
    return LegSpec(leg_id, odds, status=status)


def test_parlay_multiplies_and_voids_drop_out() -> None:
    res = settle_parlay([_leg("a", 2.0, W), _leg("b", 1.9, V), _leg("c", 1.8, HW)], 100)
    assert res.status is W and res.payout == pytest.approx(100 * 2.0 * 1.0 * 1.4)
    assert settle_parlay([_leg("a", 2.0, W), _leg("b", 3.0, L)], 50).payout == 0.0
    assert settle_parlay([_leg("a", 2.0, W), _leg("b", 3.0, P)], 50).status is P
    assert settle_parlay([_leg("a", 2.0, V), _leg("b", 3.0, V)], 50).status is V


def _nfl(leg_id: str, home: int, away: int, selection: str, line: float, market: str = "point_spread") -> LegSpec:
    return LegSpec(leg_id, 1.91, "american_football", market, selection, line, match_result={"home_score": home, "away_score": away})


def test_teaser_moves_lines_and_pays_the_table() -> None:
    # home -7.5 teased 6 -> -1.5 (wins 24-20); under 41.5 -> 47.5 (44 total)
    res = settle_teaser([_nfl("a", 24, 20, "home", -7.5), _nfl("b", 24, 20, "under", 41.5, "game_total_over_under")], 110, 6.0)
    assert res.status is W and res.detail["american_odds"] == -110
    assert res.payout == pytest.approx(110 * float(american_to_decimal(-110)))
    # a pushed leg is removed: three legs become two
    push = settle_teaser([_nfl("a", 24, 20, "home", -7.5), _nfl("b", 20, 17, "away", -3.0), _nfl("c", 30, 10, "home", -20.5)], 100, 6.0)
    assert push.legs["b"] == "VOID" and push.detail["legs_paid"] == 2
    assert settle_teaser([_nfl("a", 24, 20, "home", -10.5), _nfl("b", 24, 20, "home", 1.5)], 100, 6.0).status is L
    with pytest.raises(StructureError):
        settle_teaser([_nfl("a", 24, 20, "home", -7.5), _nfl("b", 24, 20, "home", -7.5)], 100, 5.0)
    with pytest.raises(StructureError):
        settle_teaser([LegSpec("x", 1.9, "soccer", "asian_handicap", "home", -0.5, match_result={"home_goals": 1, "away_goals": 0}),
                       _nfl("b", 1, 0, "home", -1.5)], 100, 6.0)
    ev = teaser_expected_value([0.72, 0.72], "american_football", 6.0)
    assert ev["expected_value"] == pytest.approx(0.72 * 0.72 * float(american_to_decimal(-110)) - 1)


def test_pleaser_moves_lines_against_and_a_push_loses() -> None:
    win = settle_pleaser([_nfl("a", 30, 10, "home", -3.0), _nfl("b", 30, 10, "over", 30.0, "game_total_over_under")], 10, 6.0)
    assert win.status is W and win.payout == pytest.approx(10 * 7.0)
    push = settle_pleaser([_nfl("a", 30, 21, "home", -3.0), _nfl("b", 30, 10, "over", 30.0, "game_total_over_under")], 10, 6.0)
    assert push.status is L


def test_round_robin_builds_every_parlay() -> None:
    res = settle_round_robin([_leg("a", 2.0, W), _leg("b", 2.0, W), _leg("c", 2.0, L)], 10, [2])
    assert res.total_stake == 30.0 and len(res.lines) == 3
    assert res.payout == pytest.approx(40.0) and res.status is W
    with pytest.raises(StructureError):
        settle_round_robin([_leg("a", 2.0, W), _leg("b", 2.0, W), _leg("c", 2.0, W)], 10, [4])


def test_if_bet_action_reverse_and_double_action() -> None:
    first_loses = settle_if_bet([_leg("a", 2.0, L), _leg("b", 2.0, W)], [100, 100])
    assert first_loses.payout == 0.0 and first_loses.detail["chains"][0]["steps"][1]["status"] == "NO_ACTION"
    both_win = settle_if_bet([_leg("a", 2.0, W), _leg("b", 3.0, W)], [100, 100])
    assert both_win.payout == pytest.approx(200 + 200) and both_win.profit == pytest.approx(300)
    push_then = settle_if_bet([_leg("a", 2.0, V), _leg("b", 3.0, W)], [100, 100])
    assert push_then.detail["chains"][0]["steps"][1]["status"] == "NO_ACTION"
    double = settle_if_bet([_leg("a", 2.0, V), _leg("b", 3.0, W)], [100, 100], double_action=True)
    assert double.profit == pytest.approx(200) and double.structure == "double_action"
    reverse = settle_if_bet([_leg("a", 2.0, W), _leg("b", 3.0, L)], [100, 100], reverse=True)
    assert reverse.structure == "action_reverse" and reverse.total_stake == 200.0
    # A then B: +100 then -100 = 0 profit; B then A: B loses, A no action = -100
    assert reverse.profit == pytest.approx(-100)


def test_futures_dead_heat_divides_the_stake() -> None:
    assert settle_future("Arsenal", 6.0, 100, ["arsenal"]).payout == pytest.approx(600)
    heat = settle_future("Smith", 21.0, 10, ["smith", "jones"])
    assert heat.payout == pytest.approx(105) and heat.detail["dead_heat_divisor"] == 2
    assert settle_future("Smith", 21.0, 10, ["jones"]).status is L
    assert settle_future("Smith", 21.0, 10, None).status is P
    assert settle_future("Smith", 21.0, 10, None, cancelled=True).status is V


# ================================================================ the twin bridge
def test_pillar_1_consensus_verdict() -> None:
    models = [SubModelEstimate("a", 0.60, 0.40), SubModelEstimate("b", 0.58, 0.42), SubModelEstimate("c", 0.62, 0.38)]
    assert consensus_verdict(models, "home", 1.85).status == PASS
    assert consensus_verdict(models, "home", 1.70).status == FAIL  # model b prices it at no edge: a veto
    assert consensus_verdict(models, "home", 1.85, {"a": 0.0}).status == UNVERIFIED
    assert consensus_verdict(models, "away", 2.2).status == FAIL


def test_pillar_9_sharp_benchmark_and_exchange_commission() -> None:
    quotes = {"pinnacle": {"home": 1.95, "away": 1.95}, "stake": {"home": 2.10, "away": 1.80}}
    bench = sharp_benchmark(quotes, ["home", "away"])
    assert bench is not None and bench.book == "pinnacle"
    assert bench.fair_probabilities["home"] == pytest.approx(0.5)
    assert bench.retail_edges["stake"]["home"] == pytest.approx(2.10 * 0.5 - 1)
    exchange = sharp_benchmark({"betfair_ex_uk": {"home": 2.0, "away": 2.0}}, ["home", "away"])
    assert exchange is not None and exchange.book == "betfair"
    assert exchange.overround == pytest.approx(2 / 1.95 - 1)  # 2.0 both ways is 1.95 net of the 5% commission
    assert exchange.fair_probabilities["home"] == pytest.approx(0.5)
    assert sharp_benchmark({"stake": {"home": 2.0}}, ["home", "away"]) is None


def test_confidence_index_components() -> None:
    full = {"weather": True, "absences": True, "officials": True, "model_weights": True}
    strong = confidence_index("soccer", agreement=0.95, model_count=4, model_probability=0.55, sharp_probability=0.54, edge=0.06, evidence=full)
    assert strong.score >= 80 and strong.grade == "A" and strong.recommendation == "SUBMIT_TO_CFO"
    vetoed = confidence_index("soccer", agreement=0.95, model_count=4, model_probability=0.55, sharp_probability=0.54, edge=0.06, evidence=full,
                              consensus_status=FAIL)
    assert vetoed.score <= 49 and vetoed.recommendation == "NO_BET"
    stale = confidence_index("combat", agreement=0.9, model_count=3, model_probability=0.7, sharp_probability=0.5, edge=0.45, evidence={})
    assert stale.components["sharp_alignment"] == 0 and stale.components["edge_quality"] == 0 and stale.score < 50


def test_fa4_sport_kelly_flows_through_kumbha_sizing() -> None:
    policy = gm.SizingPolicy.from_settings(get_settings())
    nfl = sport_kelly_sizing("american_football", 0.52, 2.0, Decimal("100000"), 0.0, policy, Decimal("0.01"))
    mma = sport_kelly_sizing("combat", 0.52, 2.0, Decimal("100000"), 0.0, policy, Decimal("0.01"))
    assert nfl.full_kelly == pytest.approx(0.04)
    assert nfl.fraction == pytest.approx(0.04 * 0.50) and mma.fraction == pytest.approx(0.04 * 0.20)
    assert nfl.stake == Decimal("2000.00") and mma.stake == Decimal("800.00")
    halted = sport_kelly_sizing("soccer", 0.6, 2.0, Decimal("100000"), 0.5, policy)
    assert halted.halted and halted.stake == 0


def test_fa8_omniroute_best_price_and_primary_account() -> None:
    quotes = {"pinnacle": 2.05, "betfair": 2.12, "parimatch": 2.10, "stake": 2.06, "onexbet": 2.04}
    single = route_best_price(quotes)
    assert single is not None and single.venue == "parimatch"  # within 1% of the best net price: the capital account
    best = route_best_price({**quotes, "parimatch": 1.95})
    assert best is not None and best.venue == "betfair" and best.net_odds == pytest.approx(1 + 1.12 * 0.95)
    multi = route_best_price({"betfair": 2.5, "stake": 2.10, "onexbet": 2.09, "pinnacle": 2.0}, bet_kind="multi")
    assert multi is not None and multi.venue in ("stake", "1xbet") and "betfair" not in [v for v, _ in multi.ranked]
    assert route_best_price({"unknown_book": 3.0}) is None


def test_fa2_discipline_flags_tilt_and_revenge() -> None:
    t0 = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
    history = [dsc.BetRecord(t0 - timedelta(hours=10 - i), "soccer", 1000.0, 2.0, "WON", 1000.0) for i in range(8)]
    history += [dsc.BetRecord(t0 - timedelta(minutes=50), "soccer", 1000.0, 2.0, "LOST", -1000.0),
                dsc.BetRecord(t0 - timedelta(minutes=30), "soccer", 1000.0, 2.0, "LOST", -1000.0)]
    tilt = dsc.assess(dsc.CandidateBet(t0, "esports", 2500.0, 2.0, 0.45, 300.0), history)
    assert {"TILT_STAKE_ESCALATION", "REVENGE_NON_CORE", "OVER_KELLY", "NEGATIVE_EV"} <= set(tilt.flags)
    assert tilt.core_sports == ("soccer",) and not tilt.disciplined and tilt.score == 0
    calm = dsc.assess(dsc.CandidateBet(t0 + timedelta(days=1), "soccer", 900.0, 2.1, 0.52, 1000.0), history)
    assert calm.flags == () and calm.disciplined and calm.score == 100


# ================================================================ the omni-provider service
def _book(key: str, arsenal: float, chelsea: float, draw: float) -> dict[str, Any]:
    outcomes = [{"name": "Arsenal", "price": arsenal}, {"name": "Chelsea", "price": chelsea}, {"name": "Draw", "price": draw}]
    return {"key": key, "markets": [{"key": "h2h", "outcomes": outcomes}]}


ODDS_EVENT = {
    "id": "evt1", "sport_key": "soccer_epl", "commence_time": "2026-10-12T14:00:00Z", "home_team": "Arsenal", "away_team": "Chelsea",
    "bookmakers": [
        _book("pinnacle", 2.0, 3.9, 3.6),
        _book("onexbet", 2.25, 3.5, 3.4),
        _book("betfair_ex_uk", 2.04, 4.6, 3.7),
        _book("stake", 1.9, 4.4, 4.4),
    ],
}


def test_normalisation_maps_venues_inside_the_fa3_budget() -> None:
    event = normalize_event(ODDS_EVENT, "soccer_epl")
    assert event is not None and event.sport_key == "soccer" and event.outcomes == ("home", "draw", "away")
    assert set(event.venue_odds) == {"pinnacle", "1xbet", "betfair", "stake"}
    assert event.venue_odds["1xbet"] == {"home": 2.25, "away": 3.5, "draw": 3.4}
    assert sum(event.consensus.values()) == pytest.approx(1.0)
    assert event.sharp is not None and event.sharp["book"] == "pinnacle"
    assert event.normalization_ms < get_settings().SPORTS_NORMALIZATION_BUDGET_MS
    edges = screen_event(event.as_dict(), min_edge=0.02, min_arbitrage=0.015)
    kinds = {e["kind"] for e in edges}
    assert "value" in kinds  # 1xBet's 2.25 on Arsenal against Pinnacle's fair price
    arb = [e for e in edges if e["kind"] == "arbitrage"]
    assert arb and arb[0]["booksum"] < 1 and sum(leg["stake_share"] for leg in arb[0]["legs"]) == pytest.approx(1.0, abs=1e-4)
    assert normalize_event({**ODDS_EVENT, "bookmakers": []}, "soccer_epl") is None


def test_beat_schedules_both_sports_jobs() -> None:
    schedule = celery_app.conf.beat_schedule
    assert schedule["sports-poll-consensus-odds"]["task"] == "sports.poll_multi_sport_consensus_odds"
    assert schedule["sports-poll-consensus-odds"]["schedule"] == 60.0
    assert schedule["sports-screen-arbitrage"]["task"] == "sports.screen_cross_provider_arbitrage"
    assert schedule["sports-screen-arbitrage"]["schedule"] == 30.0
    assert "app.workers.sports_tasks" in celery_app.conf.include


def test_seed_catalogue_is_consistent() -> None:
    assert {s[0] for s in SPORT_SEEDS} == set(SPORT_KEYS)
    assert {lg[0] for lg in LEAGUE_SEEDS} == set(SPORT_KEYS)
    assert len({lg[1] for lg in LEAGUE_SEEDS}) == len(LEAGUE_SEEDS)
    for _, league, _, _, tier, feed in LEAGUE_SEEDS:
        assert 1 <= tier <= 5
        if feed is not None:
            assert sport_for_odds_key(feed) is not None, league


def test_migration_chain_is_the_briefs() -> None:
    from pathlib import Path  # noqa: PLC0415

    text = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f94a1206c83e_multi_sport_engine_expansion.py").read_text()
    assert "revision: str = 'f94a1206c83e'" in text and "down_revision: Union[str, Sequence[str], None] = 'e83f4105a72d'" in text


def test_migration_seed_is_the_live_catalogue() -> None:
    """The migration's frozen rows equal what ``ensure_seeded`` would insert today."""
    import importlib.util  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from app.domain.sports.seed import league_rows, sport_rows  # noqa: PLC0415

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f94a1206c83e_multi_sport_engine_expansion.py"
    spec = importlib.util.spec_from_file_location("group79_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live_sports = [(r["sport_key"], r["display_name"], r["default_kelly_fraction"], r["primary_odds_api_sport_key"], r["supported_markets"],
                    r["supported_bet_types"], r["math_model_parameters"]) for r in sport_rows()]
    live_leagues = [(r["sport_key"], r["league_key"], r["display_name"], r["country"], r["tier"], r["odds_api_sport_key"]) for r in league_rows()]
    assert [tuple(row) for row in module.SPORTS] == live_sports
    assert [tuple(row) for row in module.LEAGUES] == live_leagues
    assert "DISCIPLINED_BET" in module.XP_ACTIONS_AFTER


# ================================================================ fixtures: databases, Redis, the app
TABLES: list[Table] = [User.__table__, SportConfiguration.__table__, LeagueRegistry.__table__, UserXPProfile.__table__, XPAuditLog.__table__,
                       UserPlacedBet.__table__, UserPlacedLeg.__table__, VaultProviderCredential.__table__]  # type: ignore[list-item]


def _closure(tables: list[Table]) -> list[Table]:
    seen: dict[str, Table] = {}
    stack = list(tables)
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen[t.name] = t
        for fk in t.foreign_keys:
            stack.append(fk.column.table)
    return [t for t in User.metadata.sorted_tables if t.name in seen]


ALL_TABLES = _closure(TABLES)


def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in ALL_TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=ALL_TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _user(sessions: async_sessionmaker[AsyncSession], role: str) -> User:
    async with sessions() as s:
        user = User(id=uuid.uuid4(), username=f"{role.lower()}-{uuid.uuid4().hex[:6]}", hashed_password="x", role=role, is_active=True)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


@pytest_asyncio.fixture
async def api(sessions: async_sessionmaker[AsyncSession], redis: Redis) -> AsyncIterator[dict[str, Any]]:
    """``app.main.app`` through ``httpx.ASGITransport`` with the test databases and Redis; ``as_role`` switches the caller."""
    users = {"QUANT": await _user(sessions, "QUANT"), "ADMIN": await _user(sessions, "ADMIN")}
    caller = {"user": users["QUANT"]}

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    previous_redis = getattr(app.state, "redis", None)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    app.state.redis = redis
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def as_role(role: str) -> None:
        caller["user"] = users[role]

    try:
        yield {"client": client, "as_role": as_role, "users": users, "sessions": sessions, "redis": redis}
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.state.redis = previous_redis


# ================================================================ the API (SQLite and PostgreSQL)
async def test_sports_api_endpoints(api: dict[str, Any]) -> None:
    """Integration test verifying all FastAPI endpoints via httpx ASGITransport."""
    client: httpx.AsyncClient = api["client"]
    # Supported sports list
    resp = await client.get("/api/v1/sports/supported")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 9
    assert all(s["developer_credit"] == DEVELOPER for s in data)

    # Odds calculation endpoint
    calc_payload = {
        "sport_key": "cricket",
        "home_stats": {"batting_rating": 105.0, "bowling_rating": 100.0},
        "away_stats": {"batting_rating": 100.0, "bowling_rating": 100.0},
        "context": {"format": "t20"},
    }
    resp = await client.post("/api/v1/sports/calculate-odds", json=calc_payload)
    assert resp.status_code == 200
    calc_data = resp.json()
    assert calc_data["sport_key"] == "cricket"
    assert "fair_home_odds" in calc_data
    assert calc_data["developer_credit"] == "Amit Ashok Kumar Patnaik"
    assert len(calc_data["sub_models"]) >= 3 and calc_data["kelly_multiplier"] == 0.25

    # Bet resolution endpoint
    resolve_payload = {
        "sport_key": "basketball",
        "market_type": "moneyline",
        "match_result": {"home_score": 112, "away_score": 104},
        "selection": "home",
    }
    resp = await client.post("/api/v1/sports/resolve-bet", json=resolve_payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["is_win"] is True
    assert res_data["status"] == "WON" and res_data["canonical_market"] == "moneyline"
    assert res_data["developer_credit"] == "Amit Ashok Kumar Patnaik"


async def test_catalogue_config_and_leagues(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    configs = (await client.get("/api/v1/sports/config")).json()
    assert len(configs) == 9
    kelly = {c["sport_key"]: c["default_kelly_fraction"] for c in configs}
    assert kelly == {"soccer": 0.35, "cricket": 0.25, "basketball": 0.40, "tennis": 0.30, "baseball": 0.35, "american_football": 0.50,
                     "ice_hockey": 0.30, "combat": 0.20, "esports": 0.25}
    assert all(c["developer_credit"] == DEVELOPER and c["supported_bet_types"] for c in configs)
    async with api["sessions"]() as s:  # seeding is idempotent
        assert await s.scalar(select(func.count()).select_from(SportConfiguration)) == 9
        assert await s.scalar(select(func.count()).select_from(LeagueRegistry)) == len(LEAGUE_SEEDS)

    bet_types = (await client.get("/api/v1/sports/bet-types")).json()
    assert len(bet_types) == 27 and all(bt["sports"] for bt in bet_types)
    teaser = next(bt for bt in bet_types if bt["key"] == "teaser")
    assert set(teaser["sports"]) == {"american_football", "basketball"}

    assert (await client.patch("/api/v1/sports/config/soccer", json={"default_kelly_fraction": 0.3})).status_code == 403
    api["as_role"]("ADMIN")
    resp = await client.patch("/api/v1/sports/config/soccer", json={"default_kelly_fraction": 0.3, "math_model_parameters": {"rho": -0.1}})
    assert resp.status_code == 200 and resp.json()["default_kelly_fraction"] == 0.3
    assert (await client.patch("/api/v1/sports/config/soccer", json={"supported_markets": ["not_a_market"]})).status_code == 422
    assert (await client.patch("/api/v1/sports/config/soccer", json={"default_kelly_fraction": 1.5})).status_code == 422
    assert (await client.patch("/api/v1/sports/config/curling", json={"is_active": False})).status_code == 404

    leagues = (await client.get("/api/v1/sports/leagues/cricket")).json()
    assert {lg["league_key"] for lg in leagues} >= {"cricket_ipl", "cricket_big_bash", "cricket_test_match"}
    assert [lg["tier"] for lg in leagues] == sorted(lg["tier"] for lg in leagues)
    new = {"sport_key": "cricket", "league_key": "cricket_sa20", "display_name": "SA20", "country": "South Africa", "tier": 2}
    created = await client.post("/api/v1/sports/leagues", json=new)
    assert created.status_code == 201 and created.json()["developer_credit"] == DEVELOPER
    assert (await client.post("/api/v1/sports/leagues", json=new)).status_code == 409
    patched = await client.patch("/api/v1/sports/leagues/cricket_sa20", json={"is_active": False})
    assert patched.status_code == 200 and patched.json()["is_active"] is False
    active = (await client.get("/api/v1/sports/leagues/cricket", params={"active_only": True})).json()
    assert "cricket_sa20" not in {lg["league_key"] for lg in active}

    # a deactivated sport refuses to price
    assert (await client.patch("/api/v1/sports/config/esports", json={"is_active": False})).status_code == 200
    blocked = await client.post("/api/v1/sports/calculate-odds",
                                json={"sport_key": "esports", "home_stats": {"team_rating": 1.1}, "away_stats": {"team_rating": 1.0}})
    assert blocked.status_code == 409
    api["as_role"]("QUANT")
    assert (await client.get("/api/v1/sports/providers")).status_code == 403


@pytest.mark.parametrize("sport", SPORT_KEYS)
async def test_calculate_odds_for_every_sport(api: dict[str, Any], sport: str) -> None:
    home, away, ctx = STATS[sport]
    resp = await api["client"].post("/api/v1/sports/calculate-odds", json={"sport_key": sport, "home_stats": home, "away_stats": away, "context": ctx})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sport_key"] == sport and len(body["sub_models"]) >= 3 and body["developer_credit"] == DEVELOPER
    assert abs(body["home_win_prob"] + body["away_win_prob"] + body["draw_prob"] - 1) < 0.002
    assert body["latency_ms"] < 250


async def test_pricing_rejects_bad_requests(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    assert (await client.post("/api/v1/sports/calculate-odds", json={"sport_key": "curling", "home_stats": {}, "away_stats": {}})).status_code == 404
    assert (await client.post("/api/v1/sports/resolve-bet", json={"sport_key": "soccer", "market_type": "most_sixes", "match_result": {},
                                                                    "selection": "home"})).status_code == 422
    no_line = {"sport_key": "soccer", "market_type": "asian_handicap", "match_result": {"home_goals": 1, "away_goals": 0}, "selection": "home"}
    assert (await client.post("/api/v1/sports/resolve-bet", json=no_line)).status_code == 422
    quarter = await client.post("/api/v1/sports/resolve-bet", json={"sport_key": "soccer", "market_type": "asian_handicap", "line": -0.75,
                                                                      "match_result": {"home_goals": 1, "away_goals": 0}, "selection": "home"})
    assert quarter.json()["status"] == "HALF_WON" and quarter.json()["is_win"] is True
    many = {f"k{i}": 1.0 for i in range(201)}
    assert (await client.post("/api/v1/sports/calculate-odds", json={"sport_key": "soccer", "home_stats": many, "away_stats": {}})).status_code == 422


async def test_structure_settlement_endpoint(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    teaser = {"structure": "teaser", "stake": 110, "points": 6.0, "legs": [
        {"leg_id": "a", "sport_key": "american_football", "market_type": "point_spread", "selection": "home", "line": -7.5,
         "match_result": {"home_score": 24, "away_score": 20}},
        {"leg_id": "b", "sport_key": "basketball", "market_type": "point_spread", "selection": "away", "line": 1.5,
         "match_result": {"home_score": 100, "away_score": 99}},
    ]}
    mixed = await client.post("/api/v1/sports/settle-structure", json=teaser)
    assert mixed.status_code == 422  # one teaser, one point tier: American football and basketball do not share one
    teaser["legs"][1].update({"sport_key": "american_football", "match_result": {"home_score": 27, "away_score": 24}})
    resp = await client.post("/api/v1/sports/settle-structure", json=teaser)
    assert resp.status_code == 200 and resp.json()["status"] == "WON" and resp.json()["developer_credit"] == DEVELOPER
    rr = {"structure": "round_robin", "stake": 10, "sizes": [2, 3], "legs": [{"leg_id": c, "odds": 2.0, "status": "WON"} for c in "abc"]}
    body = (await client.post("/api/v1/sports/settle-structure", json=rr)).json()
    assert body["total_stake"] == 40.0 and body["payout"] == pytest.approx(3 * 40 + 80)
    future = {"structure": "futures", "selection": "Mumbai Indians", "odds": 5.0, "stake": 100, "winners": ["mumbai indians"]}
    assert (await client.post("/api/v1/sports/settle-structure", json=future)).json()["payout"] == 500.0
    reverse = {"structure": "action_reverse", "stakes": [100, 100], "legs": [{"leg_id": "a", "odds": 2.0, "status": "WON"},
                                                                             {"leg_id": "b", "odds": 2.0, "status": "WON"}]}
    assert (await client.post("/api/v1/sports/settle-structure", json=reverse)).json()["profit"] == pytest.approx(400)


async def test_evaluate_runs_the_twin_pipeline(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    redis: Redis = api["redis"]
    settings = get_settings()
    fixture = "fx-79"
    weather = {"source": "test", "observed_at": datetime.now(UTC).isoformat(), "indoor": False, "wind_kmh": 12.0, "precipitation_mmh": 0.0,
               "temperature_c": 18.0, "altitude_m": 40.0, "dew_expected": False}
    injuries = {"source": "test", "observed_at": datetime.now(UTC).isoformat(),
                "absences": [{"side": "AWAY", "player": "Striker", "impact": 0.9, "status": "OUT"}],
                "manager_changed_at": {}}
    await redis.hset(f"{settings.TWIN_PREFIX}:intel:{fixture}", mapping={"weather": json.dumps(weather), "injuries": json.dumps(injuries)})
    payload = {"sport_key": "soccer", "fixture_id": fixture, "selection": "home", "bankroll_inr": 100000,
               "home_stats": {"attack_rating": 1.7, "defense_rating": 0.9}, "away_stats": {"attack_rating": 1.1, "defense_rating": 1.25},
               "venue_odds": {"pinnacle": {"home": 1.80, "draw": 3.9, "away": 4.6}, "parimatch": {"home": 1.98, "draw": 3.6, "away": 4.2},
                              "stake": {"home": 1.95, "draw": 3.7, "away": 4.0}, "onexbet": {"home": 1.90, "draw": 3.6, "away": 4.3}}}
    resp = await client.post("/api/v1/sports/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intel_sections"] == ["weather", "injuries"]
    assert body["evidence"]["weather"] and body["evidence"]["absences"]
    assert body["pillar_1_consensus"]["voting"] >= 3
    assert body["pillar_9_sharp_benchmark"]["book"] == "pinnacle"
    assert body["route"]["venue"] == "parimatch"
    assert 0 <= body["confidence"]["score"] <= 100 and body["developer_credit"] == DEVELOPER
    if body["edge_vs_model"] and body["edge_vs_model"] > 0:
        assert body["sizing"]["kelly_multiplier"] == 0.35 and Decimal(body["sizing"]["stake_inr"]) > 0
    no_draw = await client.post("/api/v1/sports/evaluate", json={**payload, "sport_key": "basketball", "selection": "draw",
                                                                 "home_stats": {}, "away_stats": {}, "venue_odds": {}})
    assert no_draw.status_code == 422


async def test_in_play_tick_publishes_the_shield(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    redis: Redis = api["redis"]
    settings = get_settings()
    user = api["users"]["QUANT"]
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"{settings.SPORTS_PREFIX}:inplay:{user.id}")
    await pubsub.get_message(timeout=1.0)
    payload = {"sport_key": "soccer", "fixture_id": "fx-live", "home_stats": {"attack_rating": 1.4, "defense_rating": 1.0},
               "away_stats": {"attack_rating": 1.2, "defense_rating": 1.1}, "live_state": {"minute": 75, "home_goals": 0, "away_goals": 2},
               "selection": "home", "entry_probability": 0.5, "stake_inr": 1000, "entry_odds": 2.0}
    resp = await client.post("/api/v1/sports/in-play/tick", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["published"] is True and body["home_win_prob"] < 0.1
    assert body["shield"]["advice"] == "CASH_OUT" and body["shield"]["rule"] in ("STOP_LOSS_FLOOR", "PROBABILITY_COLLAPSE")
    message = None
    for _ in range(20):
        message = await pubsub.get_message(timeout=0.5)
        if message and message["type"] == "message":
            break
    assert message is not None and json.loads(message["data"])["type"] == "sport_tick"
    assert await redis.hget(f"{settings.SPORTS_PREFIX}:inplay:last:{user.id}", "fx-live") is not None
    await pubsub.aclose()
    holding = await client.post("/api/v1/sports/in-play/tick", json={**payload, "live_state": {"minute": 30, "home_goals": 1, "away_goals": 0}})
    assert holding.json()["shield"]["advice"] == "HOLD"


async def test_discipline_assess_pays_xp_once(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    now = datetime.now(UTC)
    history = [{"placed_at": (now - timedelta(days=2, hours=i)).isoformat(), "sport_key": "soccer", "stake": 1000, "odds": 2.0, "result": "WON", "pnl": 1000}
               for i in range(10)]
    candidate = {"sport_key": "soccer", "stake": 500, "odds": 2.1, "model_probability": 0.53, "bet_ref": "slip-79", "bankroll_inr": 100000}
    first = await client.post("/api/v1/sports/discipline/assess", json={"candidate": candidate, "history": history})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["disciplined"] is True and body["xp_awarded"] == get_settings().XP_AWARD_DISCIPLINED_BET and body["developer_credit"] == DEVELOPER
    again = (await client.post("/api/v1/sports/discipline/assess", json={"candidate": candidate, "history": history})).json()
    assert again["xp_awarded"] == 0
    async with api["sessions"]() as s:
        profile = (await s.execute(select(UserXPProfile).where(UserXPProfile.user_id == api["users"]["QUANT"].id))).scalar_one()
        assert profile.disciplined_bets_count == 1
        assert (await s.execute(select(func.count()).select_from(XPAuditLog).where(XPAuditLog.action_type == "DISCIPLINED_BET"))).scalar_one() == 1
    loss = {"placed_at": (now - timedelta(minutes=20)).isoformat(), "sport_key": "soccer", "stake": 1000, "odds": 2.0, "result": "LOST", "pnl": -1000}
    revenge = [*history, loss]
    tilt = (await client.post("/api/v1/sports/discipline/assess", json={"candidate": {**candidate, "sport_key": "esports", "bet_ref": "slip-80"},
                                                                         "history": revenge})).json()
    assert "REVENGE_NON_CORE" in tilt["flags"] and tilt["xp_awarded"] == 0
    ledger = (await client.post("/api/v1/sports/discipline/assess", json={"candidate": {**candidate, "bet_ref": "slip-81"}})).json()
    assert ledger["core_sports"] == [] and ledger["disciplined"] is True  # an empty ledger: nothing to chase


async def test_vault_first_credentials_never_leave_the_server(api: dict[str, Any]) -> None:
    settings = get_settings().model_copy(update={"master_vault_key": SecretStr(Fernet.generate_key().decode())})
    vault = VaultCrypto.from_settings(settings)
    row_id = uuid.uuid4()
    async with api["sessions"]() as s:
        s.add(VaultProviderCredential(id=row_id, provider_id="odds_api", label="primary", key_digest=uuid.uuid4().hex, api_key_hint="…cret",
                                      encrypted_api_key=vault.encrypt_key("vault-odds-secret", context=provider_context(row_id, "api_key")),
                                      base_url="https://api.the-odds-api.com/v4", is_active=True, source="manual"))
        await s.commit()
        credential = await MultiSportOddsSyncService(settings=settings).resolve_credential(s, "the_odds_api")
    assert credential is not None and credential.source == "vault" and credential.api_key is not None
    assert credential.api_key.get_secret_value() == "vault-odds-secret"
    api["as_role"]("ADMIN")
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        resp = await api["client"].get("/api/v1/sports/providers")
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert resp.status_code == 200
    assert "vault-odds-secret" not in resp.text
    rows = {p["provider"]: p for p in resp.json()}
    assert rows["the_odds_api"]["key_source"] == "vault" and rows["the_odds_api"]["configured"] is True
    assert rows["parimatch"]["role"] == "execution" and rows["parimatch"]["target_url"] == settings.PARIMATCH_TARGET_URL
    assert {"A", "B", "C", "D", "E"} <= {p["category"] for p in rows.values()}


async def test_poll_and_screen_cycle(api: dict[str, Any]) -> None:
    redis: Redis = api["redis"]
    sessions: async_sessionmaker[AsyncSession] = api["sessions"]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.params["apiKey"] == "poll-key" and request.url.params["markets"] == "h2h"
        feed = request.url.path.split("/")[-2]
        events = [ODDS_EVENT] if feed == "soccer_epl" else []
        return httpx.Response(200, json=events, headers={"x-requests-remaining": "450"})

    settings = get_settings().model_copy(update={"ODDS_API_KEY": None, "SPORTS_FEED_MIN_INTERVAL_SECONDS": 900.0})
    service = MultiSportOddsSyncService(api_key="poll-key", settings=settings)
    now = datetime.now(UTC)
    report = await service.poll_consensus(sessions, redis, now, transport=httpx.MockTransport(handler))
    assert "soccer_epl" in report["fetched"] and report["events"] == 1 and report["quota_remaining"] == 450
    assert report["max_normalization_ms"] < settings.SPORTS_NORMALIZATION_BUDGET_MS
    assert len(calls) == report["feed_keys"]  # every active feed key, once
    again = await service.poll_consensus(sessions, redis, now, transport=httpx.MockTransport(handler))
    assert again["fetched"] == [] and again["skipped"] == report["feed_keys"]  # paced by SPORTS_FEED_MIN_INTERVAL_SECONDS

    screened = await service.screen_arbitrage(redis, now)
    assert screened["value_lines"] >= 1 and screened["arbitrages"] >= 1
    resp = await api["client"].get("/api/v1/sports/edges", params={"sport_key": "soccer"})
    assert resp.status_code == 200 and resp.json()["available"] is True
    assert {e["kind"] for e in resp.json()["edges"]} == {"value", "arbitrage"}
    assert (await api["client"].get("/api/v1/sports/edges", params={"sport_key": "basketball"})).json()["edges"] == []

    await redis.flushdb()
    low = MultiSportOddsSyncService(api_key="poll-key", settings=settings.model_copy(update={"ODDS_QUOTA_FLOOR": 1000}))
    stopped = await low.poll_consensus(sessions, redis, now, transport=httpx.MockTransport(handler))
    assert len(stopped["fetched"]) == 1 and "quota" in stopped["reason"]
    keyless = MultiSportOddsSyncService(settings=settings)
    assert "no Odds API key" in (await keyless.poll_consensus(sessions, redis, now))["reason"]


def test_kelly_and_odds_are_finite_for_extreme_inputs() -> None:
    for sport in SPORT_KEYS:
        adapter = SportAdapterRegistry.get_adapter(sport)
        home, away, ctx = STATS[sport]
        lopsided = {k: (v * 1.6 if isinstance(v, float) else v) for k, v in home.items()}
        res = adapter.calculate_fair_odds(lopsided, away, ctx)
        assert all(math.isfinite(x) for x in (res.fair_home_odds, res.fair_away_odds, res.expected_total_score))
