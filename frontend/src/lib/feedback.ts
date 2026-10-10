/** The post-execution feedback loop (backend `app/api/v1/settlement_feedback.py`, Group 73): CLV, model attribution, root causes, weights. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export interface ModelAccuracy {
  model_name: string;
  predictions: number;
  avg_brier: number;
  avg_log_loss: number;
  avg_rps: number | null;
  avg_clv_pct: number | null;
  reference: boolean;
  eligible: boolean;
  recommended_weight: number | null;
  published_weight: number | null;
}

export interface AccuracyReport {
  window_days: number;
  min_samples: number;
  developer_credit: string;
  published_at: string | null;
  models: ModelAccuracy[];
}

export interface RootCauseRow {
  bet_id: string;
  status: string;
  pnl_inr: string | null;
  booking_code: string | null;
  root_cause_tag: string;
  explanation: string;
  model_error_delta: number | null;
  clv_pct: number | null;
  settled_at: string | null;
}

export interface SettlementSummary {
  attributed_bets: number;
  clv: { bets: number; avg_clv_pct: number | null; beat_close_rate: number | null; avg_clv_sharp_pct: number | null };
  root_causes: Record<string, number>;
  recent: RootCauseRow[];
  developer_credit: string;
}

export interface SweepReport {
  settled_bets: number;
  settled_legs: number;
  attributed_bets: number;
  feedback_records: number;
  total_pnl_inr: string;
  alerts: number;
  root_causes: Record<string, number>;
  developer_credit: string;
}

export interface Recalibration {
  published: boolean;
  weights: Record<string, number>;
  computed_at: string;
  min_samples: number;
  developer_credit: string;
}

export const useModelAccuracy = () => useResource("feedback:accuracy", () => apiClient.get<AccuracyReport>("/twin/settlement/model-accuracy"), { intervalMs: 300_000 });
export const useSettlementSummary = () => useResource("feedback:summary", () => apiClient.get<SettlementSummary>("/twin/settlement/summary"), { intervalMs: 120_000 });
export const sweepSettlements = () => apiClient.post<SweepReport>("/twin/settlement/sweep");
export const recalibrateWeights = () => apiClient.post<Recalibration>("/twin/settlement/recalibrate-weights");

/** Predictors measured but never weighted in pillar 1. */
export const REFERENCE_LABEL: Record<string, string> = { ensemble: "Ashoka ensemble", closing_sharp: "Sharp close (benchmark)" };
export const ROOT_CAUSE_LABEL: Record<string, string> = {
  NONE: "Did not lose",
  INPLAY_SHOCK_RED_CARD: "In-play shock",
  STEAM_ADVERSE_SELECTION: "Adverse steam",
  WEATHER_ANOMALY: "Weather",
  MODEL_UNDERESTIMATION: "Model over-confident",
  REFEREE_STRICTNESS_BIAS: "Strict referee",
  VARIANCE_BAD_LUCK: "Variance",
};
