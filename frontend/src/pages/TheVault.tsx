import { useEffect, useState, type FormEvent } from 'react';
import { apiClient } from '../api/client';
import { useDashboardSummary } from '../lib/api';
import { downloadCsv, formatDate, formatDateTime, formatINR, formatINRCompact, formatOdds, formatPct, formatRatioPct, formatSignedINR } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { DivergingBars, LineChart } from '../ui/charts';
import { AnimatedNumber, Async, Button, ConfirmButton, type Column, DataTable, EmptyState, Field, KeyValues, NumberInput, Page, Panel, Segmented, Stat, StatusBadge, TextInput, Toggle, num } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface Overview { base_currency: string; starting_bankroll: number; total_exposure: number; available_capital: number; gross_profit: number; gross_loss: number; net_pnl: number; total_volume: number; roi_pct: number; active_bets_count: number; settled_bets_count: number }
interface GrowthNode { timestamp: string; actual_bankroll: number; projected_bankroll: number; cumulative_ev: number }
interface PeriodNode { period: string; profit: number; volume: number; yield_pct: number; bets_won: number; bets_lost: number }
interface MarketPnL { market_type: string; net_profit: number; volume: number; roi_pct: number }
interface BookPnL { exchange: string; net_profit: number; volume: number; roi_pct: number }
interface Waterfall { category: string; value: number }
interface LedgerRow { id: string; match_id: string; market_type: string; selection: string; currency: string; odds: string; stake: string; true_probability: string; status: string; placed_at: string; resolved_at: string | null; payout: string | null; exchange_bet_id: string | null }
interface RiskMetrics { var_95: number; var_99: number; cvar_95: number; max_drawdown: number; current_drawdown: number; sharpe_ratio: number; sortino_ratio: number; calmar_ratio: number; total_exposure: number; exposure_pct: number; kelly_portfolio_fraction: number; total_bets: number; win_rate: number }
interface StopLossStatus { is_triggered: boolean; trigger_reason: string | null; daily_loss_current: number; consecutive_losses_current: number; cooldown_until: string | null; session_loss_current: number; session_started_at: string | null }
interface StopLossConfig { daily_loss_limit: number | null; consecutive_loss_limit: number | null; trailing_stop_pct: number | null; cooldown_minutes: number | null; session_loss_limit: number | null; enabled: boolean }
interface CfoAlert { id: string; level: 'INFO' | 'WARNING' | 'CRITICAL'; message: string; is_read: boolean; created_at: string }

type Timeframe = 'DAILY' | 'WEEKLY' | 'MONTHLY' | 'YEARLY';

// ---------------------------------------------------------------------------
// PANELS
// ---------------------------------------------------------------------------
const GrowthPanel = ({ bankroll }: { bankroll: number }) => {
  const growth = useResource(bankroll > 0 ? `vault:growth:${Math.round(bankroll)}` : null, () => apiClient.get<GrowthNode[]>('/vault/growth', { bankroll, currency: 'INR' }), { intervalMs: 60_000 });
  return (
    <Panel title="Bankroll growth" icon="show_chart" className="lg:col-span-8" updatedAt={growth.updatedAt} subtitle="actual vs EV-projected">
      <Async resource={growth} isEmpty={(r) => r.length < 2} empty={<EmptyState icon="show_chart" title="Not enough settled history" detail="The curve appears after at least two settled positions." />}>
        {(rows) => (
          <LineChart
            caption="Bankroll over time: actual versus EV-projected"
            labels={rows.map((r) => formatDate(r.timestamp))}
            formatValue={formatINRCompact}
            series={[
              { name: 'Actual', color: 'var(--viz-series-1)', values: rows.map((r) => r.actual_bankroll) },
              { name: 'EV projected', color: 'var(--viz-series-2)', values: rows.map((r) => r.projected_bankroll), dashed: true },
            ]}
          />
        )}
      </Async>
    </Panel>
  );
};

