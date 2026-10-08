/**
 * Fleet Command: the Omni ingestion fleet's control surface (Control Panel, KAUTILYA).
 *
 * - Adapter cards: a spring toggle per data source (enable/disable cluster-wide), write-only API key
 *   fields (encrypted server-side with the vault; only a masked hint ever comes back), cadence, run now.
 * - Live health matrix: ping, success rate, last sync, throughput, quota, failure streak per source.
 * - Event horizon: ingestion batches and quorum sweeps as they land on `/omni/ws/stream`.
 *
 * Freshness: the backend publishes a `fleet` event on `/ws/events` after every run, toggle and
 * quorum sweep; services/realtime turns that into invalidate("fleet"), so the matrix redraws within
 * moments of a worker finishing. A 10s poll is the safety net.
 */
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { apiClient } from "../api/client";
import { formatAgo, formatInt, formatRatioPct, formatTime } from "../lib/format";
import { runMutation, useResource } from "../lib/resource";
import { subscribeChannel } from "../services/realtime";
import { useAuthStore } from "../store/useAuthStore";
import { useMarketStore } from "../store/useMarketStore";
import {
  Async,
  Button,
  type Column,
  ConfirmButton,
  DataTable,
  EmptyState,
  ITEM_VARIANTS,
  LIST_VARIANTS,
  LiveDot,
  Meter,
  Panel,
  Pill,
  SPRING,
  Select,
  StatusBadge,
  inputClass,
} from "../ui/kit";

// ---------------------------------------------------------------------------
// CONTRACTS (app/schemas/omni_fleet.py)
// ---------------------------------------------------------------------------
type FleetStatus = "HEALTHY" | "DEGRADED" | "FATAL" | "DISABLED" | "NEEDS_KEY" | "IDLE";

interface FleetSource {
  source_id: string;
  display_name: string;
  description: string;
  docs_url: string | null;
  requires_api_key: boolean;
  is_enabled: boolean;
  status: FleetStatus;
  has_api_key: boolean;
  api_key_hint: string | null;
  key_origin: "vault" | "environment" | null;
  interval_seconds: number;
  default_interval_seconds: number;
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
  unmapped: string[];
  unmapped_count: number;
  quota_remaining: number | null;
  runner: string | null;
}

interface FleetOverview {
  generated_at: string;
  mode: "celery" | "inprocess" | "offline";
  redis_available: boolean;
  vault_configured: boolean;
  board_cells: number | null;
  sources: FleetSource[];
}

interface DeadLetter { source_id: string; status: string; failures: number; error: string; runner: string | null; at: string }

interface HorizonEntry { id: string; kind: "batch" | "quorum"; title: string; detail: string; tone: "good" | "warning" | "neutral"; at: number }

const CADENCES = [15, 30, 60, 120, 300, 900] as const;
const MODE_LABEL: Record<FleetOverview["mode"], [string, "good" | "info" | "critical", string]> = {
  celery: ["Celery workers", "good", "dns"],
  inprocess: ["In-process fallback", "info", "memory"],
  offline: ["Ingestion offline", "critical", "cloud_off"],
};

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

// Re-render on a steady clock so "12s ago" and "next in 18s" keep moving between refetches.
function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

const useFleet = () => useResource("fleet:overview", () => apiClient.get<FleetOverview>("/omni/fleet"), { intervalMs: 10_000 });
const useDeadLetters = () => useResource("fleet:deadletter", () => apiClient.get<DeadLetter[]>("/omni/fleet/deadletter", { limit: 8 }), { intervalMs: 30_000 });

