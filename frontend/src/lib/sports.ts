/**
 * The multi-sport engine (backend `app/api/v1/sports_engine.py`, Group 79). Developed for Amit Ashok Kumar Patnaik.
 *
 * Nine sport adapters price a match from their own sub-models (at least three each, blended into one consensus),
 * settle the 27 universal bet types, and feed the twin: pillar 1's consensus verdict, pillar 9's sharp benchmark,
 * OmniRoute's venue and the 0-100 confidence index. The input presets below are each adapter's documented stat keys.
 */
import { apiClient } from "../api/client";
import { useResource } from "./resource";
import type { SportKey } from "../store/useSportStore";

export interface SupportedSport {
  sport_key: SportKey;
  display_name: string;
  standard_markets: string[];
  supported_markets: string[];
  bet_types: string[];
  model_family: string;
  kelly_multiplier: number;
  odds_api_family: string;
  has_draw: boolean;
  developer_credit: string;
}

export interface SportConfig {
  id: string;
  sport_key: SportKey;
  display_name: string;
  is_active: boolean;
  default_kelly_fraction: number;
  min_market_liquidity: number;
  primary_odds_api_sport_key: string;
  supported_markets: string[];
  supported_bet_types: string[];
  math_model_parameters: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  developer_credit: string;
}

export interface League {
  id: string;
  sport_key: SportKey;
  league_key: string;
  display_name: string;
  country: string;
  tier: number;
  is_active: boolean;
  odds_api_sport_key: string | null;
  created_at: string;
}

export interface SubModel {
  name: string;
  home_win_prob: number;
  away_win_prob: number;
  draw_prob: number;
  expected_total: number | null;
  weight: number;
}

export interface MatchProbabilities {
  sport_key: SportKey;
  home_win_prob: number;
  away_win_prob: number;
  draw_prob: number;
  fair_home_odds: number;
  fair_away_odds: number;
  fair_draw_odds: number | null;
  expected_total_score: number;
  sport_specific_metrics: Record<string, unknown>;
  market_probabilities: Record<string, number>;
  sub_models: SubModel[];
  consensus: { model_count?: number; agreement?: number; dispersion?: number; max_deviation?: number };
  evidence: Record<string, boolean>;
  kelly_multiplier: number;
  model_family: string;
  latency_ms: number;
  developer_credit: string;
}

export type Side = "home" | "away" | "draw";

export interface Evaluation {
  sport_key: SportKey;
  selection: Side;
  fair_probability: number;
  fair_odds: number;
  pillar_1_consensus: { status: string; consensus_ev: number; model_evs: Record<string, number>; voting: number; vetoes: string[]; reason: string };
  pillar_9_sharp_benchmark: { book: string; fair_probabilities: Record<string, number>; shin_z: number; overround: number } | null;
  route: { venue: string; odds: number; net_odds: number; reason: string; ranked: { venue: string; net_odds: number }[] } | null;
  edge_vs_model: number | null;
  confidence: { score: number; grade: string; recommendation: string; components: Record<string, number> };
  sizing: { stake_inr: string; fraction: number; full_kelly: number; kelly_multiplier: number; regime: string; venue: string; submit: boolean } | null;
  evidence: Record<string, boolean>;
  intel_sections: string[];
  developer_credit: string;
}

/** A +EV value line from the 30 s screen (Group 96 retired cross-venue arbitrage). */
export interface StoredEdge {
  kind: "value";
  event_id: string;
  sport_key: SportKey;
  feed_key: string;
  home: string;
  away: string;
  commence_time: string;
  outcome?: string;
  venue?: string;
  odds?: number;
  edge_pct?: number;
  benchmark?: string;
}

export interface EdgeBoard {
  screened_at: string | null;
  edges: StoredEdge[];
  available: boolean;
}

export interface StatField {
  key: string;
  label: string;
  step: number;
  home: number;
  away: number;
}

