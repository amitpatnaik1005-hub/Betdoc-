/**
 * The Bet Builder (Group 104, point b.5). Developed for Amit Ashok Kumar Patnaik.
 *
 * - 5(a) automatic parlays: Ashoka's vetted slips (every quoted fixture, 10,000 Monte Carlo paths, the anti-correlation
 *   gate), grouped into singles, multiples and system bets. Nothing to pick: load one into the workbench to rate it.
 * - 5(b) the manual workbench: the only place legs are picked by hand (`ManualParlayWorkbench`), rated by the BetDoc
 *   Slip Score, with the 1-click auto-fix.
 * - 5(c) the Chef's Table (`GET /oracle/chef-table`): Ashoka's best slips re-rated through the fortress at the retail
 *   books and served three ways, each leg with its recipe card.
 */
import { apiClient } from "../api/client";
import type { Inspection, ScorelineLink } from "./backtest_and_parlay";
import type { Slip, SlipLeg as VettedLeg } from "./oracle";
import { useResource } from "./resource";

export type BuilderMode = "auto" | "manual" | "chef";

/** The auto-fix has something to do: a pillar vetoed the slip, or a badge costs it points. */
export const canAutoFix = (rating: Inspection["rating"]): boolean =>
  rating.breakdown.some((p) => p.status === "FAIL") || rating.badges.some((b) => b.points < 0);

// --------------------------------------------------------------------------- 5(a)
export type AutoGroup = "SINGLES" | "MULTIPLES" | "SYSTEMS";
const GROUP_OF: Record<string, AutoGroup> = {
  SINGLE: "SINGLES", DOUBLE: "MULTIPLES", TREBLE: "MULTIPLES", ACCUMULATOR: "MULTIPLES",
  TRIXIE: "SYSTEMS", PATENT: "SYSTEMS", YANKEE: "SYSTEMS", CANADIAN: "SYSTEMS", HEINZ: "SYSTEMS", SUPER_HEINZ: "SYSTEMS", GOLIATH: "SYSTEMS",
};
export const AUTO_GROUPS: { id: AutoGroup; label: string; note: string }[] = [
  { id: "SINGLES", label: "Singles", note: "One leg: the edge with nothing riding on another result." },
  { id: "MULTIPLES", label: "Parlays", note: "Doubles, trebles and accumulators: legs on independent fixtures, priced together over the Monte Carlo paths." },
  { id: "SYSTEMS", label: "System bets", note: "Trixie to Heinz: every combination of the legs, so one loser does not sink the slip." },
];

/** A vetted slip's sport families ("soccer", "cricket" ...) from its legs' feed keys. */
export const slipSports = (slip: Slip): Set<string> => new Set(slip.legs.map((l) => (l.sport_key ?? "").split("_")[0]).filter(Boolean));

export function groupSlips(slips: Slip[], sport: string | null): Record<AutoGroup, Slip[]> {
  const out: Record<AutoGroup, Slip[]> = { SINGLES: [], MULTIPLES: [], SYSTEMS: [] };
  for (const slip of slips) {
    if (sport && !slipSports(slip).has(sport)) continue;
    out[GROUP_OF[slip.kind] ?? "MULTIPLES"].push(slip);
  }
  return out;
}

/** A vetted leg in the workbench's slip shape. */
export const toSlipLeg = (l: VettedLeg) => ({ leg_id: l.leg_id, fixture: l.fixture, label: l.label, market: l.market, prices: l.prices, fair_probability: l.fair_probability });

// --------------------------------------------------------------------------- 5(c)
export interface RecipeCard {
  leg_id: string;
  fixture: string;
  league: string | null;
  kickoff: string | null;
  market: string;
  selection: string;
  label: string;
  price: number | null;
  book: string | null;
  why: { fair_probability: number; implied_probability: number | null; edge: number | null; models: Record<string, number>; text: string };
  scoreline: ScorelineLink | null;
  stress_test: { code: "LOW_SCORING" | "ONE_GOAL" | "OPEN"; scenario: string; probability: number; leg_pays: number }[];
}

export interface ChefPlate {
  code: "PARLAY_OF_THE_DAY" | "SAFE_FORTRESS" | "HIGH_YIELD";
  title: string;
  slip_id: string;
  kind: string;
  slip_title: string;
  book: string | null;
  odds: string;
  stake_inr: string;
  joint_ev: number | null;
  joint_probability: number | null;
  rating: Inspection["rating"];
  pillars_passed: number;
  is_vetted: boolean;
  legs: VettedLeg[];
  recipe: RecipeCard[];
  outscored: { slip_id: string; score: number; joint_ev: number }[];
}

export interface ChefTable {
  generated_at: string;
  plates: ChefPlate[];
  rated: number;
  considered: number;
  rejected: Record<string, number>;
  thresholds: { min_joint_ev: number; min_joint_probability: number; max_quote_age_seconds: number; paths: number };
  scanned: number | null;
  simulated: number | null;
  legs_considered: number | null;
  developer_credit: string;
}

export const useChefTable = (enabled: boolean) =>
  useResource(enabled ? "oracle:chef-table" : null, () => apiClient.get<ChefTable>("/oracle/chef-table"), { intervalMs: 180_000 });

export const refreshChefTable = () => apiClient.get<ChefTable>("/oracle/chef-table", { refresh: true });
