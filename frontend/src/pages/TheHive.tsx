import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { apiClient } from '../api/client';
import { TheHiveScene } from '../components/bots/TheHiveScene';
import { COMMANDER_IDS, COMMANDER_REGISTRY, type CommanderId } from '../config/commanders.config';
import { formatAgo, formatTime, humanize } from '../lib/format';
import { invalidate, runMutation, useResource } from '../lib/resource';
import { subscribeChannel } from '../services/realtime';
import { toast } from '../store/useToastStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, ConfirmButton, EmptyState, Field, NumberInput, Page, Panel, Pill, Select, StatusBadge, TextInput } from '../ui/kit';
import { Markdown } from '../ui/markdown';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
type TaskStatus = 'BACKLOG' | 'IN_PROGRESS' | 'REVIEW' | 'DONE' | 'FAILED' | 'BLOCKED' | 'EXPIRED';
interface Task {
  id: string;
  title: string;
  description: string;
  assignee_name: CommanderId | null;
  status: TaskStatus;
  priority: number;
  max_retries: number;
  retry_count: number;
  created_at: string;
  updated_at: string;
  parent_task_ids: string[];
  child_task_ids: string[];
}
interface BoardEvent { event: string; task_id: string; assignee_name?: string | null; status?: string }
interface IntelDashboard {
  bots: { id: string; name: string; target_site: string; status: 'IDLE' | 'SCANNING' | 'ERROR'; last_scan_at: string | null }[];
  gaps: { id: string; site_name: string; missing_feature: string; severity: 'HIGH' | 'MEDIUM' | 'LOW'; is_resolved: boolean; suggestions: { id: string; suggestion_text?: string; description?: string; priority?: number }[] }[];
}

const COLUMNS: { status: TaskStatus[]; title: string; icon: string }[] = [
  { status: ['BACKLOG', 'BLOCKED'], title: 'Backlog', icon: 'inbox' },
  { status: ['IN_PROGRESS'], title: 'In progress', icon: 'sync' },
  { status: ['REVIEW'], title: 'Review', icon: 'rate_review' },
  { status: ['DONE'], title: 'Done', icon: 'task_alt' },
  { status: ['FAILED', 'EXPIRED'], title: 'Failed', icon: 'error' },
];

// ---------------------------------------------------------------------------
// TASK CARD
// ---------------------------------------------------------------------------
const TaskCard = ({ task, onClaim }: { task: Task; onClaim: (bot: CommanderId) => void }) => {
  const [bot, setBot] = useState<CommanderId>(task.assignee_name ?? 'VIDUR');
  const assignee = task.assignee_name ? COMMANDER_REGISTRY[task.assignee_name] : null;

  const complete = () => runMutation(() => apiClient.patch(`/hive/tasks/${task.id}/complete`, { bot_name: task.assignee_name, result_payload: { completed_from: 'hive-ui' } }), {
    invalidate: ['hive', 'commanders'], success: `${task.title}: done`, errorTitle: 'Could not complete task',
  });
  const fail = () => runMutation(() => apiClient.patch(`/hive/tasks/${task.id}/fail`, { bot_name: task.assignee_name, error: { reason: 'failed from hive-ui' } }), {
    invalidate: ['hive', 'commanders'], success: `${task.title}: marked failed`, errorTitle: 'Could not fail task',
  });

  return (
    <motion.li layout initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96 }} className="rounded-xl bg-white p-3 shadow-sm ring-1 ring-inset ring-slate-900/[0.06] dark:bg-white/[0.04] dark:ring-white/[0.06]">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-semibold leading-snug text-slate-800 dark:text-slate-100">{task.title}</p>
        <Pill tone={task.priority >= 75 ? 'critical' : task.priority >= 50 ? 'warning' : 'neutral'}>P{task.priority}</Pill>
      </div>
      {task.description && <p className="mt-1 line-clamp-2 text-[11px] text-slate-500 dark:text-slate-400">{task.description}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-slate-400">
        {assignee && <span className="font-bold uppercase tracking-wider" style={{ color: assignee.theme.primary }}>{assignee.name}</span>}
        {task.parent_task_ids.length > 0 && <span>· {task.parent_task_ids.length} dependenc{task.parent_task_ids.length === 1 ? 'y' : 'ies'}</span>}
        {task.retry_count > 0 && <span>· retry {task.retry_count}/{task.max_retries}</span>}
        <span>· {formatAgo(task.updated_at)}</span>
      </div>
      {task.status === 'BACKLOG' && (
        <div className="mt-2.5 flex gap-1.5">
          <select value={bot} onChange={(e) => setBot(e.target.value as CommanderId)} className="min-w-0 flex-1 rounded-lg bg-slate-50 px-2 py-1 text-[11px] ring-1 ring-inset ring-slate-900/10 dark:bg-white/[0.04] dark:ring-white/10" aria-label="Commander to dispatch">
            {COMMANDER_IDS.map((id) => <option key={id} value={id}>{COMMANDER_REGISTRY[id].name}</option>)}
          </select>
          <Button size="sm" icon="play_arrow" onClick={() => onClaim(bot)}>Dispatch</Button>
        </div>
      )}
      {task.status === 'IN_PROGRESS' && task.assignee_name && (
        <div className="mt-2.5 flex gap-1.5">
          <Button size="sm" variant="primary" icon="check" onClick={() => void complete()}>Complete</Button>
          <ConfirmButton size="sm" variant="danger" confirmLabel="Fail it?" onConfirm={() => void fail()}>Fail</ConfirmButton>
        </div>
      )}
    </motion.li>
  );
};

