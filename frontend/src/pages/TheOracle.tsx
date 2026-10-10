import { useMemo, useState } from 'react';
import { apiClient } from '../api/client';
import { type MatchOdds, type Side, bestPrices, matchOddsMarket, outcomeSide, useControls, useDashboardSummary, useLiveOdds } from '../lib/api';
import { downloadCsv, formatDateTime, formatINR, formatOdds, formatPct, formatRatioPct } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { useExecutionStore } from '../store/useExecutionStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { CashoutPanel, MyBets, ScorecardStrip, TrendingFeed, TwinPanel, VettedSlips } from '../components/oracle/AshokaDesk';
import { TwinFortressPanel } from '../components/oracle/DigitalTwinCard';
import { SettlementFeedbackPanel } from '../components/oracle/SettlementFeedbackPanel';
import { ModelRecalibrationPanel } from '../components/oracle/ModelRecalibrationPanel';
import { NeverForgetLearningJournal } from '../components/oracle/NeverForgetLearningJournal';
import { ManualParlayWorkbench } from '../components/parlay/ManualParlayWorkbench';
import { ashokaHeadline, useSlips } from '../lib/oracle';
import { Async, Button, EmptyState, Field, Meter, NumberInput, Page, Panel, Pill, Select, Stat, StatGrid, StatusBadge, num } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface ValueBetFlag {
  selection: Side;
  true_prob: number;
  bookmaker_odds: number;
  expected_value: number;
  kelly_stake_fraction: number;
  leg_id: string;
  match_id: string;
  market_type: string;
}

interface EnginePrediction {
  prediction: { home_win_prob: number; draw_prob: number; away_win_prob: number; most_likely_scoreline: string; confidence_score: number };
  value_bets: ValueBetFlag[];
}

interface RiskMetrics {
  current_drawdown: number;
  exposure_pct: number;
  win_rate: number;
  [key: string]: number;
}

interface OracleResponse {
  suggestions: { structure: Record<string, unknown>; total_ev_pct: number; capital_allocated: number; rationale: string }[];
  strategy_used: string;
  risk_temperature: number;
  total_capital_deployed: number;
}


const SIDES: readonly Side[] = ['HOME', 'DRAW', 'AWAY'];

// ---------------------------------------------------------------------------
// MARKET CONSENSUS MODEL
// Vig-free average of every bookmaker's implied probability = the market's "true" price.
// A value bet is a side where the best available price beats that consensus.
// ---------------------------------------------------------------------------
interface ConsensusRow {
  match: MatchOdds;
  fair: Partial<Record<Side, number>>;
  best: ReturnType<typeof bestPrices>;
  books: number;
}

function consensus(match: MatchOdds): ConsensusRow {
  const sums: Record<Side, number> = { HOME: 0, DRAW: 0, AWAY: 0 };
  let books = 0;
  for (const book of match.bookmakers) {
    const h2h = matchOddsMarket(book);
    if (!h2h) continue;
    const implied: Partial<Record<Side, number>> = {};
    for (const o of h2h.outcomes) {
      const side = outcomeSide(o.name, match);
      if (side && o.price > 1) implied[side] = 1 / o.price;
    }
    const overround = Object.values(implied).reduce((a, b) => a + (b ?? 0), 0);
    if (overround <= 0) continue;
    books += 1;
    for (const side of SIDES) sums[side] += (implied[side] ?? 0) / overround;
  }
  const fair: Partial<Record<Side, number>> = {};
  if (books > 0) for (const side of SIDES) if (sums[side] > 0) fair[side] = sums[side] / books;
  return { match, fair, best: bestPrices(match), books };
}

