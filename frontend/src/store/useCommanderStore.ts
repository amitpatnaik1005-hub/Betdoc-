import { create } from "zustand";
import { COMMANDER_LIST, resolveCommander } from "../config/commanders.config";
import type { CommanderId, CommanderProfile, CommanderStatus } from "../config/commanders.config";

/** The `commanders` bus event published after every supervisor sweep and on-demand probe. */
export interface RealtimeCommanderPayload {
  statuses?: Record<string, string>;
  summaries?: Record<string, string>;
  latencies_ms?: Record<string, number>;
  swept_at?: string;
}

interface CommanderState {
  activeCommander: CommanderProfile;
  statuses: Readonly<Record<CommanderId, CommanderStatus>>;
  /** Last pushed heartbeat status, activity summary and probe latency per commander. */
  liveStatuses: Readonly<Partial<Record<CommanderId, string>>>;
  summaries: Readonly<Partial<Record<CommanderId, string>>>;
  latenciesMs: Readonly<Partial<Record<CommanderId, number>>>;
  lastSweepAt: string | null;
  syncCommander: (pathname: string) => void;
  setCommanderStatus: (id: CommanderId, status: CommanderStatus) => void;
  hydrateFromRealtime: (payload: RealtimeCommanderPayload) => void;
}

const initialPathname = (): string => (typeof window !== "undefined" ? window.location.pathname : "/");

const initialStatuses = Object.fromEntries(COMMANDER_LIST.map((c) => [c.id, c.status])) as Record<CommanderId, CommanderStatus>;

const KNOWN_IDS = new Set<string>(COMMANDER_LIST.map((c) => c.id));

/** Keeps only known commanders and values of the expected type, so a malformed event can't poison the store. */
function pick<T>(source: Record<string, unknown> | undefined, isValid: (v: unknown) => v is T): Partial<Record<CommanderId, T>> {
  const out: Partial<Record<CommanderId, T>> = {};
  if (!source || typeof source !== "object") return out;
  for (const [id, value] of Object.entries(source)) {
    if (KNOWN_IDS.has(id) && isValid(value)) out[id as CommanderId] = value;
  }
  return out;
}

const isString = (v: unknown): v is string => typeof v === "string" && v.length > 0;
const isLatency = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0;

export const useCommanderStore = create<CommanderState>()((set, get) => ({
  // Correct on the very first paint: derived from the URL at store creation.
  activeCommander: resolveCommander(initialPathname()),
  statuses: initialStatuses,
  liveStatuses: {},
  summaries: {},
  latenciesMs: {},
  lastSweepAt: null,

  syncCommander: (pathname) => {
    const next = resolveCommander(pathname);
    if (get().activeCommander.id !== next.id) set({ activeCommander: next });
  },

  setCommanderStatus: (id, status) =>
    set((state) => (state.statuses[id] === status ? state : { statuses: { ...state.statuses, [id]: status } })),

  // Each event replaces the snapshot: it lists exactly the commanders with a current beat, so a commander
  // that went stale drops out instead of keeping its last pushed status.
  hydrateFromRealtime: (payload) =>
    set({
      liveStatuses: pick(payload.statuses, isString),
      summaries: pick(payload.summaries, isString),
      latenciesMs: pick(payload.latencies_ms, isLatency),
      lastSweepAt: isString(payload.swept_at) ? payload.swept_at : new Date().toISOString(),
    }),
}));
