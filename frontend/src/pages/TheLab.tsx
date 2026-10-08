import { useEffect, useState, type FormEvent } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { apiClient } from '../api/client';
import { TheLabScene } from '../components/bots/TheLabScene';
import { formatAgo, formatDateTime, formatOdds, formatPct, formatRatioPct } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { type Selection, useExecutionStore } from '../store/useExecutionStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, ConfirmButton, EmptyState, Field, KeyValues, NumberInput, Page, Panel, Select, StatusBadge, TextInput, Toggle, num } from '../ui/kit';
import { Markdown } from '../ui/markdown';

// ---------------------------------------------------------------------------
// CONTRACTS (mirror backend schemas)
// ---------------------------------------------------------------------------
interface PredictionResponse {
  prediction: { home_win_prob: number; draw_prob: number; away_win_prob: number; most_likely_scoreline: string; confidence_score: number };
  value_bets: { selection: Selection; true_prob: number; bookmaker_odds: number; expected_value: number; kelly_stake_fraction: number }[];
}
interface SourceHealth { source_name: string; status: 'ONLINE' | 'DEGRADED' | 'OFFLINE'; latency_ms: number; last_checked: string }
interface Research { id: string; category: string; topic: string; status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED'; markdown_content: string | null; created_at: string; completed_at: string | null }
interface Experiment { id: string; name: string; hypothesis: string; model_a_name: string; model_b_name: string; status: 'RUNNING' | 'CONCLUDED'; winner: string | null; metrics: Record<string, unknown> | null; created_at: string; concluded_at: string | null }
interface HumanTouchConfig { is_blended_mode_active: boolean; max_adjustment_limit_pct: number; sentiment_weight: number; momentum_weight: number; min_adjustment_threshold_pct: number; updated_at: string }
interface BlendResponse { pure_math_prob: number; adjusted_prob: number; adjustment_delta: number; confidence_tier: string; narrative_modifier: number; bypassed: boolean; below_threshold: boolean; clamped: boolean }

const RESEARCH_CATEGORIES = ['TEAM', 'MATCH', 'MARKET', 'LEAGUE'] as const;

// ---------------------------------------------------------------------------
// MODEL SANDBOX (/engine/predict)
// ---------------------------------------------------------------------------
const EMPTY = { home_team: '', away_team: '', home_xg: '', away_xg: '', home_elo: '', away_elo: '', odds_home: '', odds_draw: '', odds_away: '' };
const SAMPLE = { home_team: 'Arsenal', away_team: 'Chelsea', home_xg: '1.85', away_xg: '1.10', home_elo: '1850', away_elo: '1780', odds_home: '2.05', odds_draw: '3.60', odds_away: '3.90' };
const opt = (v: string): number | null => (Number.isFinite(num(v)) ? num(v) : null);

const ModelSandbox = () => {
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PredictionResponse | null>(null);
  const setDraft = useExecutionStore((s) => s.setDraft);
  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const odds: Partial<Record<Selection, number>> = {};
    if (opt(form.odds_home)) odds.HOME = opt(form.odds_home)!;
    if (opt(form.odds_draw)) odds.DRAW = opt(form.odds_draw)!;
    if (opt(form.odds_away)) odds.AWAY = opt(form.odds_away)!;
    setBusy(true);
    const res = await runMutation(
      () => apiClient.post<PredictionResponse>('/engine/predict', {
        home_team: form.home_team.trim() || 'Home', away_team: form.away_team.trim() || 'Away',
        home_xg: opt(form.home_xg), away_xg: opt(form.away_xg), home_elo: opt(form.home_elo), away_elo: opt(form.away_elo),
        bookmaker_odds: Object.keys(odds).length ? odds : null,
      }),
      { errorTitle: 'Ensemble rejected the inputs' },
    );
    setBusy(false);
    setResult(res ?? null);
  };

