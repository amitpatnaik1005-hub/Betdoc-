/**
 * The Sports Engine desk on The Oracle (Group 79's page, unified into ASHOKA's Oracle in Group 103; its commander
 * ARYABHATA retired): nine quantitative sport adapters on one desk.
 * Price a match from its sport's sub-model consensus, read the line markets, run the twin's pipeline on a selection
 * (pillar 1 consensus, pillar 9 sharp benchmark, OmniRoute, the 0-100 confidence index, the sport's Kelly stake) and
 * watch the beat's cross-provider edges. Architect: Amit Ashok Kumar Patnaik.
 */
import { useMemo, useState } from 'react';
import { ApiError } from '../../api/client';
import { SportSpecificStatCard } from '../sports/SportSpecificStatCard';
import { UniversalSportSelector } from '../sports/UniversalSportSelector';
import { formatAgo, formatINR, formatOdds, humanize } from '../../lib/format';
import { invalidate } from '../../lib/resource';
import {
  OMNI_VENUES,
  SPORT_FIELDS,
  calculateOdds,
  contextValue,
  evaluateSelection,
  marketLabel,
  updateSportConfig,
  useSportConfigs,
  useSportEdges,
  useSportLeagues,
  useSupportedSports,
  type Evaluation,
  type MatchProbabilities,
  type OmniVenue,
  type Side,
} from '../../lib/sports';
import { useAuthStore } from '../../store/useAuthStore';
import { sportInfo, useSportStore } from '../../store/useSportStore';
import { toast } from '../../store/useToastStore';
import { Async, Button, EmptyState, Field, KeyValues, Meter, NumberInput, Panel, Pill, Segmented, Select, Stat, StatGrid, TextInput, Toggle } from '../../ui/kit';
import { num } from '../../ui/num';

type Values = Record<string, string>;

const pct = (p: number | null | undefined, digits = 1): string => (p === null || p === undefined ? '—' : `${(p * 100).toFixed(digits)}%`);
const failure = (e: unknown): string => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Request failed');

const presetValues = (preset: (typeof SPORT_FIELDS)[keyof typeof SPORT_FIELDS], side: 'home' | 'away'): Values =>
  Object.fromEntries(preset.stats.map((f) => [f.key, String(f[side])]));
const presetContext = (preset: (typeof SPORT_FIELDS)[keyof typeof SPORT_FIELDS]): Values => Object.fromEntries(preset.context.map((c) => [c.key, c.value]));

const CONFIDENCE_TONE: Record<string, 'good' | 'warning' | 'critical' | 'accent'> = { A: 'good', B: 'accent', C: 'warning', D: 'critical' };

