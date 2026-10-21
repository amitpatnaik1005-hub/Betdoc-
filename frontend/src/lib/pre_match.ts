/**
 * The stored pre-match board (backend `app/api/v1/pre_match.py`, Group 101, point b.13a). Developed for Amit Ashok Kumar
 * Patnaik.
 *
 * Reads never reach a provider: the ingestion fleet keeps every Odds API board it buys, one row per fixture, and buys a
 * sport again only when its nearest fixture's tier says so (over 24h out: every 6h; 2-24h: hourly; closer or in play:
 * every minute). Each fixture says its tier, how old its board is and whether that is older than its tier allows.
 * `forcePreMatchRefresh` buys the fixture's sport now, unless it was bought within the cooldown (FRESH: no credit spent)
 * or the fleet's breaker, quota reserve or cache shield says no (the server's reason is toasted).
 */
import { apiClient } from "../api/client";
import { runMutation, useResource } from "./resource";

export type PreMatchWindow = "3h" | "12h" | "24h" | "72h" | "all";
export type PreMatchTier = 1 | 2 | 3;

export interface PreMatchConsensus {
  market: "h2h";
  method: string;
  books: number;
  outcomes: Record<string, { probability: number; fair_odds: number }>;
  sharp: { book: string; overround: number; fair_odds: Record<string, number> } | null;
}

export interface PreMatchFixture {
  id: string;
  match_id: string;
  sport_key: string;
  home_team: string;
  away_team: string;
  commence_time: string;
  /** `{}` when no book quoted a complete h2h market: never filled in. */
  consensus_fair_odds: PreMatchConsensus | Record<string, never>;
  /** h2h prices per book as quoted, keyed home / draw / away. */
  bookmaker_odds: Record<string, Partial<Record<"home" | "draw" | "away", number>>>;
  last_polled_at: string;
  tier: PreMatchTier;
  age_seconds: number;
  max_age_seconds: number;
  is_stale: boolean;
}

export interface PreMatchOutcomeQuote {
  name: string;
  price: number;
  point?: number;
}

export interface PreMatchFixtureDetail extends PreMatchFixture {
  /** Every market the fleet bought, each with its own `polled_at`. */
  markets_json: Record<string, { polled_at: string; books: Record<string, { last_update: string | null; outcomes: PreMatchOutcomeQuote[] }> }>;
}

export interface PreMatchRefresh {
  match_id: string;
  status: "SYNCED" | "FRESH" | "NOT_OFFERED";
  synced_at: string;
  source: "the_odds_api" | "cache";
  detail: string;
  fixture: PreMatchFixtureDetail;
}

const sportParam = (sport: string) => (sport && sport !== "all" ? sport : undefined);

/** The stored fixtures, soonest first; re-read every minute (the store, not the provider). */
export const usePreMatchFixtures = (sport = "all", window: PreMatchWindow = "all", enabled = true) =>
  useResource(
    enabled ? `pre-match:fixtures:${sport}:${window}` : null,
    () => apiClient.get<PreMatchFixture[]>("/pre-match/fixtures", { sport: sportParam(sport), window }),
    { intervalMs: 60_000 },
  );

export const usePreMatchFixture = (matchId: string | null) =>
  useResource(matchId ? `pre-match:fixture:${matchId}` : null, () =>
    apiClient.get<PreMatchFixtureDetail>(`/pre-match/fixtures/${encodeURIComponent(matchId ?? "")}`),
  );

export const forcePreMatchRefresh = (matchId: string) =>
  runMutation(() => apiClient.post<PreMatchRefresh>(`/pre-match/fixtures/${encodeURIComponent(matchId)}/force-refresh`), {
    invalidate: ["pre-match"],
    success: (r) => (r.status === "SYNCED" ? "Board refreshed from The Odds API" : r.detail),
    errorTitle: "Refresh not made",
  });
