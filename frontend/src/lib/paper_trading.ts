/**
 * The Paper Trading Sandbox and TODAR MAL's currency desk (backend `app/api/v1/paper_trading.py`, Group 80).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Every paper slip is executed by ASHVATTHAMA on the target book (Parimatch by default) at the live price it can get,
 * settled by the Group 79 sport adapters and taxed under Section 115BBJ. Money is INR on the wire; the display
 * currency is converted with TODAR MAL's rate board, never a guessed constant.
 */
import { apiClient } from "../api/client";
import { invalidate, useResource } from "./resource";

export const DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik";
export const SUPPORTED_CURRENCIES = ["INR", "USD", "EUR", "GBP", "USDT", "BTC"] as const;
export type CurrencyCode = (typeof SUPPORTED_CURRENCIES)[number];
export const CURRENCY_LABELS: Record<CurrencyCode, { symbol: string; name: string }> = {
  INR: { symbol: "₹", name: "Indian Rupee (Parimatch primary)" },
  USD: { symbol: "$", name: "US Dollar (Pinnacle benchmark)" },
  EUR: { symbol: "€", name: "Euro (Betfair Exchange)" },
  GBP: { symbol: "£", name: "British Pound" },
  USDT: { symbol: "₮", name: "Tether (Stake crypto)" },
  BTC: { symbol: "₿", name: "Bitcoin" },
};
export const PAPER_VENUES = ["parimatch", "pinnacle", "betfair", "stake", "1xbet"] as const;
export type PaperVenue = (typeof PAPER_VENUES)[number];

export type PaperBetStatus = "PENDING" | "WON" | "LOST" | "PUSH" | "VOID" | "HALF_WON" | "HALF_LOST" | "CASHED_OUT";

export interface PaperLeg {
  sport_key: string;
  match_id: string;
  event_name: string;
  market_type: string;
  selection: string;
  line: number | null;
  odds: number;
  executed_odds: number;
  status: string;
  quote_market: string | null;
  quote_selection: string | null;
}

export interface ExecutionReceipt {
  commander: string;
  mode: "PAPER" | "LIVE";
  status: "FILLED" | "REJECTED" | "TICKET";
  venue: string;
  venue_url: string | null;
  requested_odds: string;
  executed_odds: string | null;
  live_odds: string | null;
  slippage_pct: number | null;
  price_source: string;
  quote_age_seconds: number | null;
  router_latency_ms: number;
  venue_latency_ms: number;
  total_latency_ms: number;
  slip_ref: string;
  reason: string | null;
  legs: Record<string, unknown>[];
}

export interface PaperBet {
  id: string;
  account_id: string;
  client_ref: string;
  structure: "SINGLE" | "PARLAY";
  sport_key: string;
  match_id: string;
  event_name: string;
  market_type: string;
  selection: string;
  line: number | null;
  legs: PaperLeg[];
  odds: number;
  executed_odds: number;
  stake_amount: number;
  potential_payout: number;
  status: PaperBetStatus;
  payout: number | null;
  pnl: number;
  tds_amount: number;
  net_post_tax_payout: number | null;
  target_bookmaker: string;
  source: "MANUAL" | "ASHOKA" | "SPORTS_ENGINE" | "BACKTEST";
  strategy: string | null;
  fair_probability: number | null;
  confidence_score: number | null;
  kelly_stake: number | null;
  execution: ExecutionReceipt | null;
  discipline: { disciplined?: boolean; flags?: string[]; score?: number; xp_awarded?: number } | null;
  placed_at: string;
  settled_at: string | null;
  settlement_notes: string | null;
  created: boolean;
  developer_credit: string;
}

export interface PaperAccount {
  id: string;
  user_id: string;
  currency: "INR";
  virtual_balance: number;
  initial_balance: number;
  total_paper_bets: number;
  won_bets: number;
  lost_bets: number;
  push_bets: number;
  cashed_out_bets: number;
  total_staked: number;
  total_returned: number;
  total_pnl: number;
  roi_pct: number;
  total_tds: number;
  peak_equity: number;
  max_drawdown_pct: number;
  resets: number;
  is_active: boolean;
  last_reset_at: string | null;
  created_at: string;
  developer_credit: string;
}

