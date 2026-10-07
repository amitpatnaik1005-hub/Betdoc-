import { useEffect, useState, type FormEvent } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { apiClient } from '../api/client';
import { CoreFactoryScene } from '../components/bots/CoreFactoryScene';
import { emergencyStop, resumeTrading, useControls } from '../lib/api';
import { formatAgo, formatINR, formatInt, formatPct, formatRatioPct, formatTime, humanize } from '../lib/format';
import { invalidate, runMutation, useResource } from '../lib/resource';
import { subscribeChannel } from '../services/realtime';
import { useSystemStore } from '../store/useSystemStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { AnimatedNumber, Async, Button, ConfirmButton, type Column, DataTable, EmptyState, Field, KeyValues, Meter, NumberInput, Page, Panel, Pill, Stat, StatGrid, StatusBadge, TextInput, num } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface EngineStatus { cpu_usage_pct: number; memory_usage_mb: number; queue_depth: number; active_models_count: number; engine_state: string; total_smallcases: number; active_smallcases: number; recorded_at: string }
interface Smallcase { id: string; name: string; description: string; status: 'ACTIVE' | 'STANDBY' | 'DISABLED'; current_accuracy: number; cross_val_score: number; total_backtests_run: number; last_tested_at: string | null }
interface SmallcaseDetail extends Smallcase { pipeline_stages: { index: number; model_name: string; stage_kind: string; execution_mode: 'NATIVE' | 'SIMULATED' }[] }
interface TestRun { id: string; smallcase_id: string; pipeline_execution_steps: Record<string, unknown>[]; predicted_outcome: Record<string, unknown> | null; status: string; execution_time_ms: number | null; error_detail: string | null }
interface LiveEnvelope { event: string; source?: string; timestamp?: string; payload?: Record<string, unknown> }
interface DeepHealth { status: string; environment: string; dependencies: Record<string, string>; response_time_ms: number }

