/** The Health & Failover Matrix: every ingestion source, sortable, filterable, paginated. */
import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { apiClient } from "../../api/client";
import { formatAgo, formatInt, formatRatioPct } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import { Button, ConfirmButton, Meter, Pill, SPRING, Segmented, Select, StatusBadge } from "../../ui/kit";
import { BreakerCell, FailoverCell, FleetSwitch, QuotaBar, SecureKeyInline } from "./controls";
import { type FleetOverview, type FleetSource, cx, groupLabel, seconds } from "./types";

const PAGE_SIZE = 10;
const CADENCES = [15, 30, 60, 120, 300, 900] as const;

type SortKey = "name" | "status" | "quota" | "latency" | "priority";
type Filter = "all" | "live" | "tripped" | "failover" | "off";

const STATUS_RANK: Record<string, number> = { FATAL: 0, TRIPPED: 1, DEGRADED: 2, QUOTA_RESERVE: 3, NEEDS_KEY: 4, IDLE: 5, HEALTHY: 6, DISABLED: 7 };

const sortValue = (s: FleetSource, key: SortKey): number | string => {
  switch (key) {
    case "name":
      return s.display_name.toLowerCase();
    case "status":
      return STATUS_RANK[s.status] ?? 9;
    case "quota":
      return s.cost === "free" ? 2 : s.quota_fraction ?? 1.5;
    case "latency":
      return s.ping_ms ?? Number.MAX_SAFE_INTEGER;
    default:
      return s.priority;
  }
};

const matchesFilter = (s: FleetSource, filter: Filter): boolean => {
  if (filter === "live") return s.is_enabled && s.breaker_state === "closed" && s.status !== "FATAL";
  if (filter === "tripped") return s.breaker_state === "open" || s.status === "FATAL" || s.status === "DEGRADED";
  if (filter === "failover") return s.role === "failover" || s.status === "QUOTA_RESERVE";
  if (filter === "off") return !s.is_enabled;
  return true;
};

const pingTone = (ms: number | null): string =>
  ms === null ? "text-stone-400" : ms <= 400 ? "text-emerald-700 dark:text-emerald-300/90" : ms <= 1200 ? "text-amber-700 dark:text-amber-200/90" : "text-rose-600 dark:text-rose-300/90";

