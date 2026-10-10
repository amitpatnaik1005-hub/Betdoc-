/**
 * Control Panel, Quant Lab: the quantitative backtester (Group 66; renamed from "The Lab" in Group 68,
 * which is the sidebar section's name).
 *
 * - Dataset: what market history is loaded (ticks, fixtures, books and their commissions, FX).
 * - Parameters: window, target bots, train/test split, walk-forward, the Kelly sweep, simulated
 *   slippage, latency, injected voids, Monte Carlo iterations.
 * - Runs: queued and running backtests with progress; pick one to read it.
 * - Results: the metrics, the equity curve (split marked), the underwater chart, the Monte Carlo
 *   risk of ruin and its fan, the sweep, in-sample against out-of-sample, every reality penalty.
 *
 * Charts are recharts on two validated hues (blue: equity and Sharpe; red: drawdown), each in its
 * light and dark step, with a crosshair tooltip on every line and area and one per column.
 */
import { useMemo, useState } from "react";
import { Area, Bar, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, apiClient } from "../../api/client";
import { formatDate, formatDateTime, formatINR, formatINRCompact, formatInt, humanize } from "../../lib/format";
import {
  type Backtest,
  type BacktestParams,
  type BacktestResult,
  type Dataset,
  type LabBot,
  type Metrics,
  REFERENCE_STRATEGY,
  type Verdict,
  useBacktest,
  useBacktests,
  useDataset,
  useIsDark,
  useLabBots,
} from "../../lib/lab";
import { invalidate, runMutation, useResourceStore } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { Async, Button, DataTable, EmptyState, Field, KeyValues, Meter, NumberInput, Panel, Pill, Select, Stat, StatGrid, TextInput, Toggle } from "../../ui/kit";
import { RollingWalkForwardPanel, TailRiskPanel } from "../backtesting/BacktestDashboard";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

// ---------------------------------------------------------------- chart colours (validated: both modes, both surfaces)
const VIZ = {
  light: { series: "#2a78d6", loss: "#e34948", grid: "#ece9e6", axis: "#a8a29e", ink: "#44403c", muted: "#a8a29e", rest: "#d6d3d1", surface: "#ffffff" },
  dark: { series: "#3987e5", loss: "#e66767", grid: "#292524", axis: "#57534e", ink: "#e7e5e4", muted: "#78716c", rest: "#44403c", surface: "#1c1917" },
} as const;
type Viz = (typeof VIZ)["light"] | (typeof VIZ)["dark"];
const useViz = (): Viz => (useIsDark() ? VIZ.dark : VIZ.light);

const VERDICT_TONE: Record<Verdict, "good" | "warning" | "critical" | "neutral"> = {
  ROBUST: "good", DEGRADED: "warning", OVERFIT: "critical", NO_EDGE: "critical", INSUFFICIENT_DATA: "neutral",
};
const STATUS_TONE = { QUEUED: "neutral", RUNNING: "info", COMPLETED: "good", FAILED: "critical" } as const;

