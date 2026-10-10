/** Quant Lab, the Control Panel's quantitative backtester (backend `app/api/v1/lab_quant.py`, Group 66). */
import { useSyncExternalStore } from "react";
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type BacktestStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type Verdict = "ROBUST" | "DEGRADED" | "OVERFIT" | "INSUFFICIENT_DATA" | "NO_EDGE";

export interface Dataset {
  ticks: number;
  fixtures: number;
  results: Record<string, number>;
  postponed: number;
  first_tick: string | null;
  last_tick: string | null;
  books: { bookmaker_id: string; currency: string; ticks: number; commission: string }[];
  fx: { currency: string; fixings: number; first: string; last: string }[];
  static_fx_rates: Record<string, string>;
  fx_max_age_hours: number;
  datasets: string[];
  sources: string[];
  synthetic: boolean;
}

export interface LabBot {
  id: string;
  name: string;
  status: string;
  execution_mode: string;
  kelly_multiplier: string;
  allocated_capital: string;
  math_models: string[];
  risk_models: string[];
  target_bet_types: string[];
  enable_order_slicing: boolean;
  problems: string[];
}

export interface StrategySpec {
  name: string;
  math_models: string[];
  risk_models: string[];
  target_bet_types: string[];
  kelly_multiplier?: string;
  capital_inr?: string;
}

export interface BacktestParams {
  name: string;
  bot_ids: string[];
  strategies: StrategySpec[];
  start: string | null;
  end: string | null;
  train_ratio: number;
  oos_enabled: boolean;
  sweep_enabled: boolean;
  kelly_min: string;
  kelly_max: string;
  sweep_steps: number;
  latency_min_ms: number;
  latency_max_ms: number;
  slippage_pct: string;
  void_rate_pct: string;
  monte_carlo_iterations: number;
  ruin_floor_pct: number;
  resume_after_hours: number;
  seed: number;
  walk_forward_folds?: number; // Group 77: > 1 adds rolling folds
  impact_model?: "quadratic" | "sqrt";
  risk_free_rate?: number | null; // a year; null: the server's LAB_RISK_FREE_RATE
}

export interface Metrics {
  trades: number;
  voids: number;
  voids_injected: number;
  open_at_end: number;
  wins: number;
  win_rate_pct: number | null;
  staked_inr: number | null;
  pnl_inr: number | null;
  roi_pct: number | null;
  return_pct: number | null;
  cagr_pct: number | null;
  starting_capital_inr: number | null;
  final_equity_inr: number | null;
  max_drawdown_inr: number;
  max_drawdown_pct: number;
  drawdown_days: number;
  sharpe: number | null;
  sortino: number | null;
  volatility_pct: number | null;
  calmar: number | null;
  mae_avg_pct: number | null;
  mae_worst_pct: number | null;
  clv_beat_pct: number | null;
  clv_avg_pct: number | null;
  avg_odds: number | null;
  avg_stake_inr: number | null;
  commission_paid_inr: number | null;
  risk_free_rate?: number; // Group 77
  brier_score?: number | null;
  brier_skill_score?: number | null; // against the closing line, on the same fills
  brier_fills?: number;
}

export interface FoldMetrics {
  sharpe: number | null;
  sortino: number | null;
  roi_pct: number | null;
  return_pct: number | null;
  max_drawdown_pct: number;
  trades: number;
  pnl_inr: number | null;
  brier_skill_score: number | null;
}

export interface RollingFold {
  fold: number;
  in_sample_window: [string, string];
  out_of_sample_window: [string, string];
  in_sample: FoldMetrics;
  out_of_sample: FoldMetrics;
  kelly: string | null;
  verdict: { verdict: Verdict; sharpe_retention: number | null; reason: string };
}

export interface RollingWalkForward {
  folds: RollingFold[];
  summary: { folds: number; robust_folds: number; oos_sharpe_mean: number | null; oos_sharpe_min: number | null; oos_return_pct_mean: number | null; walk_forward_efficiency: number | null };
  train_ratio: number;
}

export interface SweepRow {
  kelly: string;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  roi_pct: number | null;
  return_pct: number | null;
  max_drawdown_pct: number;
  trades: number;
  pnl_inr: number | null;
}

export interface MonteCarlo {
  method: string;
  iterations: number;
  trades: number;
  starting_capital_inr: number;
  ruin_floor_inr: number;
  ruin_floor_pct: number;
  risk_of_ruin_pct: number;
  p_drawdown_50_pct: number;
  max_drawdown_pct: { p50: number; p95: number; p99: number };
  observed_max_drawdown_pct: number;
  observed_drawdown_percentile?: number;
  final_equity_inr: number;
  bootstrap: {
    risk_of_ruin_pct: number;
    p_loss_pct: number;
    final_equity_inr: { p5: number; p50: number; p95: number };
    var_cvar_inr?: Record<"95" | "99", { var: number; cvar: number }>; // Group 77: of the run's P&L
  };
  fan: { trade: number; p5: number; p50: number; p95: number }[];
}

