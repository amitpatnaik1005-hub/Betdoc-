/**
 * VIDUR's intelligence board (`/the-wire/board`, Group 108, point b.14b) and the neural event bus (`/neural-bus`,
 * point b.9). Developed for Amit Ashok Kumar Patnaik.
 *
 * Every figure is the backend's and was measured: the consensus when a story landed against now, a catalyst's
 * recorded move, the lineup model's delta from rated absences, a forecast's friction factor, a steam move in the odds
 * ticks, and the fixture's best leg priced above fair at a retail book. What was not measured is absent, never zero.
 */
import { apiClient } from "../api/client";
import type { Skin } from "./backtest_and_parlay";
import { useResource } from "./resource";

export type IntelCategory = "LINEUP_INJURY" | "WEATHER_PITCH" | "ODDS_STEAM" | "TACTICAL_ROTATION" | "GENERAL";
export type SourceTier = "TIER_1_OFFICIAL" | "TIER_2_PRESS" | "TIER_3_WIRE" | "MEASURED";
export type BoardSport = "ALL" | "CRICKET" | "FOOTBALL" | "TENNIS" | "MMA";
export type BoardCategory = "ALL" | Exclude<IntelCategory, "GENERAL">;
export type ImpactLevel = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export interface BoardFixture {
  fixture_id: string;
  home: string;
  away: string;
  sport_key: string | null;
  family: string;
  kickoff: string;
  in_play: boolean;
  side: "HOME" | "AWAY" | null;
  team: string | null;
  oracle_link: string;
}

export interface Edge {
  leg_id: string;
  label: string;
  market: string;
  book: string;
  book_label: string;
  price: number;
  fair_probability: number;
  ev: number;
}

export interface BoardMarket {
  move: { selection: string; before: number; now: number; delta_pts: number } | null;
  catalyst: { selection: string; before: number; after: number; delta_pts: number; latency_seconds: number } | null;
  lineup: { delta_pts: Record<string, number>; probabilities: Record<string, number> | null; adjusted: Record<string, number> | null; absences: number; unrated: number; side: string | null } | null;
  weather: { friction_factor: number | null; dew_expected: boolean; condition: string | null; kickoff_at: string | null } | null;
  steam: { selection: string; market: string; opening_odds: number; current_odds: number; delta_pts: number; books: string[] } | null;
  edge: Edge | null;
  legs_valued: number;
  legs_above_fair: number;
  edge_shift_pts: number | null;
  edge_recorded: { leg_id: string | null; ev: number | null; at: string } | null;
}

export interface BoardItem {
  id: string;
  kind: "NEWS" | "ABSENCES" | "WEATHER" | "STEAM";
  category: IntelCategory;
  sport: Exclude<BoardSport, "ALL"> | null;
  headline: string;
  summary: string;
  impact_level: ImpactLevel | null;
  events: string[];
  sentiment: number | null;
  source: { name: string; url: string | null; tier: SourceTier; credibility: number | null; basis: string };
  published_at: string;
  fixture: BoardFixture | null;
  also_names: string[];
  market: BoardMarket;
}

export interface WireBoard {
  as_of: string;
  market_as_of: string | null;
  tracked_fixtures: number;
  items: BoardItem[];
  total: number;
  counts: { ALL: number; by_sport: Partial<Record<BoardSport, number>>; by_category: Partial<Record<IntelCategory, number>> };
  sports: string[];
  categories: IntelCategory[];
  policy: { hours: number; steam_window_minutes: number; press_min_credibility: number; weather_min_effect: number; catalyst_impacts: string[]; refresh_seconds: number };
  developer_credit: string;
}

export type ChainModule = "WIRE" | "SCORE_PREDICTOR" | "ORACLE" | "CONSULTANT" | "VAULT";
export type StepStatus = "RUNNING" | "COMPLETED" | "SKIPPED" | "FAILED";

export interface ChainStep {
  number: number;
  module: ChainModule;
  module_name: string;
  status: StepStatus;
  summary: string;
  detail: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  ms: number | null;
}

export interface ChainTrigger {
  item_id: string;
  kind: BoardItem["kind"];
  category: IntelCategory;
  headline: string;
  impact_level: ImpactLevel | null;
  source: string;
  tier: SourceTier;
  sport: string | null;
  fixture: { fixture_id: string; home: string; away: string; kickoff: string; oracle_link: string } | null;
}