// --------------------------------------------------------------------------- row
const Row = ({
  source,
  overview,
  names,
  canEdit,
  open,
  onToggleOpen,
  onEdit,
  now,
  viewport,
}: {
  source: FleetSource;
  overview: FleetOverview;
  names: Record<string, string>;
  canEdit: boolean;
  open: boolean;
  onToggleOpen: () => void;
  onEdit: (source: FleetSource) => void;
  now: number;
  viewport: number | null;
}) => {
  const [pending, setPending] = useState<boolean | null>(null);
  const [running, setRunning] = useState(false);
  const enabled = pending ?? source.is_enabled;
  const fatal = source.status === "FATAL";
  const id = source.source_id;

  const toggle = async (next: boolean) => {
    setPending(next);
    await runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}`, { is_enabled: next }), {
      invalidate: ["fleet"],
      success: next ? (fatal ? `${source.display_name} cleared and resumed` : `${source.display_name} is live`) : `${source.display_name} switched off fleet-wide`,
      errorTitle: "Toggle rejected",
    });
    setPending(null);
  };

  const runNow = async () => {
    setRunning(true);
    await runMutation(() => apiClient.post<{ dispatched_to: string }>(`/omni/fleet/${id}/run`), {
      invalidate: ["fleet"],
      success: (r) => `${source.display_name} trial run sent to ${r.dispatched_to === "celery" ? "a Celery worker" : "the API process"}`,
      errorTitle: "Run rejected",
    });
    window.setTimeout(() => setRunning(false), 1_500);
  };

  const setCadence = (raw: string) =>
    void runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}`, { interval_seconds: raw === "default" ? null : Number(raw) }), {
      invalidate: ["fleet"],
      success: `${source.display_name} cadence updated`,
    });

  const remove = () =>
    void runMutation(() => apiClient.delete(`/omni/fleet/providers/${id}`), { invalidate: ["fleet"], success: `${source.display_name} removed from the matrix` });

  const cadenceValue = source.interval_seconds === source.default_interval_seconds ? "default" : String(source.interval_seconds);

  return (
    <Fragment>
      <tr className={cx("group transition-colors duration-300", open ? "bg-stone-50/80 dark:bg-white/[0.025]" : "hover:bg-stone-50/60 dark:hover:bg-white/[0.02]")}>
        <td className="py-4 pl-5 pr-2 sm:pl-6">
          <button type="button" onClick={onToggleOpen} aria-expanded={open} className="flex min-w-0 items-center gap-2 text-left">
            <motion.span animate={{ rotate: open ? 90 : 0 }} transition={SPRING} className="material-symbols-outlined text-[18px] text-stone-300 group-hover:text-stone-500 dark:text-stone-600">
              chevron_right
            </motion.span>
            <span className="min-w-0">
              <span className="block max-w-[9.5rem] truncate font-medium text-stone-800 dark:text-stone-100">{source.display_name}</span>
              <span className="block whitespace-nowrap text-[10px] text-stone-400 dark:text-stone-500">
                {source.kind === "config" ? "config" : "built-in"} · {source.cost} · p{source.priority}
              </span>
              <span className="block whitespace-nowrap text-[10px] text-stone-400 dark:text-stone-500">synced {formatAgo(source.last_success_at, now)}</span>
            </span>
          </button>
        </td>
        <td className="px-2 py-4">
          <FleetSwitch on={enabled} onChange={(v) => void toggle(v)} label={`${source.display_name} ingestion`} disabled={!canEdit} busy={pending !== null} />
        </td>
        <td className="px-2 py-4">
          <div className="flex flex-col items-start gap-1">
            <BreakerCell source={source} />
            {source.status !== "HEALTHY" && <StatusBadge status={source.status} label={source.status === "NEEDS_KEY" ? "Needs key" : source.status === "QUOTA_RESERVE" ? "Quota reserve" : undefined} />}
          </div>
        </td>
        <td className="px-2 py-4">
          <QuotaBar source={source} reserve={overview.quota_reserve} />
        </td>
        <td className="px-2 py-4">
          <FailoverCell source={source} names={names} />
        </td>
        <td className="px-2 py-4">
          <div className="flex flex-col gap-1.5">
            <span className={cx("whitespace-nowrap font-mono text-xs tabular-nums", pingTone(source.ping_ms))}>{source.ping_ms === null ? "—" : `${formatInt(source.ping_ms)} ms`}</span>
            {source.success_rate !== null && (
              <span className="flex items-center gap-1.5" title={`${source.runs_in_window} recent runs`}>
                <span className="w-10">
                  <Meter value={source.success_rate} tone={source.success_rate >= 0.95 ? "good" : source.success_rate >= 0.8 ? "warning" : "critical"} label={`${source.display_name} success rate`} />
                </span>
                <span className="font-mono text-[10px] tabular-nums text-stone-500 dark:text-stone-400">{formatRatioPct(source.success_rate, 0)}</span>
              </span>
            )}
          </div>
        </td>
        <td className="py-4 pl-2 pr-5 sm:pr-6">
          <SecureKeyInline source={source} canEdit={canEdit} vaultReady={overview.vault_configured} compact />
        </td>
      </tr>
      <AnimatePresence initial={false}>
        {open && (
          <tr key={`${id}-detail`}>
            <td colSpan={7} className="p-0">
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={SPRING} className="overflow-hidden">
                {/* Pinned to the visible width, so a horizontally scrolled table never hides the controls */}
                <div
                  style={viewport ? { width: viewport } : undefined}
                  className="sticky left-0 grid grid-cols-1 gap-6 bg-stone-50/80 px-6 pb-6 pt-2 sm:px-14 lg:grid-cols-3 dark:bg-white/[0.025]"
                >
                  <Detail label="About">
                    <p className="text-xs leading-relaxed text-stone-600 dark:text-stone-300">{source.description || "No description."}</p>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {source.coverage.map((g) => (
                        <Pill key={g} tone={source.scope.includes(g) ? "accent" : "neutral"}>{groupLabel(g)}</Pill>
                      ))}
                    </div>
                    {source.docs_url && (
                      <a href={source.docs_url} target="_blank" rel="noreferrer noopener" className="mt-2 inline-block text-xs font-medium text-stone-500 underline-offset-4 hover:text-stone-800 hover:underline dark:text-stone-400 dark:hover:text-stone-100">
                        API documentation
                      </a>
                    )}
                  </Detail>
                  <Detail label="Throughput & safety">
                    <Kv k="Last run" v={source.ticks_last_run === null ? "—" : `${formatInt(source.ticks_last_run)} ticks · ${formatInt(source.fixtures_last_run)} fixtures`} />
                    <Kv k="Malformed events" v={source.malformed_last_run === null ? "—" : formatInt(source.malformed_last_run)} />
                    <Kv k="De-vig" v={Object.keys(source.devig).length ? Object.entries(source.devig).map(([m, n]) => `${m} ${n}`).join(" · ") : "—"} />
                    <Kv k="Rate limit" v={`${formatInt(source.rate_limit_rpm)}/min · burst ${source.burst}${source.throttled_ms ? ` · waited ${seconds(source.throttled_ms / 1000)}` : ""}`} />
                    <Kv k="Failure streak" v={`${source.consecutive_failures} / ${source.failure_threshold} before dead-letter`} />
                    {source.unmapped_count > 0 && <Kv k="Unmapped names" v={<span title={source.unmapped.join("\n")}>{source.unmapped_count} (hover)</span>} />}
                  </Detail>
                  <Detail label="Control">
                    {source.last_error && (
                      <p className="mb-2 break-words rounded-2xl bg-rose-50 px-3 py-2 font-mono text-[11px] text-rose-800 dark:bg-rose-500/10 dark:text-rose-200">{source.last_error}</p>
                    )}
                    <label className="flex items-center justify-between gap-3 text-xs text-stone-500 dark:text-stone-400">
                      Cadence
                      <Select value={cadenceValue} onChange={(e) => setCadence(e.target.value)} disabled={!canEdit} className="w-40 py-1.5 text-xs">
                        <option value="default">Default · {seconds(source.default_interval_seconds)}</option>
                        {CADENCES.filter((c) => c !== source.default_interval_seconds).map((c) => (
                          <option key={c} value={c}>Every {seconds(c)}</option>
                        ))}
                      </Select>
                    </label>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button size="sm" icon="play_arrow" busy={running} disabled={!canEdit || !enabled || fatal || source.status === "NEEDS_KEY"} onClick={() => void runNow()}>
                        Trial run
                      </Button>
                      {source.kind === "config" && (
                        <>
                          <Button size="sm" icon="data_object" disabled={!canEdit} onClick={() => onEdit(source)}>
                            Edit mapping
                          </Button>
                          <ConfirmButton size="sm" variant="danger" icon="delete" confirmLabel="Remove provider?" disabled={!canEdit} onConfirm={remove}>
                            Remove
                          </ConfirmButton>
                        </>
                      )}
                    </div>
                  </Detail>
                </div>
              </motion.div>
            </td>
          </tr>
        )}
      </AnimatePresence>
    </Fragment>
  );
};

