/** The Hive's autonomous trading bots (backend `app/api/v1/hive_trading.py`, Group 65). */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type ExecutionMode = "PAPER_TRADE" | "SHADOW_MODE" | "LIVE_EXECUTION";
export type BotStatus = "ACTIVE" | "PAUSED" | "SUSPENDED" | "ARCHIVED";
export type ComponentKind = "MATH_MODEL" | "RISK_MODEL" | "BET_TYPE";

export interface Component {
  key: string;
  kind: ComponentKind;
  name: string;
  description: string;
  category: string | null;
  implementation: string | null;
  live_capable: boolean;
  source: string;
}

export interface Registry {
  components: Component[];
  counts: Record<string, number>;
  live_counts: Record<string, number>;
  expected: Record<string, number>;
}

export interface SubAccount {
  funding: string | null;
  available: number;
  exposure: number;
  equity: number;
  realized_pnl: number;
  open_positions: number;
}

export interface Bot {
  id: string;
  name: string;
  description: string;
  execution_mode: ExecutionMode;
  status: BotStatus;
  math_models: string[];
  risk_models: string[];
  target_bet_types: string[];
  risk_params: Record<string, Record<string, number>>;
  allocated_capital: number;
  kelly_multiplier: number;
  max_stake_pct: number;
  min_edge_pct: number;
  min_quoting_books: number;
  min_market_liquidity: number;
  enable_order_slicing: boolean;
  slice_size_inr: number;
  max_bets_per_minute: number;
  drawdown_limit_pct: number;
  cooldown_seconds: number;
  suspended_reason: string | null;
  suspended_at: string | null;
  created_at: string;
  updated_at: string;
  account: SubAccount;
  orders_last_minute: number;
  pipeline_problems: string[];
}

export interface Halt {
  halted: boolean;
  reason: string | null;
  by: string | null;
  at: string | null;
  detail: Record<string, unknown>;
}

export interface HiveEvent {
  id: string;
  bot_id: string | null;
  event: string;
  reason: string;
  fixture_id: string | null;
  market: string | null;
  selection: string | null;
  stake_inr: number | null;
  odds: number | null;
  conviction: number | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface Plan {
  id: string;
  bot_id: string;
  fixture_id: string;
  market: string;
  selection: string;
  bookmaker_id: string;
  odds: number;
  total_stake_inr: number;
  slices: { index: number; stake_inr: string; countdown_s: number; status: string; reason?: string }[];
  status: string;
  created_at: string;
}

export interface TopologyLink {
  holder: string;
  market_key: string;
  fixture_id: string;
  market: string;
  selection: string;
  stake_inr: number;
  source: "ledger" | "shadow";
  strategy: string | null;
}

export interface TopologyMarket {
  market_key: string;
  fixture_id: string;
  market: string;
  home: string;
  away: string;
  commence_time: string | null;
  holders: string[];
  selections: string[];
  collision: boolean;
  opposing: boolean; // real positions on different outcomes: a wash trade
  shadow_overlap: boolean; // a shadow bot's hypothetical position on another outcome
}

export interface Topology {
  bots: { id: string; name: string; status: string; execution_mode: string }[];
  markets: TopologyMarket[];
  links: TopologyLink[];
}

export type BotDraft = {
  name: string;
  description: string;
  execution_mode: ExecutionMode;
  math_models: string[];
  risk_models: string[];
  target_bet_types: string[];
  kelly_multiplier: string;
  max_stake_pct: string;
  min_edge_pct: string;
  min_quoting_books: string;
  min_market_liquidity: string;
  enable_order_slicing: boolean;
  slice_size_inr: string;
  max_bets_per_minute: string;
  drawdown_limit_pct: string;
  cooldown_seconds: string;
};

export const STAKING_MODEL = "math.kelly_criterion";

export const NEW_BOT: BotDraft = {
  name: "",
  description: "",
  execution_mode: "PAPER_TRADE",
  math_models: ["math.consensus", "math.devig_shin", STAKING_MODEL],
  risk_models: ["risk.drawdown", "risk.exposure"],
  target_bet_types: ["bet.match_winner_1x2", "bet.single"],
  kelly_multiplier: "0.25",
  max_stake_pct: "5",
  min_edge_pct: "1",
  min_quoting_books: "3",
  min_market_liquidity: "0",
  enable_order_slicing: false,
  slice_size_inr: "10000",
  max_bets_per_minute: "3",
  drawdown_limit_pct: "20",
  cooldown_seconds: "900",
};

export const draftOf = (bot: Bot): BotDraft => ({
  name: bot.name,
  description: bot.description,
  execution_mode: bot.execution_mode,
  math_models: bot.math_models,
  risk_models: bot.risk_models,
  target_bet_types: bot.target_bet_types,
  kelly_multiplier: String(bot.kelly_multiplier),
  max_stake_pct: String(bot.max_stake_pct),
  min_edge_pct: String(bot.min_edge_pct),
  min_quoting_books: String(bot.min_quoting_books),
  min_market_liquidity: String(bot.min_market_liquidity),
  enable_order_slicing: bot.enable_order_slicing,
  slice_size_inr: String(bot.slice_size_inr),
  max_bets_per_minute: String(bot.max_bets_per_minute),
  drawdown_limit_pct: String(bot.drawdown_limit_pct),
  cooldown_seconds: String(bot.cooldown_seconds),
});

export const payloadOf = (d: BotDraft) => ({
  name: d.name.trim(),
  description: d.description,
  execution_mode: d.execution_mode,
  math_models: d.math_models,
  risk_models: d.risk_models,
  target_bet_types: d.target_bet_types,
  kelly_multiplier: d.kelly_multiplier,
  max_stake_pct: d.max_stake_pct,
  min_edge_pct: d.min_edge_pct,
  min_quoting_books: Number(d.min_quoting_books),
  min_market_liquidity: d.min_market_liquidity,
  enable_order_slicing: d.enable_order_slicing,
  slice_size_inr: d.slice_size_inr,
  max_bets_per_minute: Number(d.max_bets_per_minute),
  drawdown_limit_pct: d.drawdown_limit_pct,
  cooldown_seconds: Number(d.cooldown_seconds),
});

export const MODE_LABEL: Record<ExecutionMode, string> = { PAPER_TRADE: "Paper", SHADOW_MODE: "Shadow", LIVE_EXECUTION: "Live" };

export const useRegistry = () => useResource("hive:registry", () => apiClient.get<Registry>("/hive/trading/registry"));
export const useHiveBots = () => useResource("hive:bots", () => apiClient.get<Bot[]>("/hive/trading/bots"), { intervalMs: 5_000 });
export const useHalt = () => useResource("hive:halt", () => apiClient.get<Halt>("/hive/trading/halt"), { intervalMs: 3_000 });
export const useTopology = () => useResource("hive:topology", () => apiClient.get<Topology>("/hive/trading/topology"), { intervalMs: 5_000 });
export const useHiveEvents = () => useResource("hive:events", () => apiClient.get<HiveEvent[]>("/hive/trading/events", { limit: 60 }), { intervalMs: 4_000 });
export const usePlans = () => useResource("hive:plans", () => apiClient.get<Plan[]>("/hive/trading/plans", { limit: 20 }), { intervalMs: 5_000 });
