import { apiClient } from "../api/client";
import { COMMANDER_LIST, COMMANDER_REGISTRY, type CommanderId, type CommanderProfile } from "../config/commanders.config";
import type { BotStatus as AvatarStatus } from "../components/bots/BotAvatar";
import { useCommanderStore } from "../store/useCommanderStore";
import { runMutation, useResource } from "./resource";

/** Hive bot status as written by the backend commander supervisor. */
export type HeartbeatStatus = "ONLINE" | "OFFLINE" | "SLEEPING" | "WORKING" | "DEGRADED" | "FATAL";

const HEARTBEAT_STATUSES: ReadonlySet<string> = new Set<HeartbeatStatus>(["ONLINE", "OFFLINE", "SLEEPING", "WORKING", "DEGRADED", "FATAL"]);
const isHeartbeatStatus = (v: unknown): v is HeartbeatStatus => typeof v === "string" && HEARTBEAT_STATUSES.has(v);

export interface BotProfile {
  id: string;
  bot_name: CommanderId;
  status: HeartbeatStatus;
  uptime_seconds: number;
  last_ping_at: string;
  tasks_completed: number;
  error_count: number;
  resource_metrics: Record<string, unknown>;
}

export interface LiveCommander {
  profile: CommanderProfile;
  heartbeat: BotProfile | null;
  /** NO SIGNAL when the supervisor has never reported, or the last beat is stale. */
  status: HeartbeatStatus | "NO SIGNAL";
  /** Measured duration of the last health probe; null without a current beat. */
  probeLatencyMs: number | null;
  /** What the last probe measured, e.g. "2 open positions"; null without a current beat. */
  activitySummary: string | null;
}

/** GET /hive/telemetry-summary and POST /hive/bots/probe. */
export interface CommanderTelemetrySummary {
  total_commanders: number;
  reporting_count: number;
  online_count: number;
  working_count: number;
  degraded_count: number;
  fatal_count: number;
  sleeping_count: number;
  offline_count: number;
  stale_count: number;
  overall_health_pct: number;
  avg_probe_latency_ms: number | null;
  stale_after_seconds: number;
  latency_warn_ms: number;
  last_sweep_at: string | null;
  developer_credit: string;
}

// Supervisor beats every 30s; past 3.2 beats the signal is lost (HIVE_SUPERVISOR_STALE_MULTIPLIER on the server).
const STALE_AFTER_MS = 96_000;

export const avatarStatus = (status: LiveCommander["status"]): AvatarStatus =>
  status === "WORKING" ? "active" : status === "ONLINE" ? "success" : status === "DEGRADED" || status === "FATAL" ? "error" : "idle";

const metricNumber = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const metricText = (v: unknown): string | null => (typeof v === "string" && v.trim().length > 0 ? v.trim() : null);

/** Probe one commander, or the whole roster when no id is given, and broadcast the fresh beats. */
export function probeCommandersNow(botName?: CommanderId) {
  return runMutation(
    () => apiClient.post<CommanderTelemetrySummary>("/hive/bots/probe", botName ? { bot_name: botName } : {}),
    {
      invalidate: ["commanders"],
      success: (r) =>
        botName
          ? `${COMMANDER_REGISTRY[botName].name} probed`
          : `${r.reporting_count - r.stale_count}/${r.total_commanders} commanders reporting · ${r.overall_health_pct}% healthy`,
      errorTitle: "Commander probe failed",
    },
  );
}

export function useCommanders() {
  const bots = useResource("commanders:bots", () => apiClient.get<BotProfile[]>("/hive/bots"), { intervalMs: 30_000 });
  const rtStatuses = useCommanderStore((s) => s.liveStatuses);
  const rtSummaries = useCommanderStore((s) => s.summaries);
  const rtLatencies = useCommanderStore((s) => s.latenciesMs);
  const lastSweepAt = useCommanderStore((s) => s.lastSweepAt);

  const byName = new Map((bots.data ?? []).map((b) => [b.bot_name, b]));
  const now = Date.now();
  const sweptMs = lastSweepAt ? Date.parse(lastSweepAt) : Number.NaN;
  const pushFresh = Number.isFinite(sweptMs) && now - sweptMs < STALE_AFTER_MS;

  const commanders: LiveCommander[] = COMMANDER_LIST.map((profile) => {
    const heartbeat = byName.get(profile.id) ?? null;
    const beatMs = heartbeat ? Date.parse(heartbeat.last_ping_at) : Number.NaN;
    const beatFresh = Number.isFinite(beatMs) && now - beatMs < STALE_AFTER_MS;
    const pushed = rtStatuses[profile.id];
    // The pushed event wins while it is newer than the fetched beat (it lands before the refetch)
    if (pushFresh && isHeartbeatStatus(pushed) && (!beatFresh || sweptMs >= beatMs)) {
      return {
        profile,
        heartbeat,
        status: pushed,
        probeLatencyMs: metricNumber(rtLatencies[profile.id]),
        activitySummary: metricText(rtSummaries[profile.id]),
      };
    }
    if (heartbeat && beatFresh) {
      const metrics = heartbeat.resource_metrics ?? {};
      return {
        profile,
        heartbeat,
        status: heartbeat.status,
        probeLatencyMs: metricNumber(metrics.probe_latency_ms),
        activitySummary: metricText(metrics.activity_summary) ?? metricText(metrics.error),
      };
    }
    return { profile, heartbeat, status: "NO SIGNAL", probeLatencyMs: null, activitySummary: null };
  });
  return { ...bots, commanders, byId: new Map(commanders.map((c) => [c.profile.id, c])), probeNow: probeCommandersNow };
}

/** "1.4ms" / "12ms" / "1.2s": compact probe latency for badges. */
export const formatProbeLatency = (ms: number): string =>
  ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : ms >= 10 ? `${Math.round(ms)}ms` : `${ms.toFixed(1)}ms`;
