/**
 * BETDOC SCORE PREDICTION (backend `app/api/v1/score_prediction.py`, Group 102, point b.16). Developed for Amit Ashok
 * Kumar Patnaik.
 *
 * Every number comes from an engine BetDoc already prices with: football scorelines from Dixon-Coles (fitted to the
 * de-vigged market, on your own xG, or the soccer adapter on team stats), cricket innings from the ball-by-ball Markov
 * chain, tennis / basketball / MMA from their adapters. Football markets are keyed exactly as the Oracle board keys its
 * legs ("Match Odds", "Totals 2.5", "Asian Handicap -0.25"), so a predicted score can back a parlay leg.
 */
import { apiClient } from "../api/client";
import type { FixtureCard } from "./backtest_and_parlay";

export type FootballSource = "market" | "xg" | "stats";
export type LegResult = "WON" | "HALF_WON" | "VOID" | "HALF_LOST" | "LOST";

export interface PricedSelection {
  market: string;
  kind: "MATCH_ODDS" | "TOTALS" | "BTTS" | "ASIAN_HANDICAP" | "DOUBLE_CHANCE" | "DRAW_NO_BET";
  line: number | null;
  selection: string;
  win: number;
  half_win: number;
  push: number;
  half_loss: number;
  loss: number;
  win_probability: number;
  fair_odds: number | null;
}

export interface FootballPrediction {
  source: FootballSource;
  expected_goals: [number, number];
  heatmap: number[][];
  heatmap_goals: number;
  /** The heatmap's share of all outcomes; the rest has a side above `heatmap_goals`. */
  heatmap_mass: number;
  top_scorelines: { score: string; home: number; away: number; probability: number }[];
  home_win: number;
  draw: number;
  away_win: number;
  btts_yes: number;
  btts_no: number;
  over: Record<string, number>;
  under: Record<string, number>;
  markets: PricedSelection[];
  rho: number | null;
  fit_error: number | null;
  detail: Record<string, unknown>;
  fixture: { match_id: string; sport_key: string; home_team: string; away_team: string; commence_time: string; last_polled_at: string; books: number } | null;
}

export interface InningsProjection {
  side: "home" | "away";
  median: number;
  interval: [number, number];
  mean: number;
  powerplay_median: number | null;
  powerplay_interval: [number, number] | null;
  phase_expected_runs: Partial<Record<"powerplay" | "middle" | "death", number>>;
  expected_sixes: number;
}

export interface CricketPrediction {
  format: "t20" | "odi";
  overs: number;
  batting_first: "home" | "away";
  first_innings: InningsProjection;
  second_innings: InningsProjection;
  chase: { target: number | null; source: "stated" | "model"; win: number; tie: number; lose: number; win_with_super_over: number | null };
  match_winner: { home: number; away: number; tie: number };
  coverage: number;
  conditions: Record<string, unknown>;
  intel_sections: string[];
}

export interface MarketInput {
  home: number;
  draw: number;
  away: number;
  totals_over?: Record<string, number>;
}

export const predictFootballFromMarket = (market: MarketInput) =>
  apiClient.post<FootballPrediction>("/prediction/football", { source: "market", market });

export const predictFootballFromXg = (home: number, away: number) =>
  apiClient.post<FootballPrediction>("/prediction/football", { source: "xg", xg: { home, away } });

export const predictStoredFixture = (matchId: string) =>
  apiClient.get<FootballPrediction>(`/prediction/football/pre-match/${encodeURIComponent(matchId)}`);

export const predictCricket = (body: { home_stats: Record<string, number>; away_stats: Record<string, number>; context: Record<string, unknown>; chase_target?: number }) =>
  apiClient.post<CricketPrediction>("/prediction/cricket", body);

// --------------------------------------------------------------------------- the Oracle board as fitting targets
/** A workbench card's de-vigged Match Odds and x.5 totals as fitting targets; null without a complete three-way market. */
export function boardTargets(card: FixtureCard): MarketInput | null {
  const mo = card.markets.find((m) => m.market === "Match Odds");
  const p = (sel: string) => mo?.selections.find((s) => s.selection === sel)?.fair_probability;
  const [home, draw, away] = [p("HOME"), p("DRAW"), p("AWAY")];
  if (home === undefined || draw === undefined || away === undefined || ![home, draw, away].every((x) => x > 0 && x < 1)) return null;
  const totals: Record<string, number> = {};
  for (const m of card.markets) {
    const line = /^Totals (\d+\.5)$/.exec(m.market)?.[1];
    const over = m.selections.find((s) => s.selection === "OVER")?.fair_probability;
    if (line && over !== undefined && over > 0 && over < 1) totals[line] = over;
  }
  return { home, draw, away, totals_over: totals };
}

// --------------------------------------------------------------------------- one scoreline against one selection
const sign = (x: number): number => (x > 1e-9 ? 1 : x < -1e-9 ? -1 : 0);

/** An x.25 / x.75 line is two bets on the neighbouring half and whole lines. */
const halves = (line: number): number[] => (Math.round(line * 4) % 2 !== 0 ? [line - 0.25, line + 0.25] : [line]);

const lineResult = (values: number[]): LegResult => {
  if (values.length === 1) return values[0] > 0 ? "WON" : values[0] < 0 ? "LOST" : "VOID";
  const sum = values[0] + values[1];
  return sum === 2 ? "WON" : sum === 1 ? "HALF_WON" : sum === 0 ? "VOID" : sum === -1 ? "HALF_LOST" : "LOST";
};

/** How a selection settles if the match ends `home`-`away` (mirrors the backend's `markets.settle_selection`). */
export function settleAt(p: Pick<PricedSelection, "kind" | "line" | "selection">, home: number, away: number): LegResult {
  const margin = home - away;
  const total = home + away;
  const sel = p.selection;
  switch (p.kind) {
    case "MATCH_ODDS":
      return (sel === "HOME" ? margin > 0 : sel === "AWAY" ? margin < 0 : margin === 0) ? "WON" : "LOST";
    case "BTTS":
      return ((home > 0 && away > 0) === (sel === "YES")) ? "WON" : "LOST";
    case "DOUBLE_CHANCE":
      return (sel === "1X" ? margin >= 0 : sel === "X2" ? margin <= 0 : margin !== 0) ? "WON" : "LOST";
    case "DRAW_NO_BET":
      return margin === 0 ? "VOID" : (sel === "HOME") === (margin > 0) ? "WON" : "LOST";
    case "TOTALS":
      return lineResult(halves(p.line ?? 0).map((l) => sign(sel === "OVER" ? total - l : l - total)));
    case "ASIAN_HANDICAP":  // the line is the HOME side's handicap
      return lineResult(halves(p.line ?? 0).map((l) => sign(sel === "HOME" ? margin + l : -(margin + l))));
  }
}

export const winsAt = (p: Pick<PricedSelection, "kind" | "line" | "selection">, home: number, away: number): boolean => {
  const r = settleAt(p, home, away);
  return r === "WON" || r === "HALF_WON";
};
