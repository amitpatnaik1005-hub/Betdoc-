import { motion } from 'framer-motion';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiClient } from '../api/client';
import MarketBoard from '../components/MarketBoard';
import { BotAvatar } from '../components/bots/BotAvatar';
import { emergencyStop, resumeTrading, useControls, useDashboardSummary } from '../lib/api';
import { avatarStatus, formatProbeLatency, useCommanders } from '../lib/commanders';
import { formatAgo, formatINR, formatInt, formatPct, formatSignedINR, formatTime, humanize } from '../lib/format';
import { invalidate, useResource } from '../lib/resource';
import { TIER_STYLE } from '../lib/backtest_and_parlay';
import { useVerdicts, VERDICT_STYLE } from '../lib/consultant';
import { useSystemStore } from '../store/useSystemStore';
import { toast } from '../store/useToastStore';
import { useUIStore } from '../store/useUIStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { AnimatedNumber, Async, Button, CARD_VARIANTS, ConfirmButton, EmptyState, Page, Panel, Pill, Stat, StatGrid, StatusBadge, type Tone } from '../ui/kit';

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

// Routable sections by commander, for roster click-through.
const COMMANDER_ROUTE: Record<string, string> = {
  KAUTILYA: '/command-center', ASHOKA: '/oracle', BAJIRAO: '/arena', VIDUR: '/hive', KUMBHA: '/vault',
  PANINI: '/lab', PRATAP: '/core', 'TODAR MAL': '/archive', CHANAKYA: '/vault',
  DRONA: '/lab', KARNA: '/hive', SHIVAJI: '/control-panel',
  BHEESHMA: '/control-panel', ASHVATTHAMA: '/consultant',
};

