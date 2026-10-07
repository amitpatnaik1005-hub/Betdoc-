import { motion } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { apiClient } from '../api/client';
import MarketBoard from '../components/MarketBoard';
import { BotAvatar } from '../components/bots/BotAvatar';
import { emergencyStop, resumeTrading, useControls, useDashboardSummary } from '../lib/api';
import { avatarStatus, useCommanders } from '../lib/commanders';
import { formatAgo, formatINR, formatInt, formatPct, formatSignedINR, formatTime, humanize } from '../lib/format';
import { invalidate, useResource } from '../lib/resource';
import { useExecutionStore, isSelection } from '../store/useExecutionStore';
import { useSystemStore } from '../store/useSystemStore';
import { toast } from '../store/useToastStore';
import { useUIStore } from '../store/useUIStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { AnimatedNumber, Async, Button, CARD_VARIANTS, ConfirmButton, EmptyState, Page, Panel, Pill, Stat, StatGrid, StatusBadge } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface EngineStatus {
  cpu_usage_pct: number;
  memory_usage_mb: number;
  queue_depth: number;
  active_models_count: number;
  engine_state: 'ONLINE' | 'DEGRADED' | 'SATURATED';
  total_smallcases: number;
  active_smallcases: number;
  recorded_at: string;
}

interface SubsystemStatus {
  name: 'vault' | 'oracle' | 'arena' | 'lab';
  status: 'ok' | 'error';
  latency_ms: number | null;
  last_error: string | null;
}

interface Telemetry {
  generated_at: string;
  overall_status: 'ok' | 'degraded' | 'down';
  realized_pnl: number | null;
  subsystems: SubsystemStatus[];
}

interface ActivityEvent {
  event_type: string;
  timestamp: string;
  message: string;
  metadata: Record<string, unknown>;
}

interface Tip {
  tip_id: string;
  match_ids: string[];
  market_types: string[];
  selections: string[];
  recommended_structure: 'SINGLE' | 'PARLAY';
  is_parlay: boolean;
  confidence_score_pct: number;
  rationale: string;
  kelly_stake_pct: number;
}

// Routable sections by commander, for roster click-through.
const COMMANDER_ROUTE: Record<string, string> = {
  KAUTILYA: '/command-center', ASHOKA: '/oracle', BAJIRAO: '/arena', VIDUR: '/hive', KUMBHA: '/vault',
  PANINI: '/lab', PRATAP: '/core', GARUDA: '/phantom', 'TODAR MAL': '/archive', CHANAKYA: '/vault',
  DRONA: '/lab', ARYABHATA: '/core', KARNA: '/hive', ARJUNA: '/arena', SHIVAJI: '/control-panel',
  BHEESHMA: '/control-panel', DEVRAYA: '/command-center',
};

/** First numeric/text metric from a commander heartbeat, for the roster subtitle. */
function headlineMetric(metrics: Record<string, unknown> | undefined): string | null {
  if (!metrics) return null;
  for (const [key, value] of Object.entries(metrics)) {
    if (key === 'probed_at' || key === 'error') continue;
    if (typeof value === 'number' || typeof value === 'string' || typeof value === 'boolean') return `${humanize(key)}: ${String(value)}`;
  }
  return typeof metrics.error === 'string' ? metrics.error : null;
}

// ---------------------------------------------------------------------------
// PANELS
// ---------------------------------------------------------------------------
const TelemetryMatrix = () => {
  const engine = useResource('core:status', () => apiClient.get<EngineStatus>('/core/status'), { intervalMs: 15_000 });
  const telemetry = useResource('telemetry:dashboard', () => apiClient.get<Telemetry>('/telemetry/dashboard'), { intervalMs: 20_000 });
  const summary = useDashboardSummary();

  return (
    <Panel title="Live telemetry matrix" icon="sensors" className="lg:col-span-8" updatedAt={engine.updatedAt}>
      <Async resource={engine} skeletonRows={3}>
        {(e) => (
          <div className="flex flex-col gap-5">
            <StatGrid cols={4}>
              <Stat label="Engine" icon="memory" value={<StatusBadge status={e.engine_state} />} hint={`queue ${formatInt(e.queue_depth)}`} />
              <Stat label="Active smallcases" icon="hub" value={<AnimatedNumber value={e.active_smallcases} format={formatInt} />} hint={`${formatInt(e.total_smallcases)} registered`} />
              <Stat label="API process CPU" icon="speed" value={<AnimatedNumber value={e.cpu_usage_pct} format={(n) => formatPct(n)} />} hint={`${formatInt(e.memory_usage_mb)} MB resident`} tone={e.cpu_usage_pct > 75 ? 'caution' : 'neutral'} />
              <Stat
                label="Win rate"
                icon="target"
                value={summary.data ? <AnimatedNumber value={summary.data.win_rate_pct} format={(n) => formatPct(n)} /> : '—'}
                hint={summary.data ? `${summary.data.active_bets_count} open · stop-loss ${summary.data.stop_loss_status.toLowerCase()}` : undefined}
              />
            </StatGrid>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {(telemetry.data?.subsystems ?? []).map((s) => (
                <div key={s.name} className="flex items-center justify-between gap-2 rounded-xl bg-slate-50 px-3 py-2 dark:bg-white/[0.03]" title={s.last_error ?? undefined}>
                  <span className="text-xs font-semibold capitalize text-slate-700 dark:text-slate-200">{s.name}</span>
                  <span className="flex items-center gap-1.5">
                    {s.latency_ms !== null && <span className="text-[10px] tabular-nums text-slate-400">{s.latency_ms.toFixed(0)}ms</span>}
                    <StatusBadge status={s.status === 'ok' ? 'ONLINE' : 'ERROR'} label={s.status} />
                  </span>
                </div>
              ))}
              {telemetry.error && <p className="col-span-full text-xs text-rose-600">{telemetry.error}</p>}
            </div>
          </div>
        )}
      </Async>
    </Panel>
  );
};