export interface NeuralChain {
  chain_id: string;
  origin: "SYSTEM" | "USER";
  mine: boolean;
  trigger: ChainTrigger;
  started_at: string;
  completed_at: string | null;
  steps: ChainStep[];
  counts: { COMPLETED: number; SKIPPED: number; FAILED: number };
  outcome: string;
}

export type NeuralFrame =
  | { type: "connection_ack"; channel: string; transport: "redis" | "in-process"; status: string }
  | { type: "pong" }
  | { type: "chain_started"; chain_id: string; trigger: ChainTrigger; started_at: string; mine: boolean }
  | { type: "step"; chain_id: string; trigger: ChainTrigger; step: ChainStep; mine: boolean }
  | { type: "chain"; chain_id: string; trigger: ChainTrigger; chain: NeuralChain; mine: boolean };

/** The five modules in chain order (the backend's `MODULES`; the bar shows them before any chain has run). */
export const MODULES: { module: ChainModule; name: string; short: string; icon: string }[] = [
  { module: "WIRE", name: "VIDUR / THE WIRE", short: "Wire", icon: "wifi_tethering" },
  { module: "SCORE_PREDICTOR", name: "SCORE PREDICTOR", short: "Predictor", icon: "scoreboard" },
  { module: "ORACLE", name: "ORACLE & BET BUILDER", short: "Oracle", icon: "auto_awesome" },
  { module: "CONSULTANT", name: "BETDOC CONSULTANT", short: "Consultant", icon: "stethoscope" },
  { module: "VAULT", name: "THE VAULT & CAPITAL LEDGER", short: "Vault", icon: "account_balance" },
];

export const CATEGORY_LABEL: Record<IntelCategory, { label: string; icon: string }> = {
  LINEUP_INJURY: { label: "Lineup & injury", icon: "personal_injury" },
  WEATHER_PITCH: { label: "Weather & pitch", icon: "partly_cloudy_day" },
  ODDS_STEAM: { label: "Odds & steam", icon: "bolt" },
  TACTICAL_ROTATION: { label: "Tactical rotation", icon: "swap_horiz" },
  GENERAL: { label: "General", icon: "newspaper" },
};

export const TIER_LABEL: Record<SourceTier, { label: string; tone: "good" | "info" | "neutral" | "accent"; hint: string }> = {
  TIER_1_OFFICIAL: { label: "Tier 1 · Official", tone: "good", hint: "A governing body, league or club" },
  TIER_2_PRESS: { label: "Tier 2 · Press", tone: "info", hint: "The press, at or over the Wire's credibility bar" },
  TIER_3_WIRE: { label: "Tier 3 · Wire", tone: "neutral", hint: "An aggregator, tabloid or unknown host" },
  MEASURED: { label: "Measured", tone: "accent", hint: "Not prose: an injury list, a forecast, the odds ticks, an operator's entry" },
};

export interface BoardFilters {
  sport: BoardSport;
  category: BoardCategory;
  q: string;
  linkedOnly: boolean;
}

export const useWireBoard = (f: BoardFilters) => {
  const q = f.q.trim();
  return useResource(`the-wire:board:${f.sport}:${f.category}:${f.linkedOnly}:${q}`,
    () => apiClient.get<WireBoard>("/the-wire/board", { sport: f.sport, category: f.category, linked_only: String(f.linkedOnly), ...(q ? { q } : {}) }),
    { intervalMs: 20_000 });
};

export const useRecentChains = () =>
  useResource("neural-bus:chains", () => apiClient.get<{ chains: NeuralChain[] }>("/neural-bus/chains", { limit: 12 }), { intervalMs: 60_000 });

export const runChain = (body: { item_id: string; leg_ids?: string[]; skin?: Skin }) =>
  apiClient.post<NeuralChain & { developer_credit: string }>("/neural-bus/chains", body);

/** "just now", "4 mins ago", "2 hours ago", "3 days ago". */
export const ago = (iso: string | null | undefined, now: number = Date.now()): string => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((now - t) / 1000));
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"} ago`;
  if (s < 45) return "just now";
  if (s < 3600) return unit(Math.max(1, Math.round(s / 60)), "min");
  if (s < 86_400) return unit(Math.floor(s / 3600), "hour");
  return unit(Math.floor(s / 86_400), "day");
};

export const pts = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)} pts`;