const seconds = (s: number): string => (s >= 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`);

// ---------------------------------------------------------------------------
// ANIMATED SWITCH: spring knob, a slow breathing halo while the source is live
// ---------------------------------------------------------------------------
const FleetSwitch = ({ on, onChange, label, disabled, busy }: { on: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean; busy?: boolean }) => {
  const reduce = useReducedMotion();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      onClick={() => onChange(!on)}
      className={cx(
        "relative inline-flex h-8 w-[3.75rem] shrink-0 items-center rounded-full p-1 transition-colors duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[#F8F6F0] dark:focus-visible:ring-offset-stone-950",
        "disabled:cursor-not-allowed disabled:opacity-50",
        on ? "justify-end bg-[var(--accent)]" : "justify-start bg-stone-200 dark:bg-stone-700",
      )}
    >
      {on && !reduce && (
        <motion.span
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 rounded-full"
          initial={{ boxShadow: "0 0 0 0 var(--accent-glow)" }}
          animate={{ boxShadow: ["0 0 0 0 var(--accent-glow)", "0 0 0 7px rgba(0,0,0,0)"] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: "easeOut" }}
        />
      )}
      <motion.span layout transition={SPRING} className="relative grid size-6 place-items-center rounded-full bg-white shadow-sm dark:bg-stone-100">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={busy ? "busy" : on ? "on" : "off"}
            initial={{ scale: 0.4, opacity: 0, rotate: -40 }}
            animate={{ scale: 1, opacity: 1, rotate: 0 }}
            exit={{ scale: 0.4, opacity: 0 }}
            transition={{ duration: 0.22 }}
            className={cx("material-symbols-outlined text-[14px]", busy && "animate-spin", on ? "text-[var(--accent-text)]" : "text-stone-400")}
          >
            {busy ? "progress_activity" : on ? "bolt" : "power_settings_new"}
          </motion.span>
        </AnimatePresence>
      </motion.span>
    </button>
  );
};

// ---------------------------------------------------------------------------
// SECURE KEY FIELD: write-only; the key leaves this component once and is never read back
// ---------------------------------------------------------------------------
const SecureKeyField = ({ source, canEdit, vaultReady }: { source: FleetSource; canEdit: boolean; vaultReady: boolean }) => {
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const id = source.source_id;

  if (!source.requires_api_key) {
    return (
      <p className="flex items-center gap-2 rounded-2xl bg-stone-50 px-4 py-3 text-xs text-stone-500 dark:bg-white/[0.03] dark:text-stone-400">
        <span className="material-symbols-outlined text-[16px] text-emerald-600 dark:text-emerald-400">public</span>
        Public API: no key required.
      </p>
    );
  }

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const key = value.trim();
    if (key.length < 8) return;
    setBusy(true);
    const ok = await runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}/api-key`, { api_key: key }), {
      invalidate: ["fleet"],
      success: `${source.display_name} key encrypted and stored`,
      errorTitle: "Key rejected",
    });
    setBusy(false);
    if (ok) {
      setValue("");
      setReveal(false);
    }
  };

  const stored =
    source.key_origin === "vault"
      ? `Encrypted in vault · ${source.api_key_hint ?? "hidden"}`
      : source.key_origin === "environment"
        ? "Using the server's environment key"
        : "No key stored: this source is waiting for one";

  return (
    <form onSubmit={save} className="flex flex-col gap-2" autoComplete="off">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={`fleet-key-${id}`} className="text-xs font-medium text-stone-500 dark:text-stone-400">API key</label>
        <span className={cx("flex items-center gap-1 text-[11px]", source.has_api_key ? "text-stone-500 dark:text-stone-400" : "text-amber-700 dark:text-amber-300/90")}>
          <span className="material-symbols-outlined text-[13px]">{source.has_api_key ? "lock" : "key_off"}</span>
          {stored}
        </span>
      </div>
      <div className="flex gap-2">
        <div className="relative min-w-0 flex-1">
          <span className="material-symbols-outlined pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-[16px] text-stone-400">key</span>
          <input
            id={`fleet-key-${id}`}
            name={`fleet-${id}-key`}
            type={reveal ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={source.has_api_key ? "Replace key" : "Paste key"}
            autoComplete="new-password"
            spellCheck={false}
            data-lpignore="true"
            data-1p-ignore="true"
            disabled={!canEdit || !vaultReady || busy}
            className={cx(inputClass, "pl-10 pr-11 font-mono")}
          />
          <button
            type="button"
            onClick={() => setReveal((r) => !r)}
            disabled={!value}
            aria-label={reveal ? "Hide key" : "Show key"}
            className="absolute right-2 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded-full text-stone-400 transition-colors hover:bg-stone-200/60 hover:text-stone-600 disabled:opacity-30 dark:hover:bg-white/10"
          >
            <span className="material-symbols-outlined text-[16px]">{reveal ? "visibility_off" : "visibility"}</span>
          </button>
        </div>
        <Button type="submit" variant="primary" icon="enhanced_encryption" busy={busy} disabled={!canEdit || !vaultReady || value.trim().length < 8}>
          Save
        </Button>
      </div>
      {source.key_origin === "vault" && canEdit && (
        <ConfirmButton
          size="sm"
          variant="ghost"
          icon="delete"
          confirmLabel="Remove key?"
          className="self-start"
          onConfirm={() => void runMutation(() => apiClient.delete(`/omni/fleet/${id}/api-key`), { invalidate: ["fleet"], success: `${source.display_name} key removed` })}
        >
          Remove stored key
        </ConfirmButton>
      )}
      {!vaultReady && <p className="text-[11px] text-rose-600 dark:text-rose-300">MASTER_VAULT_KEY is not configured on the server, so keys can't be stored.</p>}
    </form>
  );
};

// ---------------------------------------------------------------------------
// ADAPTER CARD
// ---------------------------------------------------------------------------
const AdapterCard = ({ source, canEdit, vaultReady }: { source: FleetSource; canEdit: boolean; vaultReady: boolean }) => {
  const [pending, setPending] = useState<boolean | null>(null);
  const [running, setRunning] = useState(false);
  const enabled = pending ?? source.is_enabled;
  const fatal = source.status === "FATAL";
  const id = source.source_id;

  const toggle = async (next: boolean) => {
    setPending(next);
    await runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}`, { is_enabled: next }), {
      invalidate: ["fleet"],
      success: next ? (fatal ? `${source.display_name} cleared and resumed` : `${source.display_name} is live`) : `${source.display_name} paused across the fleet`,
      errorTitle: "Toggle rejected",
    });
    setPending(null);
  };

  const setCadence = (raw: string) =>
    void runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}`, { interval_seconds: raw === "default" ? null : Number(raw) }), {
      invalidate: ["fleet"],
      success: `${source.display_name} cadence updated`,
    });

  const runNow = async () => {
    setRunning(true);
    await runMutation(() => apiClient.post<{ dispatched_to: string }>(`/omni/fleet/${id}/run`), {
      invalidate: ["fleet"],
      success: (r) => `${source.display_name} run dispatched to ${r.dispatched_to === "celery" ? "a Celery worker" : "the API process"}`,
      errorTitle: "Run rejected",
    });
    window.setTimeout(() => setRunning(false), 1_500);
  };

  const cadenceValue = source.interval_seconds === source.default_interval_seconds ? "default" : String(source.interval_seconds);

  return (
    <motion.article
      variants={ITEM_VARIANTS}
      layout
      className={cx(
        "flex flex-col gap-5 rounded-[1.375rem] p-5 ring-1 ring-inset transition-[background-color,box-shadow] duration-500",
        enabled
          ? "bg-[#FBFAF6] ring-[color-mix(in_srgb,var(--accent)_22%,transparent)] dark:bg-white/[0.035]"
          : "bg-stone-50/60 ring-stone-900/[0.05] dark:bg-white/[0.015] dark:ring-white/[0.04]",
      )}
    >
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-display text-base font-semibold text-stone-900 dark:text-stone-50">{source.display_name}</h3>
            <StatusBadge status={source.status} label={source.status === "NEEDS_KEY" ? "Needs key" : undefined} />
          </div>
          <p className="mt-1 text-xs leading-relaxed text-stone-500 dark:text-stone-400">{source.description}</p>
        </div>
        <FleetSwitch on={enabled} onChange={(v) => void toggle(v)} label={`${source.display_name} ingestion`} disabled={!canEdit} busy={pending !== null} />
      </header>

      <AnimatePresence initial={false}>
        {fatal && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={SPRING}
            className="overflow-hidden"
          >
            <div role="alert" className="flex gap-3 rounded-2xl bg-rose-50 px-4 py-3 text-xs text-rose-800 dark:bg-rose-500/10 dark:text-rose-200">
              <span className="material-symbols-outlined text-[18px]">report</span>
              <div className="min-w-0">
                <p className="font-semibold">Paused after {source.consecutive_failures} consecutive failures (dead-lettered {formatAgo(source.paused_at)}).</p>
                {source.last_error && <p className="mt-0.5 break-words font-mono text-[11px] opacity-80">{source.last_error}</p>}
                <p className="mt-1 opacity-80">Fix the cause, then switch it back on to resume.</p>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <SecureKeyField source={source} canEdit={canEdit} vaultReady={vaultReady} />

      <footer className="flex flex-wrap items-end justify-between gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-stone-500 dark:text-stone-400">Cadence</span>
          <Select value={cadenceValue} onChange={(e) => setCadence(e.target.value)} disabled={!canEdit} className="w-44 py-2">
            <option value="default">Default · every {seconds(source.default_interval_seconds)}</option>
            {CADENCES.filter((c) => c !== source.default_interval_seconds).map((c) => (
              <option key={c} value={c}>Every {seconds(c)}</option>
            ))}
          </Select>
        </label>
        <div className="flex items-center gap-2">
          {source.unmapped_count > 0 && (
            <span title={`Not in the alias dictionary (provisional ids used):\n${source.unmapped.join("\n")}`} className="cursor-help">
              <Pill tone="warning" icon="person_search">{source.unmapped_count} unmapped</Pill>
            </span>
          )}
          {source.docs_url && (
            <a href={source.docs_url} target="_blank" rel="noreferrer noopener" className="text-xs font-medium text-stone-500 underline-offset-4 hover:text-stone-800 hover:underline dark:text-stone-400 dark:hover:text-stone-100">
              API docs
            </a>
          )}
          <Button size="sm" icon="play_arrow" busy={running} disabled={!canEdit || !enabled || fatal || source.status === "NEEDS_KEY"} onClick={() => void runNow()}>
            Run now
          </Button>
        </div>
      </footer>
    </motion.article>
  );
};

