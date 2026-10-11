"""multi-sport engine expansion (Group 79)

Two tables: sports_configurations (one row per sport adapter: active flag, sport-calibrated fractional Kelly,
liquidity floor, primary feed key, markets, universal bet types, model parameters) and sports_leagues_registry
(the competitions under each sport, their feed keys and an optional DLS table override), both seeded below with
a frozen copy of app/domain/sports/seed.py. user_xp_profiles gains disciplined_bets_count and the XP audit's
action check admits DISCIPLINED_BET (FA-2). Developed for Amit Ashok Kumar Patnaik.

Revision ID: f94a1206c83e
Revises: e83f4105a72d
Create Date: 2026-10-11 03:00:00.000000

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f94a1206c83e'
down_revision: Union[str, Sequence[str], None] = 'e83f4105a72d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

XP_ACTIONS_BEFORE = ("SLIP_VETTED", "BET_WON", "LOSS_PREVENTED", "MISTAKE_MEMORIZED", "STREAK_BONUS")
XP_ACTIONS_AFTER = (*XP_ACTIONS_BEFORE, "DISCIPLINED_BET")

# sport_key, display name, Kelly fraction (FA-4), primary feed key, markets, bet types, model parameters
SPORTS = [('soccer',
  'Football (Soccer)',
  0.35,
  'soccer_epl',
  ['1x2_match_odds', 'over_under_2_5_goals', 'both_teams_to_score', 'asian_handicap', 'correct_score', 'draw_no_bet', 'clean_sheet'],
  ['alternative_lines',
   'asian_handicap',
   'btts',
   'clean_sheet',
   'correct_score',
   'double_action',
   'draw_no_bet',
   'futures',
   'game_total',
   'if_bet',
   'live_next_event',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin',
   'team_total'],
  {'model': 'dixon_coles', 'rho': -0.08, 'home_advantage': 1.25, 'covariance': 0.1}),
 ('cricket',
  'Cricket',
  0.25,
  'cricket_ipl',
  ['match_winner', 'innings_total_runs', 'first_6_overs_runs', 'most_sixes', 'top_batsman_runs'],
  ['alternative_lines',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'inning_of_highest_score',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin',
   'team_total'],
  {'model': 'dls_markov', 'format': 't20', 'dew_factor_active': True}),
 ('basketball',
  'Basketball',
  0.4,
  'basketball_nba',
  ['moneyline', 'point_spread', 'game_total_over_under', 'team_totals', 'quarter_winner'],
  ['alternative_lines',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'live_next_event',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'pleaser',
   'point_spread',
   'round_robin',
   'team_total',
   'teaser'],
  {'model': 'four_factors', 'home_advantage_points': 2.2}),
 ('tennis',
  'Tennis',
  0.3,
  'tennis_atp_us_open',
  ['match_winner', 'set_betting_correct_score', 'game_handicap', 'total_games_over_under', 'first_set_winner'],
  ['alternative_lines',
   'correct_score',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin'],
  {'model': 'omalley_barnett_markov', 'surface': 'medium_hard', 'best_of': 3}),
 ('baseball',
  'Baseball',
  0.35,
  'baseball_mlb',
  ['moneyline', 'run_line_handicap', 'game_total_runs', 'nrfi_yrfi_first_inning', 'first_5_innings_winner'],
  ['alternative_lines',
   'double_action',
   'first_5_innings',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'inning_of_highest_score',
   'live_next_event',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin',
   'team_total'],
  {'model': 'baseruns_bsr', 'park': 'standard'}),
 ('american_football',
  'American Football',
  0.5,
  'americanfootball_nfl',
  ['moneyline', 'point_spread', 'game_total_over_under', 'first_half_spread', 'team_total_points'],
  ['alternative_lines',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'live_next_event',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'pleaser',
   'point_spread',
   'round_robin',
   'team_total',
   'teaser'],
  {'model': 'epa_key_numbers', 'home_field_advantage': 1.8}),
 ('ice_hockey',
  'Ice Hockey',
  0.3,
  'icehockey_nhl',
  ['moneyline_incl_ot', 'puck_line_handicap', 'regulation_3_way', 'game_total_goals', 'empty_net_goal_yes_no'],
  ['alternative_lines',
   'btts',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'live_next_event',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin',
   'team_total'],
  {'model': 'xg_corsi_empty_net', 'home_ice_advantage': 0.22}),
 ('combat',
  'MMA / Combat Sports',
  0.2,
  'mma_mixed_martial_arts',
  ['moneyline', 'method_of_victory', 'round_totals_over_under', 'fight_goes_the_distance', 'round_betting'],
  ['alternative_lines',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'method_of_victory',
   'moneyline',
   'parlay',
   'player_props',
   'round_betting',
   'round_robin'],
  {'model': 'slpm_sapm_strike_decay', 'rounds': 3}),
 ('esports',
  'Esports',
  0.25,
  'esports_cs2',
  ['match_winner_series', 'map_handicap', 'map_total_over_under', 'map_1_winner', 'pistol_round_winner'],
  ['alternative_lines',
   'correct_score',
   'double_action',
   'futures',
   'game_props',
   'game_total',
   'if_bet',
   'map_winner',
   'moneyline',
   'parlay',
   'period_lines',
   'player_props',
   'point_spread',
   'round_robin',
   'total_maps'],
  {'model': 'hltv_rating_map_veto', 'game': 'cs2', 'series_format': 'bo3'})]
# END SPORTS

# sport_key, league_key, display name, country, tier, The Odds API key
LEAGUES = [('soccer', 'soccer_epl', 'Premier League', 'England', 1, 'soccer_epl'),
 ('soccer', 'soccer_efl_champ', 'EFL Championship', 'England', 2, 'soccer_efl_champ'),
 ('soccer', 'soccer_fa_cup', 'FA Cup', 'England', 2, 'soccer_fa_cup'),
 ('soccer', 'soccer_spain_la_liga', 'La Liga', 'Spain', 1, 'soccer_spain_la_liga'),
 ('soccer', 'soccer_italy_serie_a', 'Serie A', 'Italy', 1, 'soccer_italy_serie_a'),
 ('soccer', 'soccer_germany_bundesliga', 'Bundesliga', 'Germany', 1, 'soccer_germany_bundesliga'),
 ('soccer', 'soccer_france_ligue_one', 'Ligue 1', 'France', 1, 'soccer_france_ligue_one'),
 ('soccer', 'soccer_netherlands_eredivisie', 'Eredivisie', 'Netherlands', 2, 'soccer_netherlands_eredivisie'),
 ('soccer', 'soccer_portugal_primeira_liga', 'Primeira Liga', 'Portugal', 2, 'soccer_portugal_primeira_liga'),
 ('soccer', 'soccer_uefa_champs_league', 'UEFA Champions League', 'Europe', 1, 'soccer_uefa_champs_league'),
 ('soccer', 'soccer_uefa_europa_league', 'UEFA Europa League', 'Europe', 2, 'soccer_uefa_europa_league'),
 ('soccer', 'soccer_usa_mls', 'Major League Soccer', 'USA', 2, 'soccer_usa_mls'),
 ('soccer', 'soccer_brazil_campeonato', 'Brasileirao Serie A', 'Brazil', 2, 'soccer_brazil_campeonato'),
 ('soccer', 'soccer_argentina_primera_division', 'Liga Profesional', 'Argentina', 2, 'soccer_argentina_primera_division'),
 ('soccer', 'soccer_japan_j_league', 'J1 League', 'Japan', 3, 'soccer_japan_j_league'),
 ('soccer', 'soccer_india_isl', 'Indian Super League', 'India', 3, None),
 ('cricket', 'cricket_ipl', 'Indian Premier League', 'India', 1, 'cricket_ipl'),
 ('cricket', 'cricket_big_bash', 'Big Bash League', 'Australia', 1, 'cricket_big_bash'),
 ('cricket', 'cricket_psl', 'Pakistan Super League', 'Pakistan', 2, 'cricket_psl'),
 ('cricket', 'cricket_caribbean_premier_league', 'Caribbean Premier League', 'West Indies', 2, 'cricket_caribbean_premier_league'),
 ('cricket', 'cricket_t20_blast', 'T20 Blast', 'England', 2, 'cricket_t20_blast'),
 ('cricket', 'cricket_odi', 'One Day Internationals', 'International', 1, 'cricket_odi'),
 ('cricket', 'cricket_test_match', 'Test Matches', 'International', 1, 'cricket_test_match'),
 ('cricket', 'cricket_international_t20', 'T20 Internationals', 'International', 1, 'cricket_international_t20'),
 ('cricket', 'cricket_icc_world_cup', 'ICC Cricket World Cup', 'International', 1, 'cricket_icc_world_cup'),
 ('cricket', 'cricket_t20_world_cup', 'ICC T20 World Cup', 'International', 1, 'cricket_t20_world_cup'),
 ('cricket', 'cricket_the_ashes', 'The Ashes Test Series', 'International', 1, None),
 ('basketball', 'basketball_nba', 'NBA', 'USA', 1, 'basketball_nba'),
 ('basketball', 'basketball_wnba', 'WNBA', 'USA', 2, 'basketball_wnba'),
 ('basketball', 'basketball_euroleague', 'EuroLeague', 'Europe', 1, 'basketball_euroleague'),
 ('basketball', 'basketball_ncaab', "NCAA Men's Basketball", 'USA', 2, 'basketball_ncaab'),
 ('basketball', 'basketball_nbl', 'NBL', 'Australia', 3, 'basketball_nbl'),
 ('tennis', 'tennis_atp', 'ATP Tour', 'International', 1, None),
 ('tennis', 'tennis_wta', 'WTA Tour', 'International', 1, None),
 ('tennis', 'tennis_atp_aus_open_singles', 'Australian Open (ATP)', 'Australia', 1, 'tennis_atp_aus_open_singles'),
 ('tennis', 'tennis_atp_french_open', 'Roland Garros (ATP)', 'France', 1, 'tennis_atp_french_open'),
 ('tennis', 'tennis_wimbledon', 'Wimbledon Championships', 'United Kingdom', 1, 'tennis_atp_wimbledon'),
 ('tennis', 'tennis_us_open', 'US Open Grand Slam', 'USA', 1, 'tennis_atp_us_open'),
 ('tennis', 'tennis_wta_us_open', 'US Open (WTA)', 'USA', 1, 'tennis_wta_us_open'),
 ('baseball', 'baseball_mlb', 'MLB', 'USA', 1, 'baseball_mlb'),
 ('baseball', 'baseball_npb', 'Nippon Professional Baseball', 'Japan', 1, 'baseball_npb'),
 ('baseball', 'baseball_kbo', 'KBO League', 'South Korea', 2, 'baseball_kbo'),
 ('baseball', 'baseball_ncaa', 'NCAA Baseball', 'USA', 3, 'baseball_ncaa'),
 ('american_football', 'americanfootball_nfl', 'NFL', 'USA', 1, 'americanfootball_nfl'),
 ('american_football', 'americanfootball_ncaaf', 'NCAA Football (FBS)', 'USA', 2, 'americanfootball_ncaaf'),
 ('american_football', 'americanfootball_cfl', 'CFL', 'Canada', 3, 'americanfootball_cfl'),
 ('american_football', 'americanfootball_ufl', 'UFL', 'USA', 3, 'americanfootball_ufl'),
 ('ice_hockey', 'icehockey_nhl', 'NHL', 'USA / Canada', 1, 'icehockey_nhl'),
 ('ice_hockey', 'icehockey_ahl', 'AHL', 'USA / Canada', 3, 'icehockey_ahl'),
 ('ice_hockey', 'icehockey_sweden_hockey_league', 'SHL', 'Sweden', 2, 'icehockey_sweden_hockey_league'),
 ('ice_hockey', 'icehockey_liiga', 'Liiga', 'Finland', 2, 'icehockey_liiga'),
 ('ice_hockey', 'icehockey_khl', 'KHL', 'International', 2, None),
 ('combat', 'mma_ufc', 'UFC Main Card', 'International', 1, 'mma_mixed_martial_arts'),
 ('combat', 'mma_bellator', 'Bellator MMA', 'USA', 2, 'mma_mixed_martial_arts'),
 ('combat', 'mma_pfl', 'PFL', 'International', 2, 'mma_mixed_martial_arts'),
 ('combat', 'boxing_world_titles', 'World Title Boxing', 'International', 1, 'boxing_boxing'),
 ('esports', 'esports_cs2_major', 'CS2 Major Championships', 'International', 1, None),
 ('esports', 'esports_cs2_iem', 'IEM / ESL Pro Tour (CS2)', 'International', 1, None),
 ('esports', 'esports_lol_worlds', 'League of Legends Worlds', 'International', 1, None),
 ('esports', 'esports_dota2_ti', 'The International (Dota 2)', 'International', 1, None),
 ('esports', 'esports_valorant_champions', 'Valorant Champions Tour', 'International', 1, None)]
# END LEAGUES


def _action_check(actions: tuple[str, ...]) -> str:
    return "action_type IN (" + ", ".join(f"'{a}'" for a in actions) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('sports_configurations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('sport_key', sa.String(length=32), nullable=False),
    sa.Column('display_name', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('default_kelly_fraction', sa.Float(), nullable=False),
    sa.Column('min_market_liquidity', sa.Float(), nullable=False),
    sa.Column('primary_odds_api_sport_key', sa.String(length=64), nullable=False),
    sa.Column('supported_markets', sa.JSON(), nullable=False),
    sa.Column('supported_bet_types', sa.JSON(), nullable=False),
    sa.Column('math_model_parameters', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('default_kelly_fraction > 0 AND default_kelly_fraction <= 1', name=op.f('ck_sports_configurations_kelly_bounded')),
    sa.CheckConstraint('length(sport_key) > 0', name=op.f('ck_sports_configurations_sport_key_not_empty')),
    sa.CheckConstraint('min_market_liquidity >= 0', name=op.f('ck_sports_configurations_liquidity_not_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sports_configurations'))
    )
    op.create_index(op.f('ix_sports_configurations_sport_key'), 'sports_configurations', ['sport_key'], unique=True)
    op.create_table('sports_leagues_registry',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('sport_key', sa.String(length=32), nullable=False),
    sa.Column('league_key', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.String(length=128), nullable=False),
    sa.Column('country', sa.String(length=64), nullable=False),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('odds_api_sport_key', sa.String(length=64), nullable=True),
    sa.Column('dls_resource_table_override', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(league_key) > 0', name=op.f('ck_sports_leagues_registry_league_key_not_empty')),
    sa.CheckConstraint('tier >= 1 AND tier <= 5', name=op.f('ck_sports_leagues_registry_tier_bounded')),
    sa.ForeignKeyConstraint(['sport_key'], ['sports_configurations.sport_key'], name=op.f('fk_sports_leagues_registry_sport_key_sports_configurations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sports_leagues_registry'))
    )
    op.create_index(op.f('ix_sports_leagues_registry_league_key'), 'sports_leagues_registry', ['league_key'], unique=True)
    op.create_index(op.f('ix_sports_leagues_registry_sport_key'), 'sports_leagues_registry', ['sport_key'], unique=False)
    op.add_column('user_xp_profiles', sa.Column('disciplined_bets_count', sa.Integer(), server_default='0', nullable=False))
    op.drop_constraint(op.f('ck_user_xp_audit_logs_action_known'), 'user_xp_audit_logs', type_='check')
    op.create_check_constraint(op.f('ck_user_xp_audit_logs_action_known'), 'user_xp_audit_logs', _action_check(XP_ACTIONS_AFTER))

    now = datetime.now(timezone.utc)
    sports_table = sa.table(
        'sports_configurations',
        sa.column('id', sa.String), sa.column('sport_key', sa.String), sa.column('display_name', sa.String), sa.column('is_active', sa.Boolean),
        sa.column('default_kelly_fraction', sa.Float), sa.column('min_market_liquidity', sa.Float), sa.column('primary_odds_api_sport_key', sa.String),
        sa.column('supported_markets', sa.JSON), sa.column('supported_bet_types', sa.JSON), sa.column('math_model_parameters', sa.JSON),
        sa.column('created_at', sa.DateTime(timezone=True)), sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    op.bulk_insert(sports_table, [{
        'id': str(uuid.uuid4()), 'sport_key': key, 'display_name': name, 'is_active': True, 'default_kelly_fraction': kelly,
        'min_market_liquidity': 1000.0, 'primary_odds_api_sport_key': feed, 'supported_markets': list(markets),
        'supported_bet_types': list(bet_types), 'math_model_parameters': dict(params), 'created_at': now, 'updated_at': now,
    } for key, name, kelly, feed, markets, bet_types, params in SPORTS])

    leagues_table = sa.table(
        'sports_leagues_registry',
        sa.column('id', sa.String), sa.column('sport_key', sa.String), sa.column('league_key', sa.String), sa.column('display_name', sa.String),
        sa.column('country', sa.String), sa.column('tier', sa.Integer), sa.column('is_active', sa.Boolean), sa.column('odds_api_sport_key', sa.String),
        sa.column('created_at', sa.DateTime(timezone=True)), sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    op.bulk_insert(leagues_table, [{
        'id': str(uuid.uuid4()), 'sport_key': sport, 'league_key': league, 'display_name': name, 'country': country, 'tier': tier,
        'is_active': True, 'odds_api_sport_key': feed, 'created_at': now, 'updated_at': now,
    } for sport, league, name, country, tier, feed in LEAGUES])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DELETE FROM user_xp_audit_logs WHERE action_type = 'DISCIPLINED_BET'"))
    op.drop_constraint(op.f('ck_user_xp_audit_logs_action_known'), 'user_xp_audit_logs', type_='check')
    op.create_check_constraint(op.f('ck_user_xp_audit_logs_action_known'), 'user_xp_audit_logs', _action_check(XP_ACTIONS_BEFORE))
    op.drop_column('user_xp_profiles', 'disciplined_bets_count')
    op.drop_index(op.f('ix_sports_leagues_registry_sport_key'), table_name='sports_leagues_registry')
    op.drop_index(op.f('ix_sports_leagues_registry_league_key'), table_name='sports_leagues_registry')
    op.drop_table('sports_leagues_registry')
    op.drop_index(op.f('ix_sports_configurations_sport_key'), table_name='sports_configurations')
    op.drop_table('sports_configurations')
