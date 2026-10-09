/**
 * Control Panel, Nalanda: the tick lake and the hash-chained settlement warehouse (Group 67).
 *
 * - Storage telemetry: database size, ingestion velocity (and its last ten minutes), BRIN index
 *   efficiency, dead tuples, the Parquet cold tier, the firehose backlog, ghost spikes caught.
 * - Partitions: every weekly partition with its size, rows, dead tuples and last vacuum.
 * - Integrity: the SHA-256 chain head, a ledger verification on demand, the rebuild preview and the
 *   maintenance tasks (administrators).
 * - Tick explorer and settlement explorer: forensic search of line movements and of the raw
 *   bookmaker receipts, every record with its hashes.
 */
import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, apiClient } from "../../api/client";
import { formatAgo, formatDateTime, formatInt, formatSignedINR, humanize } from "../../lib/format";
import { useIsDark } from "../../lib/lab";
import {
  type ArchiveRecordView,
  type ColdExportRow,
  MAINTENANCE_TASKS,
  type MaintenanceTask,
  RECORD_KINDS,
  type RebuildReport,
  type Telemetry,
  type TickRow,
  type VerifyReport,
  formatBytes,
  useColdExports,
  useNalandaTelemetry,
} from "../../lib/nalanda";
import { invalidate } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";
import { Async, Button, DataTable, EmptyState, Field, KeyValues, NumberInput, Panel, Pill, Select, Stat, StatGrid, TextInput, Toggle } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
// The two validated chart hues (see the Lab): blue for prices and volume, red for ghost spikes
const VIZ = {
  light: { series: "#2a78d6", spike: "#e34948", grid: "#ece9e6", axis: "#a8a29e", muted: "#a8a29e", surface: "#ffffff" },
  dark: { series: "#3987e5", spike: "#e66767", grid: "#292524", axis: "#57534e", muted: "#78716c", surface: "#1c1917" },
} as const;
const TASK_LABEL: Record<MaintenanceTask, string> = {
  preallocate: "Pre-allocate partitions", vacuum: "Vacuum old partitions", compress: "Roll up & tier to Parquet", mirror: "Mirror the ledger", anchor: "Anchor the chain", verify: "Verify (logged)",
};

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const short = (hash: string | null | undefined): string => (hash ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : "—");
const axisProps = (viz: (typeof VIZ)["light"] | (typeof VIZ)["dark"]) => ({ stroke: viz.axis, tick: { fill: viz.muted, fontSize: 11 }, tickLine: false, axisLine: { stroke: viz.axis } });
type TipProps = { active?: boolean; payload?: readonly { value?: unknown; payload?: unknown }[]; label?: unknown };

const Tip = ({ title, rows }: { title: string; rows: [string, string][] }) => (
  <div className="rounded-2xl bg-white px-3 py-2 text-xs shadow-soft-lg ring-1 ring-stone-900/5 dark:bg-stone-800 dark:ring-white/10">
    <p className="mb-1 font-medium text-stone-500 dark:text-stone-400">{title}</p>
    {rows.map(([k, v]) => (
      <p key={k} className="flex gap-3 text-stone-800 dark:text-stone-100">
        <span className="text-stone-500 dark:text-stone-400">{k}</span>
        <span className="ml-auto font-mono tabular-nums">{v}</span>
      </p>
    ))}
  </div>
);

// ---------------------------------------------------------------- telemetry
const VelocityChart = ({ series }: { series: { t: string; rows: number }[] }) => {
  const viz = useIsDark() ? VIZ.dark : VIZ.light;
  const data = useMemo(() => series.map((p) => ({ t: new Date(p.t).getTime(), rows: p.rows / 10 })), [series]);
  return (
    <div className="h-36 w-full" role="img" aria-label="Rows ingested per second over the last ten minutes">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={viz.grid} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} minTickGap={40} {...axisProps(viz)} />
          <YAxis width={44} allowDecimals={false} {...axisProps(viz)} />
          <Tooltip
            cursor={{ stroke: viz.axis, strokeWidth: 1 }}
            content={({ active, payload, label }: TipProps) => (active && payload?.length ? <Tip title={formatDateTime(Number(label))} rows={[["rows / s", Number(payload[0].value).toFixed(1)]]} /> : null)}
          />
          <Area type="monotone" dataKey="rows" stroke={viz.series} strokeWidth={2} fill={viz.series} fillOpacity={0.1} isAnimationActive={false} activeDot={{ r: 4, stroke: viz.surface, strokeWidth: 2, fill: viz.series }} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
};