const PnLBreakdown = () => {
  const [tf, setTf] = useState<Timeframe>('DAILY');
  const periods = useResource(`vault:pnl:${tf}`, () => apiClient.get<PeriodNode[]>('/vault/pnl/timeframe', { timeframe: tf, currency: 'INR' }), { intervalMs: 60_000 });
  const markets = useResource('vault:pnl-market', () => apiClient.get<MarketPnL[]>('/vault/pnl/market', { currency: 'INR' }), { intervalMs: 60_000 });
  const books = useResource('vault:pnl-book', () => apiClient.get<BookPnL[]>('/vault/pnl/bookmaker', { currency: 'INR' }), { intervalMs: 60_000 });
  const empty = <EmptyState icon="bar_chart" title="No settled P&L yet" />;
  return (
    <Panel
      title="P&L breakdown"
      icon="bar_chart"
      className="lg:col-span-12"
      updatedAt={periods.updatedAt}
      actions={
        <Segmented
          label="Timeframe"
          size="sm"
          value={tf}
          onChange={setTf}
          options={(['DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'] as const).map((t) => ({ value: t, label: t.charAt(0) + t.slice(1).toLowerCase() }))}
        />
      }
    >
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div>
          <p className="mb-2 text-xs font-semibold text-stone-400">By {tf.toLowerCase().replace('ly', '')}</p>
          <Async resource={periods} isEmpty={(r) => r.length === 0} empty={empty}>
            {(rows) => <DivergingBars caption="Profit by period" formatValue={formatINR} items={rows.slice(-10).map((r) => ({ label: r.period, value: r.profit, detail: `${r.bets_won}W / ${r.bets_lost}L · yield ${formatPct(r.yield_pct)} · volume ${formatINR(r.volume)}` }))} />}
          </Async>
        </div>
        <div>
          <p className="mb-2 text-xs font-semibold text-stone-400">By market</p>
          <Async resource={markets} isEmpty={(r) => r.length === 0} empty={empty}>
            {(rows) => <DivergingBars caption="Net profit by market" formatValue={formatINR} items={rows.map((r) => ({ label: r.market_type, value: r.net_profit, detail: `ROI ${formatPct(r.roi_pct)} · volume ${formatINR(r.volume)}` }))} />}
          </Async>
        </div>
        <div>
          <p className="mb-2 text-xs font-semibold text-stone-400">By exchange</p>
          <Async resource={books} isEmpty={(r) => r.length === 0} empty={empty}>
            {(rows) => <DivergingBars caption="Net profit by exchange" formatValue={formatINR} items={rows.map((r) => ({ label: r.exchange, value: r.net_profit, detail: `ROI ${formatPct(r.roi_pct)} · volume ${formatINR(r.volume)}` }))} />}
          </Async>
        </div>
      </div>
    </Panel>
  );
};

const LedgerPanel = ({ ledger }: { ledger: ReturnType<typeof useResource<LedgerRow[]>> }) => {
  const columns: Column<LedgerRow>[] = [
    { key: 'when', header: 'Placed', render: (r) => <span className="whitespace-nowrap text-xs text-stone-500">{formatDateTime(r.placed_at)}</span> },
    { key: 'what', header: 'Position', render: (r) => <span>{r.selection} <span className="font-mono text-xs text-stone-400">{r.match_id}</span></span> },
    { key: 'ref', header: 'Exchange ref', render: (r) => <span className="font-mono text-[11px] text-stone-400">{r.exchange_bet_id ?? r.id.slice(0, 8)}</span> },
    { key: 'stake', header: 'Stake @ odds', align: 'right', render: (r) => `${formatINR(Number(r.stake))} @ ${formatOdds(Number(r.odds))}` },
    { key: 'payout', header: 'Payout', align: 'right', render: (r) => (r.payout === null ? '—' : formatINR(Number(r.payout))) },
    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
  ];
  return (
    <Panel title="Immutable ledger" icon="receipt_long" className="lg:col-span-12" updatedAt={ledger.updatedAt} subtitle="append-only, idempotent">
      <Async resource={ledger} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="receipt_long" title="No ledger entries" detail="Every order you place from the bet slip is recorded here." />}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} dense />}
      </Async>
    </Panel>
  );
};

