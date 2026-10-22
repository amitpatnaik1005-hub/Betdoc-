/**
 * The BetDoc Consultant (Group 105, ASHVATTHAMA, points b.6 and b.13b): the Doctor, the Lawyer, the Consultant and
 * TipMaster on the Oracle's slip, and the sealed verdict. Every figure comes from `/consultant`; the only arithmetic
 * here is the capital card's what-if, which reruns the backend's own formulas (linear in the stake) with the rates the
 * backend sent. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import type { AutoFixLeg, Inspection, Skin, Tier } from "./backtest_and_parlay";
import { useResource } from "./resource";
import type { SlipLeg } from "../store/useParlaySlipStore";

export type VerdictStatus = "APPROVED_GOLD" | "APPROVED_WITH_CURE" | "REJECTED";
export type Severity = "CRITICAL" | "WARNING" | "MILD";
export type RiskLevel = "HIGH" | "MEDIUM" | "LOW";
export type Divergence = "PUBLIC_TRAP" | "CONTRARIAN_ALPHA" | "SHARP_CONSENSUS" | "NEUTRAL_ALIGNED" | "NOT_MEASURED";
export type DiseaseCode =
  | "NEGATIVE_EV"
  | "CORRELATED_SYNDROME"
  | "HIGH_VIG_FEVER"
  | "INJURY_LINEUP_SHOCK"
  | "FATIGUE_TRAVEL_PENALTY"
  | "BOOKMAKER_LIMIT_TRAP"
  | "PILLAR_VETO";

export interface Pathology {
  code: DiseaseCode;
  severity: Severity;
  title: string;
  diagnosis: string;
  legs: string[];
  pillar: number | null;
}

export interface Prescription {
  available: boolean;
  replaced: { leg_id: string; label: string; fixture: string; reason: string };
  replacement?: AutoFixLeg;
  treats: DiseaseCode[];
  before: { score: number; tier: Tier };
  after?: { score: number; tier: Tier };
  clean?: boolean;
  leg_ids?: string[];
  joint_ev_after?: number | null;
  tried: { leg_id: string; label: string; fixture: string; ev: number | null; score: number; tier: Tier }[];
  message?: string;
}

export interface Clause {
  code: string;
  title: string;
  risk: RiskLevel;
  legs: string[];
  finding: string;
  summary: string;
  hedge: string;
  basis: string;
  bookmaker: string | null;
  reference: string | null;
}

export interface Capital {
  bankroll_inr: string | null;
  stake_inr: string;
  stake_usd: string | null;
  stake_pct_of_bankroll: number | null;
  kelly_multiplier: number;
  kelly_fraction: number;
  full_kelly: number | null;
  sizing_reason: string | null;
  regime: string | null;
  drawdown: number | null;
  capped: boolean | null;
  odds: string | null;
  payout_inr: string | null;
  tax: { gross_payout: number; stake: number; net_winnings: number; tds_rate_pct: number; tds: number; net_post_tax_payout: number; cess: number;
         effective_rate_pct: number; basis: string } | null;
  expected_profit_inr: string | null;
  stop_loss: { pct: number; floor_inr: string; min_pct: number; max_pct: number; basis: string };
  downside: { joint_probability: number | null; p_total_loss: number | null; var_95: number | null; cvar_95: number | null; std_return: number | null };
  max_payout_inr: string | null;
  payout_over_cap: boolean | null;
  fx: { inr_per_usd: string | null; as_of: string | null };
  what_if: { tds_rate: number; cess_rate: number; stop_loss_pct: number; max_fraction: number };
}

export interface TipLeg {
  leg_id: string;
  label: string;
  tickets_pct: number | null;
  money_pct: number | null;
  source: string | null;
  price: number | null;
  fair_probability: number | null;
  implied_probability: number | null;
  ev_pct: number | null;
  divergence_pct: number | null;
  rlm: boolean | null;
  edge_score: number | null;
  verdict: Divergence;
}

export interface ConsultantLeg {
  leg_id: string;
  fixture: string;
  label: string;
  market: string;
  sport_key: string | null;
  league: string | null;
  kickoff: string | null;
  price: number | null;
  prices: Record<string, number>;
  fair_probability: number | null;
  ev: number | null;
  margin: number | null;
}

export interface ConsultantReport {
  report_id: string;
  signed_at: string;
  signed_for: string;
  signed_by: string;
  verdict: { status: VerdictStatus; title: string; summary: string };
  slip: { slip_id: string; kind: string; title: string | null; skin: Skin; book: string | null; leg_ids: string[]; odds: string | null; joint_ev: number | null;
          joint_probability: number | null; pillars_passed: number; is_vetted: boolean; legs: ConsultantLeg[] };
  rating: Inspection["rating"];
  doctor: { pathologies: Pathology[]; unexamined: { number: number; title: string; reason: string }[]; prescription: Prescription | null; clean_bill: boolean };
  lawyer: { bookmaker: string | null; clauses: Clause[]; references_on_file: number };
  consultant: Capital;
  tipmaster: { verdict: Divergence; label: string; explanation: string; measured_legs: number; legs: TipLeg[] };
  developer_credit: string;
  seal: string;
  seal_algorithm: string;
  seal_holds?: boolean;
}

export interface VerdictRow {
  report_id: string;
  signed_at: string;
  verdict: VerdictStatus;
  title: string | null;
  slip_score: number;
  tier: Tier;
  bookmaker: string | null;
  legs: string[];
  odds: string | null;
  seal: string;
  seal_holds: boolean;
}

interface ConsultBody {
  leg_ids: string[];
  kind: string | null;
  skin: Skin;
}

export const consultSlip = (body: ConsultBody) => apiClient.post<ConsultantReport>("/consultant/audit", body);
export const applyPrescription = (body: ConsultBody & { replace_leg_id: string; with_leg_id: string }) =>
  apiClient.post<ConsultantReport>("/consultant/apply-prescription", body);
export const useVerdicts = (limit?: number) =>
  useResource(`consultant:verdicts:${limit ?? "default"}`, () => apiClient.get<{ verdicts: VerdictRow[] }>("/consultant/verdicts", limit ? { limit } : undefined),
              { intervalMs: 120_000 });

export const VERDICT_STYLE: Record<VerdictStatus, { tone: "good" | "warning" | "critical"; icon: string; color: string }> = {
  APPROVED_GOLD: { tone: "good", icon: "verified", color: "#10B981" },
  APPROVED_WITH_CURE: { tone: "warning", icon: "medication", color: "#F59E0B" },
  REJECTED: { tone: "critical", icon: "gpp_bad", color: "#F43F5E" },
};

export const DIVERGENCE_STYLE: Record<Divergence, { tone: "critical" | "accent" | "good" | "neutral" | "info"; icon: string }> = {
  PUBLIC_TRAP: { tone: "critical", icon: "report" },
  CONTRARIAN_ALPHA: { tone: "accent", icon: "diamond" },
  SHARP_CONSENSUS: { tone: "good", icon: "stars" },
  NEUTRAL_ALIGNED: { tone: "neutral", icon: "balance" },
  NOT_MEASURED: { tone: "info", icon: "help" },
};

/** The prescription's replacement as a slip leg (the Oracle's shared slip). */
export const toSlipLeg = (leg: AutoFixLeg): SlipLeg => ({
  leg_id: leg.leg_id, fixture: leg.fixture, label: leg.label, market: leg.market, prices: leg.prices, fair_probability: leg.fair_probability,
});

