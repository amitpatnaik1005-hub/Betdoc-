import { useState, type FormEvent } from 'react';
import { apiClient } from '../api/client';
import { downloadCsv, formatAgo, formatDate, formatInt, formatPct } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { DivergingBars } from '../ui/charts';
import { Async, Button, EmptyState, Field, KeyValues, Page, Panel, Pill, Select, StatusBadge, TextInput } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface Overview { encryption_status: string; algorithm: string; last_backup_at: string | null; total_tables: number; database_status: 'ONLINE' | 'DEGRADED'; probe_latency_ms: number | null; total_records: number }
interface TableSummary { table_name: string; row_count: number }
interface TableData { table_name: string; limit: number; offset: number; data: Record<string, unknown>[] }
interface Smallcase { id: string; name: string; status: string }
interface Backtest { id: string; smallcase_id: string; start_date: string; end_date: string; total_matches_simulated: number; roi_pct: number | null; accuracy_pct: number | null; max_drawdown_pct: number | null; status: string; error_detail: string | null; created_at: string; completed_at: string | null }

const PAGE = 50;
const cell = (v: unknown): string => (v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v));

// ---------------------------------------------------------------------------
// TABLE BROWSER
// ---------------------------------------------------------------------------
const TableBrowser = ({ tables }: { tables: ReturnType<typeof useResource<TableSummary[]>> }) => {
  const [name, setName] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const selected = name ?? tables.data?.find((t) => t.row_count > 0)?.table_name ?? tables.data?.[0]?.table_name ?? null;
  const data = useResource(selected ? `archive:table:${selected}:${offset}` : null, () => apiClient.get<TableData>(`/archive/tables/${encodeURIComponent(selected ?? '')}/data`, { limit: PAGE, offset }));
  const total = tables.data?.find((t) => t.table_name === selected)?.row_count ?? 0;
  const columns = data.data?.data[0] ? Object.keys(data.data.data[0]) : [];

  return (
    <Panel
      title="Table browser"
      icon="table_view"
      className="lg:col-span-12"
      updatedAt={data.updatedAt}
      subtitle="read-only · sensitive columns redacted · access logged"
      actions={
        <Button size="sm" icon="download" disabled={!data.data?.data.length} onClick={() => downloadCsv(`${selected}-${offset}.csv`, data.data?.data ?? [])}>
          Export page
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Table" className="w-72">
            <Select value={selected ?? ''} onChange={(e) => { setName(e.target.value); setOffset(0); }}>
              {(tables.data ?? []).map((t) => <option key={t.table_name} value={t.table_name}>{t.table_name} ({formatInt(t.row_count)})</option>)}
            </Select>
          </Field>
          <div className="flex items-center gap-2 pb-0.5 text-xs text-slate-500">
            <Button size="sm" icon="chevron_left" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Prev</Button>
            <span className="tabular-nums">{total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE, total)} of {formatInt(total)}</span>
            <Button size="sm" icon="chevron_right" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</Button>
          </div>
        </div>
        <Async resource={data} isEmpty={(d) => d.data.length === 0} empty={<EmptyState icon="table_rows" title="Empty table" />}>
          {(d) => (
            <div className="-mx-4 -mb-4 max-h-[460px] overflow-auto">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-white dark:bg-[#161514]">
                  <tr className="border-b border-slate-900/[0.06] dark:border-white/[0.06]">
                    {columns.map((c) => <th key={c} className="whitespace-nowrap px-3 py-2 text-[10px] font-bold uppercase tracking-[0.12em] text-slate-400">{c}</th>)}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-900/[0.04] dark:divide-white/[0.04]">
                  {d.data.map((row, i) => (
                    <tr key={i} className="hover:bg-slate-900/[0.02] dark:hover:bg-white/[0.02]">
                      {columns.map((c) => <td key={c} className="max-w-[260px] truncate whitespace-nowrap px-3 py-1.5 font-mono text-slate-600 dark:text-slate-300" title={cell(row[c])}>{cell(row[c])}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// BACKTEST LEDGER
// ---------------------------------------------------------------------------
const BacktestLedger = () => {
  const backtests = useResource('core:backtests', () => apiClient.get<Backtest[]>('/core/backtests', { limit: 25 }), { intervalMs: 10_000 });
  const smallcases = useResource('core:smallcases', () => apiClient.get<Smallcase[]>('/core/smallcases'));
  const today = new Date().toISOString().slice(0, 10);
  const monthAgo = new Date(Date.now() - 30 * 86_400_000).toISOString().slice(0, 10);
  const [form, setForm] = useState({ smallcase: '', start: monthAgo, end: today });
  const [busy, setBusy] = useState(false);
  const names = new Map((smallcases.data ?? []).map((s) => [s.id, s.name]));
  const smallcaseId = form.smallcase || smallcases.data?.[0]?.id || '';

  const run = async (e: FormEvent) => {
    e.preventDefault();
    if (!smallcaseId) return;
    setBusy(true);
    await runMutation(() => apiClient.post('/core/backtest', { smallcase_id: smallcaseId, start_date: form.start, end_date: form.end }), {
      invalidate: ['core', 'commanders'], success: 'Backtest queued', errorTitle: 'Backtest rejected',
    });
    setBusy(false);
  };

  return (
    <Panel title="Backtest strategy ledger" icon="history" className="lg:col-span-7" updatedAt={backtests.updatedAt} subtitle="synthetic Monte Carlo markets">
      <form id="backtest-form" onSubmit={run} className="mb-4 flex flex-wrap items-end gap-2.5">
        <Field label="Smallcase" className="min-w-[180px] flex-1">
          <Select value={smallcaseId} onChange={(e) => setForm({ ...form, smallcase: e.target.value })}>
            {(smallcases.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </Field>
        <Field label="From"><TextInput type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></Field>
        <Field label="To"><TextInput type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} /></Field>
        <Button type="submit" variant="primary" icon="play_circle" busy={busy} disabled={!smallcaseId}>Run backtest</Button>
      </form>
      {!smallcases.data?.length && smallcases.data && <p className="mb-3 text-xs text-amber-600">No smallcases yet: bootstrap the engine from Core first.</p>}
      <p className="mb-3 text-[11px] text-slate-400">The engine replays each pipeline against synthetic markets drawn per day in the range (no historical results are stored to replay).</p>
      <Async resource={backtests} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="history" title="No backtests yet" />}>
        {(rows) => (
          <ul className="flex flex-col gap-2">
            {rows.map((b) => (
              <li key={b.id} className="grid grid-cols-2 items-center gap-3 rounded-xl bg-slate-50 px-3 py-2.5 sm:grid-cols-[1.4fr_repeat(3,0.7fr)_auto] dark:bg-white/[0.03]">
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold text-slate-800 dark:text-slate-100">{names.get(b.smallcase_id) ?? b.smallcase_id.slice(0, 8)}</span>
                  <span className="text-[11px] text-slate-400">{formatDate(b.start_date)} → {formatDate(b.end_date)} · {formatInt(b.total_matches_simulated)} matches</span>
                </span>
                <span className={`text-sm font-semibold tabular-nums ${(b.roi_pct ?? 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>{b.roi_pct === null ? '—' : `${b.roi_pct >= 0 ? '+' : ''}${formatPct(b.roi_pct)}`}<span className="block text-[10px] font-normal text-slate-400">ROI</span></span>
                <span className="text-sm tabular-nums">{b.accuracy_pct === null ? '—' : formatPct(b.accuracy_pct)}<span className="block text-[10px] text-slate-400">accuracy</span></span>
                <span className="text-sm tabular-nums">{b.max_drawdown_pct === null ? '—' : formatPct(b.max_drawdown_pct)}<span className="block text-[10px] text-slate-400">max DD</span></span>
                <StatusBadge status={b.status} />
              </li>
            ))}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ARCHIVE
// ---------------------------------------------------------------------------
export const TheArchive = () => {
  const overview = useResource('archive:overview', () => apiClient.get<Overview>('/archive/overview'), { intervalMs: 60_000 });
  const tables = useResource('archive:tables', () => apiClient.get<TableSummary[]>('/archive/tables'), { intervalMs: 60_000 });
  const o = overview.data;
  const largest = [...(tables.data ?? [])].sort((a, b) => b.row_count - a.row_count).slice(0, 10);

  return (
    <Page>
      <CommanderHero
        commander="TODAR MAL"
        headline={o ? `ARCHIVE ${o.database_status}. ${formatInt(o.total_records)} records across ${o.total_tables} tables, probe ${o.probe_latency_ms?.toFixed(1) ?? '—'}ms.` : 'ARCHIVE syncing the warehouse inventory…'}
        motif={MOTIFS.stack}
        detail="Read-only warehouse view. Credential tables are never browsable, sensitive columns are redacted, and every table read is written to the access log."
        actions={
          <>
            <Button variant="primary" icon="play_circle" onClick={() => document.getElementById('backtest-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>Run backtest</Button>
            <Button icon="refresh" onClick={() => { void overview.refresh(); void tables.refresh(); }}>Re-inventory</Button>
          </>
        }
      />
      <Panel title="Vault posture" icon="lock" className="lg:col-span-5" updatedAt={overview.updatedAt}>
        <Async resource={overview}>
          {(ov) => (
            <KeyValues items={[
              { label: 'Database', value: <StatusBadge status={ov.database_status} /> },
              { label: 'Encryption', value: ov.encryption_status },
              { label: 'Algorithm', value: ov.algorithm },
              { label: 'Last backup', value: ov.last_backup_at ? formatAgo(ov.last_backup_at) : <Pill tone="warning" icon="warning">none visible</Pill> },
              { label: 'Tables', value: formatInt(ov.total_tables) },
              { label: 'Records', value: formatInt(ov.total_records) },
            ]} />
          )}
        </Async>
        {o && !o.last_backup_at && <p className="mt-3 text-[11px] text-slate-400">Set ARCHIVE_BACKUP_DIR to the directory scripts/backup.sh writes to and the newest archive's time appears here.</p>}
      </Panel>
      <Panel title="Storage by table" icon="dns" className="lg:col-span-7" updatedAt={tables.updatedAt} subtitle="largest 10">
        <Async resource={tables} isEmpty={() => largest.length === 0} empty={<EmptyState icon="dns" title="No tables" />}>
          {() => <DivergingBars caption="Row count by table" formatValue={formatInt} items={largest.map((t) => ({ label: t.table_name, value: t.row_count }))} />}
        </Async>
      </Panel>
      <TableBrowser tables={tables} />
      <BacktestLedger />
      <Panel title="Retention notes" icon="info" className="lg:col-span-5">
        <ul className="list-disc space-y-2 pl-4 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
          <li>The bet ledger is append-only and idempotent: an order key can never be written twice.</li>
          <li>Odds snapshots keep every bookmaker update, which powers the Lab's drift reports and the Oracle consensus.</li>
          <li>Exports download exactly what is on screen (one page of {PAGE} rows) as CSV.</li>
        </ul>
      </Panel>
    </Page>
  );
};
