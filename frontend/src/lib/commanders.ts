import { apiClient } from "../api/client";
import { COMMANDER_LIST, type CommanderId, type CommanderProfile } from "../config/commanders.config";
import type { BotStatus as AvatarStatus } from "../components/bots/BotAvatar";
import { useResource } from "./resource";

/** Hive bot status as written by the backend commander supervisor. */
export type HeartbeatStatus = "ONLINE" | "OFFLINE" | "SLEEPING" | "WORKING" | "DEGRADED" | "FATAL";

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
}

// Supervisor beats every 30s; three missed beats means the signal is lost.
const STALE_AFTER_MS = 95_000;

export const avatarStatus = (status: LiveCommander["status"]): AvatarStatus =>
  status === "WORKING" ? "active" : status === "ONLINE" ? "success" : status === "DEGRADED" || status === "FATAL" ? "error" : "idle";

export function useCommanders() {
  const bots = useResource("commanders:bots", () => apiClient.get<BotProfile[]>("/hive/bots"), { intervalMs: 30_000 });
  const byName = new Map((bots.data ?? []).map((b) => [b.bot_name, b]));
  const now = Date.now();
  const commanders: LiveCommander[] = COMMANDER_LIST.map((profile) => {
    const heartbeat = byName.get(profile.id) ?? null;
    const fresh = heartbeat !== null && now - new Date(heartbeat.last_ping_at).getTime() < STALE_AFTER_MS;
    return { profile, heartbeat, status: heartbeat && fresh ? heartbeat.status : "NO SIGNAL" };
  });
  return { ...bots, commanders, byId: new Map(commanders.map((c) => [c.profile.id, c])) };
}
