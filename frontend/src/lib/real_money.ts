/**
 * The real-money vault client (Group 95; its execution desk was retired in Group 97).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc.
 *
 * TODAR MAL's real cash vault (deposits, withdrawals, the cash ledger), the settled ticket history and the
 * Section 115BBJ 30% TDS certificate. Tickets settle on the server from recorded final scores.
 */

import { apiClient } from "../api/client";
import { invalidate, useResource } from "./resource";

export const DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik";

export type RealTicketStatus =
  | "PENDING_PLACEMENT"
  | "PLACED"
  | "WON"
  | "HALF_WON"
  | "LOST"
  | "HALF_LOST"
  | "PUSH"
  | "VOID"
  | "CANCELLED";

export type RealTransactionType =
  | "DEPOSIT"
  | "WITHDRAWAL"
  | "BET_STAKE_LOCKED"
  | "BET_PAYOUT_CREDIT"
  | "TDS_WITHHOLDING_DEBIT"
  | "BET_REFUND"
  | "BET_STAKE_FORFEITED";

export interface RealBankroll {
  user_id: string;
  currency: string;
  cash_balance: number;
  locked_exposure: number;
  total_bankroll: number;
  total_deposited: number;
  total_withdrawn: number;
  realized_pnl: number;
  gross_winnings: number;
  total_tds_deducted: number;
  high_water_mark: number;
  daily_starting_balance: number;
  daily_realized_loss: number;
  daily_drawdown_pct: number;
  daily_drawdown_limit_pct: number;
  consecutive_losses: number;
  loss_streak_limit: number;
  stop_loss_triggered: boolean;
  stop_loss_until: string | null;
  is_in_cooldown: boolean;
  cooldown_seconds_remaining: number;
  max_single_stake: number;
  max_stake_pct: number;
  trading_day: string | null;
  can_bet: boolean;
  developer_name: string;
}

export interface RealBetTicket {
  id: string;
  ticket_reference: string;
  bookmaker: string;
  sport: string;
  league: string;
  match_id: string;
  event_name: string;
  market_type: string;
  selection: string;
  recommended_stake: number;
  placed_stake: number;
  currency: string;
  target_odds: number;
  placed_odds: number | null;
  minimum_profitable_odds: number;
  true_probability: number;
  model_edge_pct: number;
  kelly_fraction: number;
  deep_link_url: string;
  clipboard_payload: string;
  bookmaker_slip_id: string | null;
  quote_market: string | null;
  quote_selection: string | null;
  settleable_from_score: boolean;
  status: RealTicketStatus;
  gross_return: number;
  settlement_payout: number;
  net_profit: number;
  tds_deducted: number;
  vetting_pillar_score: number;
  odds_decay_flag: boolean;
  notes: string | null;
  placed_at: string | null;
  settled_at: string | null;
  created_at: string;
  developer_name: string;
}

export interface RealCashTransaction {
  id: string;
  ticket_id: string | null;
  transaction_type: RealTransactionType;
  amount: number;
  currency: string;
  balance_before: number;
  balance_after: number;
  reference_id: string | null;
  description: string;
  created_at: string;
  developer_name: string;
}

export interface TaxCertificate {
  user_id: string;
  financial_year: string;
  gross_winnings: number;
  total_tds_withheld: number;
  cess_amount: number;
  effective_rate_pct: number;
  total_winning_tickets: number;
  jurisdiction: string;
  section: string;
  issued_at: string;
  developer_name: string;
}

export interface DepositInput {
  amount: number;
  currency?: "INR";
  reference?: string;
  notes?: string | null;
}

export interface WithdrawInput {
  amount: number;
  notes?: string | null;
}

// ========================================================================= API CALLS

export async function fetchRealBankroll(): Promise<RealBankroll> {
  return apiClient.get<RealBankroll>("/real-money/bankroll");
}

export async function depositRealCash(input: DepositInput): Promise<RealBankroll> {
  const result = await apiClient.post<RealBankroll>("/real-money/deposit", input);
  invalidate("real_money");
  return result;
}

export async function withdrawRealCash(input: WithdrawInput): Promise<RealBankroll> {
  const result = await apiClient.post<RealBankroll>("/real-money/withdraw", input);
  invalidate("real_money");
  return result;
}

export async function fetchRealTicketHistory(limit = 50): Promise<RealBetTicket[]> {
  return apiClient.get<RealBetTicket[]>(`/real-money/history?limit=${limit}`);
}

export async function fetchRealCashTransactions(limit = 50): Promise<RealCashTransaction[]> {
  return apiClient.get<RealCashTransaction[]>(`/real-money/transactions?limit=${limit}`);
}

export async function fetchTaxCertificate(fy?: string): Promise<TaxCertificate> {
  const query = fy ? `?fy=${encodeURIComponent(fy)}` : "";
  return apiClient.get<TaxCertificate>(`/real-money/tax-certificate${query}`);
}

// ========================================================================= RESOURCE HOOKS

export function useRealBankroll(enabled = true) {
  return useResource<RealBankroll>(
    enabled ? "real_money:bankroll" : null,
    fetchRealBankroll,
    { intervalMs: 15_000 }
  );
}

export function useRealTicketHistory(enabled = true, limit = 50) {
  return useResource<RealBetTicket[]>(
    enabled ? `real_money:history:${limit}` : null,
    () => fetchRealTicketHistory(limit),
    { intervalMs: 30_000 }
  );
}

export function useRealCashTransactions(enabled = true, limit = 50) {
  return useResource<RealCashTransaction[]>(
    enabled ? `real_money:transactions:${limit}` : null,
    () => fetchRealCashTransactions(limit),
    { intervalMs: 30_000 }
  );
}

export function useTaxCertificate(enabled = true, fy?: string) {
  return useResource<TaxCertificate>(
    enabled ? `real_money:tax_cert:${fy ?? "current"}` : null,
    () => fetchTaxCertificate(fy),
    { intervalMs: 60_000 }
  );
}

