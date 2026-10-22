/**
 * Group 96: The Lab's quant toolkit (PANINI), absorbed from the retired Phantom section.
 * Four server-side calculators (dutching, matched betting, Avellaneda-Stoikov market making, cointegration
 * signals), every run logged to ``phantom_calculations``; the smart odds router; and the run history.
 * Architect: Amit Ashok Kumar Patnaik.
 */
import { useMemo, useState, type ChangeEvent, type ReactNode } from 'react';
import { apiClient } from '../../api/client';
import { type MatchOdds, type Side, matchOddsMarket, outcomeSide, useLiveOdds } from '../../lib/api';
import { formatAgo, formatOdds, formatPct, humanize } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { Async, Button, DataTable, EmptyState, Field, KeyValues, NumberInput, Panel, Pill, Segmented, Select, StatusBadge, TextInput } from '../../ui/kit';
import { num } from '../../ui/num';

interface CalcLog { id: string; calc_type: string; inputs: Record<string, unknown>; outputs: Record<string, unknown>; created_at: string }
interface Comparison { best_bookmaker: string; best_odds: number; mean_odds: number; edge_percentage: number; placement_instruction: string; considered_bookmakers: string[]; ignored_bookmakers: string[] }

type Tool = 'dutching' | 'matched' | 'maker' | 'coint';
const TOOLS: readonly { value: Tool; label: string; icon: string }[] = [
  { value: 'dutching', label: 'Dutching', icon: 'call_split' },
  { value: 'matched', label: 'Matched betting', icon: 'swap_horiz' },
  { value: 'maker', label: 'Market maker', icon: 'candlestick_chart' },
  { value: 'coint', label: 'Cointegration', icon: 'timeline' },
];

const BASE = '/lab/quant-toolkit';
const HISTORY = 'lab:quant:calculations';

const list = (raw: string): number[] => raw.split(/[,\s]+/).filter(Boolean).map(Number);

/** A calculator response as label/value rows (number arrays joined; the log is shown in the history instead). */
function resultRows(res: Record<string, unknown>): { label: string; value: ReactNode }[] {
  return Object.entries(res)
    .filter(([k, v]) => k !== 'log' && v !== null && (typeof v !== 'object' || Array.isArray(v)))
    .map(([k, v]) => ({
      label: humanize(k),
      value: Array.isArray(v)
        ? v.map((x) => (typeof x === 'number' ? x.toFixed(2) : String(x))).join(' · ')
        : typeof v === 'number'
          ? Math.abs(v) < 10 && !Number.isInteger(v) ? v.toFixed(4) : v.toLocaleString('en-IN', { maximumFractionDigits: 2 })
          : String(v),
    }));
}

