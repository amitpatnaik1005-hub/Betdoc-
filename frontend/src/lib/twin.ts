/** The True Digital Betting Twin (backend `app/api/v1/digital_twin.py`, Group 72): the fortress (15 pillars since Group 75), the booking-code ledger, the in-play watch. */
import { apiClient } from "../api/client";
import type { Scorecard, Slip } from "./oracle";
import { useResource } from "./resource";

export type PillarStatus = "PASS" | "FAIL" | "UNVERIFIED" | "ADVISORY";

export interface Pillar {
  number: number;
  key: string;
  title: string;
  status: PillarStatus;
  reason: string;
  metrics: Record<string, unknown>;
}

export interface TwinAudit {
  id: string;
  slip_id: string;
  kind: Slip["kind"];
  leg_ids: string[];
  bookmaker: string | null;
  total_odds: string | null;
  stake_inr: string;
  bankroll_inr: string | null;
  kelly_fraction: number;
  joint_ev: number | null;
  joint_probability: number | null;
  consensus_ev: number | null;
  sharp_edge: number | null;
  pillars_passed: number;
  conviction_score: number;
  is_vetted: boolean;
  pillars: Pillar[];
  rejection_reasons: string[];
  slip: Slip;
  created_at: string;
  developer_credit?: string;
}

export interface ConfirmLeg {
  leg_id: string;
  audited_odds: number;
  floor: number;
  current_odds: number;
  age_seconds: number;
}

export interface ConfirmResult {
  status: PillarStatus;
  reason: string;
  bookmaker?: string;
  checked_at?: string;
  legs: ConfirmLeg[] | string[];
}

export interface TwinMonitor {
  id: string;
  bet_id: string;
  vetting_audit_id: string | null;
  is_active: boolean;
  target_profit_pct: number;
  initial_win_prob: number;
  current_win_prob: number;
  fair_value_inr: string | null;
  peak_fair_value_inr: string | null;
  cashout_offer_inr: string | null;
  last_advice: "HOLD" | "CASH_OUT" | "HEDGE_LEG" | null;
  last_tick_at: string | null;
  ticks: number;
  pullout_triggered: boolean;
  pullout_reason: string | null;
  pullout_at: string | null;
  detail: { status?: string; action?: string; closed?: string; reasons?: string[]; hedge?: { instruction: string; locked_profit_inr: string } | null };
  bet?: { stake_inr: string; status: string; bookmaker: string; booking_code: string | null; placed_odds: string | null };
}

export interface TwinLedger extends Omit<Scorecard, "cached"> {
  developer_credit: string;
  bets: { id: string; status: string; bookmaker: string; booking_code: string | null; stake_inr: string; placed_odds: string | null; pnl_inr: string | null; vetting_audit_id: string; placed_at: string }[];
}

export interface PlacedFromAudit {
  bet_id: string;
  status: string;
  booking_code: string | null;
  watch: { started: boolean; reason?: string; message?: string; monitor?: TwinMonitor };
}

export const useTwinAudits = (vetted: boolean) =>
  useResource(`twin:audits:${vetted}`, () => apiClient.get<{ developer_credit: string; audits: TwinAudit[] }>("/twin/audits", vetted ? { vetted: true, limit: 20 } : { limit: 20 }), { intervalMs: 60_000 });
export const useTwinMonitors = () => useResource("twin:monitors", () => apiClient.get<TwinMonitor[]>("/twin/monitors"), { intervalMs: 15_000 });
export const useTwinLedger = () => useResource("twin:ledger", () => apiClient.get<TwinLedger>("/twin/ledger"), { intervalMs: 60_000 });

export const vetSlip = (slip: Pick<Slip, "legs" | "kind">, bankroll?: number | null) =>
  apiClient.post<TwinAudit>("/twin/vet", { leg_ids: slip.legs.map((l) => l.leg_id), kind: slip.kind, ...(bankroll ? { bankroll_inr: bankroll } : {}) });
export const confirmAudit = (id: string) => apiClient.post<ConfirmResult>(`/twin/audits/${id}/confirm`);
export const placeFromAudit = (id: string, body: { bookmaker: string; stake_inr: string; placed_odds: string | null; booking_code: string | null; watch: boolean }) =>
  apiClient.post<PlacedFromAudit>(`/twin/audits/${id}/ledger`, body);
export const setOffer = (id: string, offer: string | null) => apiClient.put<TwinMonitor>(`/twin/monitors/${id}/offer`, { cashout_offer_inr: offer });
export const stopWatch = (id: string) => apiClient.delete<TwinMonitor>(`/twin/monitors/${id}`);
export const tickNow = () => apiClient.post<{ ran: boolean; watched: number; priced: number; alerts: { reason: string; action: string }[] }>("/twin/monitors/tick");

export const PILLAR_TONE: Record<PillarStatus, "good" | "critical" | "warning" | "neutral"> = { PASS: "good", FAIL: "critical", UNVERIFIED: "warning", ADVISORY: "neutral" };
export const PILLAR_ICON: Record<PillarStatus, string> = { PASS: "check_circle", FAIL: "cancel", UNVERIFIED: "help", ADVISORY: "info" };