const CommanderRoster = () => {
  const roster = useCommanders();
  const navigate = useNavigate();
  const live = roster.commanders.filter((c) => c.status !== 'NO SIGNAL').length;

  return (
    <Panel title="Commander roster" icon="groups" className="lg:col-span-4" subtitle={`${live}/17 reporting`} updatedAt={roster.updatedAt}>
      <Async resource={roster} skeletonRows={5}>
        {() => (
          <ul className="-mx-2 flex max-h-[360px] flex-col overflow-y-auto">
            {roster.commanders.map(({ profile, heartbeat, status }) => (
              <li key={profile.id}>
                <button
                  type="button"
                  onClick={() => navigate(COMMANDER_ROUTE[profile.id] ?? '/command-center')}
                  className="flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-slate-900/[0.03] dark:hover:bg-white/[0.04]"
                >
                  <BotAvatar botName={profile.name} status={avatarStatus(status)} size="sm" customHexColor={profile.theme.primary} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-2">
                      <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">{profile.name}</span>
                      <span className="truncate text-[10px] text-slate-400">{profile.domain}</span>
                    </span>
                    <span className="block truncate text-[11px] text-slate-500 dark:text-slate-400">
                      {headlineMetric(heartbeat?.resource_metrics) ?? 'Awaiting first heartbeat'}
                    </span>
                  </span>
                  <span className="flex flex-col items-end gap-0.5">
                    <StatusBadge status={status === 'NO SIGNAL' ? 'OFFLINE' : status} label={status} />
                    {heartbeat && <span className="text-[10px] text-slate-400">{formatAgo(heartbeat.last_ping_at)}</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

const ActivityStream = () => {
  const feed = useResource('dashboard:activity', () => apiClient.get<ActivityEvent[]>('/dashboard/activity-feed', { limit: 25 }), { intervalMs: 30_000 });
  const busEvents = useSystemStore((s) => s.events);
  const mutations = busEvents.filter((e) => e.type === 'mutation').slice(0, 8);

  return (
    <Panel title="Activity stream" icon="timeline" className="lg:col-span-7" updatedAt={feed.updatedAt}>
      <div className="flex flex-col gap-4">
        {mutations.length > 0 && (
          <div>
            <p className="mb-2 text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400">Live across sections</p>
            <ul className="flex flex-wrap gap-1.5">
              {mutations.map((e, i) => (
                <motion.li key={`${e.at}-${i}`} initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }}>
                  <Pill tone="accent" icon="bolt">
                    {e.section} · {e.method} · {formatTime(e.at)}
                  </Pill>
                </motion.li>
              ))}
            </ul>
          </div>
        )}
        <Async
          resource={feed}
          isEmpty={(rows) => rows.length === 0}
          empty={<EmptyState icon="history" title="No activity yet" detail="Orders, settlements, stop-loss events and steam alerts appear here as they happen." />}
        >
          {(rows) => (
            <ol className="relative flex flex-col gap-3 border-l border-slate-900/[0.08] pl-4 dark:border-white/[0.08]">
              {rows.map((ev, i) => (
                <li key={`${ev.timestamp}-${i}`} className="relative">
                  <span className="absolute -left-[21px] top-1.5 size-2 rounded-full bg-[var(--accent)] ring-4 ring-white dark:ring-[#161514]" />
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={ev.event_type.startsWith('STOP_LOSS') ? 'WARNING' : ev.event_type === 'STEAM_ALERT' ? 'INFO' : 'ONLINE'} label={humanize(ev.event_type)} />
                    <span className="text-[11px] text-slate-400">{formatAgo(ev.timestamp)}</span>
                  </div>
                  <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">{ev.message}</p>
                </li>
              ))}
            </ol>
          )}
        </Async>
      </div>
    </Panel>
  );
};

const TipMaster = () => {
  const tips = useResource('dashboard:tips', () => apiClient.get<Tip[]>('/dashboard/tips'), { intervalMs: 60_000 });
  const updateDraftField = useExecutionStore((s) => s.updateDraftField);
  const openRight = useUIStore((s) => s.openRight);

  const loadSingle = (tip: Tip) => {
    const selection = tip.selections[0]?.toUpperCase() ?? '';
    updateDraftField('draftMatchId', tip.match_ids[0] ?? '');
    if (isSelection(selection)) updateDraftField('draftSelection', selection);
    openRight();
    toast.info('Tip loaded into the terminal', 'Enter the current price, then execute.');
  };

  return (
    <Panel title="Tip master" icon="tips_and_updates" className="lg:col-span-5" updatedAt={tips.updatedAt}>
      <Async
        resource={tips}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon="lightbulb" title="No tips right now" detail="Tips appear when the engine finds an edge in recent market ticks." />}
      >
        {(rows) => (
          <ul className="flex flex-col gap-3">
            {rows.slice(0, 6).map((tip) => (
              <li key={tip.tip_id} className="rounded-xl bg-slate-50 p-3 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <Pill tone={tip.is_parlay ? 'info' : 'accent'}>{tip.recommended_structure}</Pill>
                  <span className="text-xs tabular-nums text-slate-500">
                    {formatPct(tip.confidence_score_pct, 0)} confidence · Kelly {formatPct(tip.kelly_stake_pct, 2)}
                  </span>
                </div>
                <p className="mt-2 text-sm font-medium text-slate-800 dark:text-slate-100">
                  {tip.selections.join(' + ')} <span className="text-slate-400">·</span> <span className="font-mono text-xs">{tip.match_ids.join(', ')}</span>
                </p>
                <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">{tip.rationale}</p>
                {!tip.is_parlay && (
                  <Button size="sm" variant="ghost" icon="receipt_long" className="mt-2 -ml-2" onClick={() => loadSingle(tip)}>
                    Load into betslip
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: COMMAND CENTER
// ---------------------------------------------------------------------------
export const CommandCenter = () => {
  const summary = useDashboardSummary();
  const engine = useResource('core:status', () => apiClient.get<EngineStatus>('/core/status'), { intervalMs: 15_000 });
  const halted = useSystemStore((s) => s.halted);
  const busStatus = useSystemStore((s) => s.busStatus);
  const controls = useControls();
  const navigate = useNavigate();
  const openRight = useUIStore((s) => s.openRight);

  const s = summary.data;
  const headline = halted
    ? 'KAUTILYA holding the line. Emergency stop engaged; every order is refused.'
    : s && engine.data
      ? `KAUTILYA ACTIVE. ${engine.data.active_smallcases} smallcases live, ${s.active_bets_count} open positions, ${formatPct(s.win_rate_pct)} win rate.`
      : 'KAUTILYA ACTIVE. Synchronising the desk…';

  return (
    <Page>
      <CommanderHero
        commander="KAUTILYA"
        headline={headline}
        motif={MOTIFS.rings}
        detail={
          s ? (
            <>
              Bankroll <strong className="text-slate-800 dark:text-slate-100">{formatINR(s.total_bankroll)}</strong>, exposure{' '}
              <strong className="text-slate-800 dark:text-slate-100">{formatINR(s.current_exposure)}</strong>, today{' '}
              <strong className={s.daily_pnl >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>{formatSignedINR(s.daily_pnl)}</strong>. Event bus{' '}
              {busStatus === 'open' ? 'live' : busStatus}.
            </>
          ) : undefined
        }
        actions={
          <>
            <Button variant="primary" icon="auto_awesome" onClick={openRight}>Ask the Scout</Button>
            <Button
              icon="monitor_heart"
              onClick={() => {
                invalidate('core', 'telemetry', 'commanders', 'dashboard');
                toast.info('Diagnostics refreshed', 'Engine, subsystems and commanders re-probed.');
              }}
            >
              Run diagnostics
            </Button>
            <Button icon="settings" onClick={() => navigate('/control-panel')}>System config</Button>
            {halted ? (
              <Button variant="danger" icon="play_circle" onClick={() => void resumeTrading(500)}>Resume trading</Button>
            ) : (
              <ConfirmButton variant="danger" icon="front_hand" confirmLabel="Confirm halt?" onConfirm={() => void emergencyStop()} disabled={!controls.data}>
                Emergency stop
              </ConfirmButton>
            )}
          </>
        }
      />
      <TelemetryMatrix />
      <CommanderRoster />
      <ActivityStream />
      <TipMaster />
      <motion.div variants={CARD_VARIANTS} className="lg:col-span-12">
        <MarketBoard />
      </motion.div>
    </Page>
  );
};