const TelemetryView = ({ t }: { t: Telemetry }) => {
  const brin = Object.values(t.storage.brin);
  const index = brin.reduce((a, b) => a + b.index_bytes, 0);
  const heap = brin.reduce((a, b) => a + b.heap_bytes, 0);
  const lakeBytes = Object.values(t.storage.tables).reduce((a, b) => a + b.bytes, 0);
  const fh = t.firehose;
  const warnings: string[] = [];
  if (t.default_partition_rows > 0) warnings.push(`${formatInt(t.default_partition_rows)} row(s) in a default partition: a week was not pre-allocated (the next pre-allocation moves them into place)`);
  if (!fh.available) warnings.push("The firehose stream cannot be read (Redis unavailable)");
  else if (!fh.leader) warnings.push("No firehose consumer holds the lease: ingestion is paused");
  return (
    <div className="flex flex-col gap-6">
      {warnings.map((w) => (
        <p key={w} className="flex items-start gap-2 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-500/10 dark:text-amber-200/90">
          <span className="material-symbols-outlined text-[16px]">warning</span>
          {w}
        </p>
      ))}
      <StatGrid cols={4}>
        <Stat label="Database size" value={formatBytes(t.storage.database_bytes)} hint={`the lake: ${formatBytes(lakeBytes)}`} icon="database" />
        <Stat label="Ingestion velocity" value={`${(fh.rows_per_second_1m ?? 0).toFixed(1)} rows/s`} hint={`${formatInt(Number(fh.totals?.ticks ?? 0))} ticks, ${formatInt(Number(fh.totals?.records ?? 0))} records`} icon="speed" />
        <Stat
          label="BRIN size vs heap"
          // A BRIN index has a fixed floor, so a tiny lake reads high; at scale this falls to thousandths of a percent.
          value={heap && index ? `${((index / heap) * 100).toFixed(index / heap < 0.01 ? 3 : 1)}%` : "—"}
          hint={heap && index ? `${formatBytes(index)} indexes ${formatBytes(heap)}` : "no data yet"}
          icon="schema"
        />
        <Stat label="Dead tuples" value={formatInt(t.dead_tuples)} hint={t.maintenance.vacuum ? `vacuumed ${formatAgo(t.maintenance.vacuum.finished_at)}` : "not vacuumed yet"} icon="cleaning_services" tone={t.dead_tuples > 100_000 ? "caution" : "neutral"} />
        <Stat label="Parquet cold tier" value={formatBytes(t.cold.parquet.bytes)} hint={`${formatInt(t.cold.parquet.files)} file(s)${t.settings.s3_mirror ? " · S3 mirror on" : ""}`} icon="ac_unit" />
        <Stat label="Chain head" value={`#${formatInt(t.chain.head_seq)}`} hint={`${short(t.chain.head_hash)} · ${t.chain.anchors} anchor(s)`} icon="link" />
        <Stat label="Firehose backlog" value={fh.lag === null || fh.lag === undefined ? "—" : formatInt(fh.lag)} hint={`${formatInt(fh.pending ?? 0)} pending · ${formatInt(fh.stream_length ?? 0)} in stream`} icon="water" tone={(fh.lag ?? 0) > 10_000 ? "caution" : "neutral"} />
        <Stat label="Ghost spikes (24h)" value={formatInt(t.anomalies_24h)} hint={`of ${formatInt(t.ticks_24h)} ticks`} icon="blur_on" />
      </StatGrid>
      {fh.series && fh.series.length > 1 && (
        <div>
          <h3 className="mb-1 text-sm font-semibold text-stone-800 dark:text-stone-100">Ingestion, last ten minutes</h3>
          <VelocityChart series={fh.series} />
        </div>
      )}
      <p className="text-xs text-stone-500 dark:text-stone-400">
        Weekly partitions, {t.settings.weeks_ahead} weeks pre-allocated · ticks become 1-minute candles after {t.settings.rollup_after_days} days and Parquet after {t.settings.cold_after_days} ·
        candles after {t.settings.candle_retention_days} · forensic reads capped at {t.settings.read_work_mem} work_mem
      </p>
    </div>
  );
};