const Detail = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex min-w-0 flex-col gap-1.5">
    <p className="text-[11px] font-medium uppercase tracking-wide text-stone-400 dark:text-stone-500">{label}</p>
    {children}
  </div>
);

const Kv = ({ k, v }: { k: string; v: ReactNode }) => (
  <div className="flex items-baseline justify-between gap-4 text-xs">
    <span className="text-stone-500 dark:text-stone-400">{k}</span>
    <span className="min-w-0 truncate text-right font-medium tabular-nums text-stone-700 dark:text-stone-200">{v}</span>
  </div>
);

// --------------------------------------------------------------------------- matrix
export const FleetMatrix = ({ overview, canEdit, now, onEdit }: { overview: FleetOverview; canEdit: boolean; now: number; onEdit: (source: FleetSource) => void }) => {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "priority", dir: 1 });
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState<number | null>(null);

  // The visible width of the scrolling table (expanded rows pin themselves to it)
  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => setViewport(Math.round(entry.contentRect.width)));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const names = useMemo(() => Object.fromEntries(overview.sources.map((s) => [s.source_id, s.display_name])), [overview.sources]);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return overview.sources
      .filter((s) => matchesFilter(s, filter))
      .filter((s) => !q || s.display_name.toLowerCase().includes(q) || s.source_id.includes(q) || s.coverage.some((g) => g.includes(q)))
      .sort((a, b) => {
        const va = sortValue(a, sort.key);
        const vb = sortValue(b, sort.key);
        return (va < vb ? -1 : va > vb ? 1 : a.source_id.localeCompare(b.source_id)) * sort.dir;
      });
  }, [overview.sources, filter, query, sort]);

  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1); // a filter can shrink the list under the current page
  const visible = rows.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE);

  const header = (label: string, key?: SortKey, align: "left" | "right" = "left", extra = "") => (
    <th scope="col" className={cx("whitespace-nowrap px-2 pb-3 pt-1 text-xs font-medium text-stone-400 dark:text-stone-500", align === "right" && "text-right", extra)}>
      {key ? (
        <button
          type="button"
          onClick={() => setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }))}
          className={cx("inline-flex items-center gap-0.5 transition-colors hover:text-stone-700 dark:hover:text-stone-200", sort.key === key && "text-stone-700 dark:text-stone-200")}
          aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : undefined}
        >
          {label}
          <span className="material-symbols-outlined text-[14px]">{sort.key === key ? (sort.dir === 1 ? "arrow_upward" : "arrow_downward") : "unfold_more"}</span>
        </button>
      ) : (
        label
      )}
    </th>
  );

  const counts = useMemo(
    () => ({
      all: overview.sources.length,
      live: overview.sources.filter((s) => matchesFilter(s, "live")).length,
      tripped: overview.sources.filter((s) => matchesFilter(s, "tripped")).length,
      failover: overview.sources.filter((s) => matchesFilter(s, "failover")).length,
      off: overview.sources.filter((s) => matchesFilter(s, "off")).length,
    }),
    [overview.sources],
  );

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented
          label="Filter providers"
          size="sm"
          value={filter}
          onChange={(v) => {
            setFilter(v);
            setPage(0);
          }}
          options={[
            { value: "all", label: `All ${counts.all}` },
            { value: "live", label: `Live ${counts.live}` },
            { value: "tripped", label: `Tripped ${counts.tripped}` },
            { value: "failover", label: `Failover ${counts.failover}` },
            { value: "off", label: `Off ${counts.off}` },
          ]}
        />
        <label className="relative w-full sm:w-64">
          <span className="material-symbols-outlined pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[16px] text-stone-400">search</span>
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
            placeholder="Search providers or markets"
            aria-label="Search providers"
            className="w-full rounded-full bg-stone-100/80 py-2 pl-9 pr-4 text-sm text-stone-800 placeholder:text-stone-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--accent)_40%,transparent)] dark:bg-stone-800/70 dark:text-stone-100 dark:focus:bg-stone-800"
          />
        </label>
      </div>

      <div ref={scroller} className="-mx-5 overflow-x-auto sm:-mx-6">
        <table className="w-full min-w-[840px] text-left text-sm">
          <thead>
            <tr>
              {header("Provider", "name", "left", "pl-5 sm:pl-6")}
              {header("On")}
              {header("Circuit", "status")}
              {header("Quota", "quota")}
              {header("Failover")}
              {header("Health", "latency")}
              {header("API key", undefined, "left", "pr-5 sm:pr-6")}
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-900/[0.04] dark:divide-white/[0.04]">
            {visible.map((source) => (
              <Row
                key={source.source_id}
                source={source}
                overview={overview}
                names={names}
                canEdit={canEdit}
                open={open === source.source_id}
                onToggleOpen={() => setOpen((o) => (o === source.source_id ? null : source.source_id))}
                viewport={viewport}
                onEdit={onEdit}
                now={now}
              />
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <p className="px-6 py-10 text-center text-sm text-stone-400">No provider matches this view.</p>}
      </div>

      <div className="flex items-center justify-between gap-3 text-xs text-stone-500 dark:text-stone-400">
        <span>
          {rows.length === 0 ? "0" : `${current * PAGE_SIZE + 1}–${Math.min(rows.length, (current + 1) * PAGE_SIZE)}`} of {rows.length}
        </span>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="ghost" icon="chevron_left" disabled={current === 0} onClick={() => setPage(current - 1)} aria-label="Previous page" />
          <span className="font-mono tabular-nums">{current + 1} / {pages}</span>
          <Button size="sm" variant="ghost" icon="chevron_right" disabled={current >= pages - 1} onClick={() => setPage(current + 1)} aria-label="Next page" />
        </div>
      </div>
    </div>
  );
};
