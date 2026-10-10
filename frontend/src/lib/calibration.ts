/** The model recalibration engine (backend `app/api/v1/model_calibration.py`, Group 74): lifecycle states and pillar 1's weights. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type Lifecycle = "ALPHA_BOOSTED" | "ACTIVE" | "PROBATION" | "BENCHED";

export interface ModelState {
  model_name: string;
  weight_in_force: number | null;
  pinned: boolean;
  status: Lifecycle | null;
  status_reason?: string;
  sample_count?: number;
  brier_skill_score?: number | null;
  brier_score_90d?: number | null;
  avg_clv_pct?: number | null;
  audited_at?: string;
}

export interface RunView {
  run_id: string;
  trigger_type: string;
  models_evaluated: number;
  models_promoted: number;
  models_demoted: number;
  benchmark_model: string;
  benchmark_brier: number | null;
  published: boolean;
  weights: Record<string, number>;
  note: string | null;
  created_at: string;
  developer_credit: string;
}

export interface WeightsReport {
  weights: Record<string, number>;
  pins: Record<string, number>;
  equal_weights: boolean;
  models: ModelState[];
  last_run: RunView | null;
  benchmark: string;
  bands: Record<string, [number, number]>;
  developer_credit: string;
}

export interface WeightAudit {
  model_name: string;
  sample_count: number;
  paired_count: number;
  brier_score_90d: number | null;
  brier_skill_score: number | null;
  avg_clv_pct: number | null;
  previous_weight: number | null;
  new_weight: number;
  previous_status: Lifecycle | null;
  status: Lifecycle;
  status_reason: string;
  run_id: string;
  created_at: string;
}

export const useCalibrationWeights = () => useResource("calibration:weights", () => apiClient.get<WeightsReport>("/twin/calibration/weights"), { intervalMs: 300_000 });
export const useCalibrationHistory = () => useResource("calibration:history", () => apiClient.get<WeightAudit[]>("/twin/calibration/history", { limit: 30 }), { intervalMs: 300_000 });
export const runRecalibration = () => apiClient.post<RunView>("/twin/calibration/recalibrate");
export const overrideWeight = (body: { model_name: string; weight: number; reason: string; pin: boolean }) => apiClient.post<RunView>("/twin/calibration/override", body);
export const resetWeights = (reason: string) => apiClient.post<RunView & { message: string }>("/twin/calibration/reset", { reason });

export const LIFECYCLE: Record<Lifecycle, { label: string; tone: "good" | "neutral" | "warning" | "critical"; icon: string }> = {
  ALPHA_BOOSTED: { label: "Alpha boost", tone: "good", icon: "rocket_launch" },
  ACTIVE: { label: "Active", tone: "neutral", icon: "how_to_vote" },
  PROBATION: { label: "Probation", tone: "warning", icon: "hourglass_top" },
  BENCHED: { label: "Benched · no veto", tone: "critical", icon: "block" },
};