const Partitions = ({ t }: { t: Telemetry }) => {
  if (!t.partitions.length) return <EmptyState icon="calendar_view_week" title="No partitions" detail="Partitioning is PostgreSQL's; this database has plain tables." />;
  const rows = [...t.partitions].sort((a, b) => a.table.localeCompare(b.table) || (b.week_start ?? "").localeCompare(a.week_start ?? ""));
  return (
    <div className="max-h-[30rem] overflow-y-auto">
      <DataTable
        dense
        rows={rows}
        rowKey={(p) => p.partition}
        columns={[
          {
            key: "p",
            header: "Partition",
            render: (p) => (
              <span className="flex flex-col" title={p.partition}>
                <span className={cx("font-mono text-xs", p.is_default && p.rows_estimate > 0 && "text-amber-700 dark:text-amber-300")}>{p.partition.slice(p.table.length + 1)}</span>
                <span className="text-[11px] text-stone-400 dark:text-stone-500">{p.table.replace(/^nalanda_/, "")}</span>
              </span>
            ),
          },
          { key: "w", header: "Week", render: (p) => <span className="whitespace-nowrap text-xs">{p.week_start ? p.week_start.slice(0, 10) : "default"}</span> },
          { key: "r", header: "Rows", align: "right", render: (p) => formatInt(p.rows_estimate) },
          { key: "s", header: "Size", align: "right", render: (p) => <span className="whitespace-nowrap">{formatBytes(p.bytes)}</span> },
          { key: "d", header: "Dead", align: "right", render: (p) => formatInt(p.dead_tuples) },
          { key: "v", header: "Vacuumed", render: (p) => <span className="whitespace-nowrap text-xs">{p.last_vacuum ? formatAgo(p.last_vacuum) : "—"}</span> },
        ]}
      />
    </div>
  );
};

// ---------------------------------------------------------------- integrity
const VerifyResult = ({ report }: { report: VerifyReport }) => (
  <div className={cx("rounded-2xl p-4", report.ok ? "bg-emerald-50 dark:bg-emerald-500/10" : "bg-rose-50 dark:bg-rose-500/10")}>
    <div className="flex flex-wrap items-center gap-2">
      <Pill tone={report.ok ? "good" : "critical"} icon={report.ok ? "verified" : "gpp_bad"}>{report.ok ? "Chain intact" : `${report.failures_total} failure(s)`}</Pill>
      <span className="text-xs text-stone-600 dark:text-stone-300">
        {formatInt(report.rows)} rows · {report.anchors_checked} anchor(s) · {report.elapsed_ms.toFixed(0)} ms{report.cached ? ` · cached ${report.age_seconds?.toFixed(0)}s ago` : ""}
      </span>
    </div>
    {report.failures.length > 0 && (
      <ul className="mt-3 flex flex-col gap-1">
        {report.failures.map((f, i) => (
          <li key={`${f.seq}-${f.problem}-${i}`} className="text-xs text-rose-800 dark:text-rose-200">
            <span className="font-mono">#{f.seq ?? "—"}</span> {humanize(f.problem)}: {f.detail}
          </li>
        ))}
      </ul>
    )}
  </div>
);