export interface Bucket {
  bets: number;
  won: number;
  lost: number;
  staked: number;
  pnl: number;
  win_rate_pct: number;
  yield_pct: number;
}

export interface PaperPerformance {
  virtual_balance: number;
  initial_balance: number;
  equity: number;
  total_pnl: number;
  net_pnl_after_tds: number;
  total_tds: number;
  roi_pct: number;
  yield_pct: number;
  win_rate_pct: number;
  total_bets: number;
  won_bets: number;
  lost_bets: number;
  push_bets: number;
  cashed_out_bets: number;
  pending_bets: number;
  open_exposure: number;
  max_drawdown_pct: number;
  profit_factor: number | null;
  sharpe_ratio: number | null;
  brier_score: number | null;
  significance: { sample: number; mean_return: number; std_return: number; t_stat: number | null; p_value: number | null; proven: boolean; verdict: string };
  status_summary: "BUILDING STATISTICAL SAMPLE" | "PROFITABLE FORWARD-TEST" | "PROFITABLE, NOT YET SIGNIFICANT" | "CALIBRATING ADVICE";
  live_ready: boolean;
  by_sport: Record<string, Bucket>;
  by_strategy: Record<string, Bucket>;
  equity_curve: { at: string; equity: number }[];
  epoch: number;
  developer_credit: string;
}

export interface TaxEstimate {
  gross_win: number;
  stake: number;
  gross_profit: number;
  tds_rate_pct: number;
  tds_deducted: number;
  net_take_home: number;
  cess: number;
  effective_rate_pct: number;
  currency: string;
  jurisdiction: string;
  compliance_notes: string;
  developer_credit: string;
}

export interface TaxLedgerEntry {
  id: string;
  paper_bet_id: string | null;
  mode: "PAPER" | "LIVE";
  gross_payout: number;
  stake: number;
  net_winnings: number;
  tds_rate: number;
  tds_amount: number;
  net_post_tax_payout: number;
  currency: string;
  jurisdiction: string;
  section: string;
  financial_year: string;
  created_at: string;
}

export interface TaxLedger {
  entries: TaxLedgerEntry[];
  by_financial_year: Record<string, { entries: number; net_winnings: number; tds: number; gross_payout: number }>;
  developer_credit: string;
}

export interface FxBoard {
  base: "INR";
  rates: Record<string, { inr_per_unit: number; as_of: string; provider: string; symbol: string }>;
  missing: Record<string, string>;
  max_age_seconds: number;
  developer_credit: string;
}

export interface Conversion {
  amount: number;
  from_currency: string;
  to_currency: string;
  converted: number;
  rate: number;
  last_updated_at: string;
  provider: string;
}

export interface LiveCheck {
  bet_id: string;
  sport_key: string;
  side: string;
  home_win_prob: number;
  away_win_prob: number;
  draw_prob: number;
  live_probability: number;
  alert: boolean;
  alert_threshold: number;
  shield: { advice: "HOLD" | "CASH_OUT"; fair_value_inr: string; reason: string | null; rule: string | null };
  message: string;
}

export interface PaperLegInput {
  sport_key: string;
  match_id: string;
  event_name?: string;
  market_type: string;
  selection: string;
  odds: number;
  line?: number | null;
  quote_market?: string | null;
  quote_selection?: string | null;
}

export interface PaperBetInput extends Partial<PaperLegInput> {
  legs?: PaperLegInput[];
  stake_amount: number;
  target_bookmaker?: string;
  source?: PaperBet["source"];
  strategy?: string | null;
  fair_probability?: number | null;
  confidence_score?: number | null;
  client_ref?: string | null;
}

