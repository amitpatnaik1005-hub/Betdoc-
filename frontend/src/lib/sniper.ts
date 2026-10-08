/** Omni-Sniper contracts (backend `app/schemas/sniper.py`) and resources. */
import { apiClient } from "../api/client";
import type { Position } from "./cfo";
import { useResource } from "./resource";

export interface VenueSession {
  authenticated: boolean;
  expires_at: string | null;
  seconds_left: number | null;
}

export interface Venue {
  id: string;
  display_name: string;
  adapter: string;
  base_url: string;
  auth_type: string;
  bets_per_second: number;
  burst: number;
  routes: string[];
  is_enabled: boolean;
  is_sandbox: boolean;
  has_credentials: boolean;
  credentials_hint: string | null;
  fixtures_mapped: number;
  session: VenueSession;
}

export interface FeedLine {
  ts: string;
  step: string;
  message: string;
  level: "info" | "success" | "warning" | "error" | string;
  ref?: string | null;
  bookmaker?: string | null;
}

export type ExecutionEvent = "EXECUTED" | "BOOKMAKER_REJECTED" | "EXECUTION_UNKNOWN" | "COMMIT_FAILED";

export interface Execution {
  id: string;
  event: ExecutionEvent;
  reason: string;
  idempotency_key: string | null;
  ledger_id: string | null;
  fixture_id: string | null;
  selection: string | null;
  stake_inr: number | null;
  odds: number | null;
  created_at: string;
  venue_id: string | null;
  remote_bet_id: string | null;
  http_status: number | null;
  latency_ms: number | null;
  matched_odds: string | null;
  request_payload: Record<string, unknown> | null;
  response_payload: unknown;
}

export interface CatalogSync {
  venue_id: string;
  events: number;
  mapped: number;
  unresolved: string[];
}

export const useVenues = () => useResource("cfo:venues", () => apiClient.get<Venue[]>("/omni/venues"), { intervalMs: 20_000 });
export const useExecutions = () => useResource("cfo:executions", () => apiClient.get<Execution[]>("/omni/executions", { limit: 50 }), { intervalMs: 20_000 });
export const useDeadLetters = (all: boolean) =>
  useResource(`cfo:dlq:${all ? "all" : "mine"}`, () => apiClient.get<Position[]>("/omni/dlq", { all }), { intervalMs: 30_000 });