export interface WhatIf {
  payout: number | null;
  netWinnings: number | null;
  tds: number | null;
  takeHome: number | null;
  floor: number;
  usd: number | null;
  overCap: boolean | null;
  pctOfBankroll: number | null;
}

/** The capital card at another stake: the backend's formulas (payout = stake x odds; Section 115BBJ TDS on the net
 * winnings; the stop-loss floor = stake x (1 - stop)), with its rates. USD only when the backend had a fresh rate. */
export const whatIf = (capital: Capital, stake: number): WhatIf => {
  const odds = capital.odds === null ? null : Number(capital.odds);
  const payout = odds === null ? null : stake * odds;
  const net = payout === null ? null : Math.max(0, payout - stake);
  const tds = net === null ? null : net * capital.what_if.tds_rate;
  const rate = capital.fx.inr_per_usd === null ? null : Number(capital.fx.inr_per_usd);
  const bankroll = capital.bankroll_inr === null ? null : Number(capital.bankroll_inr);
  return {
    payout, netWinnings: net, tds, takeHome: payout === null || tds === null ? null : payout - tds,
    floor: stake * (1 - capital.stop_loss.pct),
    usd: rate && rate > 0 ? stake / rate : null,
    overCap: capital.max_payout_inr === null || payout === null ? null : payout > Number(capital.max_payout_inr),
    pctOfBankroll: bankroll && bankroll > 0 ? stake / bankroll : null,
  };
};