// ---------------------------------------------------------------------------
// NEW TASK
// ---------------------------------------------------------------------------
const NewTask = ({ tasks }: { tasks: Task[] }) => {
  const [form, setForm] = useState({ title: '', description: '', priority: '50', assignee: '', parent: '' });
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!form.title.trim()) return;
    setBusy(true);
    const created = await runMutation(
      async () => {
        const task = await apiClient.post<Task>('/hive/tasks', {
          title: form.title.trim(), description: form.description.trim(), priority: Number(form.priority) || 50,
          max_retries: 2, payload: { origin: 'hive-ui' }, assignee_name: form.assignee || null,
        });
        if (form.parent) await apiClient.post(`/hive/tasks/${task.id}/dependencies`, { parent_task_id: form.parent });
        return task;
      },
      { invalidate: ['hive', 'commanders'], success: (t) => `Task created: ${t.title}`, errorTitle: 'Task rejected' },
    );
    setBusy(false);
    if (created) setForm({ title: '', description: '', priority: '50', assignee: '', parent: '' });
  };

  return (
    <Panel title="New task" icon="add_task" className="lg:col-span-4">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <Field label="Title"><TextInput value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="Refresh EPL injury report" maxLength={255} /></Field>
        <Field label="Description"><TextInput value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Optional context" /></Field>
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Priority (0-100)"><NumberInput value={form.priority} min="0" max="100" onChange={(e) => setForm({ ...form, priority: e.target.value })} /></Field>
          <Field label="Reserve for">
            <Select value={form.assignee} onChange={(e) => setForm({ ...form, assignee: e.target.value })}>
              <option value="">Any commander</option>
              {COMMANDER_IDS.map((id) => <option key={id} value={id}>{COMMANDER_REGISTRY[id].name}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Depends on" hint="The task stays blocked until its parent completes.">
          <Select value={form.parent} onChange={(e) => setForm({ ...form, parent: e.target.value })}>
            <option value="">No dependency</option>
            {tasks.filter((t) => t.status !== 'DONE' && t.status !== 'FAILED').map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
          </Select>
        </Field>
        <Button type="submit" variant="primary" icon="add" busy={busy} disabled={!form.title.trim()}>Create task</Button>
      </form>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// SCRAPER SWARM (competitive intel)
// ---------------------------------------------------------------------------
const ScraperSwarm = ({ intel, scanning, onScan }: { intel: ReturnType<typeof useResource<IntelDashboard>>; scanning: boolean; onScan: () => void }) => {
  const [site, setSite] = useState<string | null>(null);
  const report = useResource(site ? `rnd:report:${site}` : null, () => apiClient.get<{ markdown: string }>(`/rnd/competitive-intel/reports/${encodeURIComponent(site ?? '')}`));
  return (
    <Panel title="Scraper swarm · competitive intel" icon="hub" className="lg:col-span-12" updatedAt={intel.updatedAt} subtitle="KARNA · curated competitor feature catalog" actions={<Button size="sm" icon="radar" busy={scanning} onClick={onScan}>Scan competitors</Button>}>
      <Async resource={intel} isEmpty={(d) => d.bots.length === 0} empty={<EmptyState icon="hub" title="No competitors tracked yet" detail="Run a scan to load the catalog of tracked competitors and their feature gaps." action={<Button icon="radar" onClick={onScan}>Scan now</Button>} />}>
        {(d) => (
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
            <div className="flex flex-col gap-2">
              {d.bots.map((b) => (
                <button key={b.id} type="button" onClick={() => setSite(b.target_site.split('.')[0])} className={`flex items-center justify-between gap-3 rounded-xl px-3 py-2.5 text-left transition-colors ${site === b.target_site.split('.')[0] ? 'bg-[color-mix(in_srgb,var(--accent)_10%,transparent)]' : 'bg-slate-50 hover:bg-slate-100 dark:bg-white/[0.03] dark:hover:bg-white/[0.06]'}`}>
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold text-slate-800 dark:text-slate-100">{b.name}</span>
                    <span className="text-[11px] text-slate-400">{b.target_site} · last scan {formatAgo(b.last_scan_at)}</span>
                  </span>
                  <StatusBadge status={b.status} />
                </button>
              ))}
              <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400">Open gaps</p>
              <ul className="flex flex-col gap-1.5">
                {d.gaps.filter((g) => !g.is_resolved).slice(0, 8).map((g) => (
                  <li key={g.id} className="flex items-center justify-between gap-2 text-xs">
                    <span className="truncate text-slate-700 dark:text-slate-200">{g.missing_feature} <span className="text-slate-400">· {g.site_name}</span></span>
                    <StatusBadge status={g.severity} />
                  </li>
                ))}
              </ul>
            </div>
            <div className="min-h-[200px] rounded-xl bg-slate-50 p-4 dark:bg-white/[0.03]">
              {!site ? <EmptyState icon="article" title="Pick a competitor" detail="Opens the gap analysis report for that site." /> : (
                <Async resource={report}>{(r) => <Markdown source={r.markdown} />}</Async>
              )}
            </div>
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE HIVE
// ---------------------------------------------------------------------------
export const TheHive = () => {
  const tasks = useResource('hive:tasks', () => apiClient.get<Task[]>('/hive/tasks', { limit: 200 }), { intervalMs: 30_000 });
  const intel = useResource('rnd:dashboard', () => apiClient.get<IntelDashboard>('/rnd/competitive-intel'), { intervalMs: 60_000 });
  const [events, setEvents] = useState<(BoardEvent & { at: string })[]>([]);
  const [scanning, setScanning] = useState(false);

  // Live board: every task transition refreshes the board instantly.
  useEffect(
    () =>
      subscribeChannel('/hive/board/live', (data) => {
        const ev = data as BoardEvent;
        if (!ev || typeof ev.event !== 'string' || ev.event === 'pong') return;
        setEvents((prev) => [{ ...ev, at: new Date().toISOString() }, ...prev].slice(0, 20));
        invalidate('hive', 'commanders');
      }, 'event'),
    [],
  );

  const rows = useMemo(() => tasks.data ?? [], [tasks.data]);
  const inProgress = rows.filter((t) => t.status === 'IN_PROGRESS').length;
  const failed = rows.filter((t) => t.status === 'FAILED').length;
  const byColumn = useMemo(() => COLUMNS.map((c) => rows.filter((t) => c.status.includes(t.status)).sort((a, b) => b.priority - a.priority)), [rows]);

  const claim = async (bot: CommanderId) => {
    const task = await runMutation(() => apiClient.post<Task | null>('/hive/tasks/claim', { bot_name: bot }), { invalidate: ['hive', 'commanders'], errorTitle: 'Dispatch failed' });
    if (task) toast.success(`${COMMANDER_REGISTRY[bot].name} claimed "${task.title}"`);
    else if (task === null) toast.info(`Nothing claimable for ${COMMANDER_REGISTRY[bot].name}`, 'Higher-priority work may be reserved for another commander or still blocked.');
  };

  const scan = async () => {
    setScanning(true);
    await runMutation(() => apiClient.post('/rnd/competitive-intel/scan'), { invalidate: ['rnd', 'commanders'], success: 'Competitor scan complete', errorTitle: 'Scan failed' });
    setScanning(false);
  };

  return (
    <Page>
      <CommanderHero
        commander="VIDUR"
        headline={`HIVE ACTIVE. ${rows.length} tasks on the DAG, ${inProgress} in flight, ${failed} failed.`}
        motif={MOTIFS.hex}
        detail="Tasks are claimed by priority, blocked until their parents finish, retried, and cascade-failed to their children. The board updates live."
        scene={<TheHiveScene activeDesks={Math.min(12, Math.max(1, inProgress))} kautilyaState={failed > 0 ? 'alert' : inProgress > 0 ? 'directing' : 'observing'} bossName="Vidur" className="h-[220px]" />}
        actions={
          <>
            <Button variant="primary" icon="play_arrow" onClick={() => void claim('VIDUR')}>Dispatch next to Vidur</Button>
            <Button icon="radar" busy={scanning} onClick={() => void scan()}>Scan competitors</Button>
            <Button icon="refresh" onClick={() => void tasks.refresh()}>Refresh board</Button>
          </>
        }
      />

      <Panel title="Task DAG board" icon="view_kanban" className="lg:col-span-8" updatedAt={tasks.updatedAt} bodyClassName="p-3">
        <Async resource={tasks} skeletonRows={4}>
          {() => (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
              {COLUMNS.map((col, i) => (
                <div key={col.title} className="flex min-w-0 flex-col gap-2 rounded-xl bg-slate-50 p-2 dark:bg-white/[0.02]">
                  <div className="flex items-center justify-between px-1">
                    <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-[0.14em] text-slate-500">
                      <span className="material-symbols-outlined text-[14px]">{col.icon}</span>{col.title}
                    </span>
                    <span className="text-[10px] tabular-nums text-slate-400">{byColumn[i].length}</span>
                  </div>
                  <ul className="flex max-h-[460px] flex-col gap-2 overflow-y-auto">
                    <AnimatePresence initial={false}>
                      {byColumn[i].map((t) => <TaskCard key={t.id} task={t} onClaim={(bot) => void claim(bot)} />)}
                    </AnimatePresence>
                    {byColumn[i].length === 0 && <li className="px-1 py-4 text-center text-[11px] text-slate-400">Empty</li>}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </Async>
      </Panel>

      <div className="flex flex-col gap-6 lg:col-span-4">
        <NewTask tasks={rows} />
        <Panel title="Data firehose · live board" icon="bolt" bodyClassName="p-0">
          {events.length === 0 ? (
            <EmptyState icon="bolt" title="Listening" detail="Task transitions stream here the moment they happen." />
          ) : (
            <ul className="max-h-[260px] divide-y divide-slate-900/[0.05] overflow-y-auto dark:divide-white/[0.05]">
              {events.map((e, i) => (
                <li key={`${e.at}-${i}`} className="flex items-center justify-between gap-2 px-4 py-2 text-xs">
                  <span className="text-slate-700 dark:text-slate-200">{humanize(e.event)}{e.assignee_name ? ` · ${e.assignee_name}` : ''}</span>
                  <span className="font-mono text-[10px] text-slate-400">{formatTime(e.at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <ScraperSwarm intel={intel} scanning={scanning} onScan={() => void scan()} />
    </Page>
  );
};