const pct = (v: number | null | undefined, digits = 2): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)}%`);
const signedPct = (v: number | null | undefined, digits = 2): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`);
const ratio = (v: number | null | undefined): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(2));
const toneOf = (v: number | null | undefined): "positive" | "negative" | "neutral" => (v === null || v === undefined || v === 0 ? "neutral" : v > 0 ? "positive" : "negative");
const dateInput = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : "");
/** Axis ticks in rupees, precise enough that neighbouring ticks never read the same (₹2.95L, ₹3.00L). */
const axisINR = (v: number): string => {
  const a = Math.abs(v);
  if (a >= 1e7) return `₹${(v / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `₹${(v / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `₹${(v / 1e3).toFixed(1)}K`;
  return `₹${v.toFixed(0)}`;
};
const axisPct = (v: number): string => `${v.toFixed(Math.abs(v) < 10 ? 1 : 0)}%`;
const ms = (iso: string): number => new Date(iso).getTime();

function refusal(err: unknown): string {
  return err instanceof ApiError ? err.message : "No answer from the server";
}

// ---------------------------------------------------------------- chart chrome
interface TipRow {
  label: string;
  value: string;
  swatch?: string;
}

const ChartTip = ({ title, rows }: { title: string; rows: TipRow[] }) => (
  <div className="rounded-2xl bg-white px-3 py-2 text-xs shadow-soft-lg ring-1 ring-stone-900/5 dark:bg-stone-800 dark:ring-white/10">
    <p className="mb-1 font-medium text-stone-500 dark:text-stone-400">{title}</p>
    {rows.map((r) => (
      <p key={r.label} className="flex items-center gap-2 text-stone-800 dark:text-stone-100">
        {r.swatch && <span className="inline-block h-0.5 w-3 rounded-full" style={{ background: r.swatch }} />}
        <span className="text-stone-500 dark:text-stone-400">{r.label}</span>
        <span className="ml-auto font-mono tabular-nums">{r.value}</span>
      </p>
    ))}
  </div>
);

type TipProps = { active?: boolean; payload?: readonly { value?: unknown; dataKey?: unknown; payload?: unknown }[]; label?: unknown };

const axisProps = (viz: Viz) => ({ stroke: viz.axis, tick: { fill: viz.muted, fontSize: 11 }, tickLine: false, axisLine: { stroke: viz.axis } });

// ---------------------------------------------------------------- equity + underwater
const EquityChart = ({ result }: { result: BacktestResult }) => {
  const viz = useViz();
  const data = useMemo(() => result.curves.equity.map((p) => ({ t: ms(p.t), equity: p.equity })), [result]);
  const split = result.window.walk_forward ? ms(result.window.split) : null;
  const capital = result.metrics.starting_capital_inr ?? 0;
  if (data.length < 2) return <EmptyState icon="show_chart" title="No settled trades" detail="Nothing settled in this window, so the bankroll never moved." />;
  return (
    <div className="h-72 w-full" role="img" aria-label={`Equity curve from ${formatINR(capital)} to ${formatINR(result.metrics.final_equity_inr)}`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={viz.grid} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatDate(v)} minTickGap={48} {...axisProps(viz)} />
          <YAxis domain={["auto", "auto"]} tickFormatter={axisINR} width={72} {...axisProps(viz)} />
          <ReferenceLine y={capital} stroke={viz.axis} strokeWidth={1} />
          {split !== null && (
            <ReferenceLine x={split} stroke={viz.muted} strokeDasharray="0" strokeWidth={1} label={{ value: "out-of-sample →", position: "insideTopLeft", fill: viz.muted, fontSize: 11 }} />
          )}
          <Tooltip
            cursor={{ stroke: viz.axis, strokeWidth: 1 }}
            content={({ active, payload, label }: TipProps) =>
              active && payload?.length ? (
                <ChartTip title={formatDateTime(Number(label))} rows={[{ label: "Equity", value: formatINR(Number(payload[0].value)), swatch: viz.series }, { label: "vs start", value: signedPct(((Number(payload[0].value) - capital) / (capital || 1)) * 100) }]} />
              ) : null
            }
          />
          <Area type="stepAfter" dataKey="equity" stroke="none" fill={viz.series} fillOpacity={0.1} baseValue={capital} isAnimationActive={false} />
          <Line type="stepAfter" dataKey="equity" stroke={viz.series} strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: viz.surface, strokeWidth: 2, fill: viz.series }} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
};

const UnderwaterChart = ({ result }: { result: BacktestResult }) => {
  const viz = useViz();
  const data = useMemo(() => result.curves.underwater.map((p) => ({ t: ms(p.t), dd: p.drawdown_pct })), [result]);
  if (data.length < 2) return null;
  return (
    <div className="h-44 w-full" role="img" aria-label={`Drawdown from the running peak, deepest ${pct(result.metrics.max_drawdown_pct)}`}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={viz.grid} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => formatDate(v)} minTickGap={48} {...axisProps(viz)} />
          <YAxis domain={[(min: number) => Math.floor(min), 0]} allowDecimals={false} tickFormatter={axisPct} width={72} {...axisProps(viz)} />
          <ReferenceLine y={0} stroke={viz.axis} />
          <Tooltip
            cursor={{ stroke: viz.axis, strokeWidth: 1 }}
            content={({ active, payload, label }: TipProps) =>
              active && payload?.length ? <ChartTip title={formatDateTime(Number(label))} rows={[{ label: "Below peak", value: pct(Number(payload[0].value)), swatch: viz.loss }]} /> : null
            }
          />
          <Area type="stepAfter" dataKey="dd" stroke={viz.loss} strokeWidth={2} fill={viz.loss} fillOpacity={0.1} baseValue={0} isAnimationActive={false} activeDot={{ r: 4, stroke: viz.surface, strokeWidth: 2, fill: viz.loss }} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
};

// ---------------------------------------------------------------- Monte Carlo
const MonteCarloPanel = ({ result }: { result: BacktestResult }) => {
  const viz = useViz();
  const mc = result.monte_carlo;
  const data = useMemo(() => mc.fan.map((p) => ({ trade: p.trade, band: [p.p5, p.p95] as [number, number], p50: p.p50 })), [mc]);
  const ruinTone = mc.risk_of_ruin_pct >= 5 ? "critical" : mc.risk_of_ruin_pct >= 1 ? "warning" : "good";
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
      <div className="flex flex-col gap-4">
        <div>
          <p className="text-xs font-medium text-stone-500 dark:text-stone-400">Risk of ruin</p>
          <p className="font-sans text-5xl font-semibold tracking-tight text-stone-900 dark:text-stone-50">{pct(mc.risk_of_ruin_pct, 1)}</p>
          <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
            of {formatInt(mc.iterations)} reshuffles of the same {formatInt(mc.trades)} trades touched {mc.ruin_floor_pct === 0 ? "zero (bankrupt)" : `${mc.ruin_floor_pct}% of the bankroll`}
          </p>
          <div className="mt-2">
            <Pill tone={ruinTone} icon={ruinTone === "good" ? "verified" : "warning"}>{ruinTone === "good" ? "Under 1%" : ruinTone === "warning" ? "1–5%" : "5% or more"}</Pill>
          </div>
        </div>
        <KeyValues
          items={[
            { label: "Drawdown ≥ 50%", value: pct(mc.p_drawdown_50_pct, 1) },
            { label: "Max drawdown p50 / p95", value: `${pct(mc.max_drawdown_pct.p50, 1)} / ${pct(mc.max_drawdown_pct.p95, 1)}` },
            { label: "Observed drawdown", value: `${pct(mc.observed_max_drawdown_pct, 1)}${mc.observed_drawdown_percentile !== undefined ? ` (p${Math.round(mc.observed_drawdown_percentile)})` : ""}` },
            { label: "Bootstrap: ends in a loss", value: pct(mc.bootstrap.p_loss_pct, 1) },
          ]}
        />
      </div>
      {data.length > 1 ? (
        <div className="flex min-w-0 flex-col gap-2">
          <p className="text-xs text-stone-500 dark:text-stone-400">
            Equity after each trade across the reshuffles: the line is the median path, the wash spans the 5th to 95th percentile. Every
            reshuffle holds the same trades, so all of them end at the same equity and the fan closes there; the bootstrap row varies the end.
          </p>
          <div className="h-56 w-full" role="img" aria-label={`Monte Carlo fan over ${mc.trades} trades`}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
                <CartesianGrid stroke={viz.grid} vertical={false} />
                <XAxis dataKey="trade" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(v: number) => `#${v}`} {...axisProps(viz)} />
                <YAxis domain={["auto", "auto"]} tickFormatter={axisINR} width={72} {...axisProps(viz)} />
                <ReferenceLine y={mc.starting_capital_inr} stroke={viz.axis} />
                {mc.ruin_floor_inr > 0 && <ReferenceLine y={mc.ruin_floor_inr} stroke={viz.loss} strokeWidth={1} label={{ value: "ruin floor", position: "insideBottomRight", fill: viz.muted, fontSize: 11 }} />}
                <Tooltip
                  cursor={{ stroke: viz.axis, strokeWidth: 1 }}
                  content={({ active, payload, label }: TipProps) => {
                    if (!active || !payload?.length) return null;
                    const row = payload[0].payload as { band: [number, number]; p50: number };
                    return <ChartTip title={`After trade #${String(label)}`} rows={[{ label: "95th pct", value: formatINR(row.band[1]) }, { label: "Median", value: formatINR(row.p50), swatch: viz.series }, { label: "5th pct", value: formatINR(row.band[0]) }]} />;
                  }}
                />
                <Area type="monotone" dataKey="band" stroke="none" fill={viz.series} fillOpacity={0.12} isAnimationActive={false} />
                <Line type="monotone" dataKey="p50" stroke={viz.series} strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: viz.surface, strokeWidth: 2, fill: viz.series }} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      ) : (
        <EmptyState icon="casino" title="Too few trades to resample" />
      )}
    </div>
  );
};