export const SportsEngineDesk = () => {
  const activeSport = useSportStore((s) => s.activeSport);
  const marketFilter = useSportStore((s) => s.activeMarketFilter);
  const setMarketFilter = useSportStore((s) => s.setActiveMarketFilter);
  const credit = useSportStore((s) => s.developerCredit);
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');
  const supported = useSupportedSports();
  const configs = useSportConfigs();
  const leagues = useSportLeagues(activeSport);
  const edges = useSportEdges(activeSport);
  const preset = SPORT_FIELDS[activeSport];
  const info = sportInfo(activeSport);
  const adapter = supported.data?.find((s) => s.sport_key === activeSport);
  const config = configs.data?.find((c) => c.sport_key === activeSport);
  const inactive = useMemo(() => new Set((configs.data ?? []).filter((c) => !c.is_active).map((c) => c.sport_key)), [configs.data]);

  const [formSport, setFormSport] = useState(activeSport);
  const [home, setHome] = useState<Values>(() => presetValues(preset, 'home'));
  const [away, setAway] = useState<Values>(() => presetValues(preset, 'away'));
  const [ctx, setCtx] = useState<Values>(() => presetContext(preset));
  const [result, setResult] = useState<MatchProbabilities | null>(null);
  const [pricing, setPricing] = useState(false);
  const [selection, setSelection] = useState<Side>('home');
  const [quotes, setQuotes] = useState<Record<OmniVenue, Partial<Record<Side, string>>>>({ pinnacle: {}, betfair: {}, parimatch: {}, stake: {}, '1xbet': {} });
  const [bankroll, setBankroll] = useState('100000');
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [kellyDraft, setKellyDraft] = useState<string | null>(null);  // null: untouched, shows the stored fraction

  // A new sport starts from its adapter's league-average inputs (reset while rendering, not in an effect).
  if (formSport !== activeSport) {
    setFormSport(activeSport);
    setHome(presetValues(preset, 'home'));
    setAway(presetValues(preset, 'away'));
    setCtx(presetContext(preset));
    setResult(null);
    setEvaluation(null);
    setSelection('home');
    setKellyDraft(null);
  }
  const kellyValue = kellyDraft ?? (config ? String(config.default_kelly_fraction) : '');

  const statsOf = (values: Values): Record<string, number> =>
    Object.fromEntries(Object.entries(values).map(([k, v]) => [k, num(v)]).filter(([, v]) => Number.isFinite(v as number)));
  const contextOf = (): Record<string, unknown> => Object.fromEntries(Object.entries(ctx).filter(([, v]) => v !== '' && v !== 'none').map(([k, v]) => [k, contextValue(k, v)]));
  const valid = preset.stats.every((f) => Number.isFinite(num(home[f.key] ?? '')) && Number.isFinite(num(away[f.key] ?? '')));

  const price = async () => {
    setPricing(true);
    try {
      setResult(await calculateOdds({ sport_key: activeSport, home_stats: statsOf(home), away_stats: statsOf(away), context: contextOf() }));
    } catch (e) {
      toast.error('Pricing failed', failure(e));
    } finally {
      setPricing(false);
    }
  };

  const evaluate = async () => {
    const venue_odds: Record<string, Partial<Record<Side, number>>> = {};
    for (const venue of OMNI_VENUES) {
      const row: Partial<Record<Side, number>> = {};
      for (const side of ['home', 'away', 'draw'] as const) {
        const v = num(quotes[venue][side] ?? '');
        if (Number.isFinite(v) && v > 1) row[side] = v;
      }
      if (Object.keys(row).length) venue_odds[venue] = row;
    }
    setEvaluating(true);
    try {
      setEvaluation(await evaluateSelection({ sport_key: activeSport, home_stats: statsOf(home), away_stats: statsOf(away), context: contextOf(), selection, venue_odds,
        bankroll_inr: num(bankroll) > 0 ? num(bankroll) : 100000 }));
    } catch (e) {
      toast.error('Evaluation failed', failure(e));
    } finally {
      setEvaluating(false);
    }
  };

  const saveConfig = async (body: Parameters<typeof updateSportConfig>[1]) => {
    try {
      await updateSportConfig(activeSport, body);
      setKellyDraft(null);
      invalidate('sports:config');
      toast.success(`${info.name} configuration saved`);
    } catch (e) {
      toast.error('Configuration not saved', failure(e));
    }
  };

  const markets = useMemo(() => {
    const rows = Object.entries(result?.market_probabilities ?? {});
    return marketFilter === 'all' ? rows : rows.filter(([k]) => k.startsWith(marketFilter));
  }, [result, marketFilter]);
  const marketFamilies = useMemo(() => {
    const families = new Set(Object.keys(result?.market_probabilities ?? {}).map((k) => k.split('_')[0]));
    return ['all', ...Array.from(families).slice(0, 7)];
  }, [result]);
  const sides: Side[] = adapter?.has_draw ? ['home', 'draw', 'away'] : ['home', 'away'];
  const sideLabel = (s: Side): string => (s === 'home' ? preset.homeLabel : s === 'away' ? preset.awayLabel : 'Draw');
  const sportEdges = edges.data?.edges ?? [];

  return (
    <>
      <Panel title="Sport fleet" icon="sports" className="lg:col-span-12"
        subtitle={`${supported.data?.length ?? 9} sport adapters · ${adapter?.bet_types.length ?? 0} of the 27 universal bet types on ${info.name} · ${sportEdges.length} edge${sportEdges.length === 1 ? '' : 's'} · BetDoc quantitative fleet`}
        actions={<Button icon="refresh" busy={edges.loading && edges.data !== undefined} onClick={() => { void edges.refresh(); void configs.refresh(); }}>Refresh</Button>}>
        <UniversalSportSelector inactive={inactive} />
        <p className="mt-3 text-[11px] text-stone-500 dark:text-stone-400">
          Each sport prices from independent sub-models blended into one consensus, settles all 27 universal bet types, and feeds the
          Digital Twin's pillars before anything reaches the CFO. Architect: {credit}.
        </p>
      </Panel>

      <Panel title="Simulation parameters" icon="tune" className="lg:col-span-5" subtitle={`${info.name} · quantitative parameters`}
        actions={<Button variant="primary" icon="calculate" busy={pricing} disabled={!valid || inactive.has(activeSport)} onClick={() => void price()}>Price match</Button>}>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3">
          <span className="text-xs font-semibold uppercase tracking-wide text-stone-400">{preset.homeLabel}</span>
          <span className="text-xs font-semibold uppercase tracking-wide text-stone-400">{preset.awayLabel}</span>
          {preset.stats.map((f) => (
            <div key={f.key} className="contents">
              <Field label={f.label}><NumberInput step={f.step} value={home[f.key] ?? ''} onChange={(e) => setHome((v) => ({ ...v, [f.key]: e.target.value }))} /></Field>
              <Field label={f.label}><NumberInput step={f.step} value={away[f.key] ?? ''} onChange={(e) => setAway((v) => ({ ...v, [f.key]: e.target.value }))} /></Field>
            </div>
          ))}
          {preset.context.map((c) => (
            <Field key={c.key} label={c.label}>
              <Select value={ctx[c.key] ?? c.value} onChange={(e) => setCtx((v) => ({ ...v, [c.key]: e.target.value }))}>
                {c.options.map((o) => <option key={o} value={o}>{humanize(o)}</option>)}
              </Select>
            </Field>
          ))}
        </div>
        {inactive.has(activeSport) && <p className="text-xs text-amber-600 dark:text-amber-300">The desk has deactivated {info.name}: pricing is refused until an administrator switches it back on.</p>}
      </Panel>

      <Panel title="Fair prices" icon="query_stats" className="lg:col-span-7" subtitle={result ? `${result.sub_models.length} sub-models · ${result.latency_ms.toFixed(1)} ms` : 'Price a match to see the consensus'}>
        {result ? (
          <div className="flex flex-col gap-6">
            <StatGrid cols={sides.length === 3 ? 3 : 2}>
              {sides.map((s) => (
                <Stat key={s} label={sideLabel(s)} icon={s === 'draw' ? 'drag_handle' : 'flag'} value={pct(s === 'home' ? result.home_win_prob : s === 'away' ? result.away_win_prob : result.draw_prob)}
                  hint={`fair ${formatOdds(s === 'home' ? result.fair_home_odds : s === 'away' ? result.fair_away_odds : result.fair_draw_odds ?? 0)}`} />
              ))}
            </StatGrid>
            <KeyValues items={[
              { label: 'Expected total', value: result.expected_total_score.toFixed(2) },
              { label: 'Consensus agreement', value: pct(result.consensus.agreement ?? 0) },
              { label: 'Kelly multiplier (FA-4)', value: result.kelly_multiplier.toFixed(2) },
              { label: 'Evidence fed', value: Object.entries(result.evidence).filter(([, v]) => v).map(([k]) => humanize(k)).join(', ') || 'none' },
            ]} />
            <div className="flex flex-col gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-stone-400">Pillar 1 · sub-model votes</span>
              {result.sub_models.map((m) => (
                <div key={m.name} className="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-3 text-xs">
                  <span className="truncate font-medium text-stone-600 dark:text-stone-300" title={m.name}>{humanize(m.name)}</span>
                  <Meter value={m.home_win_prob} label={`${m.name} ${preset.homeLabel}`} />
                  <span className="font-mono text-stone-500">{pct(m.home_win_prob)} · w {m.weight.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <EmptyState icon="query_stats" title="No price yet" detail="Set both sides' inputs and price the match." />
        )}
      </Panel>

      {result && (
        <Panel title="Quantitative factors" icon="analytics" className="lg:col-span-7">
          <SportSpecificStatCard sportKey={activeSport} metrics={result.sport_specific_metrics} exclude={['consensus']} />
        </Panel>
      )}

      {result && (
        <Panel title="Line markets" icon="table_rows" className="lg:col-span-5" subtitle={`${markets.length} priced from the model's distribution`}
          actions={<Segmented size="sm" label="Market family" value={marketFilter} onChange={setMarketFilter} options={marketFamilies.map((f) => ({ value: f, label: humanize(f) }))} />}>
          <ul className="flex max-h-[520px] flex-col divide-y divide-stone-100 overflow-y-auto pr-1 dark:divide-white/[0.04]">
            {markets.map(([k, p]) => (
              <li key={k} className="flex items-center justify-between gap-3 py-2 text-xs">
                <span className="truncate text-stone-600 dark:text-stone-300" title={k}>{marketLabel(k)}</span>
                <span className="shrink-0 font-mono text-stone-800 dark:text-stone-100">{pct(p)} <span className="text-stone-400">@ {p > 0 ? formatOdds(1 / p) : '—'}</span></span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel title="Twin pipeline" icon="verified" className="lg:col-span-7" subtitle="Pillar 1 · Pillar 9 · OmniRoute · confidence · Kelly"
        actions={<Button variant="primary" icon="rule" busy={evaluating} disabled={!valid || inactive.has(activeSport)} onClick={() => void evaluate()}>Evaluate</Button>}>
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-3">
            <Segmented label="Selection" value={selection} onChange={setSelection} options={sides.map((s) => ({ value: s, label: sideLabel(s) }))} />
            <Field label="Bankroll (₹)" className="w-40"><NumberInput value={bankroll} onChange={(e) => setBankroll(e.target.value)} /></Field>
          </div>
          <div className="grid grid-cols-[6rem_repeat(3,minmax(0,1fr))] items-center gap-2 text-xs">
            <span />
            {(['home', 'away', 'draw'] as const).map((s) => <span key={s} className="font-semibold uppercase tracking-wide text-stone-400">{sideLabel(s)}</span>)}
            {OMNI_VENUES.map((venue) => (
              <div key={venue} className="contents">
                <span className="font-medium capitalize text-stone-600 dark:text-stone-300">{venue}</span>
                {(['home', 'away', 'draw'] as const).map((s) => (
                  <TextInput key={s} inputMode="decimal" placeholder="—" aria-label={`${venue} ${s} odds`} disabled={s === 'draw' && !adapter?.has_draw}
                    value={quotes[venue][s] ?? ''} onChange={(e) => setQuotes((q) => ({ ...q, [venue]: { ...q[venue], [s]: e.target.value } }))} />
                ))}
              </div>
            ))}
          </div>
          {evaluation && (
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wide text-stone-400">Confidence index</span>
                  <Pill tone={evaluation.confidence.grade === 'A' ? 'good' : evaluation.confidence.grade === 'D' ? 'critical' : 'warning'}>{evaluation.confidence.grade} · {humanize(evaluation.confidence.recommendation)}</Pill>
                </div>
                <span className="font-mono text-3xl font-semibold text-stone-900 dark:text-stone-100">{evaluation.confidence.score.toFixed(1)}</span>
                <Meter value={evaluation.confidence.score / 100} tone={CONFIDENCE_TONE[evaluation.confidence.grade] ?? 'accent'} label="confidence" />
                <KeyValues items={Object.entries(evaluation.confidence.components).map(([k, v]) => ({ label: humanize(k), value: pct(v, 0) }))} />
              </div>
              <div className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 text-xs dark:bg-white/[0.03]">
                <KeyValues items={[
                  { label: 'Fair probability', value: `${pct(evaluation.fair_probability)} (fair ${formatOdds(evaluation.fair_odds)})` },
                  { label: 'Pillar 1', value: <Pill tone={evaluation.pillar_1_consensus.status === 'PASS' ? 'good' : 'critical'}>{evaluation.pillar_1_consensus.status}</Pill> },
                  { label: 'Consensus EV', value: pct(evaluation.pillar_1_consensus.consensus_ev, 2) },
                  { label: 'Pillar 9 benchmark', value: evaluation.pillar_9_sharp_benchmark ? `${evaluation.pillar_9_sharp_benchmark.book} ${pct(evaluation.pillar_9_sharp_benchmark.fair_probabilities[selection])}` : 'no sharp market' },
                  { label: 'Route', value: evaluation.route ? `${evaluation.route.venue} @ ${formatOdds(evaluation.route.odds)}` : 'no quotes' },
                  { label: 'Edge', value: evaluation.edge_vs_model === null ? '—' : pct(evaluation.edge_vs_model, 2) },
                  { label: 'Kelly stake', value: evaluation.sizing ? `${formatINR(Number(evaluation.sizing.stake_inr))} (${evaluation.sizing.kelly_multiplier.toFixed(2)} Kelly, ${humanize(evaluation.sizing.regime)})` : 'no edge: no stake' },
                ]} />
                <p className="text-stone-500 dark:text-stone-400">{evaluation.pillar_1_consensus.reason}{evaluation.route ? ` · ${evaluation.route.reason}` : ''}</p>
              </div>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Cross-provider edges" icon="bolt" className="lg:col-span-5" updatedAt={edges.updatedAt}
        subtitle={edges.data?.screened_at ? `screened ${formatAgo(edges.data.screened_at)}` : 'the beat screens every 30 s for +EV value lines'}>
        <Async resource={edges} isEmpty={(d) => d.edges.length === 0}
          empty={<EmptyState icon="bolt" title={`No ${info.name} edges right now`} detail={edges.data?.available === false ? 'Redis is unreachable from the API.' : 'Value lines appear here once the boards show one.'} />}>
          {(d) => (
            <ul className="flex max-h-[520px] flex-col gap-2 overflow-y-auto pr-1">
              {d.edges.map((e, i) => (
                <li key={`${e.event_id}:${e.kind}:${e.venue ?? i}:${e.outcome ?? ''}`} className="rounded-2xl bg-stone-50 p-3 text-xs dark:bg-white/[0.03]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-semibold text-stone-800 dark:text-stone-100">{e.home} v {e.away}</span>
                    <Pill tone="info">+{e.edge_pct?.toFixed(2)}%</Pill>
                  </div>
                  <p className="mt-1 text-stone-500 dark:text-stone-400">
                    {`${humanize(e.outcome ?? '')} at ${e.venue} @ ${formatOdds(e.odds)} vs ${e.benchmark}`}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>

      <Panel title="Leagues" icon="emoji_events" className="lg:col-span-7" updatedAt={leagues.updatedAt}>
        <Async resource={leagues} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="emoji_events" title="No leagues registered" />}>
          {(rows) => (
            <ul className="grid gap-2 sm:grid-cols-2">
              {rows.map((l) => (
                <li key={l.id} className="flex items-center justify-between gap-2 rounded-2xl bg-stone-50 px-3 py-2 text-xs dark:bg-white/[0.03]">
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-stone-800 dark:text-stone-100">{l.display_name}</span>
                    <span className="text-stone-400">{l.country} · tier {l.tier}{l.odds_api_sport_key ? '' : ' · no feed'}</span>
                  </span>
                  {!l.is_active && <Pill tone="neutral">off</Pill>}
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>

      <Panel title="Desk configuration" icon="settings" className="lg:col-span-5" updatedAt={configs.updatedAt}>
        {config ? (
          <div className="flex flex-col gap-4 text-xs">
            <KeyValues items={[
              { label: 'Primary feed', value: config.primary_odds_api_sport_key },
              { label: 'Liquidity floor', value: formatINR(config.min_market_liquidity) },
              { label: 'Markets', value: config.supported_markets.map(humanize).join(', ') },
              { label: 'Bet types', value: `${config.supported_bet_types.length} of 27` },
              { label: 'Updated', value: formatAgo(config.updated_at) },
            ]} />
            {isAdmin ? (
              <div className="flex flex-wrap items-end gap-3">
                <div className="flex items-center gap-2">
                  <Toggle checked={config.is_active} label={`${info.name} active`} onChange={(next) => void saveConfig({ is_active: next })} />
                  <span className="text-stone-500">{config.is_active ? 'Trading' : 'Deactivated'}</span>
                </div>
                <Field label="Kelly fraction" className="w-32"><NumberInput step={0.01} value={kellyValue} onChange={(e) => setKellyDraft(e.target.value)} /></Field>
                <Button icon="save" disabled={!(num(kellyValue) >= 0.01 && num(kellyValue) <= 1)} onClick={() => void saveConfig({ default_kelly_fraction: num(kellyValue) })}>Save</Button>
              </div>
            ) : (
              <p className="text-stone-400">Kelly fraction {config.default_kelly_fraction.toFixed(2)} · administrators change the desk's settings.</p>
            )}
          </div>
        ) : (
          <Async resource={configs}>{() => <EmptyState icon="settings" title="No configuration" />}</Async>
        )}
      </Panel>
    </>
  );
};