function valueBets(rows: ConsensusRow[], kellyFraction: number): (ValueBetFlag & { label: string; bookmaker: string })[] {
  const out: (ValueBetFlag & { label: string; bookmaker: string })[] = [];
  for (const row of rows) {
    if (row.books < 2) continue; // a single book has no consensus to beat
    for (const side of SIDES) {
      const p = row.fair[side];
      const best = row.best[side];
      if (!p || !best) continue;
      const ev = p * best.price - 1;
      if (ev <= 0) continue;
      const b = best.price - 1;
      const kelly = Math.max(0, ((p * b - (1 - p)) / b) * kellyFraction);
      out.push({
        selection: side,
        true_prob: p,
        bookmaker_odds: best.price,
        expected_value: ev,
        kelly_stake_fraction: kelly,
        leg_id: `${row.match.id}:${side}`,
        match_id: row.match.id,
        market_type: 'MATCH_WINNER_1X2',
        label: `${row.match.home_team} v ${row.match.away_team}`,
        bookmaker: best.bookmaker,
      });
    }
  }
  return out.sort((a, b) => b.expected_value - a.expected_value);
}

// ---------------------------------------------------------------------------
// PANELS
// ---------------------------------------------------------------------------
const ProbabilityBar = ({ fair }: { fair: Partial<Record<Side, number>> }) => {
  const h = (fair.HOME ?? 0) * 100;
  const d = (fair.DRAW ?? 0) * 100;
  const a = (fair.AWAY ?? 0) * 100;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-stone-900/[0.06] dark:bg-white/[0.07]">
        <div style={{ width: `${h}%`, background: 'var(--viz-series-1)' }} />
        <div style={{ width: `${d}%` }} className="bg-stone-300 dark:bg-stone-600" />
        <div style={{ width: `${a}%`, background: 'var(--viz-series-2)' }} />
      </div>
      <div className="flex justify-between text-[10px] tabular-nums text-stone-500 dark:text-stone-400">
        <span>H {h.toFixed(1)}%</span>
        {d > 0 && <span>D {d.toFixed(1)}%</span>}
        <span>A {a.toFixed(1)}%</span>
      </div>
    </div>
  );
};

