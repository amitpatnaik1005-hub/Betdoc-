/**
 * The street-smart synthesizer, the system splitter and TODAR MAL's daily market health (Group 115, points b.5, b.10,
 * b.12 and b.16). Backend `app/api/v1/synthesizer.py`. Every leg comes from the live board, priced at Parimatch or 1xBet;
 * money arrives as decimal strings. Nothing here places a bet: the tickets are placed by hand and confirmed with the
 * book's booking code. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import type { HandcraftedPlacement, RealBetTicket } from "./real_money";
import { invalidate, useResource } from "./resource";
import type { TicketPlacement } from "./slip_ornament";

export type DeskBook = "parimatch" | "1xbet";
export type AdvisoryTier = "REST_DAY" | "SELECTIVE" | "PRIME_HARVEST";

export interface SynthesizedLeg {
  leg_id: string;
  fixture_id: string;
  fixture: string;
  home: string;
  away: string;
  sport_key: string | null;
  sport: string;
  league: string | null;
  kickoff: string | null;
  market: string;
  selection: string;
  label: string;
  book: string;
  code: string;
  odds: string;
  fair_probability: number;
  ev: number;
  consensus_books: number;
  prices: Record<string, number>;
  recipe_rationale: string;
}

export interface Synthesis {
  book: DeskBook;
  book_label: string;
  legs: SynthesizedLeg[];
  leg_count: number;
  total_odds: string;
  target_stake_inr: string;
  target_return_inr: string;
  target_odds: string;
  projected_return_inr: string;
  joint_probability: number;
  joint_ev: number;
  expected_return_inr: string;
  reached_target: boolean;
  shortfall: string | null;
  sports_represented: string[];
  is_multi_sport: boolean;
  diversification_relaxed: boolean;
  anchors_available: number;
  rules: Record<string, number>;
  independence_note: string;
  developer_name: string;
}

export interface SplitLeg {
  leg_id: string;
  fixture: string;
  market: string;
  label: string;
  code: string;
  odds: string;
  fair_probability: number;
  sport: string;
}

export interface SplitSlipCard {
  tier: "A" | "B" | "C";
  tier_name: string;
  tag: string;
  leg_count: number;
  stake_inr: string;
  odds: string;
  projected_payout_inr: string;
  win_probability: number;
  ev: number;
  covers_session_stake: boolean;
  legs: SplitLeg[];
  ticket_reference: string | null;
}

export interface SplitScenario {
  code: "ALL_WIN" | "A_AND_B" | "A_ONLY" | "NONE";
  label: string;
  probability: number;
  return_inr: string;
  net_inr: string;
}

export interface SystemSplit {
  book: DeskBook;
  book_label: string;
  total_session_stake_inr: string;
  slip_a_fortress: SplitSlipCard;
  slip_b_growth: SplitSlipCard;
  slip_c_moonshot: SplitSlipCard;
  capital_shield_active: boolean;
  needed_slip_a_odds: string;
  scenarios: SplitScenario[];
  expected_session_return_inr: string;
  recover_stake_probability: number;
  single_ticket: { legs: number; odds: string; payout_inr: string; probability: number; expected_return_inr: string; recover_probability: number };
  formula_explanation: string;
  developer_name: string;
}

export interface RecordedSplit extends SystemSplit {
  status: "RECORDED";
  ticket_references: string[];
  message: string;
}

export interface MarketHealth {
  date: string;
  health_score: number;
  advisory_tier: AdvisoryTier;
  advisory_message: string;
  should_bet: boolean;
  deductions: { code: string; points: number; reason: string }[];
  unmeasured: string[];
  active_fixtures_count: number;
  top_tier_fixtures_count: number | null;
  average_bookmaker_margin_pct: number | null;
  rotation_risks_count: number | null;
  anchors_count: number;
  top_anchors: { leg_id: string; label: string; fixture: string; sport: string; odds: string; book: string; fair_probability: number }[];
  dispatched_at: string | null;
  dispatched_telegram: boolean;
  dispatched_sms: boolean;
  updated_at: string;
  developer_name: string;
}

/** Group 116: the ticket if every leg wins, after the 30% TDS on the net winnings (the server's figures, for the share card) */
export interface TicketProjection {
  stake_inr: string;
  odds: string;
  gross_payout_inr: string;
  net_winnings_inr: string;
  tds_inr: string;
  take_home_inr: string;
}

export type SystemTicket = RealBetTicket & { legs: SplitLeg[]; placement: TicketPlacement; projection: TicketProjection };

export interface SynthesizeInput {
  target_stake_inr: number;
  target_return_inr: number;
  bookmaker: DeskBook;
  max_legs?: number;
  preferred_sports?: string[];
}

export const useMarketHealth = () => useResource("synthesizer:health", () => apiClient.get<MarketHealth>("/synthesizer/market-health"), { intervalMs: 300_000 });
// under real_money, so every real-money write (a placement, a cashout, a settlement) refetches it
export const useSystemTickets = () =>
  useResource("real_money:system_tickets", () => apiClient.get<{ tickets: SystemTicket[] }>("/synthesizer/tickets"), { intervalMs: 60_000 });

export const synthesizeStreetSmart = (input: SynthesizeInput) => apiClient.post<Synthesis>("/synthesizer/synthesize-street-smart", input);

export const splitSystemBets = (legIds: string[], sessionStake: number, bookmaker: DeskBook) =>
  apiClient.post<SystemSplit>("/synthesizer/split-system-bets", { leg_ids: legIds, total_session_stake_inr: sessionStake, bookmaker });

export async function recordSystemTickets(legIds: string[], sessionStake: number, bookmaker: DeskBook): Promise<RecordedSplit> {
  const result = await apiClient.post<RecordedSplit>("/synthesizer/record-system-tickets", { leg_ids: legIds, total_session_stake_inr: sessionStake, bookmaker });
  invalidate("real_money");
  return result;
}

/** A recorded ticket placed by hand: the stake locked against the book's booking code, its legs tracked by the Oracle. */
export async function confirmSystemTicket(reference: string, input: { booking_code: string; placed_odds?: number; placed_stake_inr?: number;
                                                                       override_odds_decay?: boolean }): Promise<HandcraftedPlacement> {
  const result = await apiClient.post<HandcraftedPlacement>(`/real-money/tickets/${encodeURIComponent(reference)}/confirm-handcrafted`, input);
  invalidate("real_money", "oracle");
  return result;
}

export async function cancelSystemTicket(reference: string): Promise<{ ticket_reference: string; status: string }> {
  const result = await apiClient.post<{ ticket_reference: string; status: string }>(`/synthesizer/tickets/${encodeURIComponent(reference)}/cancel`);
  invalidate("real_money");
  return result;
}
