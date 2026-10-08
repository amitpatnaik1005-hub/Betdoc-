import { useMemo, useState, type ReactNode } from 'react';
import { apiClient } from '../api/client';
import { PhantomHologramScene } from '../components/bots/PhantomHologramScene';
import { type MatchOdds, type Side, matchOddsMarket, outcomeSide, useLiveOdds } from '../lib/api';
import { formatAgo, formatINR, formatOdds, formatPct, humanize } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, type Column, DataTable, EmptyState, Field, KeyValues, NumberInput, Page, Panel, Segmented, Pill, Select, StatusBadge, TextInput, num } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface Opportunity { id: string; event_name: string; market_type: string; total_implied_probability: number; guaranteed_profit_pct: number; target_total_stake: number; stakes: number[]; is_active: boolean; created_at: string }
interface CalcLog { id: string; calc_type: string; inputs: Record<string, unknown>; outputs: Record<string, unknown>; created_at: string }
interface Surebet { match_id: string; market_type: string; legs: { selection_id: string; bookmaker_id: string; odds: number; recommended_stake_pct: number }[]; profit_pct: number }
interface Comparison { best_bookmaker: string; best_odds: number; mean_odds: number; edge_percentage: number; placement_instruction: string; considered_bookmakers: string[]; ignored_bookmakers: string[] }

type Tool = 'arbitrage' | 'dutching' | 'matched' | 'maker' | 'coint';
const TOOLS: { key: Tool; label: string; icon: string }[] = [
  { key: 'arbitrage', label: 'Arbitrage', icon: 'balance' },
  { key: 'dutching', label: 'Dutching', icon: 'call_split' },
  { key: 'matched', label: 'Matched betting', icon: 'swap_horiz' },
  { key: 'maker', label: 'Market maker', icon: 'candlestick_chart' },
  { key: 'coint', label: 'Cointegration', icon: 'timeline' },
];

const list = (raw: string): number[] => raw.split(/[,\s]+/).filter(Boolean).map(Number);

/** Render a calculator response as label/value rows (arrays of numbers joined, logs hidden). */
function resultRows(res: Record<string, unknown>): { label: string; value: ReactNode }[] {
  return Object.entries(res)
    .filter(([k, v]) => k !== 'log' && k !== 'opportunity' && v !== null && (typeof v !== 'object' || Array.isArray(v)))
    .map(([k, v]) => ({
      label: humanize(k),
      value: Array.isArray(v) ? v.map((x) => (typeof x === 'number' ? x.toFixed(2) : String(x))).join(' · ') : typeof v === 'number' ? (Math.abs(v) < 10 && !Number.isInteger(v) ? v.toFixed(4) : v.toLocaleString('en-IN', { maximumFractionDigits: 2 })) : typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v),
    }));
}

