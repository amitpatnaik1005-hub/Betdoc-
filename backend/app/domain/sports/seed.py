"""The sports desk's starting catalogue: nine sports and their leagues (Group 79). Developed for Amit Ashok Kumar Patnaik.

Migration ``f94a1206c83e`` inserts a frozen copy of these rows; ``app.services.sports_catalog.ensure_seeded``
inserts any that are missing at run time (a fresh test database, a sport added later). League keys are
The Odds API sport keys wherever the feed carries the competition (``odds_api_sport_key``); competitions it
does not carry keep a descriptive key and a null feed key.
"""

from __future__ import annotations

from typing import Any

from app.domain.sports.twin_bridge import SPORT_KELLY_MULTIPLIERS

# sport_key, display name, primary feed key, standard markets, model parameters (merged into the adapter's context)
SPORT_SEEDS: tuple[tuple[str, str, str, tuple[str, ...], dict[str, Any]], ...] = (
    ("soccer", "Football (Soccer)", "soccer_epl",
     ("1x2_match_odds", "over_under_2_5_goals", "both_teams_to_score", "asian_handicap", "correct_score", "draw_no_bet", "clean_sheet"),
     {"model": "dixon_coles", "rho": -0.08, "home_advantage": 1.25, "covariance": 0.10}),
    ("cricket", "Cricket", "cricket_ipl",
     ("match_winner", "innings_total_runs", "first_6_overs_runs", "most_sixes", "top_batsman_runs"),
     {"model": "dls_markov", "format": "t20", "dew_factor_active": True}),
    ("basketball", "Basketball", "basketball_nba",
     ("moneyline", "point_spread", "game_total_over_under", "team_totals", "quarter_winner"),
     {"model": "four_factors", "home_advantage_points": 2.2}),
    ("tennis", "Tennis", "tennis_atp_us_open",
     ("match_winner", "set_betting_correct_score", "game_handicap", "total_games_over_under", "first_set_winner"),
     {"model": "omalley_barnett_markov", "surface": "medium_hard", "best_of": 3}),
    ("baseball", "Baseball", "baseball_mlb",
     ("moneyline", "run_line_handicap", "game_total_runs", "nrfi_yrfi_first_inning", "first_5_innings_winner"),
     {"model": "baseruns_bsr", "park": "standard"}),
    ("american_football", "American Football", "americanfootball_nfl",
     ("moneyline", "point_spread", "game_total_over_under", "first_half_spread", "team_total_points"),
     {"model": "epa_key_numbers", "home_field_advantage": 1.8}),
    ("ice_hockey", "Ice Hockey", "icehockey_nhl",
     ("moneyline_incl_ot", "puck_line_handicap", "regulation_3_way", "game_total_goals", "empty_net_goal_yes_no"),
     {"model": "xg_corsi_empty_net", "home_ice_advantage": 0.22}),
    ("combat", "MMA / Combat Sports", "mma_mixed_martial_arts",
     ("moneyline", "method_of_victory", "round_totals_over_under", "fight_goes_the_distance", "round_betting"),
     {"model": "slpm_sapm_strike_decay", "rounds": 3}),
    ("esports", "Esports", "esports_cs2",
     ("match_winner_series", "map_handicap", "map_total_over_under", "map_1_winner", "pistol_round_winner"),
     {"model": "hltv_rating_map_veto", "game": "cs2", "series_format": "bo3"}),
)