// ---------------------------------------------------------------------------
// SMALLCASE REGISTRY
// ---------------------------------------------------------------------------
const SmallcaseRegistry = ({ smallcases, onStamp }: { smallcases: ReturnType<typeof useResource<Smallcase[]>>; onStamp: (on: boolean) => void }) => {
  const [openId, setOpenId] = useState<string | null>(null);
  const detail = useResource(openId ? `core:smallcase:${openId}` : null, () => apiClient.get<SmallcaseDetail>(`/core/smallcases/${openId}`));

  const toggle = (s: Smallcase) => {
    const target = s.status === 'ACTIVE' ? 'STANDBY' : 'ACTIVE';
    void runMutation(() => apiClient.post(`/core/smallcases/${s.id}/toggle`, { expected_status: s.status, target_status: target }), {
      invalidate: ['core', 'commanders'], success: `${s.name} → ${target}`, errorTitle: 'Toggle refused',
    });
  };
  const stress = async (s: Smallcase) => {
    onStamp(true);
    await runMutation(() => apiClient.post<TestRun>(`/core/smallcases/${s.id}/stress-test`), {
      invalidate: ['core'], success: (r) => `${s.name} stress test ${r.status.toLowerCase()} in ${r.execution_time_ms?.toFixed(0) ?? '—'}ms`, errorTitle: 'Stress test failed',
    });
    onStamp(false);
  };

  const columns: Column<Smallcase>[] = [
    { key: 'name', header: 'Smallcase', render: (s) => <button type="button" className="text-left" onClick={() => setOpenId(openId === s.id ? null : s.id)}><span className="font-medium text-slate-800 underline-offset-2 hover:underline dark:text-slate-100">{s.name}</span><span className="block max-w-[280px] truncate text-[11px] text-slate-400">{s.description}</span></button> },
    { key: 'acc', header: 'Accuracy', align: 'right', render: (s) => formatRatioPct(s.current_accuracy) },
    { key: 'cv', header: 'Cross-val', align: 'right', render: (s) => formatRatioPct(s.cross_val_score) },
    { key: 'bt', header: 'Backtests', align: 'right', render: (s) => formatInt(s.total_backtests_run) },
    { key: 'status', header: 'Status', render: (s) => <StatusBadge status={s.status} /> },
    { key: 'act', header: '', align: 'right', render: (s) => (
      <div className="flex justify-end gap-1.5">
        <Button size="sm" variant="ghost" icon={s.status === 'ACTIVE' ? 'pause' : 'play_arrow'} disabled={s.status === 'DISABLED'} onClick={() => toggle(s)}>{s.status === 'ACTIVE' ? 'Standby' : 'Activate'}</Button>
        <Button size="sm" variant="ghost" icon="bolt" onClick={() => void stress(s)}>Stress</Button>
      </div>
    ) },
  ];

  return (
    <Panel title="Smallcase registry" icon="hub" className="lg:col-span-8" updatedAt={smallcases.updatedAt}>
      <Async resource={smallcases} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="hub" title="Engine not bootstrapped" detail="Bootstrap the engine to register the default model pipelines." />}>
        {(rows) => (
          <div className="flex flex-col gap-4">
            <DataTable columns={columns} rows={rows} rowKey={(s) => s.id} dense />
            <AnimatePresence>
              {openId && detail.data && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
                  <p className="mb-2 mt-4 text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400">{detail.data.name} · pipeline</p>
                  <ol className="flex flex-wrap items-center gap-2">
                    {detail.data.pipeline_stages.map((st, i) => (
                      <li key={st.index} className="flex items-center gap-2">
                        <span className="rounded-xl bg-slate-50 px-3 py-2 text-xs dark:bg-white/[0.04]">
                          <span className="font-semibold text-slate-800 dark:text-slate-100">{st.model_name}</span>
                          <span className="block text-[10px] text-slate-400">{humanize(st.stage_kind)} · <span className={st.execution_mode === 'NATIVE' ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'}>{st.execution_mode}</span></span>
                        </span>
                        {i < detail.data!.pipeline_stages.length - 1 && <span className="material-symbols-outlined text-[16px] text-slate-300">arrow_forward</span>}
                      </li>
                    ))}
                  </ol>
                  <p className="mt-2 text-[11px] text-slate-400">NATIVE stages run the real math model; SIMULATED stages are deterministic stand-ins seeded per run.</p>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// TEST BENCH
// ---------------------------------------------------------------------------
const TestBench = ({ smallcases, onStamp }: { smallcases: Smallcase[]; onStamp: (on: boolean) => void }) => {
  const [ctx, setCtx] = useState({ home_team: 'Arsenal', away_team: 'Chelsea', home_xg: '1.7', away_xg: '1.1', home: '2.05', draw: '3.6', away: '3.9' });
  const [runs, setRuns] = useState<TestRun[] | null>(null);
  const [busy, setBusy] = useState(false);
  const names = new Map(smallcases.map((s) => [s.id, s.name]));
  const active = smallcases.filter((s) => s.status === 'ACTIVE').slice(0, 5);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    if (active.length === 0) return;
    setBusy(true);
    onStamp(true);
    const res = await runMutation(() => apiClient.post<TestRun[]>('/core/test-compare', {
      smallcase_ids: active.map((s) => s.id),
      match_context: { home_team: ctx.home_team, away_team: ctx.away_team, home_xg: num(ctx.home_xg), away_xg: num(ctx.away_xg), odds: { home: num(ctx.home), draw: num(ctx.draw), away: num(ctx.away) } },
    }), { invalidate: ['core'], errorTitle: 'Test bench failed' });
    onStamp(false);
    setBusy(false);
    if (res) setRuns(res);
  };

  return (
    <Panel title="Test bench" icon="biotech" className="lg:col-span-7" subtitle={`compares ${active.length} active smallcase${active.length === 1 ? '' : 's'}`}>
      <form onSubmit={run} className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        <Field label="Home"><TextInput value={ctx.home_team} onChange={(e) => setCtx({ ...ctx, home_team: e.target.value })} /></Field>
        <Field label="Away"><TextInput value={ctx.away_team} onChange={(e) => setCtx({ ...ctx, away_team: e.target.value })} /></Field>
        <Field label="Home xG"><NumberInput value={ctx.home_xg} onChange={(e) => setCtx({ ...ctx, home_xg: e.target.value })} /></Field>
        <Field label="Away xG"><NumberInput value={ctx.away_xg} onChange={(e) => setCtx({ ...ctx, away_xg: e.target.value })} /></Field>
        <Field label="Home odds"><NumberInput value={ctx.home} onChange={(e) => setCtx({ ...ctx, home: e.target.value })} /></Field>
        <Field label="Draw odds"><NumberInput value={ctx.draw} onChange={(e) => setCtx({ ...ctx, draw: e.target.value })} /></Field>
        <Field label="Away odds"><NumberInput value={ctx.away} onChange={(e) => setCtx({ ...ctx, away: e.target.value })} /></Field>
        <div className="flex items-end"><Button type="submit" variant="primary" icon="play_arrow" busy={busy} disabled={active.length === 0} className="w-full">Compare</Button></div>
      </form>
      {active.length === 0 && <p className="mt-3 text-xs text-amber-600">Activate at least one smallcase to use the bench.</p>}
      {runs && (
        <ul className="mt-4 flex flex-col gap-2">
          {runs.map((r) => {
            const out = r.predicted_outcome ?? {};
            const probs = (out.probabilities ?? out.probs ?? null) as Record<string, number> | null;
            return (
              <li key={r.id} className="rounded-xl bg-slate-50 p-3 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">{names.get(r.smallcase_id) ?? r.smallcase_id.slice(0, 8)}</span>
                  <span className="flex items-center gap-2"><span className="text-[11px] tabular-nums text-slate-400">{r.execution_time_ms?.toFixed(1)}ms · {r.pipeline_execution_steps.length} stages</span><StatusBadge status={r.status} /></span>
                </div>
                {probs && (
                  <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                    {Object.entries(probs).map(([k, v]) => <span key={k} className="tabular-nums text-slate-600 dark:text-slate-300">{humanize(k)} <strong>{formatRatioPct(Number(v))}</strong></span>)}
                  </div>
                )}
                {!probs && Object.keys(out).length > 0 && <p className="mt-1 truncate font-mono text-[11px] text-slate-500">{JSON.stringify(out).slice(0, 160)}</p>}
                {r.error_detail && <p className="mt-1 text-xs text-rose-600">{r.error_detail}</p>}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: CORE
// ---------------------------------------------------------------------------
export const Core = () => {
  const navigate = useNavigate();
  const engine = useResource('core:status', () => apiClient.get<EngineStatus>('/core/status'), { intervalMs: 15_000 });
  const smallcases = useResource('core:smallcases', () => apiClient.get<Smallcase[]>('/core/smallcases'), { intervalMs: 60_000 });
  const health = useResource('core:deep-health', () => apiClient.get<DeepHealth>('/health/'), { intervalMs: 30_000 });
  const controls = useControls();
  const halted = useSystemStore((s) => s.halted);
  const [events, setEvents] = useState<(LiveEnvelope & { at: string })[]>([]);
  const [stamping, setStamping] = useState(false);
  const [bootBusy, setBootBusy] = useState(false);

  // Engine Room conveyor belt: test bench + backtest progress pushed live.
  useEffect(
    () =>
      subscribeChannel('/core/live', (data) => {
        const env = data as LiveEnvelope;
        if (!env || typeof env.event !== 'string' || env.event === 'engine.pong') return;
        setEvents((prev) => [{ ...env, at: env.timestamp ?? new Date().toISOString() }, ...prev].slice(0, 30));
        if (env.event.startsWith('backtest') || env.event.startsWith('test')) invalidate('core');
      }, 'event'),
    [],
  );

  const e = engine.data;
  const c = controls.data;
  const ticketCount = (smallcases.data ?? []).reduce((acc, s) => acc + s.total_backtests_run, 0);

  return (
    <Page>
      <CommanderHero
        commander="PRATAP"
        headline={e ? `PRATAP ${e.engine_state}. ${e.active_smallcases}/${e.total_smallcases} smallcases live, queue ${e.queue_depth}, ${formatPct(e.cpu_usage_pct)} CPU.` : 'PRATAP synchronising the engine room…'}
        motif={MOTIFS.circuit}
        detail={halted ? 'All markets are locked by the emergency stop.' : 'Hard limits from the Control Panel are enforced on every order before it reaches an exchange.'}
        scene={<CoreFactoryScene isStamping={stamping} ticketsGenerated={ticketCount} className="h-[220px]" />}
        actions={
          <>
            <Button variant="primary" icon="rocket_launch" busy={bootBusy} onClick={async () => {
              setBootBusy(true);
              await runMutation(() => apiClient.post<Smallcase[]>('/core/bootstrap'), { invalidate: ['core', 'commanders'], success: (r) => `Engine bootstrapped: ${r.length} smallcases registered`, errorTitle: 'Bootstrap failed' });
              setBootBusy(false);
            }}>Bootstrap engine</Button>
            {halted ? (
              <Button variant="danger" icon="lock_open" onClick={() => void resumeTrading(500)}>Unlock markets</Button>
            ) : (
              <ConfirmButton variant="danger" icon="lock" confirmLabel="Lock everything?" onConfirm={() => void emergencyStop()}>Lock all markets</ConfirmButton>
            )}
            <Button icon="monitor_heart" onClick={() => { void health.refresh(); void engine.refresh(); }}>Deep health check</Button>
          </>
        }
      />

      <Panel title="Subsystem diagnostics" icon="developer_board" className="lg:col-span-8" updatedAt={engine.updatedAt}>
        <Async resource={engine}>
          {(s) => (
            <div className="flex flex-col gap-5">
              <StatGrid cols={4}>
                <Stat label="Engine" value={<StatusBadge status={s.engine_state} />} hint={`sampled ${formatAgo(s.recorded_at)}`} />
                <Stat label="Process CPU" value={<AnimatedNumber value={s.cpu_usage_pct} format={(n) => formatPct(n)} />} hint="measured (psutil)" tone={s.cpu_usage_pct > 75 ? 'caution' : 'neutral'} />
                <Stat label="Resident memory" value={<AnimatedNumber value={s.memory_usage_mb} format={(n) => `${formatInt(n)} MB`} />} />
                <Stat label="Active models" value={formatInt(s.active_models_count)} hint={`queue depth ${s.queue_depth}`} />
              </StatGrid>
              <Meter value={s.cpu_usage_pct / 100} tone={s.cpu_usage_pct > 90 ? 'critical' : s.cpu_usage_pct > 75 ? 'warning' : 'good'} label="CPU load" />
              {health.data && (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries(health.data.dependencies).map(([dep, st]) => (
                    <div key={dep} className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 dark:bg-white/[0.03]">
                      <span className="text-xs font-semibold capitalize text-slate-700 dark:text-slate-200">{dep}</span>
                      <StatusBadge status={st === 'ok' ? 'ONLINE' : st === 'uninitialized' ? 'IDLE' : 'ERROR'} label={st} />
                    </div>
                  ))}
                  <div className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 dark:bg-white/[0.03]">
                    <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">Round trip</span>
                    <span className="text-xs tabular-nums text-slate-500">{health.data.response_time_ms}ms</span>
                  </div>
                  <div className="flex items-center justify-between rounded-xl bg-slate-50 px-3 py-2 dark:bg-white/[0.03]">
                    <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">Env</span>
                    <Pill tone="neutral">{health.data.environment}</Pill>
                  </div>
                </div>
              )}
            </div>
          )}
        </Async>
      </Panel>

      <Panel title="Hard risk limits" icon="shield" className="lg:col-span-4" updatedAt={controls.updatedAt} actions={<Button size="sm" icon="tune" onClick={() => navigate('/control-panel')}>Adjust</Button>}>
        <Async resource={controls}>
          {() => c && (
            <div className="flex flex-col gap-3">
              <StatusBadge status={halted ? 'CRITICAL' : 'ONLINE'} label={halted ? 'Locked' : 'Enforced'} />
              <KeyValues items={[
                { label: 'Max bet size', value: formatINR(c.max_bet_size) },
                { label: 'Max daily exposure', value: formatINR(c.max_daily_exposure) },
                { label: 'Global stop-loss', value: formatINR(c.global_stop_loss) },
                { label: 'Default Kelly', value: `×${c.default_kelly_fraction}` },
                { label: 'Bots', value: c.bots_enabled ? 'enabled' : 'disabled' },
                { label: 'Last emergency stop', value: c.last_emergency_stop_at ? formatAgo(c.last_emergency_stop_at) : 'never' },
              ]} />
            </div>
          )}
        </Async>
      </Panel>

      <SmallcaseRegistry smallcases={smallcases} onStamp={setStamping} />

      <Panel title="Engine room · live" icon="conveyor_belt" className="lg:col-span-4" bodyClassName="p-0">
        {events.length === 0 ? (
          <EmptyState icon="conveyor_belt" title="Conveyor idle" detail="Test bench runs and backtest progress stream here." />
        ) : (
          <ul className="max-h-[360px] divide-y divide-slate-900/[0.05] overflow-y-auto dark:divide-white/[0.05]">
            {events.map((ev, i) => (
              <li key={`${ev.at}-${i}`} className="flex items-center justify-between gap-2 px-4 py-2 text-xs">
                <span className="truncate text-slate-700 dark:text-slate-200">{ev.event}{ev.payload && 'progress_pct' in ev.payload ? ` · ${Number(ev.payload.progress_pct).toFixed(0)}%` : ''}</span>
                <span className="shrink-0 font-mono text-[10px] text-slate-400">{formatTime(ev.at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <TestBench smallcases={smallcases.data ?? []} onStamp={setStamping} />
      <Panel title="Model accuracy" icon="insights" className="lg:col-span-5" updatedAt={smallcases.updatedAt}>
        <Async resource={smallcases} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="insights" title="No models yet" />}>
          {(rows) => (
            <ul className="flex flex-col gap-3">
              {[...rows].sort((a, b) => b.cross_val_score - a.cross_val_score).slice(0, 8).map((s) => (
                <li key={s.id}>
                  <div className="mb-1 flex justify-between text-xs"><span className="text-slate-700 dark:text-slate-200">{s.name}</span><span className="tabular-nums text-slate-500">CV {formatRatioPct(s.cross_val_score)}</span></div>
                  <Meter value={s.cross_val_score} label={`${s.name} cross-validation`} />
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>
    </Page>
  );
};