export interface ContextOption {
  key: string;
  label: string;
  options: readonly string[];
  value: string;
}

/** Each adapter's core inputs (the keys its sub-models read) with neutral league-average defaults. */
export const SPORT_FIELDS: Record<SportKey, { stats: StatField[]; context: ContextOption[]; homeLabel: string; awayLabel: string }> = {
  soccer: {
    homeLabel: "Home", awayLabel: "Away",
    stats: [
      { key: "attack_rating", label: "Attack rating", step: 0.05, home: 1.35, away: 1.15 },
      { key: "defense_rating", label: "Defence rating (goals allowed factor)", step: 0.05, home: 1.1, away: 1.2 },
    ],
    context: [],
  },
  cricket: {
    homeLabel: "Home XI", awayLabel: "Away XI",
    stats: [
      { key: "batting_rating", label: "Batting rating", step: 1, home: 100, away: 100 },
      { key: "bowling_rating", label: "Bowling rating", step: 1, home: 100, away: 100 },
    ],
    context: [
      { key: "format", label: "Format", options: ["t20", "odi", "test"], value: "t20" },
      { key: "dew_intensity", label: "Evening dew", options: ["none", "light", "moderate", "heavy"], value: "none" },
    ],
  },
  basketball: {
    homeLabel: "Home", awayLabel: "Away",
    stats: [
      { key: "efg_pct", label: "eFG%", step: 0.005, home: 0.54, away: 0.54 },
      { key: "tov_pct", label: "TOV%", step: 0.005, home: 0.13, away: 0.13 },
      { key: "orb_pct", label: "ORB%", step: 0.005, home: 0.25, away: 0.25 },
      { key: "ftr", label: "FT rate", step: 0.005, home: 0.2, away: 0.2 },
      { key: "pace", label: "Pace", step: 0.5, home: 99.5, away: 99.5 },
      { key: "off_rating", label: "Offensive rating", step: 0.5, home: 114, away: 114 },
      { key: "def_rating", label: "Defensive rating", step: 0.5, home: 114, away: 114 },
    ],
    context: [{ key: "venue_city", label: "Venue", options: ["standard", "denver", "utah"], value: "standard" }],
  },
  tennis: {
    homeLabel: "Player A", awayLabel: "Player B",
    stats: [{ key: "first_serve_win_pct", label: "1st-serve points won", step: 0.01, home: 0.72, away: 0.7 }],
    context: [
      { key: "surface", label: "Surface", options: ["grass", "fast_hard", "medium_hard", "slow_hard", "clay"], value: "medium_hard" },
      { key: "best_of", label: "Best of", options: ["3", "5"], value: "3" },
    ],
  },
  baseball: {
    homeLabel: "Home", awayLabel: "Away",
    stats: [
      { key: "hits_per_9", label: "Hits / 9", step: 0.1, home: 8.2, away: 8.2 },
      { key: "walks_per_9", label: "Walks / 9", step: 0.1, home: 3.1, away: 3.1 },
      { key: "home_runs_per_9", label: "HR / 9", step: 0.1, home: 1.1, away: 1.1 },
      { key: "total_bases_per_9", label: "Total bases / 9", step: 0.1, home: 13.5, away: 13.5 },
      { key: "stolen_bases_per_9", label: "Steals / 9", step: 0.1, home: 0.7, away: 0.7 },
    ],
    context: [{ key: "park", label: "Park", options: ["standard", "coors_field", "great_american", "fenway_park", "yankee_stadium", "dodger_stadium", "petco_park"], value: "standard" }],
  },
  american_football: {
    homeLabel: "Home", awayLabel: "Away",
    stats: [
      { key: "pass_epa", label: "Pass EPA / play", step: 0.01, home: 0.05, away: 0.02 },
      { key: "rush_epa", label: "Rush EPA / play", step: 0.01, home: -0.05, away: -0.07 },
    ],
    context: [],
  },
  ice_hockey: {
    homeLabel: "Home", awayLabel: "Away",
    stats: [
      { key: "expected_goals_per_60", label: "xG / 60", step: 0.05, home: 3.1, away: 2.9 },
      { key: "corsi_for_pct", label: "Corsi For %", step: 0.5, home: 51.5, away: 49 },
    ],
    context: [],
  },
  combat: {
    homeLabel: "Fighter A", awayLabel: "Fighter B",
    stats: [
      { key: "slpm", label: "Sig. strikes landed / min", step: 0.1, home: 4.2, away: 3.8 },
      { key: "sapm", label: "Sig. strikes absorbed / min", step: 0.1, home: 3.1, away: 3.4 },
      { key: "td_avg_per_15", label: "Takedowns / 15 min", step: 0.1, home: 1.8, away: 1.2 },
      { key: "td_def_pct", label: "Takedown defence", step: 0.01, home: 0.7, away: 0.65 },
      { key: "finish_rate", label: "Finish rate", step: 0.01, home: 0.6, away: 0.55 },
    ],
    context: [{ key: "rounds", label: "Rounds", options: ["3", "5"], value: "3" }],
  },
  esports: {
    homeLabel: "Team A", awayLabel: "Team B",
    stats: [{ key: "team_rating", label: "HLTV 2.0 team rating", step: 0.01, home: 1.05, away: 0.98 }],
    context: [
      { key: "series_format", label: "Series", options: ["bo1", "bo3", "bo5"], value: "bo3" },
      { key: "game", label: "Title", options: ["cs2", "valorant", "lol", "dota2"], value: "cs2" },
    ],
  },
};