# sport_key, league_key, display name, country, tier, The Odds API key (None: not carried by the feed)
LEAGUE_SEEDS: tuple[tuple[str, str, str, str, int, str | None], ...] = (
    # soccer
    ("soccer", "soccer_epl", "Premier League", "England", 1, "soccer_epl"),
    ("soccer", "soccer_efl_champ", "EFL Championship", "England", 2, "soccer_efl_champ"),
    ("soccer", "soccer_fa_cup", "FA Cup", "England", 2, "soccer_fa_cup"),
    ("soccer", "soccer_spain_la_liga", "La Liga", "Spain", 1, "soccer_spain_la_liga"),
    ("soccer", "soccer_italy_serie_a", "Serie A", "Italy", 1, "soccer_italy_serie_a"),
    ("soccer", "soccer_germany_bundesliga", "Bundesliga", "Germany", 1, "soccer_germany_bundesliga"),
    ("soccer", "soccer_france_ligue_one", "Ligue 1", "France", 1, "soccer_france_ligue_one"),
    ("soccer", "soccer_netherlands_eredivisie", "Eredivisie", "Netherlands", 2, "soccer_netherlands_eredivisie"),
    ("soccer", "soccer_portugal_primeira_liga", "Primeira Liga", "Portugal", 2, "soccer_portugal_primeira_liga"),
    ("soccer", "soccer_uefa_champs_league", "UEFA Champions League", "Europe", 1, "soccer_uefa_champs_league"),
    ("soccer", "soccer_uefa_europa_league", "UEFA Europa League", "Europe", 2, "soccer_uefa_europa_league"),
    ("soccer", "soccer_usa_mls", "Major League Soccer", "USA", 2, "soccer_usa_mls"),
    ("soccer", "soccer_brazil_campeonato", "Brasileirao Serie A", "Brazil", 2, "soccer_brazil_campeonato"),
    ("soccer", "soccer_argentina_primera_division", "Liga Profesional", "Argentina", 2, "soccer_argentina_primera_division"),
    ("soccer", "soccer_japan_j_league", "J1 League", "Japan", 3, "soccer_japan_j_league"),
    ("soccer", "soccer_india_isl", "Indian Super League", "India", 3, None),
    # cricket
    ("cricket", "cricket_ipl", "Indian Premier League", "India", 1, "cricket_ipl"),
    ("cricket", "cricket_big_bash", "Big Bash League", "Australia", 1, "cricket_big_bash"),
    ("cricket", "cricket_psl", "Pakistan Super League", "Pakistan", 2, "cricket_psl"),
    ("cricket", "cricket_caribbean_premier_league", "Caribbean Premier League", "West Indies", 2, "cricket_caribbean_premier_league"),
    ("cricket", "cricket_t20_blast", "T20 Blast", "England", 2, "cricket_t20_blast"),
    ("cricket", "cricket_odi", "One Day Internationals", "International", 1, "cricket_odi"),
    ("cricket", "cricket_test_match", "Test Matches", "International", 1, "cricket_test_match"),
    ("cricket", "cricket_international_t20", "T20 Internationals", "International", 1, "cricket_international_t20"),
    ("cricket", "cricket_icc_world_cup", "ICC Cricket World Cup", "International", 1, "cricket_icc_world_cup"),
    ("cricket", "cricket_t20_world_cup", "ICC T20 World Cup", "International", 1, "cricket_t20_world_cup"),
    ("cricket", "cricket_the_ashes", "The Ashes Test Series", "International", 1, None),
    # basketball
    ("basketball", "basketball_nba", "NBA", "USA", 1, "basketball_nba"),
    ("basketball", "basketball_wnba", "WNBA", "USA", 2, "basketball_wnba"),
    ("basketball", "basketball_euroleague", "EuroLeague", "Europe", 1, "basketball_euroleague"),
    ("basketball", "basketball_ncaab", "NCAA Men's Basketball", "USA", 2, "basketball_ncaab"),
    ("basketball", "basketball_nbl", "NBL", "Australia", 3, "basketball_nbl"),
    # tennis
    ("tennis", "tennis_atp", "ATP Tour", "International", 1, None),
    ("tennis", "tennis_wta", "WTA Tour", "International", 1, None),
    ("tennis", "tennis_atp_aus_open_singles", "Australian Open (ATP)", "Australia", 1, "tennis_atp_aus_open_singles"),
    ("tennis", "tennis_atp_french_open", "Roland Garros (ATP)", "France", 1, "tennis_atp_french_open"),
    ("tennis", "tennis_wimbledon", "Wimbledon Championships", "United Kingdom", 1, "tennis_atp_wimbledon"),
    ("tennis", "tennis_us_open", "US Open Grand Slam", "USA", 1, "tennis_atp_us_open"),
    ("tennis", "tennis_wta_us_open", "US Open (WTA)", "USA", 1, "tennis_wta_us_open"),
    # baseball
    ("baseball", "baseball_mlb", "MLB", "USA", 1, "baseball_mlb"),
    ("baseball", "baseball_npb", "Nippon Professional Baseball", "Japan", 1, "baseball_npb"),
    ("baseball", "baseball_kbo", "KBO League", "South Korea", 2, "baseball_kbo"),
    ("baseball", "baseball_ncaa", "NCAA Baseball", "USA", 3, "baseball_ncaa"),
    # american football
    ("american_football", "americanfootball_nfl", "NFL", "USA", 1, "americanfootball_nfl"),
    ("american_football", "americanfootball_ncaaf", "NCAA Football (FBS)", "USA", 2, "americanfootball_ncaaf"),
    ("american_football", "americanfootball_cfl", "CFL", "Canada", 3, "americanfootball_cfl"),
    ("american_football", "americanfootball_ufl", "UFL", "USA", 3, "americanfootball_ufl"),
    # ice hockey
    ("ice_hockey", "icehockey_nhl", "NHL", "USA / Canada", 1, "icehockey_nhl"),
    ("ice_hockey", "icehockey_ahl", "AHL", "USA / Canada", 3, "icehockey_ahl"),
    ("ice_hockey", "icehockey_sweden_hockey_league", "SHL", "Sweden", 2, "icehockey_sweden_hockey_league"),
    ("ice_hockey", "icehockey_liiga", "Liiga", "Finland", 2, "icehockey_liiga"),
    ("ice_hockey", "icehockey_khl", "KHL", "International", 2, None),
    # combat
    ("combat", "mma_ufc", "UFC Main Card", "International", 1, "mma_mixed_martial_arts"),
    ("combat", "mma_bellator", "Bellator MMA", "USA", 2, "mma_mixed_martial_arts"),
    ("combat", "mma_pfl", "PFL", "International", 2, "mma_mixed_martial_arts"),
    ("combat", "boxing_world_titles", "World Title Boxing", "International", 1, "boxing_boxing"),
    # esports
    ("esports", "esports_cs2_major", "CS2 Major Championships", "International", 1, None),
    ("esports", "esports_cs2_iem", "IEM / ESL Pro Tour (CS2)", "International", 1, None),
    ("esports", "esports_lol_worlds", "League of Legends Worlds", "International", 1, None),
    ("esports", "esports_dota2_ti", "The International (Dota 2)", "International", 1, None),
    ("esports", "esports_valorant_champions", "Valorant Champions Tour", "International", 1, None),
)


def sport_rows() -> list[dict[str, Any]]:
    from app.domain.sports.registry import SportAdapterRegistry  # noqa: PLC0415 - registry imports every adapter

    rows = []
    for key, name, odds_key, markets, params in SPORT_SEEDS:
        adapter = SportAdapterRegistry.get_adapter(key)
        rows.append({"sport_key": key, "display_name": name, "is_active": True, "default_kelly_fraction": SPORT_KELLY_MULTIPLIERS[key],
                     "min_market_liquidity": 1000.0, "primary_odds_api_sport_key": odds_key, "supported_markets": list(markets),
                     "supported_bet_types": sorted(adapter.BET_TYPES), "math_model_parameters": dict(params)})
    return rows


def league_rows() -> list[dict[str, Any]]:
    return [{"sport_key": s, "league_key": k, "display_name": n, "country": c, "tier": t, "is_active": True, "odds_api_sport_key": o}
            for s, k, n, c, t, o in LEAGUE_SEEDS]