const Toolkit = () => {
  const [tool, setTool] = useState<Tool>('dutching');
  const [f, setF] = useState({
    odds: '2.10, 3.40, 3.60', stake: '1000',
    backStake: '100', backOdds: '3.0', layOdds: '3.1', layComm: '2', mode: 'STANDARD',
    mid: '2.0', inventory: '0', gamma: '0.1', sigma: '0.2', horizon: '1', now: '0', k: '1.5',
    z: '2.3', entry: '2', exit: '0.5', stop: '3.5',
  });
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  const calls: Record<Tool, () => Promise<Record<string, unknown>>> = {
    dutching: () => apiClient.post(`${BASE}/dutching`, { target_total_stake: num(f.stake), odds: list(f.odds) }),
    matched: () => apiClient.post(`${BASE}/matched-betting`, { back_stake: num(f.backStake), back_odds: num(f.backOdds), lay_odds: num(f.layOdds), lay_commission_pct: num(f.layComm), mode: f.mode }),
    maker: () => apiClient.post(`${BASE}/market-maker`, { mid_price: num(f.mid), inventory: num(f.inventory), gamma: num(f.gamma), volatility_sigma: num(f.sigma), time_horizon_t: num(f.horizon), current_time_t: num(f.now), liquidity_k: num(f.k) }),
    coint: () => apiClient.post(`${BASE}/cointegration`, { current_z_score: num(f.z), entry_threshold: num(f.entry), exit_threshold: num(f.exit), stop_loss_threshold: num(f.stop) }),
  };

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await runMutation(calls[tool], { invalidate: [HISTORY], errorTitle: 'Calculation rejected' });
      if (res) setResult(res);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Quant toolkit" icon="construction" className="lg:col-span-7" subtitle="PANINI · every run is logged to the audit trail">
      <div className="mb-6">
        <Segmented<Tool> label="Calculator" value={tool} onChange={(t) => { setTool(t); setResult(null); }} options={TOOLS} />
      </div>
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="grid grid-cols-2 content-start gap-2.5">
          {tool === 'dutching' && (
            <>
              <Field label="Odds per outcome" className="col-span-2" hint="comma-separated, one per selection"><TextInput value={f.odds} onChange={set('odds')} /></Field>
              <Field label="Total stake ₹"><NumberInput value={f.stake} onChange={set('stake')} /></Field>
            </>
          )}
          {tool === 'matched' && (
            <>
              <Field label="Back stake ₹"><NumberInput value={f.backStake} onChange={set('backStake')} /></Field>
              <Field label="Back odds"><NumberInput value={f.backOdds} onChange={set('backOdds')} /></Field>
              <Field label="Lay odds"><NumberInput value={f.layOdds} onChange={set('layOdds')} /></Field>
              <Field label="Lay commission %"><NumberInput value={f.layComm} onChange={set('layComm')} /></Field>
              <Field label="Mode" className="col-span-2"><Select value={f.mode} onChange={set('mode')}>{['STANDARD', 'UNDERLAY', 'OVERLAY'].map((m) => <option key={m} value={m}>{humanize(m)}</option>)}</Select></Field>
            </>
          )}
          {tool === 'maker' && (
            <>
              <Field label="Mid price"><NumberInput value={f.mid} onChange={set('mid')} /></Field>
              <Field label="Inventory"><NumberInput value={f.inventory} onChange={set('inventory')} /></Field>
              <Field label="Risk aversion γ"><NumberInput value={f.gamma} onChange={set('gamma')} /></Field>
              <Field label="Volatility σ"><NumberInput value={f.sigma} onChange={set('sigma')} /></Field>
              <Field label="Horizon T"><NumberInput value={f.horizon} onChange={set('horizon')} /></Field>
              <Field label="Now t"><NumberInput value={f.now} onChange={set('now')} /></Field>
              <Field label="Liquidity k"><NumberInput value={f.k} onChange={set('k')} /></Field>
            </>
          )}
          {tool === 'coint' && (
            <>
              <Field label="Current z-score"><NumberInput value={f.z} onChange={set('z')} /></Field>
              <Field label="Entry |z|"><NumberInput value={f.entry} onChange={set('entry')} /></Field>
              <Field label="Exit |z|"><NumberInput value={f.exit} onChange={set('exit')} /></Field>
              <Field label="Stop |z|"><NumberInput value={f.stop} onChange={set('stop')} /></Field>
            </>
          )}
          <Button variant="primary" icon="calculate" busy={busy} onClick={() => void run()} className="col-span-2">Calculate</Button>
        </div>
        <div className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
          {result ? (
            <div className="flex flex-col gap-3">
              {'signal' in result && (
                <StatusBadge status={String(result.signal).includes('STOP') ? 'CRITICAL' : String(result.signal) === 'HOLD' ? 'IDLE' : 'INFO'} label={humanize(String(result.signal))} />
              )}
              <KeyValues items={resultRows(result)} />
            </div>
          ) : (
            <EmptyState icon="calculate" title="Run a calculation" detail="Dutching and matched-betting stakes, Avellaneda-Stoikov quotes and cointegration signals are computed server-side." />
          )}
        </div>
      </div>
    </Panel>
  );
};

