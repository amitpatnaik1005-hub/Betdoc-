import { create } from "zustand";
import { apiClient } from "../api/client";

// Mirrors the backend response_model. FastAPI serialises by alias, so markets
// carry `outcomes`, not `selections`.
export interface OddsSelection {
  name: string;
  price: number;
}

export interface OddsMarket {
  key: string;
  last_update: string | null;
  outcomes: OddsSelection[];
}

export interface OddsBookmaker {
  key: string;
  title: string;
  last_update: string;
  markets: OddsMarket[];
}

export interface NormalizedMatchOdds {
  id: string;
  sport_key: string;
  commence_time: string;
  home_team: string;
  away_team: string;
  bookmakers: OddsBookmaker[];
}

interface OddsState {
  matches: Record<string, NormalizedMatchOdds>;
  isLoading: boolean;
  error: string | null;
  usingFallback: boolean;
  startPolling: (intervalMs: number, sport?: string) => void;
  stopPolling: () => void;
  refresh: () => Promise<void>;
}

const MIN_INTERVAL_MS = 5_000;

let pollHandle: ReturnType<typeof setInterval> | null = null;
let inFlight: boolean = false;
let currentSport: string | undefined;
// Bumped on start/stop so responses from a previous polling session are ignored.
let generation: number = 0;

const VOLATILE_KEYS: ReadonlySet<string> = new Set(["last_update"]);
const NO_KEYS: ReadonlySet<string> = new Set();

export function deepEqual(
  a: unknown,
  b: unknown,
  ignoreKeys: ReadonlySet<string> = NO_KEYS,
): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;

  const aIsArray: boolean = Array.isArray(a);
  if (aIsArray !== Array.isArray(b)) return false;

  if (aIsArray) {
    const arrA = a as unknown[];
    const arrB = b as unknown[];
    if (arrA.length !== arrB.length) return false;
    for (let i = 0; i < arrA.length; i++) {
      if (!deepEqual(arrA[i], arrB[i], ignoreKeys)) return false;
    }
    return true;
  }

  const objA = a as Record<string, unknown>;
  const objB = b as Record<string, unknown>;
  const keysA: string[] = Object.keys(objA).filter((k) => !ignoreKeys.has(k));
  const keysB: string[] = Object.keys(objB).filter((k) => !ignoreKeys.has(k));
  if (keysA.length !== keysB.length) return false;
  for (const key of keysA) {
    if (!Object.prototype.hasOwnProperty.call(objB, key)) return false;
    if (!deepEqual(objA[key], objB[key], ignoreKeys)) return false;
  }
  return true;
}

function isMatch(value: unknown): value is NormalizedMatchOdds {
  if (value === null || typeof value !== "object") return false;
  const m = value as Record<string, unknown>;
  return typeof m.id === "string" && m.id.length > 0 && Array.isArray(m.bookmakers);
}

export const useOddsStore = create<OddsState>()((set, get) => {
  const applyFallback = (message: string): void => {
    const s = get();
    // Keep last-known-good matches; only flip flags that actually change.
    if (s.usingFallback && s.error === message && !s.isLoading) return;
    set({ usingFallback: true, error: message, isLoading: false });
  };

  const fetchOnce = async (gen: number): Promise<void> => {
    if (inFlight) return; // never overlap requests
    inFlight = true;
    try {
      const qs: string = currentSport ? `?sport=${encodeURIComponent(currentSport)}` : "";
      // BASE_URL already includes /api/v1.
      const data: unknown = await apiClient.get<unknown>(`/odds/live${qs}`);
      if (gen !== generation) return;

      if (!Array.isArray(data) || data.length === 0) {
        applyFallback("No live odds available");
        return;
      }

      const prev: Record<string, NormalizedMatchOdds> = get().matches;
      const next: Record<string, NormalizedMatchOdds> = {};
      let changed: boolean = false;

      for (const item of data) {
        if (!isMatch(item)) continue;
        const old: NormalizedMatchOdds | undefined = prev[item.id];
        if (old !== undefined && deepEqual(old, item, VOLATILE_KEYS)) {
          next[item.id] = old; // reuse reference: subscribers to this match don't re-render
        } else {
          next[item.id] = item;
          changed = true;
        }
      }
      if (Object.keys(prev).length !== Object.keys(next).length) changed = true;

      const s = get();
      const patch: Partial<OddsState> = {};
      if (changed) patch.matches = next;
      if (s.usingFallback) patch.usingFallback = false;
      if (s.error !== null) patch.error = null;
      if (s.isLoading) patch.isLoading = false;

      // Strict no-op when nothing changed: zero store notifications.
      if (Object.keys(patch).length > 0) set(patch);
    } catch (err: unknown) {
      if (gen !== generation) return;
      applyFallback(err instanceof Error ? err.message : "Failed to load odds");
    } finally {
      inFlight = false;
    }
  };

  return {
    matches: {},
    isLoading: false,
    error: null,
    usingFallback: false,

    startPolling: (intervalMs: number, sport?: string): void => {
      get().stopPolling();
      generation += 1;
      currentSport = sport;
      const gen: number = generation;

      if (Object.keys(get().matches).length === 0 && !get().isLoading) {
        set({ isLoading: true });
      }

      void fetchOnce(gen);
      pollHandle = setInterval(() => {
        void fetchOnce(gen);
      }, Math.max(intervalMs, MIN_INTERVAL_MS));
    },

    stopPolling: (): void => {
      if (pollHandle !== null) {
        clearInterval(pollHandle);
        pollHandle = null;
      }
      generation += 1;
    },

    refresh: async (): Promise<void> => {
      await fetchOnce(generation);
    },
  };
});