const Integrity = ({ t, isAdmin }: { t: Telemetry; isAdmin: boolean }) => {
  const [report, setReport] = useState<VerifyReport | null>(t.firehose.last_verification ?? null);
  const [rebuild, setRebuild] = useState<RebuildReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async <T,>(label: string, fn: () => Promise<T>, done: (r: T) => void) => {
    setBusy(label);
    try {
      done(await fn());
    } catch (err: unknown) {
      toast.error(`${label} failed`, refusal(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex flex-col gap-5">
      <KeyValues items={Object.entries(t.chain.records).sort().map(([k, v]) => ({ label: humanize(k), value: formatInt(v) }))} />
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" icon="verified_user" busy={busy === "Verification"} onClick={() => void run("Verification", () => apiClient.get<VerifyReport>("/nalanda/verify-ledger", { fresh: isAdmin }), setReport)}>
          Verify ledger
        </Button>
        {isAdmin && (
          <Button icon="restore" busy={busy === "Rebuild"} onClick={() => void run("Rebuild", () => apiClient.get<RebuildReport>("/nalanda/rebuild"), setRebuild)}>
            Rebuild preview
          </Button>
        )}
      </div>
      {report && <VerifyResult report={report} />}
      {rebuild && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-stone-500 dark:text-stone-400">
            {formatInt(rebuild.postings)} postings folded, as of chain #{rebuild.as_of_seq ?? 0}. {rebuild.ok ? "Every account the archive rebuilds matches the live ledger." : "Differences below."}
          </p>
          <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
            {rebuild.accounts.map((a) => (
              <li key={`${a.user_id}-${a.bot_id}`} className="flex items-center justify-between gap-2 text-xs">
                <span className="font-mono text-stone-600 dark:text-stone-300">{a.user_id.slice(0, 8)}{a.bot_id ? ` · bot ${a.bot_id.slice(0, 6)}` : ""}</span>
                <span className="font-mono tabular-nums text-stone-700 dark:text-stone-200">₹{a.derived_available} / ₹{a.derived_exposure}</span>
                <Pill tone={a.status === "MATCH" ? "good" : a.status === "NOT_ARCHIVED" ? "neutral" : "critical"}>{humanize(a.status)}</Pill>
              </li>
            ))}
          </ul>
        </div>
      )}
      {isAdmin && (
        <div className="flex flex-col gap-2 border-t border-stone-100 pt-4 dark:border-white/5">
          <p className="text-xs font-medium text-stone-500 dark:text-stone-400">Maintenance</p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {MAINTENANCE_TASKS.map((task) => {
              const last = t.maintenance[task];
              return (
                <Button
                  key={task}
                  size="sm"
                  variant="ghost"
                  className="justify-between"
                  busy={busy === task}
                  onClick={() =>
                    void run(task, () => apiClient.post<{ status: string }>(`/nalanda/maintenance/${task}`), (r) => {
                      (r.status === "OK" ? toast.success : toast.error)(`${TASK_LABEL[task]}: ${r.status}`);
                      invalidate("nalanda");
                    })
                  }
                >
                  <span>{TASK_LABEL[task]}</span>
                  <span className="text-[11px] text-stone-400">{last ? `${last.status === "OK" ? "✓" : "✕"} ${formatAgo(last.finished_at)}` : "never"}</span>
                </Button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- tick explorer
const TickChart = ({ ticks }: { ticks: TickRow[] }) => {
  const viz = useIsDark() ? VIZ.dark : VIZ.light;
  const data = useMemo(
    // Rows arrive in ingest order; a held ghost spike is written a batch late, so plot by observed time.
    () =>
      ticks
        .map((t) => ({ t: new Date(t.observed_at).getTime(), odds: t.is_anomaly ? null : Number(t.odds), spike: t.is_anomaly ? Number(t.odds) : null, raw: t }))
        .sort((a, b) => a.t - b.t),
    [ticks],
  );
  // Minute labels repeat on a short window; show seconds when the whole window is under an hour.
  const span = data.length > 1 ? data[data.length - 1].t - data[0].t : 0;
  const tickLabel = (v: number) =>
    span < 3_600_000 ? new Date(v).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : formatDateTime(v);
  return (
    <div className="h-64 w-full" role="img" aria-label="Odds over time; ghost spikes marked separately">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={viz.grid} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={tickLabel} minTickGap={60} {...axisProps(viz)} />
          <YAxis scale="log" domain={[(min: number) => min / 1.1, (max: number) => max * 1.25]} allowDataOverflow tickFormatter={(v: number) => v.toFixed(2)} width={52} {...axisProps(viz)} />
          <Tooltip
            cursor={{ stroke: viz.axis, strokeWidth: 1 }}
            content={({ active, payload }: TipProps) => {
              if (!active || !payload?.length) return null;
              const raw = (payload[0].payload as { raw: TickRow }).raw;
              return <Tip title={formatDateTime(raw.observed_at)} rows={[["odds", raw.odds], ["book", raw.bookmaker_id], ...(raw.is_anomaly ? ([["ghost spike", `z ${raw.anomaly_z?.toFixed(1) ?? "?"}`]] as [string, string][]) : [])]} />;
            }}
          />
          <Line type="stepAfter" dataKey="odds" stroke={viz.series} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} activeDot={{ r: 4, stroke: viz.surface, strokeWidth: 2, fill: viz.series }} />
          <Scatter dataKey="spike" fill={viz.spike} stroke={viz.surface} strokeWidth={2} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
};

const TickExplorer = () => {
  const [f, setF] = useState({ fixture_id: "", market: "", selection: "", bookmaker_id: "", since: "", until: "", include_anomalies: true, limit: "500" });
  const [rows, setRows] = useState<TickRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const search = async () => {
    setBusy(true);
    try {
      const res = await apiClient.get<{ ticks: TickRow[] }>("/nalanda/ticks", {
        fixture_id: f.fixture_id || undefined, market: f.market || undefined, selection: f.selection || undefined, bookmaker_id: f.bookmaker_id || undefined,
        since: f.since ? new Date(f.since).toISOString() : undefined, until: f.until ? new Date(f.until).toISOString() : undefined, include_anomalies: f.include_anomalies, limit: Number(f.limit) || 500,
      });
      setRows(res.ticks);
    } catch (err: unknown) {
      toast.error("Search failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const chartable = rows && rows.length > 1 && new Set(rows.map((r) => `${r.fixture_id}|${r.market}|${r.selection}|${r.bookmaker_id}`)).size === 1;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Fixture id"><TextInput value={f.fixture_id} onChange={(e) => setF({ ...f, fixture_id: e.target.value })} placeholder="e.g. 2b9f…" /></Field>
        <Field label="Market">
          <Select value={f.market} onChange={(e) => setF({ ...f, market: e.target.value })}>
            <option value="">Any</option>
            <option>Match Odds</option>
            <option>Over/Under 2.5</option>
            <option>Asian Handicap</option>
          </Select>
        </Field>
        <Field label="Selection"><TextInput value={f.selection} onChange={(e) => setF({ ...f, selection: e.target.value })} placeholder="HOME" /></Field>
        <Field label="Bookmaker"><TextInput value={f.bookmaker_id} onChange={(e) => setF({ ...f, bookmaker_id: e.target.value })} placeholder="pinnacle" /></Field>
        <Field label="From"><TextInput type="datetime-local" value={f.since} onChange={(e) => setF({ ...f, since: e.target.value })} /></Field>
        <Field label="To"><TextInput type="datetime-local" value={f.until} onChange={(e) => setF({ ...f, until: e.target.value })} /></Field>
        <Field label="Limit"><NumberInput min="1" max="5000" value={f.limit} onChange={(e) => setF({ ...f, limit: e.target.value })} /></Field>
        <div className="flex items-end justify-between gap-3">
          <label className="flex items-center gap-2 pb-2 text-sm text-stone-700 dark:text-stone-200">
            <Toggle label="Include ghost spikes" checked={f.include_anomalies} onChange={(v) => setF({ ...f, include_anomalies: v })} />
            Ghost spikes
          </label>
          <Button variant="primary" icon="search" busy={busy} onClick={() => void search()}>Search</Button>
        </div>
      </div>
      <p className="text-xs text-stone-500 dark:text-stone-400">Without a window, the last 24 hours. Every query is bounded by time, so only those weeks' partitions are read.</p>
      {rows === null ? null : rows.length === 0 ? (
        <EmptyState icon="query_stats" title="No ticks match" />
      ) : (
        <>
          {chartable ? <TickChart ticks={rows} /> : <p className="text-xs text-stone-500 dark:text-stone-400">Narrow to one fixture, market, selection and bookmaker to chart its line.</p>}
          <div className="max-h-[28rem] overflow-y-auto">
            <DataTable
              dense
              rows={rows.slice(0, 300)}
              rowKey={(r) => `${r.stream_id}|${r.bookmaker_id}|${r.selection}|${r.created_at}`}
              columns={[
                { key: "o", header: "Observed", render: (r) => <span className="whitespace-nowrap text-xs">{formatDateTime(r.observed_at)}</span> },
                { key: "f", header: "Fixture", render: (r) => <span className="text-xs"><span className="block font-mono">{r.fixture_id.slice(0, 18)}</span><span className="text-stone-500">{r.market} · {r.selection}</span></span> },
                { key: "b", header: "Book", render: (r) => <span className="whitespace-nowrap text-xs">{r.bookmaker_id} <span className="text-stone-400">({r.source})</span></span> },
                { key: "x", header: "Odds", align: "right", render: (r) => r.odds },
                {
                  key: "a", header: "Flags", render: (r) => (
                    <span className="flex gap-1">
                      {r.is_anomaly && <Pill tone="critical" icon="blur_on">Ghost z {r.anomaly_z?.toFixed(1)}</Pill>}
                      {r.is_suspended && <Pill tone="neutral">Suspended</Pill>}
                    </span>
                  ),
                },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- settlement explorer
const SettlementExplorer = ({ isAdmin }: { isAdmin: boolean }) => {
  const [f, setF] = useState({ kind: "", ledger_id: "", fixture_id: "", user_id: "" });
  const [rows, setRows] = useState<ArchiveRecordView[] | null>(null);
  const [picked, setPicked] = useState<ArchiveRecordView | null>(null);
  const [busy, setBusy] = useState(false);
  const search = async (before?: number) => {
    setBusy(true);
    try {
      const res = await apiClient.get<{ records: ArchiveRecordView[] }>("/nalanda/settlements", {
        kind: f.kind || undefined, ledger_id: f.ledger_id || undefined, fixture_id: f.fixture_id || undefined, user_id: isAdmin && f.user_id ? f.user_id : undefined, before_seq: before, limit: 100,
      });
      setRows((prev) => (before && prev ? [...prev, ...res.records] : res.records));
      if (!before) setPicked(null);
    } catch (err: unknown) {
      toast.error("Search failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const oldest = rows && rows.length ? rows[rows.length - 1].seq : undefined;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="Kind">
          <Select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            <option value="">Any</option>
            {RECORD_KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}
          </Select>
        </Field>
        <Field label="Bet (ledger id)"><TextInput value={f.ledger_id} onChange={(e) => setF({ ...f, ledger_id: e.target.value })} /></Field>
        <Field label="Fixture id"><TextInput value={f.fixture_id} onChange={(e) => setF({ ...f, fixture_id: e.target.value })} /></Field>
        {isAdmin ? <Field label="User id"><TextInput value={f.user_id} onChange={(e) => setF({ ...f, user_id: e.target.value })} /></Field> : <div />}
        <div className="flex items-end"><Button variant="primary" icon="search" busy={busy} onClick={() => void search()} className="w-full">Search</Button></div>
      </div>
      {!isAdmin && <p className="text-xs text-stone-500 dark:text-stone-400">You see your own records; an administrator sees the whole warehouse.</p>}
      {rows === null ? null : rows.length === 0 ? (
        <EmptyState icon="receipt_long" title="No records match" />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="flex flex-col gap-2">
            <div className="max-h-[30rem] overflow-y-auto">
              <DataTable
                dense
                rows={rows}
                rowKey={(r) => String(r.seq)}
                onRowClick={setPicked}
                rowTitle="Open the record"
                columns={[
                  { key: "s", header: "#", align: "right", render: (r) => <span className={cx(picked?.seq === r.seq && "font-semibold")}>{r.seq}</span> },
                  { key: "k", header: "Kind", render: (r) => <span className="whitespace-nowrap text-xs">{humanize(r.record_kind)}</span> },
                  { key: "t", header: "Archived", render: (r) => <span className="whitespace-nowrap text-xs">{formatDateTime(r.created_at)}</span> },
                  { key: "a", header: "Amount", align: "right", render: (r) => <span className="whitespace-nowrap">{r.amount_inr === null ? "—" : formatSignedINR(Number(r.amount_inr))}</span> },
                  { key: "h", header: "Hash", render: (r) => <span className="font-mono text-[11px] text-stone-500">{short(r.row_hash)}</span> },
                ]}
              />
            </div>
            {oldest !== undefined && oldest > 1 && <Button size="sm" variant="ghost" icon="expand_more" busy={busy} onClick={() => void search(oldest)}>Older records</Button>}
          </div>
          <div className="min-w-0 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
            {picked ? (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone="info">{humanize(picked.record_kind)}</Pill>
                  <span className="text-xs text-stone-500 dark:text-stone-400">#{picked.seq} · {picked.source} · {picked.source_id}</span>
                </div>
                <KeyValues
                  items={[
                    { label: "Occurred", value: formatDateTime(picked.occurred_at) },
                    { label: "Bet", value: picked.ledger_id ?? "—" },
                    { label: "Fixture", value: picked.fixture_id ?? "—" },
                    { label: "Previous hash", value: <span className="font-mono text-[11px] break-all">{picked.prev_hash}</span> },
                    { label: "Row hash", value: <span className="font-mono text-[11px] break-all">{picked.row_hash}</span> },
                  ]}
                />
                <pre className="max-h-80 overflow-auto rounded-xl bg-white p-3 font-mono text-[11px] leading-relaxed text-stone-700 ring-1 ring-stone-900/5 dark:bg-stone-950 dark:text-stone-300 dark:ring-white/5">
                  {JSON.stringify(picked.payload, null, 2)}
                </pre>
              </div>
            ) : (
              <EmptyState icon="description" title="Pick a record" detail="Its payload (the bookmaker's own request and response, for audit events) and its hashes show here." />
            )}
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- cold tier
const ColdTier = () => {
  const exports = useColdExports(true);
  return (
    <Async resource={exports} skeletonRows={2} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="ac_unit" title="Nothing tiered yet" detail="Tick partitions older than the cold threshold are exported nightly." />}>
      {(list: ColdExportRow[]) => (
        <DataTable
          dense
          rows={list}
          rowKey={(r) => r.id}
          columns={[
            { key: "f", header: "File", render: (r) => <span className="font-mono text-xs">{r.partition ?? r.file.split(/[\\/]/).pop()}</span> },
            { key: "r", header: "Range", render: (r) => <span className="whitespace-nowrap text-xs">{r.from.slice(0, 10)} → {r.to.slice(0, 10)}</span> },
            { key: "n", header: "Rows", align: "right", render: (r) => formatInt(r.rows) },
            { key: "b", header: "Size", align: "right", render: (r) => formatBytes(r.bytes) },
            { key: "h", header: "SHA-256", render: (r) => <span className="font-mono text-[11px] text-stone-500">{short(r.sha256)}</span> },
            { key: "s", header: "Status", render: (r) => <Pill tone={r.status === "DROPPED" ? "good" : r.status === "FAILED" ? "critical" : "info"}>{humanize(r.status)}</Pill> },
            { key: "m", header: "Mirror", render: (r) => <span className="text-xs">{r.mirror_target ? "S3 ✓" : "local only"}</span> },
          ]}
        />
      )}
    </Async>
  );
};

// ---------------------------------------------------------------- the tab
export const NalandaArchive = () => {
  const telemetry = useNalandaTelemetry();
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  return (
    <>
      <Panel title="Nalanda · storage telemetry" icon="account_balance" className="lg:col-span-12" subtitle="the tick lake and the settlement warehouse" updatedAt={telemetry.updatedAt}>
        <Async resource={telemetry} skeletonRows={3}>{(t) => <TelemetryView t={t} />}</Async>
      </Panel>
      <Panel title="Partitions" icon="calendar_view_week" className="lg:col-span-7" subtitle="weekly · BRIN on the time column">
        <Async resource={telemetry} skeletonRows={4}>{(t) => <Partitions t={t} />}</Async>
      </Panel>
      <Panel title="Integrity" icon="verified_user" className="lg:col-span-5" subtitle="SHA-256 hash chain · external anchors">
        <Async resource={telemetry} skeletonRows={3}>{(t) => <Integrity t={t} isAdmin={isAdmin} />}</Async>
      </Panel>
      <Panel title="Tick explorer" icon="query_stats" className="lg:col-span-12" subtitle="line movements, ghost spikes flagged">
        <TickExplorer />
      </Panel>
      <Panel title="Settlement explorer" icon="receipt_long" className="lg:col-span-12" subtitle="ledger postings, receipts, bookmaker responses">
        <SettlementExplorer isAdmin={isAdmin} />
      </Panel>
      {isAdmin && (
        <Panel title="Cold tier" icon="ac_unit" className="lg:col-span-12" subtitle="Parquet exports and their checksums">
          <ColdTier />
        </Panel>
      )}
    </>
  );
};