const RiskPanel = ({ bankroll }: { bankroll: number }) => {
  const risk = useResource(bankroll > 0 ? `capital:risk:${Math.round(bankroll)}` : null, () => apiClient.get<RiskMetrics>('/capital/risk', { bankroll }), { intervalMs: 60_000 });
  return (
    <Panel title="Risk engine · CHANAKYA" icon="shield" className="lg:col-span-4" updatedAt={risk.updatedAt}>
      <Async resource={risk}>
        {(r) => (
          <KeyValues items={[
            { label: 'VaR 95% / 99%', value: `${formatINR(r.var_95)} / ${formatINR(r.var_99)}` },
            { label: 'CVaR 95%', value: formatINR(r.cvar_95) },
            { label: 'Drawdown now / max', value: `${formatRatioPct(r.current_drawdown)} / ${formatRatioPct(r.max_drawdown)}` },
            { label: 'Sharpe · Sortino', value: `${r.sharpe_ratio.toFixed(2)} · ${r.sortino_ratio.toFixed(2)}` },
            { label: 'Exposure', value: `${formatINR(r.total_exposure)} (${formatPct(r.exposure_pct)})` },
            { label: 'Portfolio Kelly', value: formatRatioPct(r.kelly_portfolio_fraction, 2) },
            { label: 'Win rate', value: `${formatRatioPct(r.win_rate)} of ${r.total_bets}` },
          ]} />
        )}
      </Async>
    </Panel>
  );
};

