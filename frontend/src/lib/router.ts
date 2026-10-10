/** The Smart Order Router (backend `app/api/v1/execution_router.py`, Group 71): routed orders, venue slices, breakers. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type OrderStatus = "ROUTING" | "RESERVED" | "DISPATCHING" | "FILLED" | "PARTIAL" | "UNCONFIRMED" | "REJECTED" | "ABORTED";
export type SliceStatus = "RESERVED" | "DISPATCHED" | "FILLED" | "PARTIAL" | "REJECTED" | "UNKNOWN" | "RELEASED";

export interface RoutedSlice {
  id: string;
  slice_index: number;
  idempotency_key: string;
  client_ref: string;
  venue_id: string;
  account_id: string | null;
  stake: string;
  currency: string;
  quoted_odds: string | null;
  guard_odds: string | null;
  commission: string | null;
  net_ev: string | null;
  status: SliceStatus;
  filled_stake: string;
  matched_odds: string | null;
  remote_bet_id: string | null;
  ledger_id: string | null;
  reason: string | null;
  orphaned: boolean;
  reserved_at: string | null;
  dispatched_at: string | null;
  settled_at: string | null;
  released_at: string | null;
}

export interface RoutedOrder {
  id: string;
  order_id: string;
  match_id: string;
  market: string;
  selection: string;
  odds: string;
  min_acceptable_odds: string;
  max_slippage_pct: string;
  desired_total_stake: string;
  currency: string;
  target_bookmakers: string[];
  status: OrderStatus;
  filled_stake: string;
  blended_odds: string | null;
  reason: string | null;
  detail: Record<string, unknown> & { message?: string };
  hedge_state: string | null;
  receipt_sha256: string | null;
  nalanda_seq: number | null;
  created_at: string | null;
  completed_at: string | null;
  slices: RoutedSlice[];
}

export interface VenueBreaker {
  venue_id: string;
  state: "LIVE" | "PAUSED";
  consecutive_failures: number;
  paused_until: string | null;
  trips: number;
  last_failure_reason: string | null;
  last_failure_at: string | null;
  last_success_at: string | null;
}

export interface VenueBreakers {
  generated_at: string;
  policy: { failures: number; window_seconds: number; pause_seconds: number; orphan_seconds: number };
  venues: VenueBreaker[];
}

export interface RouteRequest {
  order_id: string;
  match_id: string;
  market?: string;
  selection: string;
  odds: string;
  desired_total_stake: string;
  max_slippage_pct?: string;
  min_acceptable_odds: string;
  target_bookmakers: string[];
  true_prob?: string;
}

export const useRoutedOrders = (active: boolean) =>
  useResource<RoutedOrder[]>(`router:orders:${active ? "active" : "all"}`, () => apiClient.get<RoutedOrder[]>("/router/orders", { active, limit: 50 }), { intervalMs: 3_000 });
export const useVenueBreakers = () => useResource<VenueBreakers>("router:venues", () => apiClient.get<VenueBreakers>("/router/venues"), { intervalMs: 5_000 });

export const routeOrder = (body: RouteRequest) => apiClient.post<RoutedOrder>("/router/orders", body);
export const releaseSlice = (id: string) => apiClient.post<RoutedOrder>(`/router/slices/${id}/release`);
export const resetVenue = (venue: string) => apiClient.post<VenueBreaker>(`/router/venues/${encodeURIComponent(venue)}/reset`);