const OddsRouter = ({ fixtures }: { fixtures: MatchOdds[] }) => {
  const [matchId, setMatchId] = useState('');
  const [side, setSide] = useState<Side>('HOME');
  const [result, setResult] = useState<Comparison | null>(null);
  const match = fixtures.find((m) => m.id === matchId) ?? fixtures[0];

  const prices = useMemo(() => {
    if (!match) return {} as Record<string, number>;
    const out: Record<string, number> = {};
    for (const b of match.bookmakers) {
      const o = matchOddsMarket(b)?.outcomes.find((x) => outcomeSide(x.name, match) === side);
      if (o && o.price > 1) out[b.title] = o.price;
    }
    return out;
  }, [match, side]);

  const route = async () => {
    if (!match) return;
    const res = await runMutation(
      () => apiClient.post<Comparison>('/bookmakers/compare-odds', { sport: match.sport_key, league: match.sport_key, match: `${match.home_team} v ${match.away_team}`, market: 'h2h', selection: side, odds: prices, execute: false }),
      { errorTitle: 'Routing failed' },
    );
    if (res) setResult(res);
  };

  return (
    <Panel title="Smart odds router" icon="alt_route" className="lg:col-span-5" subtitle="best enabled bookmaker for a selection">
      {fixtures.length === 0 ? (
        <EmptyState icon="alt_route" title="No live fixtures" detail="Routing compares the live prices stored by the odds poller." />
      ) : (
        <div className="flex flex-col gap-3">
          <Field label="Fixture">
            <Select value={match?.id ?? ''} onChange={(e) => { setMatchId(e.target.value); setResult(null); }}>
              {fixtures.map((m) => <option key={m.id} value={m.id}>{m.home_team} v {m.away_team}</option>)}
            </Select>
          </Field>
          <div className="grid grid-cols-3 gap-1.5" role="group" aria-label="Selection">
            {(['HOME', 'DRAW', 'AWAY'] as const).map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={side === s}
                onClick={() => { setSide(s); setResult(null); }}
                className={`rounded-xl py-1.5 text-xs font-bold ${side === s ? 'bg-[var(--accent)] text-[var(--accent-ink)]' : 'bg-stone-100 text-stone-600 dark:bg-white/[0.05] dark:text-stone-300'}`}
              >
                {s}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">{Object.keys(prices).length} books quote this side. Bookmakers disabled in the Control Panel are ignored.</p>
          <Button variant="primary" icon="alt_route" disabled={Object.keys(prices).length < 2} onClick={() => void route()}>Route</Button>
          {result && (
            <KeyValues items={[
              { label: 'Best', value: `${result.best_bookmaker} @ ${formatOdds(result.best_odds)}` },
              { label: 'Market mean', value: formatOdds(result.mean_odds) },
              { label: 'Edge vs mean', value: formatPct(result.edge_percentage, 2) },
              { label: 'Ignored', value: result.ignored_bookmakers.length ? result.ignored_bookmakers.join(', ') : 'none' },
            ]} />
          )}
          {result && <p className="text-xs text-stone-600 dark:text-stone-300">{result.placement_instruction}</p>}
        </div>
      )}
    </Panel>
  );
};

const History = () => {
  const calculations = useResource(HISTORY, () => apiClient.get<CalcLog[]>(`${BASE}/calculations`, { limit: 15 }), { intervalMs: 60_000 });
  return (
    <Panel title="Quant calculation history" icon="history" className="lg:col-span-12" subtitle="the newest 15 runs" updatedAt={calculations.updatedAt}>
      <Async resource={calculations} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="history" title="No calculations yet" detail="Each calculator run is recorded here." />}>
        {(rows) => (
          <DataTable
            dense
            rows={rows}
            rowKey={(r) => r.id}
            columns={[
              { key: 'type', header: 'Calculator', render: (r) => <Pill tone="accent">{humanize(r.calc_type)}</Pill> },
              { key: 'in', header: 'Inputs', render: (r) => <span className="font-mono text-[11px] text-stone-500" title={JSON.stringify(r.inputs)}>{JSON.stringify(r.inputs).slice(0, 90)}</span> },
              { key: 'out', header: 'Outputs', render: (r) => <span className="font-mono text-[11px] text-stone-500" title={JSON.stringify(r.outputs)}>{JSON.stringify(r.outputs).slice(0, 90)}</span> },
              { key: 'at', header: 'When', align: 'right', render: (r) => <span className="text-xs text-stone-500">{formatAgo(r.created_at)}</span> },
            ]}
          />
        )}
      </Async>
    </Panel>
  );
};

/** The Lab's quant workbench: calculators, odds router and run history, laid out on the page's 12-column grid. */
export const QuantToolkitPanel = () => {
  const odds = useLiveOdds();
  return (
    <>
      <Toolkit />
      <OddsRouter fixtures={odds.data ?? []} />
      <History />
    </>
  );
};