export interface Trade {
  bot: string;
  fixture: string;
  market: string;
  selection: string;
  bookmaker: string;
  currency: string;
  decided_at: string;
  filled_at: string;
  latency_ms: number;
  queue_ms: number;
  requested_odds: string;
  arrival_odds: string;
  fill_odds: string;
  closing_odds: string | null;
  stake_inr: string;
  commission_inr: string;
  impact_pct: string;
  fill: string;
  status: string;
  void_reason: string | null;
  pnl_inr: string | null;
  fx_source: string;
  mae_pct: number;
}

export interface BacktestResult {
  dataset: { fingerprint: string; datasets: string[]; ticks: number; fixtures: number; first_tick: string | null; last_tick: string | null; horizon: string };
  window: { start: string; end: string; split: string; train_ratio: number; walk_forward: boolean };
  locked_parameters: { kelly_multiplier: string | null; chosen_by: string; window: string; fingerprint: string };
  metrics: Metrics;
  per_bot: { bot_id: string; name: string; origin: string; trades: number; voids: number; staked_inr: string; pnl_inr: string; roi_pct: number | null; final_equity_inr: string; kelly_multiplier: string; suspensions: number }[];
  curves: { equity: { t: string; equity: number }[]; underwater: { t: string; drawdown_pct: number }[] };
  penalties: {
    signals: number;
    orders_filled: number;
    orders_rejected: number;
    rejections: Record<string, number>;
    latency_ms_avg: number | null;
    latency_ms_max: number | null;
    filled_worse_after_latency: number;
    latency_edge_decay_rejections: number;
    throttled: number;
    queued_behind_rate_limit: number;
    queue_seconds_total: number;
    partial_fills: number;
    impact_avg_pct: number | null;
    impact_max_pct: number | null;
    participation_max_pct: number | null;
    fx: Record<string, Record<string, number>>;
    fx_fallback_conversions: number;
    decisions: Record<string, number>;
  };
  monte_carlo: MonteCarlo;
  sweep: { enabled: boolean; objective: string; window: string; rows: SweepRow[]; best_kelly: string | null };
  walk_forward: {
    enabled: boolean;
    in_sample?: Metrics;
    out_of_sample?: Metrics;
    verdict?: { verdict: Verdict; sharpe_retention: number | null; reason: string };
    locked_parameters?: { kelly_multiplier: string | null; fingerprint: string };
    rolling?: RollingWalkForward; // Group 77
  };
  events: { at: string; event: string; reason?: string; bot?: string; market?: string; swing_pct?: number }[];
  trades: Trade[];
  stream: { frames: number; evaluated: number; signals: number; shocks: number; skipped: Record<string, number> };
  bots: { id: string; name: string; origin: string; starting_capital_inr: string; components: { key: string; name: string; kind: string; registry_id: string }[] }[];
  warnings: string[];
  elapsed_seconds: number;
}

export interface BacktestSummary {
  roi_pct: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  trades: number | null;
  risk_of_ruin_pct: number | null;
  verdict: Verdict | null;
  best_kelly: string | null;
}

export interface Backtest {
  id: string;
  name: string;
  status: BacktestStatus;
  progress: number;
  stage: string;
  params: Partial<BacktestParams>;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  summary: BacktestSummary | null;
}

export interface BacktestDetail extends Backtest {
  result: BacktestResult | null;
}

/** The reference strategy: Aryabhata's consensus and Shin de-vig, Kelly-staked, drawdown and exposure capped. */
export const REFERENCE_STRATEGY: StrategySpec = {
  name: "Reference: consensus + Shin",
  math_models: ["math.consensus", "math.devig_shin", "math.kelly_criterion"],
  risk_models: ["risk.drawdown", "risk.exposure"],
  target_bet_types: ["bet.match_winner_1x2", "bet.over_under_goals", "bet.single"],
};

export const useDataset = () => useResource("lab:dataset", () => apiClient.get<Dataset>("/lab/quant/dataset"));
export const useLabBots = () => useResource("lab:bots", () => apiClient.get<LabBot[]>("/lab/quant/bots"));
export const useBacktests = (polling: boolean) =>
  useResource("lab:backtests", () => apiClient.get<Backtest[]>("/lab/quant/backtests", { limit: 20 }), { intervalMs: polling ? 1_500 : 15_000 });
/** One run in full. Keyed by its status too: a run that just completed fetches its result afresh. */
export const useBacktest = (id: string | null, status: string) =>
  useResource(id ? `lab:backtest:${id}:${status}` : null, () => apiClient.get<BacktestDetail>(`/lab/quant/backtests/${id}`));

const subscribeTheme = (onChange: () => void): (() => void) => {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
};

/** The app's theme (the `dark` class on <html>), for charts that need concrete colours. */
export const useIsDark = (): boolean => useSyncExternalStore(subscribeTheme, () => document.documentElement.classList.contains("dark"), () => false);