const StopLossPanel = () => {
  const summary = useDashboardSummary();
  const bankroll = summary.data?.total_bankroll ?? 0;
  const status = useResource(bankroll > 0 ? `capital:stop-loss:${Math.round(bankroll)}` : null, () => apiClient.get<StopLossStatus>('/capital/stop-loss/status', { bankroll }), { intervalMs: 30_000 });
  const config = useResource('capital:stop-loss-config', () => apiClient.get<StopLossConfig>('/capital/stop-loss/config'));
  const [draft, setDraft] = useState<Record<keyof Omit<StopLossConfig, 'enabled'>, string> & { enabled: boolean } | null>(null);
  useEffect(() => {
    if (config.data) {
      const c = config.data;
      const s = (v: number | null) => (v === null ? '' : String(v));
      setDraft({ daily_loss_limit: s(c.daily_loss_limit), consecutive_loss_limit: s(c.consecutive_loss_limit), trailing_stop_pct: s(c.trailing_stop_pct), cooldown_minutes: s(c.cooldown_minutes), session_loss_limit: s(c.session_loss_limit), enabled: c.enabled });
    }
  }, [config.data]);

  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    const n = (v: string) => (Number.isFinite(num(v)) && num(v) > 0 ? num(v) : null);
    void runMutation(() => apiClient.post('/capital/stop-loss/configure', {
      daily_loss_limit: n(draft.daily_loss_limit), consecutive_loss_limit: n(draft.consecutive_loss_limit) && Math.round(n(draft.consecutive_loss_limit)!),
      trailing_stop_pct: n(draft.trailing_stop_pct), cooldown_minutes: n(draft.cooldown_minutes) && Math.round(n(draft.cooldown_minutes)!),
      session_loss_limit: n(draft.session_loss_limit), enabled: draft.enabled,
    }), { invalidate: ['capital', 'dashboard', 'commanders'], success: 'Stop-loss limits saved', errorTitle: 'Stop-loss config rejected' });
  };

  const s = status.data;
  return (
    <Panel title="Stop-loss guard" icon="emergency_home" className="lg:col-span-4" updatedAt={status.updatedAt}>
      <div className="flex flex-col gap-4">
        {s && (
          <div className="flex items-center justify-between gap-2 rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]">
            <StatusBadge status={s.is_triggered ? 'CRITICAL' : 'ONLINE'} label={s.is_triggered ? 'Triggered' : 'Armed'} />
            <span className="text-right text-[11px] text-stone-500 dark:text-stone-400">
              today {formatSignedINR(-s.daily_loss_current)} · {s.consecutive_losses_current} losses in a row
              {s.cooldown_until ? ` · cooldown until ${formatDateTime(s.cooldown_until)}` : ''}
            </span>
          </div>
        )}
        {s?.trigger_reason && <p className="text-xs text-rose-600 dark:text-rose-400">{s.trigger_reason}</p>}
        {draft && (
          <form onSubmit={save} className="grid grid-cols-2 gap-2.5">
            <Field label="Daily loss ₹"><NumberInput value={draft.daily_loss_limit} onChange={(e) => setDraft({ ...draft, daily_loss_limit: e.target.value })} placeholder="off" /></Field>
            <Field label="Session loss ₹"><NumberInput value={draft.session_loss_limit} onChange={(e) => setDraft({ ...draft, session_loss_limit: e.target.value })} placeholder="off" /></Field>
            <Field label="Losses in a row"><NumberInput value={draft.consecutive_loss_limit} onChange={(e) => setDraft({ ...draft, consecutive_loss_limit: e.target.value })} placeholder="off" /></Field>
            <Field label="Trailing stop %"><NumberInput value={draft.trailing_stop_pct} onChange={(e) => setDraft({ ...draft, trailing_stop_pct: e.target.value })} placeholder="off" /></Field>
            <Field label="Cooldown (min)"><NumberInput value={draft.cooldown_minutes} onChange={(e) => setDraft({ ...draft, cooldown_minutes: e.target.value })} placeholder="off" /></Field>
            <div className="flex items-end justify-between gap-2 pb-2">
              <span className="text-xs text-stone-500">Enabled</span>
              <Toggle label="Stop-loss enabled" checked={draft.enabled} onChange={(v) => setDraft({ ...draft, enabled: v })} />
            </div>
            <Button type="submit" variant="primary" icon="save" className="col-span-1">Save</Button>
            <ConfirmButton icon="restart_alt" confirmLabel="Reset session?" onConfirm={() => void runMutation(() => apiClient.post('/capital/stop-loss/reset-session'), { invalidate: ['capital', 'dashboard'], success: 'Session counters reset', errorTitle: 'Reset failed' })}>
              New session
            </ConfirmButton>
          </form>
        )}
      </div>
    </Panel>
  );
};