// ---------------------------------------------------------------------------
// QUANT TOOLKIT
// ---------------------------------------------------------------------------
const Toolkit = () => {
  const [tool, setTool] = useState<Tool>('arbitrage');
  const [f, setF] = useState({
    event: 'IND v AUS', market: 'Match Odds', odds: '2.10, 2.05', commissions: '0, 2', stake: '1000', minMargin: '0',
    backStake: '100', backOdds: '3.0', layOdds: '3.1', layComm: '2', mode: 'STANDARD',
    mid: '2.0', inventory: '0', gamma: '0.1', sigma: '0.2', horizon: '1', now: '0', k: '1.5',
    z: '2.3', entry: '2', exit: '0.5', stop: '3.5',
  });
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  const run = async () => {
    setBusy(true);
    setResult(null);
    const calls: Record<Tool, () => Promise<Record<string, unknown>>> = {
      arbitrage: () => apiClient.post('/phantom/arbitrage', { event_name: f.event, market_type: f.market, odds: list(f.odds), commissions_pct: list(f.commissions), target_total_stake: num(f.stake), minimum_profit_margin_pct: num(f.minMargin) }),
      dutching: () => apiClient.post('/phantom/dutching', { target_total_stake: num(f.stake), odds: list(f.odds) }),
      matched: () => apiClient.post('/phantom/matched-betting', { back_stake: num(f.backStake), back_odds: num(f.backOdds), lay_odds: num(f.layOdds), lay_commission_pct: num(f.layComm), mode: f.mode }),
      maker: () => apiClient.post('/phantom/market-maker', { mid_price: num(f.mid), inventory: num(f.inventory), gamma: num(f.gamma), volatility_sigma: num(f.sigma), time_horizon_t: num(f.horizon), current_time_t: num(f.now), liquidity_k: num(f.k) }),
      coint: () => apiClient.post('/phantom/cointegration', { current_z_score: num(f.z), entry_threshold: num(f.entry), exit_threshold: num(f.exit), stop_loss_threshold: num(f.stop) }),
    };
    const res = await runMutation(calls[tool], { invalidate: ['phantom', 'commanders'], errorTitle: 'Calculation rejected' });
    setBusy(false);
    if (res) setResult(res);
  };

  return (
    <Panel title="Quant toolkit" icon="construction" className="lg:col-span-7" subtitle="every run is logged to the audit trail">
      <div className="mb-6">
        <Segmented label="Tool" value={tool} onChange={(t) => { setTool(t); setResult(null); }} options={TOOLS.map((t) => ({ value: t.key, label: t.label, icon: t.icon }))} />
      </div>
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="grid grid-cols-2 content-start gap-2.5">
          {(tool === 'arbitrage' || tool === 'dutching') && (
            <>
              {tool === 'arbitrage' && <Field label="Event" className="col-span-2"><TextInput value={f.event} onChange={set('event')} /></Field>}
              <Field label="Odds per outcome" className="col-span-2" hint="comma-separated, one per leg"><TextInput value={f.odds} onChange={set('odds')} /></Field>
              {tool === 'arbitrage' && <Field label="Commission % per leg" className="col-span-2"><TextInput value={f.commissions} onChange={set('commissions')} /></Field>}
              <Field label="Total stake ₹"><NumberInput value={f.stake} onChange={set('stake')} /></Field>
              {tool === 'arbitrage' && <Field label="Min margin %"><NumberInput value={f.minMargin} onChange={set('minMargin')} /></Field>}
            </>
          )}
          {tool === 'matched' && (
            <>
              <Field label="Back stake ₹"><NumberInput value={f.backStake} onChange={set('backStake')} /></Field>
              <Field label="Back odds"><NumberInput value={f.backOdds} onChange={set('backOdds')} /></Field>
              <Field label="Lay odds"><NumberInput value={f.layOdds} onChange={set('layOdds')} /></Field>
              <Field label="Lay commission %"><NumberInput value={f.layComm} onChange={set('layComm')} /></Field>
              <Field label="Mode" className="col-span-2"><Select value={f.mode} onChange={set('mode')}>{['STANDARD', 'UNDERLAY', 'OVERLAY'].map((m) => <option key={m}>{m}</option>)}</Select></Field>
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
              {'is_arbitrage' in result && <StatusBadge status={result.is_arbitrage ? 'ONLINE' : 'IDLE'} label={result.is_arbitrage ? 'Arbitrage locked' : 'No arbitrage'} />}
              {'signal' in result && <StatusBadge status={String(result.signal).includes('STOP') ? 'CRITICAL' : String(result.signal) === 'HOLD' ? 'IDLE' : 'INFO'} label={humanize(String(result.signal))} />}
              <KeyValues items={resultRows(result)} />
            </div>
          ) : (
            <EmptyState icon="calculate" title="Run a calculation" detail="Avellaneda-Stoikov quoting, cointegration signals, dutching and arbitrage staking are computed server-side." />
          )}
        </div>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// ODDS ROUTER (/bookmakers/compare-odds)
// ---------------------------------------------------------------------------
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
    const res = await runMutation(() => apiClient.post<Comparison>('/bookmakers/compare-odds', { sport: match.sport_key, league: match.sport_key, match: `${match.home_team} v ${match.away_team}`, market: 'h2h', selection: side, odds: prices, execute: false }), { errorTitle: 'Routing failed' });
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
          <div className="grid grid-cols-3 gap-1.5">
            {(['HOME', 'DRAW', 'AWAY'] as const).map((s) => (
              <button key={s} type="button" onClick={() => { setSide(s); setResult(null); }} className={`rounded-xl py-1.5 text-xs font-bold ${side === s ? 'bg-[var(--accent)] text-[var(--accent-ink)]' : 'bg-stone-100 text-stone-600 dark:bg-white/[0.05] dark:text-stone-300'}`}>{s}</button>
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

// ---------------------------------------------------------------------------
// NAMED EXPORT: PHANTOM
// ---------------------------------------------------------------------------
export const Phantom = () => {
  const opportunities = useResource('phantom:opportunities', () => apiClient.get<Opportunity[]>('/phantom/opportunities', { limit: 50 }), { intervalMs: 30_000 });
  const calculations = useResource('phantom:calculations', () => apiClient.get<CalcLog[]>('/phantom/calculations', { limit: 30 }), { intervalMs: 30_000 });
  const surebets = useResource('signals:surebets', () => apiClient.get<Surebet[]>('/signals/surebets', { max_staleness_minutes: 30 }), { intervalMs: 60_000 });
  const odds = useLiveOdds();

  const active = (opportunities.data ?? []).filter((o) => o.is_active);
  const best = [...(surebets.data ?? [])].sort((a, b) => b.profit_pct - a.profit_pct)[0];
  const found = active.length > 0 || Boolean(best);

  const oppColumns: Column<Opportunity>[] = [
    { key: 'event', header: 'Event', render: (o) => <span><span className="font-medium">{o.event_name}</span> <span className="text-xs text-stone-400">· {o.market_type}</span></span> },
    { key: 'profit', header: 'Locked profit', align: 'right', render: (o) => <span className="font-semibold text-emerald-600 dark:text-emerald-400">{formatPct(o.guaranteed_profit_pct, 2)}</span> },
    { key: 'stakes', header: 'Stake split', align: 'right', render: (o) => <span className="font-mono text-xs">{o.stakes.map((s) => formatINR(s)).join(' / ')}</span> },
    { key: 'status', header: 'Status', render: (o) => <StatusBadge status={o.is_active ? 'ACTIVE' : 'EXPIRED'} /> },
    { key: 'when', header: 'Found', align: 'right', render: (o) => <span className="text-xs text-stone-500">{formatAgo(o.created_at)}</span> },
  ];

  return (
    <Page>
      <CommanderHero
        commander="GARUDA"
        headline={found ? `GARUDA LOCKED ON. ${active.length} live arbitrage${active.length === 1 ? '' : 's'}${best ? `, best surebet ${formatPct(best.profit_pct, 2)}` : ''}.` : 'GARUDA SCANNING. No risk-free spread on fresh prices yet.'}
        motif={MOTIFS.waves}
        detail="Arbitrage, dutching and market-making maths on live prices. Every calculation is persisted to an auditable log."
        scene={<PhantomHologramScene scanProgress={found ? 100 : Math.min(95, (surebets.data?.length ?? 0) * 10 + 35)} targetFound={found} targetLabel={best ? best.match_id : active[0]?.event_name} className="h-[220px]" />}
        actions={
          <>
            <Button variant="primary" icon="radar" busy={surebets.loading && surebets.data !== undefined} onClick={() => void surebets.refresh()}>Scan surebets</Button>
            <Button icon="refresh" onClick={() => { void opportunities.refresh(); void calculations.refresh(); }}>Refresh ledger</Button>
          </>
        }
      />
      <Toolkit />
      <OddsRouter fixtures={odds.data ?? []} />
      <Panel title="Live surebets" icon="verified" className="lg:col-span-5" updatedAt={surebets.updatedAt}>
        <Async resource={surebets} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="verified" title="No surebets" detail="Cross-book prices on fresh ticks don't currently sum below 100%." />}>
          {(rows) => (
            <ul className="flex flex-col gap-2">
              {rows.slice(0, 8).map((s) => (
                <li key={`${s.match_id}-${s.market_type}`} className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs text-stone-700 dark:text-stone-200">{s.match_id} · {s.market_type}</span>
                    <Pill tone="good">{formatPct(s.profit_pct, 2)}</Pill>
                  </div>
                  <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">{s.legs.map((l) => `${l.selection_id} ${formatOdds(l.odds)} @ ${l.bookmaker_id} (${formatPct(l.recommended_stake_pct, 0)})`).join(' · ')}</p>
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>
      <Panel title="Shadow execution ledger" icon="blur_on" className="lg:col-span-7" updatedAt={opportunities.updatedAt}>
        <Async resource={opportunities} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="blur_on" title="No arbitrage recorded" detail="Arbitrage runs that find a locked profit are persisted here." />}>
          {(rows) => <DataTable columns={oppColumns} rows={rows} rowKey={(o) => o.id} dense />}
        </Async>
      </Panel>
      <Panel title="Calculation audit trail" icon="history" className="lg:col-span-12" updatedAt={calculations.updatedAt}>
        <Async resource={calculations} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="history" title="No calculations yet" />}>
          {(rows) => (
            <DataTable
              dense
              rows={rows}
              rowKey={(r) => r.id}
              columns={[
                { key: 'type', header: 'Type', render: (r) => <Pill tone="accent">{humanize(r.calc_type)}</Pill> },
                { key: 'in', header: 'Inputs', render: (r) => <span className="font-mono text-[11px] text-stone-500">{JSON.stringify(r.inputs).slice(0, 90)}</span> },
                { key: 'out', header: 'Outputs', render: (r) => <span className="font-mono text-[11px] text-stone-500">{JSON.stringify(r.outputs).slice(0, 90)}</span> },
                { key: 'at', header: 'When', align: 'right', render: (r) => <span className="text-xs text-stone-500">{formatAgo(r.created_at)}</span> },
              ]}
            />
          )}
        </Async>
      </Panel>
    </Page>
  );
};