  const p = result?.prediction;
  return (
    <Panel title="Model sandbox · PANINI ensemble" icon="functions" className="lg:col-span-7" subtitle="Poisson 30% · Dixon-Coles 40% · Elo 30%">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Home team"><TextInput value={form.home_team} onChange={set('home_team')} placeholder="Arsenal" /></Field>
          <Field label="Away team"><TextInput value={form.away_team} onChange={set('away_team')} placeholder="Chelsea" /></Field>
          <Field label="Home xG"><NumberInput value={form.home_xg} onChange={set('home_xg')} /></Field>
          <Field label="Away xG"><NumberInput value={form.away_xg} onChange={set('away_xg')} /></Field>
          <Field label="Home Elo"><NumberInput value={form.home_elo} onChange={set('home_elo')} /></Field>
          <Field label="Away Elo"><NumberInput value={form.away_elo} onChange={set('away_elo')} /></Field>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Home odds"><NumberInput value={form.odds_home} onChange={set('odds_home')} /></Field>
          <Field label="Draw odds"><NumberInput value={form.odds_draw} onChange={set('odds_draw')} /></Field>
          <Field label="Away odds"><NumberInput value={form.odds_away} onChange={set('odds_away')} /></Field>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" icon="play_arrow" busy={busy}>Run ensemble</Button>
          <Button icon="science" onClick={() => setForm(SAMPLE)}>Load sample</Button>
          <Button variant="ghost" icon="restart_alt" onClick={() => { setForm(EMPTY); setResult(null); }}>Reset</Button>
        </div>
      </form>
      <AnimatePresence>
        {p && result && (
          <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-8 flex flex-col gap-5">
            {(['home_win_prob', 'draw_prob', 'away_win_prob'] as const).map((k, i) => (
              <div key={k}>
                <div className="mb-1 flex justify-between text-xs text-stone-600 dark:text-stone-300">
                  <span>{['Home', 'Draw', 'Away'][i]}</span>
                  <span className="font-semibold tabular-nums">{formatRatioPct(p[k])}</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-stone-900/[0.06] dark:bg-white/[0.07]">
                  <motion.div className="h-full rounded-full" style={{ background: ['var(--viz-series-1)', 'var(--viz-axis)', 'var(--viz-series-2)'][i] }} initial={{ width: 0 }} animate={{ width: `${p[k] * 100}%` }} />
                </div>
              </div>
            ))}
            <KeyValues items={[{ label: 'Most likely scoreline', value: p.most_likely_scoreline }, { label: 'Confidence', value: formatRatioPct(p.confidence_score) }]} />
            {result.value_bets.length > 0 ? (
              <ul className="flex flex-col gap-2">
                {result.value_bets.map((v) => (
                  <li key={v.selection} className="flex items-center justify-between gap-2 rounded-xl bg-emerald-50 px-3 py-2 text-sm dark:bg-emerald-500/10">
                    <span className="text-emerald-800 dark:text-emerald-200">{v.selection} @ {formatOdds(v.bookmaker_odds)} · EV {formatPct(v.expected_value * 100, 2)} · Kelly {formatRatioPct(v.kelly_stake_fraction, 2)}</span>
                    <Button size="sm" icon="receipt_long" onClick={() => setDraft({ matchId: `${form.home_team}-v-${form.away_team}`.toLowerCase().replace(/\s+/g, '-'), selection: v.selection, odds: v.bookmaker_odds, trueProbability: v.true_prob, label: `${form.home_team} v ${form.away_team}`, source: 'Lab sandbox' })}>Stage</Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-stone-500">No selection clears the +2% EV threshold at those prices.</p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// SOURCE HEALTH
// ---------------------------------------------------------------------------
const SourceHealth = ({ health }: { health: ReturnType<typeof useResource<SourceHealth[]>> }) => (
  <Panel title="Data source health" icon="lan" className="lg:col-span-5" updatedAt={health.updatedAt} actions={<Button size="sm" icon="refresh" busy={health.loading} onClick={() => void health.refresh()}>Probe</Button>}>
    <Async resource={health}>
      {(rows) => (
        <ul className="flex flex-col gap-2">
          {rows.map((s) => (
            <li key={s.source_name} className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2.5 dark:bg-white/[0.03]">
              <span className="text-sm font-medium text-stone-800 dark:text-stone-100">{s.source_name}</span>
              <span className="flex items-center gap-2">
                <span className="text-[11px] tabular-nums text-stone-400">{s.latency_ms}ms</span>
                <StatusBadge status={s.status} />
              </span>
            </li>
          ))}
          <li className="pt-1 text-[11px] text-stone-400">Live HTTP probes from the API server (4s timeout). Over 1.5s or a 5xx counts as degraded.</li>
        </ul>
      )}
    </Async>
  </Panel>
);

// ---------------------------------------------------------------------------
// RESEARCH DESK
// ---------------------------------------------------------------------------
const ResearchDesk = ({ research }: { research: ReturnType<typeof useResource<Research[]>> }) => {
  const [category, setCategory] = useState<string>('TEAM');
  const [topic, setTopic] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const open = research.data?.find((r) => r.id === openId) ?? null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!topic.trim()) return;
    setBusy(true);
    const created = await runMutation(() => apiClient.post<Research>('/lab/research', { category, topic: topic.trim() }), {
      invalidate: ['lab:research'],
      success: 'Research queued: the data agent is compiling it',
      errorTitle: 'Could not queue research',
    });
    setBusy(false);
    if (created) {
      setTopic('');
      setOpenId(created.id);
    }
  };

  return (
    <Panel title="Research desk" icon="description" className="lg:col-span-7" updatedAt={research.updatedAt} subtitle="DataResearchAgent · stored odds + ledger">
      <form onSubmit={submit} className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end">
        <Field label="Category" className="sm:w-36">
          <Select value={category} onChange={(e) => setCategory(e.target.value)}>
            {RESEARCH_CATEGORIES.map((c) => <option key={c}>{c}</option>)}
          </Select>
        </Field>
        <Field label="Topic (name teams as they appear in the odds feed)" className="flex-1">
          <TextInput id="research-topic" value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="Arsenal v Chelsea price drift" maxLength={255} />
        </Field>
        <Button type="submit" variant="primary" icon="send" busy={busy} disabled={!topic.trim()}>Queue</Button>
      </form>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,240px)_1fr]">
        <Async resource={research} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="description" title="No reports yet" />}>
          {(rows) => (
            <ul className="flex max-h-[420px] flex-col gap-1.5 overflow-y-auto">
              {rows.map((r) => (
                <li key={r.id}>
                  <button type="button" onClick={() => setOpenId(r.id)} className={`w-full rounded-xl px-3 py-2 text-left transition-colors ${openId === r.id ? 'bg-[color-mix(in_srgb,var(--accent)_10%,transparent)]' : 'hover:bg-stone-900/[0.03] dark:hover:bg-white/[0.04]'}`}>
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-stone-800 dark:text-stone-100">{r.topic}</span>
                      <StatusBadge status={r.status} />
                    </span>
                    <span className="text-[11px] text-stone-400">{r.category} · {formatAgo(r.created_at)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Async>
        <div className="min-h-[200px] rounded-xl bg-stone-50 p-4 dark:bg-white/[0.03]">
          {open?.markdown_content ? (
            <Markdown source={open.markdown_content} />
          ) : open ? (
            <EmptyState icon="hourglass_top" title={open.status === 'FAILED' ? 'Report failed' : 'Compiling…'} detail={open.status === 'FAILED' ? 'The agent could not finish this report.' : 'This refreshes automatically when the agent finishes.'} />
          ) : (
            <EmptyState icon="article" title="Select a report" />
          )}
        </div>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// A/B EXPERIMENTS
// ---------------------------------------------------------------------------
const Experiments = ({ experiments }: { experiments: ReturnType<typeof useResource<Experiment[]>> }) => {
  const [form, setForm] = useState({ name: '', hypothesis: '', model_a_name: 'DixonColes', model_b_name: 'Ensemble' });
  const [busy, setBusy] = useState(false);
  const valid = form.name.trim() && form.hypothesis.trim() && form.model_a_name.trim() && form.model_b_name.trim();

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    const ok = await runMutation(() => apiClient.post('/lab/experiments', form), { invalidate: ['lab:experiments', 'commanders'], success: 'Experiment started', errorTitle: 'Could not start experiment' });
    setBusy(false);
    if (ok) setForm((f) => ({ ...f, name: '', hypothesis: '' }));
  };

  const conclude = (x: Experiment, winner: string) =>
    runMutation(() => apiClient.patch(`/lab/experiments/${x.id}/conclude`, { winner, metrics: { concluded_from: 'lab-ui' } }), {
      invalidate: ['lab:experiments', 'commanders'],
      success: `${x.name}: ${winner} wins`,
      errorTitle: 'Could not conclude',
    });

  return (
    <Panel title="A/B experiments" icon="compare_arrows" className="lg:col-span-5" updatedAt={experiments.updatedAt}>
      <form onSubmit={create} className="mb-4 grid grid-cols-2 gap-2.5">
        <Field label="Name" className="col-span-2"><TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Draw-bias correction" /></Field>
        <Field label="Hypothesis" className="col-span-2"><TextInput value={form.hypothesis} onChange={(e) => setForm({ ...form, hypothesis: e.target.value })} placeholder="B improves Brier score on draws" /></Field>
        <Field label="Model A"><TextInput value={form.model_a_name} onChange={(e) => setForm({ ...form, model_a_name: e.target.value })} /></Field>
        <Field label="Model B"><TextInput value={form.model_b_name} onChange={(e) => setForm({ ...form, model_b_name: e.target.value })} /></Field>
        <Button type="submit" variant="primary" icon="add" busy={busy} disabled={!valid} className="col-span-2">Start experiment</Button>
      </form>
      <Async resource={experiments} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="compare_arrows" title="No experiments" />}>
        {(rows) => (
          <ul className="flex max-h-[300px] flex-col gap-2 overflow-y-auto">
            {rows.map((x) => (
              <li key={x.id} className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{x.name}</span>
                  <StatusBadge status={x.status} />
                </div>
                <p className="mt-0.5 text-[11px] text-stone-500 dark:text-stone-400">{x.model_a_name} vs {x.model_b_name} · {x.hypothesis}</p>
                {x.status === 'RUNNING' ? (
                  <div className="mt-2 flex gap-1.5">
                    <ConfirmButton size="sm" variant="ghost" confirmLabel={`${x.model_a_name} wins?`} onConfirm={() => void conclude(x, x.model_a_name)}>A wins</ConfirmButton>
                    <ConfirmButton size="sm" variant="ghost" confirmLabel={`${x.model_b_name} wins?`} onConfirm={() => void conclude(x, x.model_b_name)}>B wins</ConfirmButton>
                  </div>
                ) : (
                  <p className="mt-1.5 text-[11px] text-emerald-600 dark:text-emerald-400">Winner: {x.winner} · {formatDateTime(x.concluded_at)}</p>
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
// HUMAN TOUCH (FA-8)
// ---------------------------------------------------------------------------
const HumanTouch = () => {
  const config = useResource('human-touch:config', () => apiClient.get<HumanTouchConfig>('/human-touch/config'));
  const [draft, setDraft] = useState<HumanTouchConfig | null>(null);
  const [blend, setBlend] = useState({ prob: '0.55', sentiment: '0.3', factor: 'derby_intensity', value: '0.8', impact: '0.4' });
  const [result, setResult] = useState<BlendResponse | null>(null);
  useEffect(() => { if (config.data) setDraft(config.data); }, [config.data]);

  const save = () => draft && runMutation(
    () => apiClient.put('/human-touch/config', {
      is_blended_mode_active: draft.is_blended_mode_active, max_adjustment_limit_pct: draft.max_adjustment_limit_pct,
      sentiment_weight: draft.sentiment_weight, momentum_weight: draft.momentum_weight, min_adjustment_threshold_pct: draft.min_adjustment_threshold_pct,
    }),
    { invalidate: ['human-touch'], success: 'Human Touch configuration saved', errorTitle: 'Configuration rejected' },
  );

  const runBlend = async () => {
    const res = await runMutation(() => apiClient.post<BlendResponse>('/human-touch/blend', {
      pure_math_prob: num(blend.prob), sentiment_score: num(blend.sentiment),
      factors: blend.factor.trim() ? [{ name: blend.factor.trim(), value: num(blend.value), impact: num(blend.impact) }] : [],
    }), { errorTitle: 'Blend rejected' });
    setResult(res ?? null);
  };

  const slider = (key: 'max_adjustment_limit_pct' | 'sentiment_weight' | 'momentum_weight' | 'min_adjustment_threshold_pct', label: string, max: number, step: number) =>
    draft && (
      <Field label={`${label}: ${draft[key]}`}>
        <input type="range" min={0} max={max} step={step} value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })} className="accent-[var(--accent)]" />
      </Field>
    );

  return (
    <Panel title="Human Touch · FA-8 narrative blending" icon="psychology_alt" className="lg:col-span-12" updatedAt={config.updatedAt}>
      <Async resource={config}>
        {() => draft && (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-stone-800 dark:text-stone-100">Blended mode</p>
                  <p className="text-xs text-stone-500 dark:text-stone-400">When off, the pure math probability passes through untouched.</p>
                </div>
                <Toggle label="Blended mode" checked={draft.is_blended_mode_active} onChange={(v) => setDraft({ ...draft, is_blended_mode_active: v })} />
              </div>
              {slider('max_adjustment_limit_pct', 'Max adjustment %', 25, 0.5)}
              {slider('min_adjustment_threshold_pct', 'Min threshold %', 5, 0.1)}
              {slider('sentiment_weight', 'Sentiment weight', 1, 0.05)}
              {slider('momentum_weight', 'Momentum weight', 1, 0.05)}
              <Button variant="primary" icon="save" onClick={() => void save()}>Save configuration</Button>
            </div>
            <div className="flex flex-col gap-3 rounded-xl bg-stone-50 p-4 dark:bg-white/[0.03]">
              <p className="text-sm font-semibold text-stone-800 dark:text-stone-100">Blend calculator</p>
              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Math probability (0-1)"><NumberInput value={blend.prob} onChange={(e) => setBlend({ ...blend, prob: e.target.value })} /></Field>
                <Field label="Sentiment (-1 to 1)"><NumberInput value={blend.sentiment} onChange={(e) => setBlend({ ...blend, sentiment: e.target.value })} /></Field>
                <Field label="Factor" className="col-span-2"><TextInput value={blend.factor} onChange={(e) => setBlend({ ...blend, factor: e.target.value })} /></Field>
                <Field label="Factor value (0-1)"><NumberInput value={blend.value} onChange={(e) => setBlend({ ...blend, value: e.target.value })} /></Field>
                <Field label="Impact (-1 to 1)"><NumberInput value={blend.impact} onChange={(e) => setBlend({ ...blend, impact: e.target.value })} /></Field>
              </div>
              <Button icon="calculate" onClick={() => void runBlend()}>Blend</Button>
              {result && (
                <KeyValues items={[
                  { label: 'Pure math', value: formatRatioPct(result.pure_math_prob) },
                  { label: 'Adjusted', value: formatRatioPct(result.adjusted_prob) },
                  { label: 'Delta', value: `${result.adjustment_delta >= 0 ? '+' : ''}${(result.adjustment_delta * 100).toFixed(2)} pts` },
                  { label: 'Tier', value: <StatusBadge status={result.confidence_tier === 'HIGH_CONFIDENCE' ? 'ONLINE' : result.confidence_tier === 'CONTRARIAN' ? 'WARNING' : 'IDLE'} label={result.confidence_tier.replace('_', ' ')} /> },
                  { label: 'Flags', value: [result.bypassed && 'bypassed', result.below_threshold && 'below threshold', result.clamped && 'clamped'].filter(Boolean).join(', ') || 'none' },
                ]} />
              )}
            </div>
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE LAB
// ---------------------------------------------------------------------------
export const TheLab = () => {
  const health = useResource('lab:health', () => apiClient.get<SourceHealth[]>('/lab/health'), { intervalMs: 120_000 });
  const research = useResource('lab:research', () => apiClient.get<Research[]>('/lab/research', { limit: 25 }), { intervalMs: 10_000 });
  const experiments = useResource('lab:experiments', () => apiClient.get<Experiment[]>('/lab/experiments', { limit: 25 }), { intervalMs: 30_000 });

  const running = (experiments.data ?? []).filter((x) => x.status === 'RUNNING').length;
  const compiling = (research.data ?? []).filter((r) => r.status === 'PENDING' || r.status === 'RUNNING').length;
  const online = (health.data ?? []).filter((s) => s.status === 'ONLINE').length;
  const lastDone = (research.data ?? []).find((r) => r.status === 'COMPLETED');
  const state = compiling > 0 || running > 0 ? 'synthesizing' : lastDone ? 'complete' : 'idle';

  return (
    <Page>
      <CommanderHero
        commander="PANINI"
        headline={`PANINI ACTIVE. ${running} experiment${running === 1 ? '' : 's'} running, ${compiling} report${compiling === 1 ? '' : 's'} compiling, ${online}/${health.data?.length ?? 3} sources reachable.`}
        motif={MOTIFS.hex}
        detail={lastDone ? <>Latest report: <strong className="text-stone-800 dark:text-stone-100">{lastDone.topic}</strong> ({formatAgo(lastDone.completed_at)}).</> : 'Queue a research topic or start an A/B experiment to put the lab to work.'}
        scene={<TheLabScene analysisState={state} activeBeakers={Math.max(1, Math.min(6, running + compiling))} statusLabel={state === 'synthesizing' ? 'Synthesizing' : state === 'complete' ? 'Report ready' : 'Standing by'} className="h-[220px]" />}
        actions={
          <>
            <Button variant="primary" icon="description" onClick={() => document.getElementById('research-topic')?.focus()}>New research</Button>
            <Button icon="lan" busy={health.loading && health.data !== undefined} onClick={() => void health.refresh()}>Probe sources</Button>
          </>
        }
      />
      <ModelSandbox />
      <SourceHealth health={health} />
      <ResearchDesk research={research} />
      <Experiments experiments={experiments} />
      <HumanTouch />
    </Page>
  );
};