// ---------------------------------------------------------------------------
// LIVE HEALTH MATRIX
// ---------------------------------------------------------------------------
const pingTone = (ms: number | null): string =>
  ms === null ? "text-stone-400" : ms <= 400 ? "text-emerald-700 dark:text-emerald-300/90" : ms <= 1200 ? "text-amber-700 dark:text-amber-200/90" : "text-rose-600 dark:text-rose-300/90";

const HealthMatrix = ({ sources, now }: { sources: FleetSource[]; now: number }) => {
  const columns: Column<FleetSource>[] = [
    {
      key: "adapter",
      header: "Adapter",
      render: (s) => {
        const fresh = s.last_success_at !== null && now - new Date(s.last_success_at).getTime() < s.interval_seconds * 2_500;
        return (
          <span className="flex items-center gap-2.5">
            <LiveDot active={s.status === "HEALTHY" && fresh} tone={s.status === "FATAL" ? "critical" : s.status === "DEGRADED" ? "warning" : "good"} />
            <span className="min-w-0">
              <span className="block truncate font-medium text-stone-800 dark:text-stone-100">{s.display_name}</span>
              <span className="text-[11px] text-stone-400">{s.runner ? `via ${s.runner}` : "not run yet"}</span>
            </span>
          </span>
        );
      },
    },
    { key: "status", header: "Status", render: (s) => <StatusBadge status={s.status} label={s.status === "NEEDS_KEY" ? "Needs key" : undefined} /> },
    {
      key: "ping",
      header: "Ping",
      align: "right",
      render: (s) => <span className={cx("whitespace-nowrap", pingTone(s.ping_ms))}>{s.ping_ms === null ? "—" : `${formatInt(s.ping_ms)} ms`}</span>,
    },
    {
      key: "success",
      header: "Success rate",
      className: "min-w-[150px]",
      render: (s) =>
        s.success_rate === null ? (
          <span className="text-xs text-stone-400">no runs yet</span>
        ) : (
          <span className="flex items-center gap-2.5">
            <span className="w-16"><Meter value={s.success_rate} tone={s.success_rate >= 0.95 ? "good" : s.success_rate >= 0.8 ? "warning" : "critical"} label={`${s.display_name} success rate`} /></span>
            <span className="font-mono text-xs tabular-nums">{formatRatioPct(s.success_rate, 0)}</span>
            <span className="text-[11px] text-stone-400">of {s.runs_in_window}</span>
          </span>
        ),
    },
    {
      key: "sync",
      header: "Last sync",
      render: (s) => {
        const next = s.last_attempt_at && s.is_enabled && s.status !== "FATAL" ? new Date(s.last_attempt_at).getTime() + s.interval_seconds * 1_000 - now : null;
        return (
          <span className="flex flex-col whitespace-nowrap">
            <span className="text-stone-700 dark:text-stone-200">{formatAgo(s.last_success_at, now)}</span>
            <span className="text-[11px] text-stone-400">{next === null ? `every ${seconds(s.interval_seconds)}` : next > 0 ? `next in ${seconds(next / 1_000)}` : "due now"}</span>
          </span>
        );
      },
    },
    {
      key: "throughput",
      header: "Throughput",
      align: "right",
      render: (s) => (s.ticks_last_run === null ? "—" : <span title={`${s.fixtures_last_run ?? 0} fixtures`}>{formatInt(s.ticks_last_run)} <span className="text-[11px] text-stone-400">ticks</span></span>),
    },
    {
      key: "quota",
      header: "Quota",
      align: "right",
      render: (s) => (s.quota_remaining === null ? <span className="text-stone-400">{s.requires_api_key ? "—" : "∞"}</span> : <span className={s.quota_remaining < 50 ? "text-amber-700 dark:text-amber-200/90" : undefined}>{formatInt(s.quota_remaining)}</span>),
    },
    {
      key: "failures",
      header: "Failures",
      align: "right",
      render: (s) => <span className={s.consecutive_failures > 0 ? "text-rose-600 dark:text-rose-300/90" : "text-stone-400"}>{s.consecutive_failures}/{s.failure_threshold}</span>,
    },
  ];
  return <DataTable columns={columns} rows={sources} rowKey={(s) => s.source_id} dense />;
};