// ---------------------------------------------------------------- sweep
const SweepChart = ({ result }: { result: BacktestResult }) => {
  const viz = useViz();
  const rows = result.sweep.rows.map((r) => ({ ...r, kellyLabel: `×${Number(r.kelly).toFixed(2)}`, sharpeValue: r.sharpe ?? 0 }));
  const best = result.sweep.best_kelly;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-stone-500 dark:text-stone-400">
        Annualised Sharpe of each Kelly multiplier on the {result.sweep.window === "in_sample" ? "in-sample window" : "full window"}; the highlighted column was locked.
      </p>
      <div className="h-52 w-full" role="img" aria-label="Sharpe ratio by Kelly multiplier">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 16, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke={viz.grid} vertical={false} />
            <XAxis dataKey="kellyLabel" {...axisProps(viz)} />
            <YAxis tickFormatter={(v: number) => v.toFixed(1)} width={40} {...axisProps(viz)} />
            <ReferenceLine y={0} stroke={viz.axis} />
            <Tooltip
              cursor={{ fill: viz.grid, fillOpacity: 0.5 }}
              content={({ active, payload }: TipProps) => {
                if (!active || !payload?.length) return null;
                const r = payload[0].payload as (typeof rows)[number];
                return <ChartTip title={`Kelly ${r.kellyLabel}${r.kelly === best ? " · locked" : ""}`} rows={[{ label: "Sharpe", value: ratio(r.sharpe) }, { label: "ROI", value: signedPct(r.roi_pct) }, { label: "Max drawdown", value: pct(r.max_drawdown_pct) }, { label: "Trades", value: formatInt(r.trades) }]} />;
              }}
            />
            <Bar dataKey="sharpeValue" maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}>
              {rows.map((r) => <Cell key={r.kelly} fill={r.kelly === best ? viz.series : viz.rest} />)}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <DataTable
        dense
        rows={result.sweep.rows}
        rowKey={(r) => r.kelly}
        columns={[
          { key: "k", header: "Kelly ×", render: (r) => <span className={cx("font-mono", r.kelly === best && "font-semibold")}>{Number(r.kelly).toFixed(4)}{r.kelly === best ? " ✓" : ""}</span> },
          { key: "s", header: "Sharpe", align: "right", render: (r) => ratio(r.sharpe) },
          { key: "so", header: "Sortino", align: "right", render: (r) => ratio(r.sortino) },
          { key: "roi", header: "ROI", align: "right", render: (r) => signedPct(r.roi_pct) },
          { key: "dd", header: "Max DD", align: "right", render: (r) => pct(r.max_drawdown_pct) },
          { key: "n", header: "Trades", align: "right", render: (r) => formatInt(r.trades) },
        ]}
      />
    </div>
  );
};