export interface OracleSlipInput {
  legs: { sport_key: string | null; match_id: string; market: string; selection: string; odds: number; event_name?: string }[];
  stake_amount: number;
  target_bookmaker?: string;
  fair_probability?: number | null;
  confidence_score?: number | null;
  strategy?: string;
  slip_id?: string | null;
  event_name?: string | null;
}

const BASE = "/paper-trading";

// ------------------------------------------------------------------ reads
export const usePaperAccount = (enabled = true) =>
  useResource(enabled ? "paper:account" : null, () => apiClient.get<PaperAccount>(`${BASE}/account`), { intervalMs: 60_000 });
export const usePaperPerformance = (enabled = true) =>
  useResource(enabled ? "paper:performance" : null, () => apiClient.get<PaperPerformance>(`${BASE}/performance`), { intervalMs: 60_000 });
export const usePaperBets = (status?: PaperBetStatus | "") =>
  useResource(`paper:bets:${status || "all"}`, () => apiClient.get<PaperBet[]>(`${BASE}/bets`, { status: status || undefined, limit: 200 }), { intervalMs: 60_000 });
export const useTaxLedger = (financialYear?: string) =>
  useResource(`paper:tax:${financialYear || "all"}`, () => apiClient.get<TaxLedger>(`${BASE}/tax/ledger`, { financial_year: financialYear || undefined }));
export const useFxBoard = (enabled = true) =>
  useResource(enabled ? "fx:board" : null, () => apiClient.get<FxBoard>(`${BASE}/currency/board`), { intervalMs: 900_000 });

// ------------------------------------------------------------------ writes (each one refreshes the sandbox's resources)
const settle = async <T>(work: Promise<T>): Promise<T> => {
  const out = await work;
  invalidate("paper");
  return out;
};

export const placePaperBet = (input: PaperBetInput) => settle(apiClient.post<PaperBet>(`${BASE}/place-bet`, input));
export const placeOracleSlip = (input: OracleSlipInput) => settle(apiClient.post<PaperBet>(`${BASE}/place-oracle-slip`, input));
export const resetPaperAccount = (initialBalance?: number) =>
  settle(apiClient.post<PaperAccount>(`${BASE}/reset`, initialBalance ? { initial_balance: initialBalance } : {}));
export const settlePaperBet = (betId: string, matchResult: Record<string, unknown>, notes?: string) =>
  settle(apiClient.post<PaperBet>(`${BASE}/bets/${betId}/settle`, { match_result: matchResult, settlement_notes: notes || undefined }));
export const cashOutPaperBet = (betId: string, amount: number) => settle(apiClient.post<PaperBet>(`${BASE}/bets/${betId}/cashout`, { amount }));
export const liveCheck = (betId: string, body: { live_state: Record<string, unknown>; home_stats?: Record<string, unknown>; away_stats?: Record<string, unknown>; side?: string }) =>
  apiClient.post<LiveCheck>(`${BASE}/bets/${betId}/live-check`, body);
export const estimateTax = (grossWin: number, stake: number) => apiClient.get<TaxEstimate>(`${BASE}/tax/estimate`, { gross_win: grossWin, stake });
export const convertCurrency = (amount: number, from: CurrencyCode, to: CurrencyCode) =>
  apiClient.get<Conversion>(`${BASE}/currency/convert`, { amount, from, to });
export const refreshFxRates = async () => {
  const board = await apiClient.post<FxBoard>(`${BASE}/currency/refresh`);
  invalidate("fx");
  return board;
};
export const requestLiveTicket = (body: { target_bookmaker?: string; odds: number; stake_amount: number; description: string }) =>
  apiClient.post<ExecutionReceipt>(`${BASE}/execution/live-ticket`, body);

/** A parlay's or single's settled outcome tone for pills. */
export const statusTone = (status: PaperBetStatus): "good" | "critical" | "warning" | "info" | "neutral" =>
  status === "WON" || status === "HALF_WON" ? "good"
    : status === "LOST" || status === "HALF_LOST" ? "critical"
    : status === "CASHED_OUT" ? "warning"
    : status === "PENDING" ? "info" : "neutral";
