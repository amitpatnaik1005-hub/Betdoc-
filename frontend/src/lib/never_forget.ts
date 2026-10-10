/** The Never-Forget shield and experience (backend `app/api/v1/never_forget.py` and `user_xp.py`, Group 75). */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type RuleStatus = "ACTIVE" | "EXPERIMENTAL" | "ARCHIVED";

/** The shield's measured record: how the legs it vetoed actually finished. */
export interface VetoRecord {
  vetoes: number;
  resolved: number;
  lost: number;
  won: number;
  void: number;
  stake_withheld_inr: string;
  stake_withheld_on_losers_inr: string;
}

export interface NeverForgetRule {
  id: string;
  mistake_id: string;
  rule_code: string;
  title: string;
  description: string;
  shape: string;
  action: string;
  status: RuleStatus;
  status_reason: string;
  specificity: { comparable?: number; matched?: number; rate?: number | null };
  times_triggered: number;
  last_triggered_at: string | null;
  created_at: string;
}

export interface MistakeMemory {
  id: string;
  fixture_id: string;
  home: string;
  away: string;
  sport_key: string | null;
  league: string | null;
  market: string;
  selection: string;
  placed_odds: string;
  leg_result: string;
  shape: string;
  loss_root_cause: string;
  root_cause_explanation: string;
  situation: Record<string, number>;
  fingerprint: Record<string, number>;
  extracted_lesson: string;
  developer_credit: string;
  created_at: string;
  rule: NeverForgetRule | null;
  record: VetoRecord;
}

export interface Prevention {
  id: string;
  rule_id: string;
  rule_code: string | null;
  mistake_id: string;
  audit_id: string | null;
  fixture_id: string;
  home: string;
  away: string;
  market: string;
  selection: string;
  odds: string;
  similarity_score: number;
  stake_withheld_inr: string | null;
  veto_reason: string;
  outcome: string | null;
  resolved_at: string | null;
  created_at: string;
}

export interface NeverForgetStats {
  enabled: boolean;
  total_mistakes_memorized: number;
  rules: Record<RuleStatus, number>;
  fleet: VetoRecord;
  mine: VetoRecord;
  policy: { threshold: number; gamma: number; min_coverage: number; scope: string; weights: Record<string, number> };
  developer_credit: string;
}

export interface XpProfile {
  user_id: string;
  total_xp: number;
  level: number;
  rank_title: string;
  current_level_min_xp: number;
  next_level_xp: number | null;
  next_rank: string | null;
  progress_pct: number;
  slips_vetted_count: number;
  bets_won_count: number;
  losses_prevented_count: number;
  mistakes_learned_count: number;
  streak_bonuses_count: number;
  last_action_at: string | null;
  tiers: { level: number; rank: string; min_xp: number }[];
  awards: Record<string, number>;
  streak_days_required: number;
  fleet_total_xp: number;
  developer_credit: string;
}

export interface XpAward {
  id: string;
  action_type: string;
  xp_amount: number;
  source_ref: string;
  description: string;
  created_at: string;
}

export const useNeverForgetStats = () => useResource("neverforget:stats", () => apiClient.get<NeverForgetStats>("/twin/never-forget/stats"), { intervalMs: 120_000 });
export const useMistakeMemories = () => useResource("neverforget:memories", () => apiClient.get<MistakeMemory[]>("/twin/never-forget/memories", { limit: 30 }), { intervalMs: 300_000 });
export const usePreventions = () => useResource("neverforget:preventions", () => apiClient.get<Prevention[]>("/twin/never-forget/preventions", { limit: 30 }), { intervalMs: 120_000 });
export const useXpProfile = () => useResource("xp:profile", () => apiClient.get<XpProfile>("/user/xp/profile"), { intervalMs: 120_000 });
export const useXpHistory = () => useResource("xp:history", () => apiClient.get<XpAward[]>("/user/xp/history", { limit: 30 }), { intervalMs: 300_000 });
export const archiveRule = (id: string, reason: string) => apiClient.post<NeverForgetRule>(`/twin/never-forget/rules/${id}/archive`, { reason });
export const activateRule = (id: string, reason: string) => apiClient.post<NeverForgetRule>(`/twin/never-forget/rules/${id}/activate`, { reason });

export const RULE_STATUS: Record<RuleStatus, { label: string; tone: "good" | "neutral" | "warning" | "critical"; icon: string }> = {
  ACTIVE: { label: "Guarding", tone: "critical", icon: "block" },
  EXPERIMENTAL: { label: "Shadow only", tone: "warning", icon: "visibility" },
  ARCHIVED: { label: "Archived", tone: "neutral", icon: "inventory_2" },
};

export const XP_ACTION_LABEL: Record<string, string> = {
  SLIP_VETTED: "Slip vetted",
  BET_WON: "Bet won",
  LOSS_PREVENTED: "Loss shielded",
  MISTAKE_MEMORIZED: "Lesson memorised",
  STREAK_BONUS: "Disciplined streak",
};

/** The situation's evidence as the fortress read it, in words. */
export const SITUATION_LABEL: Record<string, (v: number) => string> = {
  rain: (v) => `rain ${v} mm/h`,
  wind: (v) => `wind ${v} km/h`,
  fatigue: (v) => `rest ${v}h`,
  cards: (v) => `${v} cards/game`,
  penalties: (v) => `${v} pens/90`,
  steam: (v) => (v >= 0.5 ? "steam against" : "no adverse steam"),
  odds: (v) => `odds ${v}`,
  model_ev: (v) => `model EV ${(v * 100).toFixed(1)}%`,
  sharp_edge: (v) => `edge ${(v * 100).toFixed(1)}%`,
  public: (v) => `public ${(v * 100).toFixed(0)}%`,
};