// ---------------------------------------------------------------- walk-forward
const WalkForward = ({ result }: { result: BacktestResult }) => {
  const wf = result.walk_forward;
  if (!wf.enabled || !wf.in_sample || !wf.out_of_sample || !wf.verdict) return <EmptyState icon="call_split" title="Walk-forward off" detail="Turn on the out-of-sample test to lock the parameters on the training window and prove them on unseen data." />;
  const rows: { label: string; pick: (m: Metrics) => string }[] = [
    { label: "Trades", pick: (m) => formatInt(m.trades) },
    { label: "ROI", pick: (m) => signedPct(m.roi_pct) },
    { label: "Return", pick: (m) => signedPct(m.return_pct) },
    { label: "Sharpe", pick: (m) => ratio(m.sharpe) },
    { label: "Sortino", pick: (m) => ratio(m.sortino) },
    { label: "Max drawdown", pick: (m) => pct(m.max_drawdown_pct) },
    { label: "CLV beat", pick: (m) => pct(m.clv_beat_pct, 1) },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={VERDICT_TONE[wf.verdict.verdict]} icon={wf.verdict.verdict === "ROBUST" ? "verified" : "warning"}>{humanize(wf.verdict.verdict)}</Pill>
        <span className="text-sm text-stone-600 dark:text-stone-300">{wf.verdict.reason}</span>
        {wf.verdict.sharpe_retention !== null && (wf.verdict.verdict === "ROBUST" || wf.verdict.verdict === "DEGRADED") && (
          <span className="text-xs text-stone-500 dark:text-stone-400">· Sharpe kept {pct(wf.verdict.sharpe_retention * 100, 0)}</span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[22rem] text-sm">
          <thead>
            <tr className="text-xs text-stone-400 dark:text-stone-500">
              <th scope="col" className="pb-2 text-left font-medium">Locked at Kelly ×{result.locked_parameters.kelly_multiplier ?? "bot setting"}</th>
              <th scope="col" className="pb-2 text-right font-medium">In-sample ({Math.round(result.window.train_ratio * 100)}%)</th>
              <th scope="col" className="pb-2 text-right font-medium">Out-of-sample</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label} className="border-t border-stone-100 dark:border-white/5">
                <th scope="row" className="py-2 text-left font-normal text-stone-500 dark:text-stone-400">{r.label}</th>
                <td className="py-2 text-right font-mono tabular-nums text-stone-800 dark:text-stone-200">{r.pick(wf.in_sample as Metrics)}</td>
                <td className="py-2 text-right font-mono tabular-nums text-stone-800 dark:text-stone-200">{r.pick(wf.out_of_sample as Metrics)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-stone-500 dark:text-stone-400">
        Split at {formatDateTime(result.window.split)}. The in-sample run read history only up to the split (a time-locked store); the out-of-sample run used exactly the locked parameters (fingerprint <span className="font-mono">{result.locked_parameters.fingerprint}</span>).
      </p>
    </div>
  );
};

// ---------------------------------------------------------------- results
const Penalties = ({ result }: { result: BacktestResult }) => {
  const p = result.penalties;
  const m = result.metrics;
  return (
    <KeyValues
      items={[
        { label: "Signals replayed", value: formatInt(p.signals) },
        { label: "Orders filled / refused", value: `${formatInt(p.orders_filled)} / ${formatInt(p.orders_rejected)}` },
        { label: "Network latency (avg / max)", value: `${p.latency_ms_avg?.toFixed(0) ?? "—"} / ${p.latency_ms_max ?? "—"} ms` },
        { label: "Line fell under min EV in flight", value: formatInt(p.latency_edge_decay_rejections) },
        { label: "Filled at a worse price", value: formatInt(p.filled_worse_after_latency) },
        { label: "Queued behind 2 bets/s", value: `${formatInt(p.queued_behind_rate_limit)} (${p.queue_seconds_total.toFixed(1)}s)` },
        { label: "Refused by the rate limit", value: formatInt(p.throttled) },
        { label: "Partial fills (liquidity / impact)", value: formatInt(p.partial_fills) },
        { label: "Market impact (avg / max)", value: `${pct(p.impact_avg_pct, 3)} / ${pct(p.impact_max_pct, 2)}` },
        { label: "Voids (injected)", value: `${formatInt(m.voids)} (${formatInt(m.voids_injected)})` },
        { label: "Commission paid", value: formatINR(m.commission_paid_inr) },
        { label: "FX static-rate fallbacks", value: formatInt(p.fx_fallback_conversions) },
      ]}
    />
  );
};

const RESULT_TABS = ["Trades", "Decisions", "Bots"] as const;

const Details = ({ result }: { result: BacktestResult }) => {
  const [tab, setTab] = useState<(typeof RESULT_TABS)[number]>("Trades");
  const decisions = Object.entries(result.penalties.decisions).sort((a, b) => b[1] - a[1]);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1">
        {RESULT_TABS.map((t) => (
          <Button key={t} size="sm" variant={t === tab ? "primary" : "ghost"} onClick={() => setTab(t)}>{t}</Button>
        ))}
      </div>
      {tab === "Trades" ? (
        result.trades.length ? (
          <DataTable
            dense
            rows={[...result.trades].reverse().slice(0, 60)}
            rowKey={(r) => `${r.decided_at}|${r.bot}|${r.fixture}|${r.selection}|${r.fill_odds}`}
            columns={[
              { key: "at", header: "Decided", render: (r) => <span className="whitespace-nowrap text-xs">{formatDateTime(r.decided_at)}</span> },
              {
                key: "f",
                header: "Fixture",
                render: (r) => (
                  <span className="text-xs">
                    <span className="block whitespace-nowrap text-stone-800 dark:text-stone-200">{r.fixture}</span>
                    <span className="block whitespace-nowrap text-stone-500 dark:text-stone-400">{r.selection} · {r.bookmaker} ({r.currency})</span>
                  </span>
                ),
              },
              { key: "o", header: "Asked → filled", align: "right", className: "whitespace-nowrap", render: (r) => `${r.requested_odds} → ${r.fill_odds}` },
              { key: "c", header: "Close", align: "right", render: (r) => r.closing_odds ?? "—" },
              { key: "l", header: "Latency", align: "right", className: "whitespace-nowrap", render: (r) => `${r.latency_ms + r.queue_ms} ms` },
              { key: "s", header: "Stake", align: "right", className: "whitespace-nowrap", render: (r) => formatINR(Number(r.stake_inr)) },
              { key: "st", header: "Result", render: (r) => <Pill tone={r.status === "WON" ? "good" : r.status === "LOST" ? "critical" : "neutral"}>{r.void_reason ? `Void · ${humanize(r.void_reason)}` : humanize(r.status)}</Pill> },
              { key: "p", header: "P&L", align: "right", className: "whitespace-nowrap", render: (r) => (r.pnl_inr === null ? "—" : formatINR(Number(r.pnl_inr))) },
            ]}
          />
        ) : (
          <EmptyState icon="receipt_long" title="No trades" />
        )
      ) : tab === "Decisions" ? (
        <ul className="grid grid-cols-1 gap-x-8 gap-y-1.5 sm:grid-cols-2">
          {decisions.map(([k, v]) => (
            <li key={k} className="flex items-baseline justify-between gap-3 text-sm">
              <span className="text-stone-500 dark:text-stone-400">{humanize(k.replace(":", " · "))}</span>
              <span className="font-mono tabular-nums text-stone-800 dark:text-stone-200">{formatInt(v)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex flex-col gap-4">
          {result.bots.map((b) => {
            const row = result.per_bot.find((p) => p.bot_id === b.id);
            return (
              <div key={b.id} className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-semibold text-stone-800 dark:text-stone-100">{b.name} <span className="text-xs font-normal text-stone-500">{b.origin === "bot" ? "Hive bot" : "strategy"}</span></p>
                  {row && <p className="font-mono text-sm tabular-nums text-stone-700 dark:text-stone-300">{formatInt(row.trades)} trades · ROI {signedPct(row.roi_pct)} · {formatINR(Number(row.pnl_inr))}{row.suspensions ? ` · ${row.suspensions} suspension(s)` : ""}</p>}
                </div>
                <p className="mt-2 text-xs text-stone-500 dark:text-stone-400">Registry components: {b.components.map((c) => c.name).join(" → ")}</p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

const Results = ({ run }: { run: Backtest }) => {
  const detail = useBacktest(run.status === "COMPLETED" ? run.id : null, run.status);
  const result = detail.data?.result;
  if (run.status === "FAILED") {
    return (
      <Panel title={`${run.name} · failed`} icon="error" className="lg:col-span-12">
        <p className="text-sm text-rose-700 dark:text-rose-300">{run.error ?? "The run stopped without a reason."}</p>
      </Panel>
    );
  }
  if (run.status !== "COMPLETED") return null;
  if (!result) return <Panel title={run.name} icon="science" className="lg:col-span-12"><Async resource={detail} skeletonRows={4}>{() => null}</Async></Panel>;
  const m = result.metrics;
  return (
    <>
      <Panel
        title={run.name}
        icon="science"
        className="lg:col-span-12"
        subtitle={`${formatDate(result.window.start)} → ${formatDate(result.window.end)} · ${formatInt(result.stream.signals)} signals · ${result.elapsed_seconds.toFixed(1)}s`}
      >
        <div className="flex flex-col gap-6">
          {result.warnings.length > 0 && (
            <ul className="flex flex-col gap-1.5">
              {result.warnings.map((w) => (
                <li key={w} className="flex items-start gap-2 text-xs text-amber-800 dark:text-amber-200/90">
                  <span className="material-symbols-outlined text-[16px]">info</span>
                  {w}
                </li>
              ))}
            </ul>
          )}
          <StatGrid cols={5}>
            <Stat label="ROI" value={signedPct(m.roi_pct)} tone={toneOf(m.roi_pct)} hint={`${formatINR(m.pnl_inr)} on ${formatINRCompact(m.staked_inr)} staked`} />
            <Stat label="Max drawdown" value={pct(m.max_drawdown_pct)} hint={`${formatINR(m.max_drawdown_inr)} · ${m.drawdown_days.toFixed(0)} days`} />
            <Stat label="Sharpe" value={ratio(m.sharpe)} tone={toneOf(m.sharpe)} hint="annualised, daily" />
            <Stat label="Sortino" value={ratio(m.sortino)} tone={toneOf(m.sortino)} />
            <Stat label="Calmar" value={ratio(m.calmar)} tone={toneOf(m.calmar)} />
            <Stat label="MAE (avg / worst)" value={pct(m.mae_avg_pct, 1)} hint={`worst ${pct(m.mae_worst_pct, 1)} of stake`} />
            <Stat label="CLV beat" value={pct(m.clv_beat_pct, 1)} hint={`avg CLV ${signedPct(m.clv_avg_pct)}`} />
            <Stat label="Trades" value={formatInt(m.trades)} hint={`${pct(m.win_rate_pct, 1)} won · avg odds ${m.avg_odds?.toFixed(2) ?? "—"}`} />
            <Stat label="Return" value={signedPct(m.return_pct)} tone={toneOf(m.return_pct)} hint={`${formatINRCompact(m.starting_capital_inr)} → ${formatINRCompact(m.final_equity_inr)}`} />
            <Stat label="Risk of ruin" value={pct(result.monte_carlo.risk_of_ruin_pct, 1)} tone={result.monte_carlo.risk_of_ruin_pct >= 5 ? "negative" : "neutral"} hint={`${formatInt(result.monte_carlo.iterations)} reshuffles`} />
          </StatGrid>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-stone-800 dark:text-stone-100">Equity curve</h3>
            <EquityChart result={result} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold text-stone-800 dark:text-stone-100">Underwater: depth below the running peak</h3>
            <UnderwaterChart result={result} />
          </div>
        </div>
      </Panel>
      <Panel title="Monte Carlo resampling" icon="casino" className="lg:col-span-12" subtitle="permutation of the settled trades; bootstrap for the spread of outcomes">
        <div className="flex flex-col gap-6">
          <MonteCarloPanel result={result} />
          <TailRiskPanel result={result} />
        </div>
      </Panel>
      <Panel title="Kelly sweep" icon="tune" className="lg:col-span-7" subtitle={result.sweep.enabled ? `objective: Sharpe · best ×${result.sweep.best_kelly}` : "off"}>
        {result.sweep.enabled ? <SweepChart result={result} /> : <EmptyState icon="tune" title="Sweep off" detail="The bots ran at their own Kelly multipliers." />}
      </Panel>
      <Panel title="Walk-forward test" icon="call_split" className="lg:col-span-5" subtitle="in-sample vs out-of-sample">
        <WalkForward result={result} />
      </Panel>
      <Panel title="Rolling walk-forward" icon="view_timeline" className="lg:col-span-12" subtitle="each fold tuned on its own past, judged on its future">
        <RollingWalkForwardPanel result={result} />
      </Panel>
      <Panel title="Reality penalties" icon="gavel" className="lg:col-span-12" subtitle="latency, rate limit, impact, voids, FX">
        <Penalties result={result} />
      </Panel>
      <Panel title="Inside the run" icon="list_alt" className="lg:col-span-12">
        <Details result={result} />
      </Panel>
    </>
  );
};

// ---------------------------------------------------------------- dataset
const DatasetPanel = ({ data }: { data: Dataset }) => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const [busy, setBusy] = useState(false);
  const seed = async () => {
    setBusy(true);
    await runMutation(() => apiClient.post("/lab/quant/dataset/seed", { rows: 10_000 }), { invalidate: ["lab"], success: "10,000 synthetic ticks loaded", errorTitle: "Seeding refused" });
    setBusy(false);
  };
  if (!data.ticks) {
    return (
      <EmptyState
        icon="database"
        title="No market history loaded"
        detail="Quant Lab backtests against lab_hist_* rows. Load the synthetic year (10,000 ticks) to start, or run python -m app.db.seed_historical_ticks."
        action={isAdmin ? <Button variant="primary" icon="download" busy={busy} onClick={() => void seed()}>Load synthetic dataset</Button> : undefined}
      />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <StatGrid cols={4}>
        <Stat label="Ticks" value={formatInt(data.ticks)} hint={data.synthetic ? "synthetic dataset" : data.sources.join(", ")} />
        <Stat label="Fixtures" value={formatInt(data.fixtures)} hint={`${formatInt(data.postponed)} postponed`} />
        <Stat label="Span" value={data.first_tick && data.last_tick ? `${Math.round((ms(data.last_tick) - ms(data.first_tick)) / 86_400_000)} days` : "—"} hint={`${formatDate(data.first_tick)} → ${formatDate(data.last_tick)}`} />
        <Stat label="FX fixings" value={formatInt(data.fx.reduce((a, f) => a + f.fixings, 0))} hint={`static fallback after ${data.fx_max_age_hours}h`} />
      </StatGrid>
      <div className="flex flex-wrap gap-2">
        {data.books.map((b) => (
          <Pill key={b.bookmaker_id} tone={Number(b.commission) > 0 ? "info" : "neutral"}>
            {b.bookmaker_id} · {b.currency}{Number(b.commission) > 0 ? ` · ${(Number(b.commission) * 100).toFixed(0)}% commission` : ""}
          </Pill>
        ))}
      </div>
      {data.synthetic && (
        <p className="text-xs text-stone-500 dark:text-stone-400">
          Generated data (a latent truth, sharp and soft books, news, suspensions, postponements): results prove the engine and a strategy's mechanics, not a real-world edge.
        </p>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- the form
interface Draft {
  name: string;
  bots: string[];
  reference: boolean;
  start: string;
  end: string;
  trainPct: number;
  oos: boolean;
  sweep: boolean;
  kellyMin: string;
  kellyMax: string;
  steps: string;
  slippage: string;
  latencyMin: string;
  latencyMax: string;
  voidPct: string;
  iterations: string;
  ruinFloor: string;
  folds: string;
  impact: "quadratic" | "sqrt";
  riskFree: string;
  review: string;
  seed: string;
}

const DRAFT: Draft = {
  name: "Backtest", bots: [], reference: true, start: "", end: "", trainPct: 75, oos: true, sweep: true, kellyMin: "0.1", kellyMax: "0.5", steps: "10",
  slippage: "0.25", latencyMin: "1500", latencyMax: "3000", voidPct: "2", iterations: "1000", ruinFloor: "0", review: "6", seed: "66",
  folds: "1", impact: "quadratic", riskFree: "4",
};

const RunForm = ({ dataset, bots }: { dataset: Dataset; bots: LabBot[] }) => {
  const [d, setD] = useState<Draft>(DRAFT);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setD((prev) => ({ ...prev, [key]: value }));
  const start = d.start || dateInput(dataset.first_tick);
  const end = d.end || dateInput(dataset.last_tick);
  const picked = d.bots.length + (d.reference ? 1 : 0);

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    const body: BacktestParams = {
      name: d.name.trim() || "Backtest",
      bot_ids: d.bots,
      strategies: d.reference ? [REFERENCE_STRATEGY] : [],
      start: start ? `${start}T00:00:00Z` : null,
      end: end ? `${end}T23:59:59Z` : null,
      train_ratio: d.trainPct / 100,
      oos_enabled: d.oos,
      sweep_enabled: d.sweep,
      kelly_min: d.kellyMin,
      kelly_max: d.kellyMax,
      sweep_steps: Number(d.steps),
      latency_min_ms: Number(d.latencyMin),
      latency_max_ms: Number(d.latencyMax),
      slippage_pct: d.slippage,
      void_rate_pct: d.voidPct,
      monte_carlo_iterations: Number(d.iterations),
      ruin_floor_pct: Number(d.ruinFloor),
      resume_after_hours: Number(d.review),
      seed: Number(d.seed),
      walk_forward_folds: Number(d.folds),
      impact_model: d.impact,
      risk_free_rate: Number(d.riskFree) / 100,
    };
    try {
      await apiClient.post<Backtest>("/lab/quant/backtests", body);
      invalidate("lab");
    } catch (err: unknown) {
      setProblem(refusal(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <Field label="Name"><TextInput value={d.name} maxLength={120} onChange={(e) => set("name", e.target.value)} /></Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="From"><TextInput type="date" value={start} min={dateInput(dataset.first_tick)} max={end} onChange={(e) => set("start", e.target.value)} /></Field>
        <Field label="To"><TextInput type="date" value={end} min={start} max={dateInput(dataset.last_tick)} onChange={(e) => set("end", e.target.value)} /></Field>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-stone-500 dark:text-stone-400">Target bots</legend>
        <label className="flex items-start gap-2 rounded-xl px-2 py-1.5 hover:bg-stone-50 dark:hover:bg-white/[0.03]">
          <input type="checkbox" aria-label={REFERENCE_STRATEGY.name} className="mt-1 accent-[var(--accent)]" checked={d.reference} onChange={(e) => set("reference", e.target.checked)} />
          <span className="text-sm text-stone-700 dark:text-stone-200">{REFERENCE_STRATEGY.name}<span className="block text-xs text-stone-500 dark:text-stone-400">consensus · Shin · Kelly; drawdown and exposure capped</span></span>
        </label>
        {bots.map((b) => {
          const blocked = b.problems.length > 0;
          return (
            <label key={b.id} className={cx("flex items-start gap-2 rounded-xl px-2 py-1.5", blocked ? "opacity-60" : "hover:bg-stone-50 dark:hover:bg-white/[0.03]")} title={blocked ? b.problems.join("; ") : undefined}>
              <input
                type="checkbox"
                aria-label={b.name}
                className="mt-1 accent-[var(--accent)]"
                disabled={blocked}
                checked={d.bots.includes(b.id)}
                onChange={(e) => set("bots", e.target.checked ? [...d.bots, b.id] : d.bots.filter((id) => id !== b.id))}
              />
              <span className="text-sm text-stone-700 dark:text-stone-200">
                {b.name}
                <span className="block text-xs text-stone-500 dark:text-stone-400">{blocked ? b.problems[0] : `${humanize(b.execution_mode)} · Kelly ×${Number(b.kelly_multiplier).toFixed(2)} · ${b.math_models.length} models`}</span>
              </span>
            </label>
          );
        })}
        {bots.length === 0 && <p className="px-2 text-xs text-stone-500 dark:text-stone-400">No Hive bots yet: create them in the Hive bots tab, or run the reference strategy.</p>}
      </fieldset>
      <Field label={`Train / test split: ${d.trainPct}% / ${100 - d.trainPct}%`} hint="Walk-forward: tune on the first part, lock, prove on the rest.">
        <input type="range" min={50} max={90} step={5} value={d.trainPct} onChange={(e) => set("trainPct", Number(e.target.value))} className="w-full accent-[var(--accent)]" aria-label="Train share" />
      </Field>
      <div className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-3 dark:bg-white/[0.03]">
        <div className="flex items-center justify-between gap-3"><span className="text-sm text-stone-700 dark:text-stone-200">Out-of-sample forward test</span><Toggle label="Out-of-sample test" checked={d.oos} onChange={(v) => set("oos", v)} /></div>
        <div className="flex items-center justify-between gap-3"><span className="text-sm text-stone-700 dark:text-stone-200">Kelly sweep (max Sharpe)</span><Toggle label="Kelly sweep" checked={d.sweep} onChange={(v) => set("sweep", v)} /></div>
        {d.sweep && (
          <div className="grid grid-cols-3 gap-2">
            <Field label="Kelly from"><NumberInput min="0.01" max="1" step="0.05" value={d.kellyMin} onChange={(e) => set("kellyMin", e.target.value)} /></Field>
            <Field label="to"><NumberInput min="0.01" max="1" step="0.05" value={d.kellyMax} onChange={(e) => set("kellyMax", e.target.value)} /></Field>
            <Field label="steps"><NumberInput min="2" max="20" step="1" value={d.steps} onChange={(e) => set("steps", e.target.value)} /></Field>
          </div>
        )}
      </div>
      <Field label="Simulated slippage %" hint="A cost on every fill's price, on top of latency and impact.">
        <NumberInput min="0" max="5" step="0.05" value={d.slippage} onChange={(e) => set("slippage", e.target.value)} />
      </Field>
      <button type="button" onClick={() => setAdvanced((v) => !v)} className="flex items-center gap-1 self-start text-xs font-medium text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200" aria-expanded={advanced}>
        <span className="material-symbols-outlined text-[16px]">{advanced ? "expand_less" : "expand_more"}</span>
        Latency, voids, Monte Carlo, folds, impact
      </button>
      {advanced && (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Latency min ms"><NumberInput min="0" max="20000" value={d.latencyMin} onChange={(e) => set("latencyMin", e.target.value)} /></Field>
          <Field label="Latency max ms"><NumberInput min="0" max="20000" value={d.latencyMax} onChange={(e) => set("latencyMax", e.target.value)} /></Field>
          <Field label="Injected voids %"><NumberInput min="0" max="20" step="0.5" value={d.voidPct} onChange={(e) => set("voidPct", e.target.value)} /></Field>
          <Field label="Monte Carlo runs"><NumberInput min="100" max="10000" step="100" value={d.iterations} onChange={(e) => set("iterations", e.target.value)} /></Field>
          <Field label="Ruin floor %" hint="0 = bankrupt"><NumberInput min="0" max="90" value={d.ruinFloor} onChange={(e) => set("ruinFloor", e.target.value)} /></Field>
          <Field label="Breaker review (h)" hint="0 = never resumes"><NumberInput min="0" max="720" value={d.review} onChange={(e) => set("review", e.target.value)} /></Field>
          <Field label="Seed"><NumberInput min="0" value={d.seed} onChange={(e) => set("seed", e.target.value)} /></Field>
          <Field label="Rolling folds" hint="1 = one split; more: tuned and tested fold by fold"><NumberInput min="1" max="10" step="1" value={d.folds} onChange={(e) => set("folds", e.target.value)} /></Field>
          <Field label="Impact model" hint="sqrt: 1 − k·√(stake ÷ liquidity)">
            <Select value={d.impact} onChange={(e) => set("impact", e.target.value as Draft["impact"])}>
              <option value="quadratic">Quadratic past 5%</option>
              <option value="sqrt">Square-root</option>
            </Select>
          </Field>
          <Field label="Risk-free rate % a year" hint="Sharpe and Sortino in excess of it"><NumberInput min="0" max="50" step="0.5" value={d.riskFree} onChange={(e) => set("riskFree", e.target.value)} /></Field>
        </div>
      )}
      {problem && <p className="rounded-xl bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" role="alert">{problem}</p>}
      <Button variant="primary" icon="play_arrow" busy={busy} disabled={picked === 0} onClick={() => void submit()}>Run backtest</Button>
    </div>
  );
};

// ---------------------------------------------------------------- runs
const RunList = ({ runs, selected, onSelect }: { runs: Backtest[]; selected: string | null; onSelect: (id: string) => void }) => {
  const remove = (id: string) => void runMutation(() => apiClient.delete(`/lab/quant/backtests/${id}`), { invalidate: ["lab"], errorTitle: "Not deleted" });
  return (
    <ul className="flex max-h-[38rem] flex-col gap-2 overflow-y-auto">
      {runs.map((r) => {
        const live = r.status === "QUEUED" || r.status === "RUNNING";
        const s = r.summary;
        return (
          <li key={r.id}>
            <div
              role="button"
              tabIndex={0}
              onClick={() => onSelect(r.id)}
              onKeyDown={(e) => (e.key === "Enter" ? onSelect(r.id) : undefined)}
              className={cx(
                "flex flex-col gap-2 rounded-2xl px-4 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
                r.id === selected ? "bg-stone-100 dark:bg-white/[0.06]" : "hover:bg-stone-50 dark:hover:bg-white/[0.03]",
              )}
            >
              <div className="flex items-center justify-between gap-3">
                <p className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{r.name}</p>
                <div className="flex shrink-0 items-center gap-2">
                  {s?.verdict && <Pill tone={VERDICT_TONE[s.verdict]}>{humanize(s.verdict)}</Pill>}
                  <Pill tone={STATUS_TONE[r.status]}>{humanize(r.status)}</Pill>
                </div>
              </div>
              {live ? (
                <div className="flex flex-col gap-1">
                  <Meter value={r.progress} label={`${r.name} progress`} />
                  <p className="text-xs text-stone-500 dark:text-stone-400">{r.stage} · {Math.round(r.progress * 100)}%</p>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-3 text-xs text-stone-500 dark:text-stone-400">
                  <span className="font-mono tabular-nums">
                    {s ? `ROI ${signedPct(s.roi_pct)} · Sharpe ${ratio(s.sharpe)} · DD ${pct(s.max_drawdown_pct, 1)} · ruin ${pct(s.risk_of_ruin_pct, 1)}${s.best_kelly ? ` · Kelly ×${s.best_kelly}` : ""}` : r.error ?? ""}
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {formatDateTime(r.created_at)}
                    <button type="button" aria-label={`Delete ${r.name}`} onClick={(e) => { e.stopPropagation(); remove(r.id); }} className="material-symbols-outlined rounded-full p-0.5 text-[16px] text-stone-400 hover:bg-stone-200/60 hover:text-stone-700 dark:hover:bg-white/10 dark:hover:text-stone-200">delete</button>
                  </span>
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
};

// ---------------------------------------------------------------- the tab
export const QuantLab = () => {
  const dataset = useDataset();
  const bots = useLabBots();
  const cached = useResourceStore((s) => s.entries["lab:backtests"]?.data as Backtest[] | undefined);
  const busy = cached?.some((r) => r.status === "QUEUED" || r.status === "RUNNING") ?? false;
  const runs = useBacktests(busy);
  const [selected, setSelected] = useState<string | null>(null);
  const list = runs.data ?? [];
  const current = list.find((r) => r.id === selected) ?? list.find((r) => r.status === "COMPLETED") ?? list[0] ?? null;

  return (
    <>
      <Panel title="Quant Lab · backtesting" icon="science" className="lg:col-span-12" subtitle="historical replay through the live Aryabhata scorer and Hive pipeline">
        <Async resource={dataset} skeletonRows={2}>{(data) => <DatasetPanel data={data} />}</Async>
      </Panel>
      <Panel title="New backtest" icon="tune" className="lg:col-span-5">
        <Async resource={dataset} skeletonRows={4}>
          {(data) => (data.ticks ? <Async resource={bots} skeletonRows={2}>{(list) => <RunForm dataset={data} bots={list} />}</Async> : <EmptyState icon="database" title="Load the history first" />)}
        </Async>
      </Panel>
      <Panel title="Backtests" icon="history" className="lg:col-span-7" subtitle={busy ? "running…" : undefined} updatedAt={runs.updatedAt}>
        <Async resource={runs} skeletonRows={3} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="science" title="No backtests yet" detail="Pick a window and bots, then run." />}>
          {(items) => <RunList runs={items} selected={current?.id ?? null} onSelect={setSelected} />}
        </Async>
      </Panel>
      {current && <Results key={current.id} run={current} />}
    </>
  );
};
