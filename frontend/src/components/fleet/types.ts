/** Fleet Command contracts (backend: app/schemas/omni_fleet.py). */
import { apiClient } from "../../api/client";
import { useResource } from "../../lib/resource";

export type FleetStatus = "HEALTHY" | "DEGRADED" | "TRIPPED" | "QUOTA_RESERVE" | "FATAL" | "DISABLED" | "NEEDS_KEY" | "IDLE";
export type FleetRole = "always_on" | "primary" | "failover" | "standby" | "unavailable";

export interface FailoverNote {
  group: string;
  replacing: string;
  reason: string;
}

export interface FleetSource {
  source_id: string;
  display_name: string;
  description: string;
  docs_url: string | null;
  kind: "builtin" | "config";
  cost: "free" | "metered";
  priority: number;
  coverage: string[];
  requires_api_key: boolean;
  is_enabled: boolean;
  status: FleetStatus;
  role: FleetRole;
  availability: string;
  scope: string[];
  covering: FailoverNote[];
  breaker_state: "closed" | "open" | "half_open";
  breaker_remaining_seconds: number | null;
  has_api_key: boolean;
  api_key_hint: string | null;
  key_origin: "vault" | "environment" | null;
  secret_env: string | null;
  interval_seconds: number;
  default_interval_seconds: number;
  rate_limit_rpm: number;
  burst: number;
  consecutive_failures: number;
  failure_threshold: number;
  paused_at: string | null;
  last_error: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  ping_ms: number | null;
  success_rate: number | null;
  runs_in_window: number;
  ticks_last_run: number | null;
  fixtures_last_run: number | null;
  malformed_last_run: number | null;
  throttled_ms: number | null;
  devig: Record<string, number>;
  unmapped: string[];
  unmapped_count: number;
  quota_remaining: number | null;
  quota_used: number | null;
  quota_limit: number | null;
  quota_fraction: number | null;
  runner: string | null;
  spec: Record<string, unknown> | null;
}

export interface FleetGroup {
  group: string;
  active: string[];
  free: string[];
  down: Record<string, string>;
  failover: boolean;
  uncovered: boolean;
}

export interface FleetOverview {
  generated_at: string;
  mode: "celery" | "inprocess" | "offline";
  redis_available: boolean;
  vault_configured: boolean;
  board_cells: number | null;
  quota_reserve: number;
  sources: FleetSource[];
  groups: FleetGroup[];
}

export interface DeadLetter {
  source_id: string;
  status: string;
  failures: number;
  error: string;
  runner: string | null;
  at: string;
}

export interface PreviewTick {
  match_id: string;
  home_team: string;
  away_team: string;
  selection: string;
  odds: number;
  true_probability: number;
  home_canonical: boolean;
  away_canonical: boolean;
}

export interface ProviderPreview {
  events_seen: number;
  events_normalized: number;
  malformed: number;
  unmapped: string[];
  devig: Record<string, number>;
  ticks: PreviewTick[];
}

export const useFleet = () => useResource("fleet:overview", () => apiClient.get<FleetOverview>("/omni/fleet"), { intervalMs: 10_000 });
export const useDeadLetters = () =>
  useResource("fleet:deadletter", () => apiClient.get<DeadLetter[]>("/omni/fleet/deadletter", { limit: 8 }), { intervalMs: 30_000 });

/** "soccer_epl" -> "Soccer · EPL"-ish: readable without a sport catalogue in the browser. */
export const groupLabel = (key: string): string => {
  const [sport, ...rest] = key.split("_");
  const league = rest.join(" ").toUpperCase();
  return league ? `${sport.charAt(0).toUpperCase()}${sport.slice(1)} · ${league}` : key;
};

export const seconds = (s: number): string => (s >= 3600 ? `${Math.round(s / 3600)}h` : s >= 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`);

export const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