/** First numeric/text metric from a commander heartbeat, for the roster subtitle. */
function headlineMetric(metrics: Record<string, unknown> | undefined): string | null {
  if (!metrics) return null;
  for (const [key, value] of Object.entries(metrics)) {
    if (key === 'probed_at' || key === 'error' || key === 'probe_latency_ms') continue;
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
                <div key={s.name} className="flex items-center justify-between gap-2 rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]" title={s.last_error ?? undefined}>
                  <span className="text-xs font-semibold capitalize text-stone-700 dark:text-stone-200">{s.name}</span>
                  <span className="flex items-center gap-1.5">
                    {s.latency_ms !== null && <span className="text-[10px] tabular-nums text-stone-400">{s.latency_ms.toFixed(0)}ms</span>}
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
  const [probing, setProbing] = useState(false);
  const count = (...states: string[]) => roster.commanders.filter((c) => states.includes(c.status)).length;
  const live = roster.commanders.length - count('NO SIGNAL');
  const chips: { label: string; n: number; tone: Tone }[] = [
    { label: 'Working', n: count('WORKING'), tone: 'info' },
    { label: 'Online', n: count('ONLINE'), tone: 'good' },
    { label: 'Degraded', n: count('DEGRADED', 'FATAL'), tone: 'warning' },
  ];
  const probeAll = async () => {
    setProbing(true);
    try {
      await roster.probeNow();
    } finally {
      setProbing(false);
    }
  };

  return (
    <Panel
      title="Commander roster"
      icon="groups"
      className="lg:col-span-4"
      subtitle={`${live}/${roster.commanders.length} reporting`}
      updatedAt={roster.updatedAt}
      actions={
        <Button size="sm" variant="ghost" icon="sensors" busy={probing} onClick={() => void probeAll()} title="Run every commander's health probe now">
          {probing ? 'Probing…' : 'Probe all'}
        </Button>
      }
    >
      <Async resource={roster} skeletonRows={5}>
        {() => (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-1.5">
              {chips.map(({ label, n, tone }) => (
                <Pill key={label} tone={n > 0 ? tone : 'neutral'}>
                  <span className="tabular-nums">{n}</span> {label}
                </Pill>
              ))}
            </div>
            <ul className="-mx-2 flex max-h-[360px] flex-col overflow-y-auto">
              {roster.commanders.map(({ profile, heartbeat, status, activitySummary, probeLatencyMs }) => (
                <li key={profile.id}>
                  <button
                    type="button"
                    onClick={() => navigate(COMMANDER_ROUTE[profile.id] ?? '/command-center')}
                    className="flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-stone-900/[0.03] dark:hover:bg-white/[0.04]"
                  >
                    <BotAvatar botName={profile.name} status={avatarStatus(status)} size="sm" customHexColor={profile.theme.primary} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline gap-2">
                        <span className="text-sm font-semibold text-stone-800 dark:text-stone-100">{profile.name}</span>
                        <span className="truncate text-[10px] text-stone-400">{profile.domain}</span>
                      </span>
                      <span
                        className="block truncate text-[11px] text-stone-500 dark:text-stone-400"
                        title={activitySummary ?? undefined}
                      >
                        {activitySummary ?? headlineMetric(heartbeat?.resource_metrics) ?? 'Awaiting first heartbeat'}
                      </span>
                    </span>
                    <span className="flex flex-col items-end gap-0.5">
                      <StatusBadge status={status === 'NO SIGNAL' ? 'OFFLINE' : status} label={status} />
                      <span className="flex items-center gap-1.5 text-[10px] text-stone-400">
                        {probeLatencyMs != null && (
                          <span className="font-mono tabular-nums" title="Duration of the last health probe">{formatProbeLatency(probeLatencyMs)}</span>
                        )}
                        {heartbeat && <span>{formatAgo(heartbeat.last_ping_at)}</span>}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
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
            <p className="mb-2 text-xs font-semibold text-stone-400">Live across sections</p>
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
            <ol className="relative flex flex-col gap-3 border-l border-stone-900/[0.08] pl-4 dark:border-white/[0.08]">
              {rows.map((ev, i) => (
                <li key={`${ev.timestamp}-${i}`} className="relative">
                  <span className="absolute -left-[21px] top-1.5 size-2 rounded-full bg-[var(--accent)] ring-4 ring-white dark:ring-[#1c1917]" />
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={ev.event_type.startsWith('STOP_LOSS') ? 'WARNING' : ev.event_type === 'STEAM_ALERT' ? 'INFO' : 'ONLINE'} label={humanize(ev.event_type)} />
                    <span className="text-[11px] text-stone-400">{formatAgo(ev.timestamp)}</span>
                  </div>
                  <p className="mt-1 text-sm text-stone-700 dark:text-stone-200">{ev.message}</p>
                </li>
              ))}
            </ol>
          )}
        </Async>
      </div>
    </Panel>
  );
};

// Group 105: TipMaster moved into the BetDoc Consultant, where the measured crowd is set against BetDoc's maths for the
// slip on the board (its old picks drew their "sharp" probability from a hash). Here: the latest signed verdicts.
const ConsultantDesk = () => {
  const verdicts = useVerdicts(5);
  const navigate = useNavigate();
  return (
    <Panel title="BetDoc Consultant" icon="medical_services" className="lg:col-span-5" updatedAt={verdicts.updatedAt}
           actions={<Button size="sm" variant="ghost" icon="arrow_forward" onClick={() => navigate('/consultant')}>Open</Button>}>
      <Async
        resource={verdicts}
        isEmpty={(d) => d.verdicts.length === 0}
        empty={<EmptyState icon="medical_services" title="No verdict yet" detail="ASHVATTHAMA's Doctor, Lawyer and Consultant examine the slip you build on the Oracle, with TipMaster's crowd comparison." />}
      >
        {(d) => (
          <ul className="flex flex-col gap-2.5">
            {d.verdicts.map((v) => (
              <li key={v.report_id} className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <Pill tone={VERDICT_STYLE[v.verdict].tone} icon={VERDICT_STYLE[v.verdict].icon}>{v.title ?? v.verdict}</Pill>
                  <span className="font-mono text-xs" style={{ color: TIER_STYLE[v.tier].color }}>{Math.round(v.slip_score)} {v.tier}</span>
                </div>
                <p className="mt-1.5 truncate text-xs text-stone-600 dark:text-stone-300">{v.legs.join(' + ')}</p>
                <p className="mt-0.5 text-[11px] text-stone-400">{v.bookmaker ?? '—'} @ {v.odds ?? '—'} · {formatAgo(v.signed_at)}{v.seal_holds ? '' : ' · seal broken'}</p>
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
              Bankroll <strong className="text-stone-800 dark:text-stone-100">{formatINR(s.total_bankroll)}</strong>, exposure{' '}
              <strong className="text-stone-800 dark:text-stone-100">{formatINR(s.current_exposure)}</strong>, today{' '}
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
      <ConsultantDesk />
      <motion.div variants={CARD_VARIANTS} className="lg:col-span-12">
        <MarketBoard />
      </motion.div>
    </Page>
  );
};
