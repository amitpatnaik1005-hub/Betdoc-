/** CFO ledger contracts (backend `app/schemas/cfo_vault.py`) and the shared bankroll resource. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type LedgerStatus = "PENDING" | "WON" | "LOST" | "REJECTED" | "VOID" | "REQUIRES_MANUAL_INTERVENTION";

export interface Position {
  id: string;
  fixture_id: string;
  market: string;
  selection: string;
  bookmaker_id: string;
  remote_bet_id: string | null;
  stake_inr: number;
  odds: number;
  potential_pnl: number;
  realized_pnl: number | null;
  status: LedgerStatus;
  reconcile_required: boolean;
  commence_time?: string | null;
  resolve_attempts?: number;
  last_resolve_error?: string | null;
  created_at: string;
  settled_at: string | null;
}

export interface RiskSettings {
  daily_drawdown_pct: number;
  max_market_exposure_pct: number;
  max_loss_streak: number;
  velocity_max_cv_pct: number;
  max_slippage_pct: number;
}

export interface Bankroll {
  opened: boolean;
  currency: string;
  available_balance: number;
  exposure_balance: number;
  equity: number;
  peak_balance: number;
  pnl_24h: number;
  drawdown_limit: number | null;
  loss_streak: number | null;
  kill_switch: boolean;
  execution_mode: "paper" | "live";
  limits: RiskSettings;
  open_positions: Position[];
}

export interface ExecutionReceipt {
  status: "EXECUTED" | "UNKNOWN";
  message: string;
  ledger_id: string;
  remote_bet_id: string | null;
  bookmaker_id: string;
  fixture_id: string;
  selection: string;
  stake_inr: number;
  odds: number;
  potential_pnl: number;
  available_balance: number;
  exposure_balance: number;
  execution_mode: "paper" | "live";
}

/** Plain-language names for the reasons the ledger refuses an order (audit log `reason`). */
export const REFUSALS: Record<string, string> = {
  BLOCKED_BY_KILL_SWITCH: "Trading is halted",
  BLOCKED_BY_DRAWDOWN: "Daily drawdown limit reached",
  BLOCKED_BY_LOSS_STREAK: "Loss-streak pause",
  BLOCKED_BY_MARKET_EXPOSURE: "Fixture exposure cap",
  BLOCKED_BY_VELOCITY: "Price moving too fast",
  STAKE_ABOVE_CAP: "Above your stake cap",
  STAKE_ABOVE_MAX_BET: "Above the max bet",
  INSUFFICIENT_BALANCE: "Not enough available balance",
  BANKROLL_LOCKED: "Another order is in flight",
  DUPLICATE_REQUEST: "Already submitted",
  SIGNAL_EXPIRED: "Signal expired",
  PRICE_MOVED: "Price moved",
  RISK_SERVICES_UNAVAILABLE: "Risk checks unavailable",
  UNMAPPED_FIXTURE: "Bookmaker doesn't list this match",
  UNMAPPED_SELECTION: "Bookmaker doesn't list this selection",
  NO_EXECUTION_VENUE: "No execution venue for this bookmaker",
  SLIPPAGE_REJECTED: "Price dropped below your floor",
  OUTBOUND_THROTTLED: "Outbound rate limit: try again",
  AUTH_FAILED: "Bookmaker refused the session",
};

export const useBankroll = () => useResource("cfo:bankroll", () => apiClient.get<Bankroll>("/omni/bankroll"), { intervalMs: 15_000 });

/** Profit if it wins, rounded down to the paisa exactly as the ledger books it. */
export const potentialProfit = (stake: number, odds: number): number => Math.floor(Math.round(stake * 100) * (odds - 1)) / 100;
