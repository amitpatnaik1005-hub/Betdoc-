/** Typed endpoints and hooks shared by more than one section. Section-only calls live in their page. */
import { useEffect } from "react";
import { apiClient } from "../api/client";
import { useSystemStore } from "../store/useSystemStore";
import { useResource, runMutation } from "./resource";

// --------------------------------------------------------------------------- dashboard
export interface DashboardSummary {
  total_bankroll: number;
  daily_pnl: number;
  active_bets_count: number;
  win_rate_pct: number;
  current_exposure: number;
  stop_loss_status: string;
}

export const useDashboardSummary = () =>
  useResource("dashboard:summary", () => apiClient.get<DashboardSummary>("/dashboard/summary", { tz_offset_hours: 5.5 }), {
    intervalMs: 20_000,
  });

// --------------------------------------------------------------------------- control panel (system limits)
export interface ControlSettings {
  id: number;
  developer_name: string;
  app_version: string;
  build_info: string;
  theme: "light" | "dark" | "auto";
  accent_color: string;
  reduce_motion: boolean;
  odds_api_key: string | null;
  news_api_key: string | null;
  omniroute_url: string | null;
  bots_enabled: boolean;
  research_frequency_minutes: number;
  default_kelly_fraction: number;
  global_stop_loss: number;
  max_bet_size: number;
  max_daily_exposure: number;
  last_emergency_stop_at: string | null;
  created_at: string;
  updated_at: string;
}

export const isHalted = (s: ControlSettings | undefined): boolean => s !== undefined && s.max_daily_exposure <= 0;

export function useControls() {
  const controls = useResource("system:controls", () => apiClient.get<ControlSettings>("/control-panel"), { intervalMs: 60_000 });
  const data = controls.data;
  useEffect(() => {
    if (data) useSystemStore.getState().setHalted(isHalted(data));
  }, [data]);
  return controls;
}

export const emergencyStop = () =>
  runMutation(() => apiClient.post<ControlSettings>("/control-panel/emergency-stop"), {
    invalidate: ["system", "control-panel", "dashboard", "commanders"],
    success: "Emergency stop engaged: all trading halted",
    errorTitle: "Emergency stop failed",
  });

export const resumeTrading = (maxDailyExposure: number) =>
  runMutation(
    async () => {
      const result = await apiClient.patch<ControlSettings>("/control-panel", { max_daily_exposure: maxDailyExposure, bots_enabled: true });
      useSystemStore.getState().setHalted(false);
      return result;
    },
    {
      invalidate: ["system", "control-panel", "dashboard", "commanders"],
      success: "Trading resumed",
      errorTitle: "Could not resume trading",
    },
  );

// --------------------------------------------------------------------------- live odds (/odds/live)
export interface OddsOutcome {
  name: string;
  price: number;
  point?: number | null;
}
export interface OddsMarket {
  key: string;
  last_update?: string;
  outcomes: OddsOutcome[];
}
export interface OddsBookmaker {
  key: string;
  title: string;
  last_update: string;
  markets: OddsMarket[];
}
export interface MatchOdds {
  id: string;
  sport_key: string;
  commence_time: string;
  home_team: string;
  away_team: string;
  bookmakers: OddsBookmaker[];
}

export type Side = "HOME" | "DRAW" | "AWAY";
export interface BestPrice {
  side: Side;
  price: number;
  bookmaker: string;
}

// The Odds API calls the 1X2 market "h2h" with team-named outcomes; the poller stores it as
// "Match Odds" with HOME / DRAW / AWAY. Both shapes reach the UI.
const MATCH_ODDS_KEYS: ReadonlySet<string> = new Set(["h2h", "match odds", "match_odds", "1x2"]);

/** The bookmaker's head-to-head (1X2) market, whichever name it was stored under. */
export const matchOddsMarket = (book: OddsBookmaker) => book.markets.find((m) => MATCH_ODDS_KEYS.has(m.key.toLowerCase()));

/** HOME / DRAW / AWAY for an outcome named either by team or by side. */
export function outcomeSide(name: string, match: MatchOdds): Side | null {
  const upper = name.toUpperCase();
  if (upper === "HOME" || upper === "DRAW" || upper === "AWAY") return upper;
  if (name === match.home_team) return "HOME";
  if (name === match.away_team) return "AWAY";
  return upper === "X" || upper === "TIE" ? "DRAW" : null;
}

/** Best head-to-head price per side across every bookmaker quoting the match. */
export function bestPrices(match: MatchOdds): Partial<Record<Side, BestPrice>> {
  const best: Partial<Record<Side, BestPrice>> = {};
  for (const book of match.bookmakers) {
    for (const o of matchOddsMarket(book)?.outcomes ?? []) {
      const side = outcomeSide(o.name, match);
      if (!side || !Number.isFinite(o.price)) continue;
      if (!best[side] || o.price > best[side]!.price) best[side] = { side, price: o.price, bookmaker: book.title };
    }
  }
  return best;
}

/** Overround-free probability per side from the best prices (consensus "true odds"). */
export function fairProbabilities(best: Partial<Record<Side, BestPrice>>): Partial<Record<Side, number>> {
  const sides = (Object.keys(best) as Side[]).filter((s) => best[s]);
  const total = sides.reduce((acc, s) => acc + 1 / best[s]!.price, 0);
  return Object.fromEntries(sides.map((s) => [s, 1 / best[s]!.price / total]));
}

export const useLiveOdds = () =>
  useResource("signals:odds-live", () => apiClient.get<MatchOdds[]>("/odds/live"), { intervalMs: 30_000 });

// --------------------------------------------------------------------------- exchange accounts
export interface ExchangeAccount {
  id: string;
  exchange_name: string;
  is_active: boolean;
}

export const useExchanges = () => useResource("exchanges:list", () => apiClient.get<ExchangeAccount[]>("/exchanges"));