const NUMERIC_CONTEXT = new Set(["best_of", "rounds"]);

/** A context option's value as the adapter reads it (numbers stay numbers). */
export const contextValue = (key: string, value: string): string | number => (NUMERIC_CONTEXT.has(key) ? Number(value) : value);

export const OMNI_VENUES = ["pinnacle", "betfair", "parimatch", "stake", "1xbet"] as const;
export type OmniVenue = (typeof OMNI_VENUES)[number];

export const useSupportedSports = () => useResource("sports:supported", () => apiClient.get<SupportedSport[]>("/sports/supported"), { intervalMs: 600_000 });
export const useSportConfigs = () => useResource("sports:config", () => apiClient.get<SportConfig[]>("/sports/config"), { intervalMs: 300_000 });
export const useSportLeagues = (sport: SportKey) =>
  useResource(`sports:leagues:${sport}`, () => apiClient.get<League[]>(`/sports/leagues/${sport}`), { intervalMs: 600_000 });
export const useSportEdges = (sport: SportKey) =>
  useResource(`sports:edges:${sport}`, () => apiClient.get<EdgeBoard>("/sports/edges", { sport_key: sport }), { intervalMs: 30_000 });

export const calculateOdds = (body: { sport_key: SportKey; home_stats: Record<string, number>; away_stats: Record<string, number>; context?: Record<string, unknown> }) =>
  apiClient.post<MatchProbabilities>("/sports/calculate-odds", body);

export const evaluateSelection = (body: {
  sport_key: SportKey;
  home_stats: Record<string, number>;
  away_stats: Record<string, number>;
  context?: Record<string, unknown>;
  selection: Side;
  venue_odds: Record<string, Partial<Record<Side, number>>>;
  bankroll_inr: number;
}) => apiClient.post<Evaluation>("/sports/evaluate", body);

export const updateSportConfig = (sport: SportKey, body: Partial<Pick<SportConfig, "is_active" | "default_kelly_fraction" | "min_market_liquidity">>) =>
  apiClient.patch<SportConfig>(`/sports/config/${sport}`, body);

/** "asian_handicap_home_-0.75" -> "Asian handicap home -0.75" */
export const marketLabel = (key: string): string => {
  const text = key.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};
