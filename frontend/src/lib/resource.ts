/**
 * Live resource cache (Zustand): every panel reads server state through `useResource`.
 *
 * - Keys are `section:name` (e.g. `vault:overview`). `invalidate("vault")` refetches every mounted
 *   resource in that section, which is how realtime events and mutations keep sections in sync.
 * - Fetches are de-duplicated per key; stale data stays on screen while a refetch runs.
 * - `intervalMs` polls as a safety net while mounted and the tab is visible; realtime events do the
 *   fast path.
 */
import { useCallback, useEffect, useRef } from "react";
import { create } from "zustand";
import { toast } from "../store/useToastStore";

export interface ResourceEntry<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  updatedAt: number | null;
}

interface ResourceStore {
  entries: Record<string, ResourceEntry<unknown>>;
}

const EMPTY: ResourceEntry<never> = { data: undefined, error: null, loading: false, updatedAt: null };

export const useResourceStore = create<ResourceStore>()(() => ({ entries: {} }));

interface Registration {
  fetcher: () => Promise<unknown>;
  refs: number;
  inflight: Promise<void> | null;
}

const registry = new Map<string, Registration>();

const patch = (key: string, next: Partial<ResourceEntry<unknown>>): void =>
  useResourceStore.setState((s) => ({
    entries: { ...s.entries, [key]: { ...(s.entries[key] ?? EMPTY), ...next } },
  }));

const errorText = (err: unknown): string => (err instanceof Error ? err.message : "Request failed");

export function refresh(key: string): Promise<void> {
  const reg = registry.get(key);
  if (!reg) return Promise.resolve();
  if (reg.inflight) return reg.inflight;
  patch(key, { loading: true });
  reg.inflight = reg
    .fetcher()
    .then((data) => patch(key, { data, error: null, loading: false, updatedAt: Date.now() }))
    .catch((err: unknown) => patch(key, { error: errorText(err), loading: false }))
    .finally(() => {
      reg.inflight = null;
    });
  return reg.inflight;
}

const matches = (key: string, prefix: string): boolean => key === prefix || key.startsWith(`${prefix}:`);

/** Refetch every mounted resource whose key equals, or is namespaced under, one of the prefixes. */
export function invalidate(...prefixes: string[]): void {
  for (const key of registry.keys()) {
    if (prefixes.some((p) => matches(key, p))) void refresh(key);
  }
}

export interface ResourceResult<T> extends ResourceEntry<T> {
  refresh: () => Promise<void>;
}

export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  options: { intervalMs?: number } = {},
): ResourceResult<T> {
  const { intervalMs } = options;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!key) return;
    const existing = registry.get(key);
    const reg: Registration = existing ?? { fetcher: () => fetcherRef.current(), refs: 0, inflight: null };
    reg.fetcher = () => fetcherRef.current();
    reg.refs += 1;
    registry.set(key, reg);
    void refresh(key);

    let timer: number | undefined;
    if (intervalMs && intervalMs > 0) {
      timer = window.setInterval(() => {
        if (document.visibilityState === "visible") void refresh(key);
      }, intervalMs);
    }
    return () => {
      if (timer !== undefined) window.clearInterval(timer);
      reg.refs -= 1;
      if (reg.refs <= 0) registry.delete(key);
    };
  }, [key, intervalMs]);

  const entry = useResourceStore((s) => (key ? s.entries[key] : undefined)) as ResourceEntry<T> | undefined;
  const doRefresh = useCallback(() => (key ? refresh(key) : Promise.resolve()), [key]);
  return { ...(entry ?? EMPTY), loading: entry ? entry.loading : Boolean(key), refresh: doRefresh };
}

// --------------------------------------------------------------------------- mutations
export interface MutationOptions<T> {
  /** Section prefixes to refetch on success (realtime events also do this for other tabs). */
  invalidate?: string[];
  success?: string | ((result: T) => string);
  /** Prefix for the error toast; the server's message is appended. */
  errorTitle?: string;
}

/** Run a write, toast the outcome, refresh affected sections. Resolves to the result, or undefined on failure. */
export async function runMutation<T>(fn: () => Promise<T>, options: MutationOptions<T> = {}): Promise<T | undefined> {
  try {
    const result = await fn();
    if (options.invalidate?.length) invalidate(...options.invalidate);
    if (options.success) {
      toast.success(typeof options.success === "function" ? options.success(result) : options.success);
    }
    return result;
  } catch (err: unknown) {
    toast.error(options.errorTitle ?? "Action failed", errorText(err));
    return undefined;
  }
}
