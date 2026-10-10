/** KUMBHA's capital growth (backend `app/api/v1/cfo_growth.py`, Group 76): the sizing policy, forecasts, advisories, rebalancing. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type Regime = "OPTIMAL_GROWTH" | "CAUTIOUS_THROTTLED" | "DEFENSIVE_CAPITAL_PRESERVATION" | "CIRCUIT_BREAKER_HALT" | "LATCHED_HALT";

export interface SizingPolicy {
  kelly_fraction: number;
  max_fraction: number;
  regimes: { name: Regime; from_drawdown: number; multiplier: number }[];
  skill: { slope: number; floor: number; cap: number };
  longshot: { pivot_odds: number; exponent: number; floor: number };
}

export interface PolicyState {
  bankroll_inr: string | null;
  drawdown: number;
  window_days: number;
  regime: Regime;
  damper: number;
  halt_latched: boolean;
  effective_ceiling: number;
  stake_ceiling_inr: string | null;
  skill_bss: number | null;
  skill_multiplier: number;
  ruin_bound_halving: number;
  policy: SizingPolicy;
  horizons: number[];
  default_paths: number;
  strategies: { code: string; label: string; kelly: number | null; fixed: number | null }[];
  active_strategy: string;
  developer_credit: string;
}

export interface StrategyRow {
  strategy_code: string;
  strategy_name: string;
  kelly_multiplier: number | null;
  fixed_fraction: number | null;
  base_fraction: number;
  effective_fraction: number;
  drawdown_damper_multiplier: number;
  recommended_stake_on_next_bet_inr: string | null;
  simulated_cagr_pct: number;
  median_end_multiple: number;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  prob_circuit_breaker: number;
  prob_ruin: number;
  median_max_drawdown_pct: number;
  p95_max_drawdown_pct: number;
  ruin_bound_halving: number | null;
}

export interface History {
  bets: number;
  per_day: number;
  hit_rate: number;
  mean_odds: number;
  mean_model_probability: number;
  realised_roi: number;
  median_full_kelly: number;
}

export interface StrategyBoard {
  current_bankroll_inr: string | null;
  current_drawdown_pct: number;
  regime: Regime;
  damper: number;
  halt_latched: boolean;
  skill_bss: number | null;
  skill_multiplier: number;
  active_strategy: string;
  history: History;
  typical_bet: { full_kelly: number; odds: number };
  horizon_days: number;
  paths: number;
  strategies: StrategyRow[];
  developer_credit: string;
}

export interface CurvePoint {
  day: number;
  p1: number;
  p5: number;
  p25: number;
  p50: number;
  p75: number;
  p95: number;
  p99: number;
}

export interface Simulation {
  id: string;
  strategy: string;
  horizon_days: number;
  paths: number;
  trades: number;
  seed: string;
  starting_bankroll_inr: string;
  median_ending_bankroll_inr: string;
  mean_ending_bankroll_inr: string;
  expected_cagr_pct: number;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  prob_circuit_breaker: number;
  prob_ruin: number;
  median_max_drawdown: number;
  p95_max_drawdown: number;
  percentile_curve: CurvePoint[];
  parameters: { history?: History; skill_bss?: number | null; ruin_level?: number };
  developer_credit: string;
  created_at: string;
}

export interface Advisory {
  id: string;
  insight_code: "OPTIMAL_GROWTH_TRAJECTORY" | "VARIANCE_THROTTLE" | "CAPITAL_PRESERVATION_HALT" | "VENUE_REBALANCE";
  severity: "INFO" | "RECOMMENDATION" | "WARNING" | "CRITICAL";
  regime: Regime | null;
  title: string;
  message: string;
  action_directive: string | null;
  metrics_snapshot: Record<string, unknown>;
  is_acknowledged: boolean;
  acknowledged_at: string | null;
  acknowledgement_note: string | null;
  developer_credit: string;
  created_at: string;
}

export interface Transfer {
  id: string;
  plan_id: string;
  source_venue: string;
  destination_venue: string;
  amount_inr: string;
  reason: string;
  status: "PENDING" | "APPROVED" | "EXECUTED" | "DISMISSED" | "SUPERSEDED";
  source_balance_before: string;
  dest_balance_before: string;
  source_target: string;
  dest_target: string;
  status_note: string | null;
  executed_at: string | null;
  created_at: string;
}

export interface RebalancePlan {
  total_bankroll_inr: string;
  venue_balances: Record<string, string>;
  ev_flow_inr: Record<string, number>;
  unpriced: Record<string, string>;
  ev_without_account: string[];
  target_allocations: Record<string, string>;
  target_weights: Record<string, number>;
  transfers: { source_venue: string; destination_venue: string; amount_inr: string }[];
  reason: string | null;
  parameters: { gamma: number; min_transfer_inr: string; lookback_days: number };
  recorded: Transfer[];
  developer_credit: string;
}

export const useGrowthPolicy = () => useResource("growth:policy", () => apiClient.get<PolicyState>("/the-vault/cfo/growth/policy"), { intervalMs: 60_000 });
export const useStrategyBoard = () => useResource("growth:strategies", () => apiClient.get<StrategyBoard>("/the-vault/cfo/growth/strategies"), { intervalMs: 600_000 });
export const useSimulations = () => useResource("growth:simulations", () => apiClient.get<Simulation[]>("/the-vault/cfo/growth/simulations", { limit: 5 }), { intervalMs: 600_000 });
export const useGrowthAdvisories = () => useResource("growth:advisories", () => apiClient.get<Advisory[]>("/the-vault/cfo/growth/advisories", { limit: 20 }), { intervalMs: 60_000 });
export const useRebalance = (enabled: boolean) =>
  useResource(enabled ? "growth:rebalance" : null, () => apiClient.get<RebalancePlan>("/the-vault/cfo/growth/rebalance", { limit: 20 }), { intervalMs: 300_000 });
export const runForecast = (strategy: string, horizon_days: number) => apiClient.post<Simulation>("/the-vault/cfo/growth/simulate", { strategy, horizon_days });
export const acknowledgeAdvisory = (id: string, note?: string) => apiClient.post<Advisory & { signed_off: number }>(`/the-vault/cfo/growth/advisories/${id}/ack`, note ? { note } : {});
export const recordRebalance = () => apiClient.post<RebalancePlan>("/the-vault/cfo/growth/rebalance");
export const setTransferStatus = (id: string, status: "APPROVED" | "EXECUTED" | "DISMISSED", note?: string) =>
  apiClient.patch<Transfer>(`/the-vault/cfo/growth/rebalance/${id}`, note ? { status, note } : { status });

export const REGIME: Record<Regime, { label: string; tone: "good" | "warning" | "serious" | "critical" }> = {
  OPTIMAL_GROWTH: { label: "Full sizing", tone: "good" },
  CAUTIOUS_THROTTLED: { label: "Cautious", tone: "warning" },
  DEFENSIVE_CAPITAL_PRESERVATION: { label: "Defensive", tone: "serious" },
  CIRCUIT_BREAKER_HALT: { label: "Halted", tone: "critical" },
  LATCHED_HALT: { label: "Halted · awaiting sign-off", tone: "critical" },
};