// ---------------------------------------------------------------------------
// EVENT HORIZON: what the workers just pushed, straight off the Omni live channel
// ---------------------------------------------------------------------------
function toHorizon(data: unknown): HorizonEntry | null {
  if (!data || typeof data !== "object") return null;
  const m = data as { kind?: string; provider_name?: string; payload?: Record<string, unknown>; at?: string };
  const at = m.at ? new Date(m.at).getTime() : Date.now();
  const p = m.payload ?? {};
  if (m.kind === "omni.fleet.batch") {
    const scope = Array.isArray(p.scope) ? (p.scope as string[]).join(", ") : "";
    return { id: `${m.kind}-${m.provider_name}-${at}`, kind: "batch", title: `${m.provider_name ?? "Source"} · ${formatInt(Number(p.ticks ?? 0))} ticks`, detail: `${formatInt(Number(p.fixtures ?? 0))} fixtures${scope ? ` · ${scope}` : ""} · ${formatInt(Number(p.latency_ms ?? 0))} ms`, tone: "good", at };
  }
  if (m.kind === "omni.quorum") {
    const quarantined = Number(p.quarantined ?? 0);
    return { id: `${m.kind}-${at}`, kind: "quorum", title: `Quorum · ${formatInt(Number(p.resolved ?? 0))} agreed`, detail: quarantined ? `${quarantined} quarantined (sources disagree)` : "every multi-source cell agrees", tone: quarantined ? "warning" : "neutral", at };
  }
  return null;
}