const EnginePanel = ({ rows }: { rows: ConsensusRow[] }) => {
  const setDraft = useExecutionStore((s) => s.setDraft);
  const [matchId, setMatchId] = useState('');
  const [homeElo, setHomeElo] = useState('');
  const [awayElo, setAwayElo] = useState('');
  const [homeXg, setHomeXg] = useState('');
  const [awayXg, setAwayXg] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<EnginePrediction | null>(null);

  const row = rows.find((r) => r.match.id === matchId) ?? rows[0];
  const hasInputs = [homeElo, awayElo].every((v) => Number.isFinite(num(v))) || [homeXg, awayXg].every((v) => Number.isFinite(num(v)));

  const run = async () => {
    if (!row) return;
    setBusy(true);
    const odds = Object.fromEntries(SIDES.filter((s) => row.best[s]).map((s) => [s, row.best[s]!.price]));
    const body = {
      home_team: row.match.home_team,
      away_team: row.match.away_team,
      home_elo: Number.isFinite(num(homeElo)) ? num(homeElo) : null,
      away_elo: Number.isFinite(num(awayElo)) ? num(awayElo) : null,
      home_xg: Number.isFinite(num(homeXg)) ? num(homeXg) : null,
      away_xg: Number.isFinite(num(awayXg)) ? num(awayXg) : null,
      bookmaker_odds: odds,
      match_id: row.match.id,
    };
    const res = await runMutation(() => apiClient.post<EnginePrediction>('/engine/predict', body), { errorTitle: 'Engine prediction failed' });
    setBusy(false);
    if (res) setResult(res);
  };

  return (
    <Panel title="PANINI ensemble · on demand" icon="psychology" className="lg:col-span-5" subtitle="Poisson + Dixon-Coles + Elo">
      {rows.length === 0 ? (
        <EmptyState icon="psychology" title="No fixtures to model" detail="The ensemble needs a live fixture plus Elo ratings or expected goals." />
      ) : (
        <div className="flex flex-col gap-4">
          <Field label="Fixture">
            <Select value={row?.match.id ?? ''} onChange={(e) => { setMatchId(e.target.value); setResult(null); }}>
              {rows.map((r) => (
                <option key={r.match.id} value={r.match.id}>{r.match.home_team} v {r.match.away_team}</option>
              ))}
            </Select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Home Elo"><NumberInput value={homeElo} onChange={(e) => setHomeElo(e.target.value)} placeholder="1650" /></Field>
            <Field label="Away Elo"><NumberInput value={awayElo} onChange={(e) => setAwayElo(e.target.value)} placeholder="1580" /></Field>
            <Field label="Home xG"><NumberInput value={homeXg} onChange={(e) => setHomeXg(e.target.value)} placeholder="1.6" /></Field>
            <Field label="Away xG"><NumberInput value={awayXg} onChange={(e) => setAwayXg(e.target.value)} placeholder="1.1" /></Field>
          </div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">BetDoc stores no team ratings, so the ensemble runs on the Elo and/or xG you supply here, priced against the live best odds.</p>
          <Button variant="primary" icon="play_arrow" busy={busy} disabled={!hasInputs} onClick={() => void run()}>Run ensemble</Button>
          {result && row && (
            <div className="flex flex-col gap-3 rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
              <ProbabilityBar fair={{ HOME: result.prediction.home_win_prob, DRAW: result.prediction.draw_prob, AWAY: result.prediction.away_win_prob }} />
              <p className="text-xs text-stone-500 dark:text-stone-400">
                Most likely {result.prediction.most_likely_scoreline} · confidence {formatRatioPct(result.prediction.confidence_score)}
              </p>
              {result.value_bets.length === 0 ? (
                <p className="text-xs text-stone-500">No side clears the value threshold at current prices.</p>
              ) : (
                result.value_bets.map((v) => (
                  <div key={v.leg_id} className="flex items-center justify-between gap-2">
                    <span className="text-sm text-stone-700 dark:text-stone-200">
                      {v.selection} @ {formatOdds(v.bookmaker_odds)} · EV {formatPct(v.expected_value * 100, 2)}
                    </span>
                    <Button size="sm" icon="receipt_long" onClick={() => setDraft({ matchId: row.match.id, selection: v.selection, odds: v.bookmaker_odds, trueProbability: v.true_prob, label: `${row.match.home_team} v ${row.match.away_team}`, source: 'Oracle · ensemble' })}>
                      Stage
                    </Button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
};

const GoldenAlpha = ({ bets }: { bets: ValueBetFlag[] }) => {
  const summary = useDashboardSummary();
  const controls = useControls();
  const bankroll = summary.data?.total_bankroll ?? 0;
  const risk = useResource(bankroll > 0 ? `capital:risk:${Math.round(bankroll)}` : null, () => apiClient.get<RiskMetrics>('/capital/risk', { bankroll }));
  const [result, setResult] = useState<OracleResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const allocate = async () => {
    if (!risk.data || bankroll <= 0) return;
    setBusy(true);
    const res = await runMutation(
      () =>
        apiClient.post<OracleResponse>('/oracle/suggest', {
          available_value_bets: bets.slice(0, 200).map(({ selection, true_prob, bookmaker_odds, expected_value, kelly_stake_fraction, leg_id, match_id, market_type }) => ({
            selection, true_prob, bookmaker_odds, expected_value, kelly_stake_fraction, leg_id, match_id, market_type,
          })),
          risk_metrics: risk.data,
          strategy_params: {
            max_allowed_drawdown: 0.2,
            target_win_rate: 0.5,
            base_kelly_fraction: controls.data?.default_kelly_fraction || 0.25,
            max_legs_per_combination: 3,
          },
          bankroll,
          max_exposure_pct: Math.min(100, ((controls.data?.max_daily_exposure ?? bankroll * 0.2) / bankroll) * 100),
        }),
      { errorTitle: 'Allocation failed' },
    );
    setBusy(false);
    if (res) setResult(res);
  };

  return (
    <Panel title="Golden alpha · portfolio" icon="workspace_premium" className="lg:col-span-7" subtitle="ASHOKA core-satellite allocator">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-stone-600 dark:text-stone-300">
            Allocates {bets.length} value bet{bets.length === 1 ? '' : 's'} against your bankroll ({formatINR(bankroll)}), live drawdown and the Control Panel exposure cap.
          </p>
          <Button variant="primary" icon="auto_awesome" busy={busy} disabled={bets.length === 0 || !risk.data || bankroll <= 0} onClick={() => void allocate()}>
            Build portfolio
          </Button>
        </div>
        {bankroll <= 0 && summary.data && <p className="text-xs text-amber-600">The allocator needs a positive bankroll (from your risk mandate).</p>}
        {result && (
          <div className="flex flex-col gap-3">
            <StatGrid cols={3}>
              <Stat label="Strategy" value={<span className="text-base">{result.strategy_used.replace(/_/g, ' ')}</span>} />
              <Stat label="Risk temperature" value={result.risk_temperature.toFixed(2)} />
              <Stat label="Capital deployed" value={formatINR(result.total_capital_deployed)} />
            </StatGrid>
            {result.suggestions.length === 0 ? (
              <EmptyState icon="shield" title="No allocation" detail="Risk limits or drawdown kept capital on the bench." />
            ) : (
              <ul className="flex flex-col gap-2">
                {result.suggestions.map((s, i) => (
                  <li key={i} className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                    <div className="flex items-center justify-between gap-2">
                      <Pill tone="accent">{String(s.structure.bet_type ?? s.structure.type ?? 'structure')}</Pill>
                      <span className="text-sm font-semibold tabular-nums text-emerald-600 dark:text-emerald-400">EV {formatPct(s.total_ev_pct, 2)} · {formatINR(s.capital_allocated)}</span>
                    </div>
                    <p className="mt-1.5 text-xs leading-relaxed text-stone-500 dark:text-stone-400">{s.rationale}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ORACLE
// ---------------------------------------------------------------------------
export const TheOracle = () => {
  const odds = useLiveOdds();
  const controls = useControls();
  const setDraft = useExecutionStore((s) => s.setDraft);
  const kellyFraction = controls.data?.default_kelly_fraction || 0.25;

  const rows = useMemo(() => (odds.data ?? []).map(consensus).filter((r) => r.books > 0), [odds.data]);
  const bets = useMemo(() => valueBets(rows, kellyFraction), [rows, kellyFraction]);
  const best = bets[0];

  const summary = useDashboardSummary();
  const slips = useSlips(summary.data?.total_bankroll ? Math.round(summary.data.total_bankroll) : null);  // stakes sized on the bankroll the header shows
  const legacy = best
    ? `ASHOKA ACTIVE. ${bets.length} value bet${bets.length === 1 ? '' : 's'} across ${rows.length} fixtures. Best edge ${formatPct(best.expected_value * 100, 2)}.`
    : `ASHOKA ACTIVE. ${rows.length} fixtures priced; no side beats the consensus right now.`;

  return (
    <Page>
      <CommanderHero
        commander="ASHOKA"
        headline={slips.data ? ashokaHeadline(slips.data) : legacy}
        motif={MOTIFS.constellation}
        detail="Every slip runs 10,000 Monte Carlo paths through Poisson, Dixon-Coles and the de-vigged market, then the anti-correlation gate and the 1000% filter (joint EV ≥ +7.5% and at least 55% likely). Parimatch and 1xBet side by side; odds only from authorised feeds or what you type in."
        actions={
          <>
            <Button variant="primary" icon="refresh" busy={(odds.loading && odds.data !== undefined) || (slips.loading && slips.data !== undefined)} onClick={() => { void odds.refresh(); void slips.refresh(); }}>Recompute</Button>
            <Button
              icon="download"
              disabled={bets.length === 0}
              onClick={() =>
                downloadCsv(`betdoc-value-bets-${new Date().toISOString().slice(0, 10)}.csv`, bets.map((b) => ({
                  fixture: b.label, match_id: b.match_id, selection: b.selection, bookmaker: b.bookmaker, odds: b.bookmaker_odds,
                  fair_probability: b.true_prob.toFixed(4), expected_value: b.expected_value.toFixed(4), kelly_fraction: b.kelly_stake_fraction.toFixed(4),
                })))
              }
            >
              Export value bets
            </Button>
          </>
        }
      />

      <ScorecardStrip />
      <VettedSlips slips={slips} />
      <TwinFortressPanel />
      <SettlementFeedbackPanel />
      <ModelRecalibrationPanel />
      <NeverForgetLearningJournal />
      <ManualParlayWorkbench />
      <CashoutPanel />
      <TrendingFeed />
      <MyBets />
      <TwinPanel />

      <Panel title="Match predictor matrix" icon="query_stats" className="lg:col-span-7" updatedAt={odds.updatedAt} subtitle="market consensus">
        <Async resource={odds} skeletonRows={5} isEmpty={() => rows.length === 0} empty={<EmptyState icon="query_stats" title="No priced fixtures" detail="Predictions appear once the odds poller has stored bookmaker prices." />}>
          {() => (
            <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {rows.slice(0, 12).map((r) => (
                <li key={r.match.id} className="flex flex-col gap-2 rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{r.match.home_team} v {r.match.away_team}</p>
                      <p className="text-[11px] text-stone-400">{formatDateTime(r.match.commence_time)} · {r.books} books</p>
                    </div>
                    <StatusBadge status={r.books >= 3 ? 'STABLE' : 'LOW'} label={r.books >= 3 ? 'Deep' : 'Thin'} />
                  </div>
                  <ProbabilityBar fair={r.fair} />
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>

      <Panel title="Value bets" icon="paid" className="lg:col-span-5" subtitle={`fractional Kelly ×${kellyFraction}`}>
        {bets.length === 0 ? (
          <EmptyState icon="paid" title="No value right now" detail="No best price currently beats the vig-free consensus." />
        ) : (
          <ul className="flex max-h-[520px] flex-col gap-2.5 overflow-y-auto">
            {bets.slice(0, 15).map((b) => (
              <li key={b.leg_id} className="rounded-xl bg-stone-50 p-3 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{b.label}</span>
                  <Pill tone="good" icon="trending_up">EV {formatPct(b.expected_value * 100, 2)}</Pill>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 text-[11px] text-stone-500 dark:text-stone-400">
                  <span>{b.selection} @ <strong className="text-stone-800 dark:text-stone-100">{formatOdds(b.bookmaker_odds)}</strong></span>
                  <span>fair {formatRatioPct(b.true_prob)}</span>
                  <span>{b.bookmaker}</span>
                </div>
                <div className="mt-2 flex items-center gap-3">
                  <Meter value={Math.min(1, b.kelly_stake_fraction * 10)} label="Kelly stake" />
                  <span className="shrink-0 text-[11px] tabular-nums text-stone-500">{formatRatioPct(b.kelly_stake_fraction, 2)} of bank</span>
                </div>
                <Button size="sm" variant="ghost" icon="receipt_long" className="mt-1.5 -ml-2" onClick={() => setDraft({ matchId: b.match_id, selection: b.selection, odds: b.bookmaker_odds, trueProbability: b.true_prob, label: b.label, source: `Oracle · ${b.bookmaker}` })}>
                  Execute Oracle bet
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <GoldenAlpha bets={bets} />
      <EnginePanel rows={rows} />
    </Page>
  );
};