const CfoPanel = ({ overview }: { overview: Overview | undefined }) => {
  const alerts = useResource('the-vault:alerts', () => apiClient.get<CfoAlert[]>('/the-vault/cfo/alerts', { limit: 20 }), { intervalMs: 60_000 });
  const [mode, setMode] = useState<'advisory' | 'stress' | 'tax'>('advisory');
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [stress, setStress] = useState({ scenario: 'Black swan weekend', shock: '35', survival: '50' });
  const [tax, setTax] = useState({ year: String(new Date().getFullYear()), allowance: '0', rate: '30' });
  const bankroll = overview ? overview.starting_bankroll + overview.net_pnl : 0;

  const run = async () => {
    setResult(null);
    const call =
      mode === 'advisory'
        ? () => apiClient.post<Record<string, unknown>>('/the-vault/cfo/advisory', { current_bankroll: bankroll, active_exposure: overview?.total_exposure ?? 0, variance_threshold_pct: 20 })
        : mode === 'stress'
          ? () => apiClient.post<Record<string, unknown>>('/the-vault/cfo/stress-test', { scenario: stress.scenario, portfolio_value: bankroll, shock_pct: num(stress.shock), survival_threshold_pct: num(stress.survival) })
          : () => apiClient.post<Record<string, unknown>>('/the-vault/cfo/taxes', { year: Math.round(num(tax.year)), total_profit: overview?.net_pnl ?? 0, tax_allowance: num(tax.allowance), tax_rate_pct: num(tax.rate) });
    const res = await runMutation(call, { invalidate: ['the-vault'], errorTitle: 'CFO request failed' });
    if (res) setResult(res);
  };

  return (
    <Panel title="CFO advisory desk" icon="account_balance" className="lg:col-span-4" updatedAt={alerts.updatedAt}>
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-3 gap-1 rounded-xl bg-stone-100 p-0.5 dark:bg-white/[0.05]">
          {(['advisory', 'stress', 'tax'] as const).map((m) => (
            <button key={m} type="button" onClick={() => { setMode(m); setResult(null); }} className={`rounded-lg py-1 text-[11px] font-semibold capitalize ${mode === m ? 'bg-white text-stone-900 shadow-sm dark:bg-white/10 dark:text-white' : 'text-stone-500'}`}>
              {m === 'stress' ? 'Stress test' : m}
            </button>
          ))}
        </div>
        {mode === 'advisory' && <p className="text-xs text-stone-500 dark:text-stone-400">Scores capital health from your live bankroll ({formatINR(bankroll)}) and exposure ({formatINR(overview?.total_exposure)}).</p>}
        {mode === 'stress' && (
          <div className="grid grid-cols-2 gap-2.5">
            <Field label="Scenario" className="col-span-2"><TextInput value={stress.scenario} onChange={(e) => setStress({ ...stress, scenario: e.target.value })} /></Field>
            <Field label="Shock %"><NumberInput value={stress.shock} onChange={(e) => setStress({ ...stress, shock: e.target.value })} /></Field>
            <Field label="Survive above %"><NumberInput value={stress.survival} onChange={(e) => setStress({ ...stress, survival: e.target.value })} /></Field>
          </div>
        )}
        {mode === 'tax' && (
          <div className="grid grid-cols-3 gap-2.5">
            <Field label="Year"><NumberInput value={tax.year} onChange={(e) => setTax({ ...tax, year: e.target.value })} /></Field>
            <Field label="Allowance ₹"><NumberInput value={tax.allowance} onChange={(e) => setTax({ ...tax, allowance: e.target.value })} /></Field>
            <Field label="Rate %" hint="IN: 30% flat"><NumberInput value={tax.rate} onChange={(e) => setTax({ ...tax, rate: e.target.value })} /></Field>
          </div>
        )}
        <Button variant="primary" icon="calculate" disabled={bankroll <= 0} onClick={() => void run()}>Run {mode === 'stress' ? 'stress test' : mode}</Button>
        {result && (
          <div className="rounded-xl bg-stone-50 p-3 text-xs dark:bg-white/[0.03]">
            {'capital_health_score' in result && <p className="mb-2 text-sm font-semibold">Health {String(result.capital_health_score)}/100 · variance <StatusBadge status={String(result.variance_status)} /></p>}
            {Array.isArray(result.suggestions) && <ul className="list-disc space-y-1 pl-4 text-stone-600 dark:text-stone-300">{(result.suggestions as string[]).map((s) => <li key={s}>{s}</li>)}</ul>}
            {'survived' in result && <p className="text-sm">{result.survived ? 'Survives' : 'Breaks'} · simulated P&L {formatINR(Number(result.simulated_pnl))} · drawdown {formatPct(Number(result.simulated_drawdown_pct))}<br /><span className="text-stone-500">{String(result.recommendation)}</span></p>}
            {'estimated_tax' in result && <p className="text-sm">Taxable {formatINR(Number(result.taxable_amount))} · estimated tax <strong>{formatINR(Number(result.estimated_tax))}</strong></p>}
          </div>
        )}
        <div>
          <p className="mb-2 text-xs font-semibold text-stone-400">Alerts</p>
          <Async resource={alerts} isEmpty={(r) => r.length === 0} empty={<p className="text-xs text-stone-400">No CFO alerts.</p>}>
            {(rows) => (
              <ul className="flex max-h-[180px] flex-col gap-1.5 overflow-y-auto">
                {rows.map((a) => (
                  <li key={a.id} className={`flex items-start justify-between gap-2 text-xs ${a.is_read ? 'opacity-50' : ''}`}>
                    <span className="flex items-start gap-1.5"><StatusBadge status={a.level} /><span className="text-stone-600 dark:text-stone-300">{a.message}</span></span>
                    {!a.is_read && <button type="button" className="shrink-0 text-[10px] font-semibold text-[var(--accent-text)]" onClick={() => void runMutation(() => apiClient.patch(`/the-vault/cfo/alerts/${a.id}/read`), { invalidate: ['the-vault'] })}>Mark read</button>}
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </div>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE VAULT
// ---------------------------------------------------------------------------
export const TheVault = () => {
  const summary = useDashboardSummary();
  const bankroll = summary.data?.total_bankroll ?? 0;
  const overview = useResource(bankroll > 0 ? `vault:overview:${Math.round(bankroll)}` : null, () => apiClient.get<Overview>('/vault/overview', { bankroll, currency: 'INR' }), { intervalMs: 30_000 });
  const waterfall = useResource(bankroll > 0 ? `vault:waterfall:${Math.round(bankroll)}` : null, () => apiClient.get<Waterfall[]>('/vault/pnl/waterfall', { bankroll, currency: 'INR' }), { intervalMs: 60_000 });
  const ledger = useResource('ledger:list', () => apiClient.get<LedgerRow[]>('/ledger', { limit: 100 }), { intervalMs: 30_000 });
  const o = overview.data;

  return (
    <Page>
      <CommanderHero
        commander="KUMBHA"
        headline={o ? `KUMBHA ACTIVE. ${formatINR(o.available_capital)} available, ${formatINR(o.total_exposure)} deployed, ROI ${formatPct(o.roi_pct)}.` : 'KUMBHA ACTIVE. Reconciling the book…'}
        motif={MOTIFS.vault}
        detail={summary.data && bankroll <= 0 ? 'Your risk mandate has no bankroll yet, so capital views are paused.' : 'Every figure below is computed from the ledger in real time; nothing is cached client-side.'}
        stats={o && (
          <div className="grid grid-cols-2 gap-x-8 gap-y-4 pt-1 sm:grid-cols-4">
            <Stat label="Net P&L" value={<AnimatedNumber value={o.net_pnl} format={formatSignedINR} />} tone={o.net_pnl >= 0 ? 'positive' : 'negative'} />
            <Stat label="Volume" value={<AnimatedNumber value={o.total_volume} format={formatINRCompact} />} />
            <Stat label="Open / settled" value={`${o.active_bets_count} / ${o.settled_bets_count}`} />
            <Stat label="Gross win / loss" value={<span className="text-base">{formatINRCompact(o.gross_profit)} / {formatINRCompact(o.gross_loss)}</span>} />
          </div>
        )}
        actions={
          <>
            <Button
              variant="primary"
              icon="download"
              disabled={!ledger.data?.length}
              onClick={() => downloadCsv(`betdoc-ledger-${new Date().toISOString().slice(0, 10)}.csv`, (ledger.data ?? []) as unknown as Record<string, unknown>[])}
            >
              Export ledger
            </Button>
            <Button icon="refresh" onClick={() => { void overview.refresh(); void ledger.refresh(); }}>Reconcile now</Button>
          </>
        }
      />
      <GrowthPanel bankroll={bankroll} />
      <Panel title="P&L waterfall" icon="waterfall_chart" className="lg:col-span-4" updatedAt={waterfall.updatedAt}>
        <Async resource={waterfall} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="waterfall_chart" title="No movements yet" />}>
          {(rows) => <DivergingBars caption="Bankroll waterfall" formatValue={formatINR} items={rows.map((r) => ({ label: r.category, value: r.value }))} />}
        </Async>
      </Panel>
      <PnLBreakdown />
      <RiskPanel bankroll={bankroll} />
      <StopLossPanel />
      <CfoPanel overview={o} />
      <LedgerPanel ledger={ledger} />
    </Page>
  );
};