const EventHorizon = ({ boardCells }: { boardCells: number | null }) => {
  const [entries, setEntries] = useState<HorizonEntry[]>([]);
  const feedConnected = useMarketStore((s) => s.isConnected);
  const liveMatches = useMarketStore((s) => Object.keys(s.matches).length);

  useEffect(
    () =>
      subscribeChannel(
        "/omni/ws/stream",
        (data) => {
          const entry = toHorizon(data);
          if (entry) setEntries((prev) => [entry, ...prev.filter((e) => e.id !== entry.id)].slice(0, 10));
        },
        "kind",
      ),
    [],
  );

  return (
    <Panel title="Event horizon" icon="satellite_alt" className="lg:col-span-12 2xl:col-span-4" subtitle="live from the workers">
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-2xl bg-stone-50 px-4 py-3 dark:bg-white/[0.03]">
            <p className="text-[11px] text-stone-500 dark:text-stone-400">Board cells (Redis)</p>
            <p className="font-mono text-lg tabular-nums text-stone-800 dark:text-stone-100">{boardCells === null ? "—" : formatInt(boardCells)}</p>
          </div>
          <div className="rounded-2xl bg-stone-50 px-4 py-3 dark:bg-white/[0.03]">
            <p className="flex items-center gap-1.5 text-[11px] text-stone-500 dark:text-stone-400"><LiveDot active={feedConnected} /> Your live feed</p>
            <p className="font-mono text-lg tabular-nums text-stone-800 dark:text-stone-100">{formatInt(liveMatches)} <span className="text-xs text-stone-400">matches</span></p>
          </div>
        </div>
        {entries.length === 0 ? (
          <EmptyState icon="satellite_alt" title="Listening for the next batch" detail="Each ingest run and quorum sweep appears here the moment a worker publishes it." />
        ) : (
          <motion.ul variants={LIST_VARIANTS} initial="hidden" animate="show" className="flex flex-col gap-1.5">
            <AnimatePresence initial={false}>
              {entries.map((e) => (
                <motion.li
                  key={e.id}
                  layout
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={SPRING}
                  className="flex items-start gap-3 rounded-2xl px-3 py-2.5 transition-colors hover:bg-stone-50 dark:hover:bg-white/[0.03]"
                >
                  <span
                    className={cx(
                      "mt-0.5 grid size-7 shrink-0 place-items-center rounded-full",
                      e.tone === "good" ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300" : e.tone === "warning" ? "bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-200" : "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-300",
                    )}
                  >
                    <span className="material-symbols-outlined text-[15px]">{e.kind === "batch" ? "download" : "balance"}</span>
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-stone-800 dark:text-stone-100">{e.title}</span>
                    <span className="block truncate text-[11px] text-stone-500 dark:text-stone-400">{e.detail}</span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] tabular-nums text-stone-400">{formatTime(e.at)}</span>
                </motion.li>
              ))}
            </AnimatePresence>
          </motion.ul>
        )}
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: FLEET COMMAND
// ---------------------------------------------------------------------------
export const FleetCommand = () => {
  const fleet = useFleet();
  const deadLetters = useDeadLetters();
  const canEdit = useAuthStore((s) => s.user?.role === "ADMIN");
  const now = useNow();
  const data = fleet.data;
  const live = useMemo(() => (data?.sources ?? []).filter((s) => s.status === "HEALTHY").length, [data]);
  const [modeLabel, modeTone, modeIcon] = MODE_LABEL[data?.mode ?? "offline"];

  return (
    <>
      <Panel
        title="Fleet command"
        icon="hub"
        className="lg:col-span-12"
        updatedAt={fleet.updatedAt}
        subtitle="sanctioned sports-data APIs"
        actions={
          data && (
            <div className="hidden flex-wrap items-center gap-2 sm:flex">
              <Pill tone={modeTone} icon={modeIcon}>{modeLabel}</Pill>
              <Pill tone={data.redis_available ? "good" : "critical"} icon="bolt">Redis {data.redis_available ? "up" : "down"}</Pill>
              <Pill tone="neutral">{live}/{data.sources.length} healthy</Pill>
            </div>
          )
        }
      >
        <Async resource={fleet} skeletonRows={3}>
          {(overview) => (
            <div className="flex flex-col gap-4">
              {!canEdit && (
                <p className="flex items-center gap-2 px-1 text-xs text-stone-500 dark:text-stone-400">
                  <span className="material-symbols-outlined text-[15px]">shield_person</span>
                  Read-only: switching sources and storing keys needs an admin account.
                </p>
              )}
              <motion.div variants={LIST_VARIANTS} initial="hidden" animate="show" className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                {overview.sources.map((source) => (
                  <AdapterCard key={source.source_id} source={source} canEdit={canEdit} vaultReady={overview.vault_configured} />
                ))}
              </motion.div>
            </div>
          )}
        </Async>
      </Panel>

      <Panel title="Live health matrix" icon="monitor_heart" className="lg:col-span-12 2xl:col-span-8" updatedAt={fleet.updatedAt} subtitle="per adapter, refreshed as workers report">
        <Async resource={fleet} isEmpty={(o) => o.sources.length === 0} empty={<EmptyState icon="monitor_heart" title="No adapters registered" />}>
          {(overview) => (
            <div className="flex flex-col gap-4">
              <HealthMatrix sources={overview.sources} now={now} />
              {(deadLetters.data?.length ?? 0) > 0 && (
                <div className="flex flex-col gap-2 pt-2">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">
                    <span className="material-symbols-outlined text-[15px]">move_to_inbox</span> Dead letters
                  </p>
                  <ul className="flex flex-col gap-1">
                    {deadLetters.data!.slice(0, 4).map((d) => (
                      <li key={`${d.source_id}-${d.at}`} className="flex items-baseline justify-between gap-3 rounded-xl bg-rose-50/60 px-3 py-2 text-xs dark:bg-rose-500/[0.06]">
                        <span className="min-w-0 truncate"><span className="font-semibold text-rose-800 dark:text-rose-200">{d.source_id}</span> <span className="text-stone-500 dark:text-stone-400">· {d.failures} failures · {d.error}</span></span>
                        <span className="shrink-0 font-mono text-[11px] text-stone-400">{formatAgo(d.at, now)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Async>
      </Panel>

      <EventHorizon boardCells={data?.board_cells ?? null} />
    </>
  );
};
