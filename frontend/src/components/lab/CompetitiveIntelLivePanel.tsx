/**
 * Group 85: live competitive market intelligence (Commanders KARNA & PANINI).
 * Parimatch and 1xBet against the zero-vig consensus on the live board, public ticket and money shares where they
 * are measured, reverse line movement, measured bookmaker margins, and the public trap radar.
 * Architect & Developer: Amit Ashok Kumar Patnaik
 *
 * Nothing on this panel is seeded: a share nobody measured reads "not measured", a USD figure needs a fresh rate.
 */
import { useState } from 'react';
import { apiClient } from '../../api/client';
import { formatINR, formatOdds, formatPct } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { useAuthStore } from '../../store/useAuthStore';
import { type Selection, useExecutionStore } from '../../store/useExecutionStore';
import { Async, Button, EmptyState, NumberInput, Panel, Pill, Select, TextInput, Toggle } from '../../ui/kit';
import { num } from '../../ui/num';

export type ConsensusVerdict = 'SHARP_CONTRARIAN' | 'PUBLIC_TRAP' | 'SHARP_CONSENSUS' | 'NEUTRAL';
export type SignalVerdict = 'PUBLIC_TRAP' | 'SHARP_VERIFIED' | 'CAUTION_HIGH_VIG' | 'NEUTRAL';

export interface PublicConsensusSnapshot {
  id: string;
  match_id: string;
  sport: string;
  sport_key: string | null;
  league: string | null;
  home_team: string;
  away_team: string;
  kickoff_at: string | null;
  market_type: string;
  selection: Selection;
  selection_label: string;
  public_ticket_pct: number | null;
  sharp_money_pct: number | null;
  public_source: string | null;
  divergence_pct: number | null;
  rlm_detected: boolean;
  opening_odds: number | null;
  parimatch_odds: number | null;
  one_xbet_odds: number | null;
  sharp_fair_odds: number | null;
  parimatch_margin_pct: number | null;
  one_xbet_margin_pct: number | null;
  best_bookmaker: string;
  best_odds: number;
  betdoc_true_prob: number;
  betdoc_ev_pct: number;
  public_trap_flag: boolean;
  contrarian_edge_score: number;
  verdict: ConsensusVerdict;
  plain_english_summary: string;
  recommended_stake_inr: number;
  recommended_stake_usd: number | null;
  source_provider: string;
  captured_at: string;
}

export interface BookmakerBenchmark {
  id: string;
  bookmaker_key: string;
  bookmaker_display: string;
  sport: string;
  market_type: string;
  measured_overround_pct: number;
  payout_efficiency_pct: number;
  best_price_share_pct: number | null;
  sample_size: number;
  updated_at: string;
}

export interface PublicTrapSignal {
  id: string;
  origin: 'DERIVED' | 'RECORDED';
  source_site: string;
  sport: string;
  match_id: string;
  match_title: string;
  public_selection: Selection;
  public_pick: string;
  public_backing_pct: number;
  betdoc_verdict: SignalVerdict;
  betdoc_recommended_pick: string;
  best_bookmaker: string | null;
  best_odds: number | null;
  betdoc_ev_pct: number | null;
  plain_english_rationale: string;
  stake_inr: number;
  stake_usd: number | null;
  recorded_by: string | null;
  created_at: string;
  graded_at: string;
}

const SPORTS_FILTER = ['All', 'Cricket', 'Football', 'Tennis', 'Basketball', 'MMA/UFC'] as const;
const BOOK_STYLE: Record<string, { backgroundColor: string; color: string }> = {
  parimatch: { backgroundColor: '#F8FF13', color: '#0D0E12' },
  '1xbet': { backgroundColor: '#007ACC', color: '#FFFFFF' },
  sharp_consensus: { backgroundColor: '#10B981', color: '#052E16' },
};
const VERDICT_TONE: Record<ConsensusVerdict | SignalVerdict, 'critical' | 'good' | 'warning' | 'neutral'> = {
  PUBLIC_TRAP: 'critical', SHARP_CONTRARIAN: 'good', SHARP_CONSENSUS: 'good', SHARP_VERIFIED: 'good', CAUTION_HIGH_VIG: 'warning', NEUTRAL: 'neutral',
};

const GLOSSARY_ITEMS = [
  { term: 'Public Ticket %', meaning: 'Share of bet slips on a side, as a named splits feed measured it (or BetDoc\'s own recorded bets). Over 70% often means an inflated price.' },
  { term: 'Sharp Money %', meaning: 'Share of the money staked on a side. Money well above tickets means a few large, usually professional, stakes.' },
  { term: 'RLM (Reverse Line Movement)', meaning: 'The crowd backs Team A, yet the price on A drifts out (or a side the crowd ignores is cut): the books are respecting bigger money elsewhere.' },
  { term: 'Bookmaker Overround (Margin)', meaning: 'The commission built into Parimatch\'s or 1xBet\'s prices, measured from their live odds. Lower margin, bigger payouts.' },
  { term: 'Public Trap', meaning: 'A crowd favourite worth less than its price (negative EV) or that the money avoids. Skip it, or take the other side when it has value.' },
] as const;

const pctOrDash = (v: number | null, digits = 1) => (v === null ? 'not measured' : `${v.toFixed(digits)}%`);
const label = (verdict: string) => verdict.replace(/_/g, ' ');

const ShareBar = ({ title, value, colour }: { title: string; value: number | null; colour: string }) => (
  <div className="rounded-xl bg-white p-2.5 dark:bg-stone-900/70">
    <div className="mb-1 flex justify-between text-xs">
      <span className="text-stone-500">{title}</span>
      <span className="font-mono font-bold">{pctOrDash(value)}</span>
    </div>
    <div className="h-2 overflow-hidden rounded-full bg-stone-200 dark:bg-stone-800">
      {value !== null && <div className={`h-full rounded-full ${colour}`} style={{ width: `${Math.min(100, value)}%` }} />}
    </div>
  </div>
);

const RecordPick = ({ rows }: { rows: PublicConsensusSnapshot[] }) => {
  const fixtures = Array.from(new Map(rows.map((r) => [r.match_id, `${r.home_team} v ${r.away_team}`])).entries());
  const [matchId, setMatchId] = useState('');
  const [selection, setSelection] = useState<Selection>('HOME');
  const [source, setSource] = useState('');
  const [backing, setBacking] = useState('');
  const [busy, setBusy] = useState(false);
  const share = num(backing);
  const valid = matchId !== '' && source.trim().length >= 2 && Number.isFinite(share) && share >= 0 && share <= 100;
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-2xl bg-stone-50 p-3 dark:bg-white/[0.03]">
      <span className="w-full text-[11px] font-semibold uppercase tracking-wider text-stone-500">Record a public pick from a named source (admin)</span>
      <Select value={matchId} onChange={(e) => setMatchId(e.target.value)} aria-label="Fixture" className="min-w-[12rem] flex-1">
        <option value="">Fixture…</option>
        {fixtures.map(([id, title]) => <option key={id} value={id}>{title}</option>)}
      </Select>
      <Select value={selection} onChange={(e) => setSelection(e.target.value as Selection)} aria-label="Selection" className="w-28">
        <option value="HOME">Home</option>
        <option value="DRAW">Draw</option>
        <option value="AWAY">Away</option>
      </Select>
      <TextInput value={source} onChange={(e) => setSource(e.target.value)} placeholder="Source (e.g. Covers)" aria-label="Source" className="w-44" />
      <NumberInput value={backing} onChange={(e) => setBacking(e.target.value)} placeholder="Public %" min={0} max={100} aria-label="Public backing percent" className="w-28" />
      <Button size="sm" icon="add" busy={busy} disabled={!valid} onClick={async () => {
        setBusy(true);
        await runMutation(() => apiClient.post('/rnd/competitive-intel/public-traps', { source_site: source.trim(), match_id: matchId, selection, public_backing_pct: share }),
          { invalidate: ['comp-intel'], success: 'Public pick recorded and graded', errorTitle: 'Could not record the pick' });
        setBusy(false);
      }}>Record</Button>
    </div>
  );
};

export const CompetitiveIntelLivePanel = () => {
  const [sport, setSport] = useState<string>('All');
  const [trapOnly, setTrapOnly] = useState(false);
  const [currency, setCurrency] = useState<'INR' | 'USD'>('INR');
  const [showGuide, setShowGuide] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');
  const setDraft = useExecutionStore((s) => s.setDraft);
  const sportQuery = sport !== 'All' ? { sport } : {};

  const consensus = useResource(`comp-intel:consensus:${sport}:${trapOnly ? 'trap' : 'all'}`, () =>
    apiClient.get<PublicConsensusSnapshot[]>('/rnd/competitive-intel/live-consensus', { ...sportQuery, ...(trapOnly ? { public_trap_only: true } : {}) }), { intervalMs: 120_000 });
  const benchmarks = useResource(`comp-intel:benchmarks:${sport}`, () =>
    apiClient.get<BookmakerBenchmark[]>('/rnd/competitive-intel/bookmaker-benchmarks', sportQuery), { intervalMs: 300_000 });
  const signals = useResource(`comp-intel:traps:${sport}`, () =>
    apiClient.get<PublicTrapSignal[]>('/rnd/competitive-intel/public-traps', sportQuery), { intervalMs: 120_000 });

  const money = (inr: number, usd: number | null) =>
    currency === 'INR' ? formatINR(inr) : usd === null ? '— (no fresh USD rate)' : `$${usd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const handleSync = async () => {
    setSyncing(true);
    await runMutation(() => apiClient.post('/rnd/competitive-intel/live-consensus/sync'), {
      invalidate: ['comp-intel'], success: 'KARNA re-read the live board: prices, margins, splits and public picks', errorTitle: 'Live intel sync failed',
    });
    await Promise.all([consensus.refresh(), benchmarks.refresh(), signals.refresh()]);
    setSyncing(false);
  };

  return (
    <Panel
      title="KARNA · Live Public vs. Sharp Money & Bookmaker Margin Intelligence"
      icon="radar"
      className="lg:col-span-12"
      subtitle="Cricket & Football first · Parimatch vs. 1xBet · Public Trap Radar · Architect: Amit Ashok Kumar Patnaik"
      updatedAt={consensus.updatedAt}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => setCurrency((c) => (c === 'INR' ? 'USD' : 'INR'))}
                  className="rounded-full border border-amber-500/40 bg-amber-500/10 px-3 py-1 font-mono text-xs font-bold text-amber-700 transition hover:bg-amber-500/20 dark:text-amber-300"
                  title="Primary INR (₹); USD ($) at TODAR MAL's fresh rate">
            {currency === 'INR' ? '₹ INR (primary) ⇄ $ USD' : '$ USD (secondary) ⇄ ₹ INR'}
          </button>
          <button type="button" onClick={() => setShowGuide((g) => !g)} className="rounded-full bg-stone-900/[0.05] px-3 py-1 text-xs font-medium text-stone-600 dark:bg-white/[0.06] dark:text-stone-300">
            {showGuide ? 'Hide quick guide' : 'Show quick guide'}
          </button>
          <Button size="sm" variant="primary" icon="sync" busy={syncing} onClick={() => void handleSync()}>Sync live intel</Button>
        </div>
      }
    >
      <div className="flex flex-col gap-6">
        {showGuide && (
          <div className="rounded-2xl border border-emerald-500/25 bg-emerald-500/[0.05] p-4 dark:bg-emerald-500/[0.06]">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-bold uppercase tracking-wider text-emerald-700 dark:text-emerald-300">Quick guide · this section in 3 steps</p>
              <span className="text-[11px] text-stone-500 dark:text-stone-400">1. Pick a sport → 2. Follow the money, avoid public traps → 3. Stage the better book's price</span>
            </div>
            <div className="mt-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-5">
              {GLOSSARY_ITEMS.map((g) => (
                <div key={g.term} className="rounded-xl bg-white/80 p-2.5 shadow-sm dark:bg-stone-900/70">
                  <p className="text-xs font-bold text-stone-900 dark:text-stone-100">{g.term}</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-stone-600 dark:text-stone-400">{g.meaning}</p>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="Sport filter">
            {SPORTS_FILTER.map((s) => (
              <button key={s} type="button" role="tab" aria-selected={sport === s} onClick={() => setSport(s)}
                      className={`rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${sport === s ? 'bg-stone-900 text-white dark:bg-amber-400 dark:text-stone-950' : 'bg-stone-100 text-stone-600 hover:bg-stone-200 dark:bg-white/[0.05] dark:text-stone-300'}`}>
                {s}
              </button>
            ))}
          </div>
          <Toggle label="Public traps only" checked={trapOnly} onChange={setTrapOnly} />
        </div>

        <div>
          <h3 className="mb-2.5 text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">1. Bookmaker margin & payout efficiency (Parimatch vs. 1xBet, measured)</h3>
          <Async resource={benchmarks} skeletonRows={2} isEmpty={(rows) => rows.length === 0}
                 empty={<EmptyState icon="percent" title="No margins measured yet" detail="Margins come from markets that Parimatch or 1xBet price in full on the live board." />}>
            {(rows) => (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                {rows.map((b) => (
                  <div key={b.id} className="flex flex-col justify-between gap-2 rounded-2xl border border-stone-200/70 bg-stone-50/80 p-3.5 dark:border-white/[0.07] dark:bg-white/[0.03]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="rounded-lg px-2.5 py-0.5 text-xs font-bold" style={BOOK_STYLE[b.bookmaker_key] ?? BOOK_STYLE.sharp_consensus}>{b.bookmaker_display}</span>
                      <Pill tone="neutral">{b.sport} · {b.sample_size} mkt{b.sample_size === 1 ? '' : 's'}</Pill>
                    </div>
                    <div className="mt-1 grid grid-cols-3 gap-2 text-center">
                      {[
                        ['Margin', formatPct(b.measured_overround_pct, 2), 'text-stone-800 dark:text-stone-100'],
                        ['Payout', formatPct(b.payout_efficiency_pct, 2), 'text-emerald-600 dark:text-emerald-400'],
                        ['Best price', b.best_price_share_pct === null ? '—' : formatPct(b.best_price_share_pct, 1), 'text-sky-600 dark:text-sky-400'],
                      ].map(([k, v, tone]) => (
                        <div key={k} className="rounded-xl bg-white p-2 dark:bg-stone-900/70">
                          <p className="text-[10px] text-stone-400">{k}</p>
                          <p className={`font-mono text-sm font-bold ${tone}`}>{v}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Async>
        </div>

        <div>
          <h3 className="mb-2.5 text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">2. Public ticket % vs. money % & reverse line movement</h3>
          <Async resource={consensus} skeletonRows={3} isEmpty={(rows) => rows.length === 0}
                 empty={<EmptyState icon="radar" title="Nothing on the board for this filter" detail="Fixtures appear once Parimatch or 1xBet price them on the live board." />}>
            {(rows) => (
              <div className="flex flex-col gap-4">
                {isAdmin && <RecordPick rows={rows} />}
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  {rows.map((r) => (
                    <div key={r.id} className={`flex flex-col gap-3 rounded-2xl border p-4 transition ${r.public_trap_flag ? 'border-rose-500/40 bg-rose-500/[0.04]' : 'border-stone-200/80 bg-stone-50/90 dark:border-white/[0.08] dark:bg-white/[0.03]'}`}>
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <Pill tone="accent">{r.sport}</Pill>
                            {r.league && <span className="truncate text-xs text-stone-500 dark:text-stone-400">{r.league}</span>}
                          </div>
                          <p className="mt-1 text-base font-bold text-stone-900 dark:text-stone-100">{r.home_team} v {r.away_team}</p>
                          <p className="text-xs font-semibold text-stone-600 dark:text-stone-300">Selection: <span className="text-amber-600 dark:text-amber-400">{r.selection_label}</span></p>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {r.rlm_detected && <Pill tone="warning">RLM</Pill>}
                          <Pill tone={VERDICT_TONE[r.verdict]}>{label(r.verdict)}</Pill>
                        </div>
                      </div>
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <ShareBar title="Public tickets" value={r.public_ticket_pct} colour="bg-amber-500" />
                        <ShareBar title="Money share" value={r.sharp_money_pct} colour="bg-emerald-500" />
                      </div>
                      {r.public_source && <p className="-mt-1 text-[10px] text-stone-400">Shares measured by {r.public_source}{r.opening_odds ? ` · opened ${formatOdds(r.opening_odds)}` : ''}</p>}
                      <div className="grid grid-cols-4 gap-2 text-center text-xs">
                        {[
                          ['Parimatch', r.parimatch_odds === null ? '—' : formatOdds(r.parimatch_odds), r.parimatch_margin_pct],
                          ['1xBet', r.one_xbet_odds === null ? '—' : formatOdds(r.one_xbet_odds), r.one_xbet_margin_pct],
                          ['Zero-vig', r.sharp_fair_odds === null ? '—' : formatOdds(r.sharp_fair_odds), null],
                        ].map(([k, v, m]) => (
                          <div key={k as string} className="rounded-xl bg-white p-2 dark:bg-stone-900/70">
                            <span className="block text-[10px] text-stone-400">{k}</span>
                            <span className="font-mono font-bold">{v}</span>
                            {m !== null && <span className="block text-[10px] text-stone-400">margin {(m as number).toFixed(2)}%</span>}
                          </div>
                        ))}
                        <div className="rounded-xl bg-white p-2 dark:bg-stone-900/70">
                          <span className="block text-[10px] text-stone-400">BetDoc EV</span>
                          <span className={`font-mono font-bold ${r.betdoc_ev_pct >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
                            {r.betdoc_ev_pct >= 0 ? '+' : ''}{r.betdoc_ev_pct.toFixed(1)}%
                          </span>
                        </div>
                      </div>
                      <p className="text-xs leading-relaxed text-stone-600 dark:text-stone-300">{r.plain_english_summary}</p>
                      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-stone-200/60 pt-2.5 dark:border-white/[0.06]">
                        <span className="text-xs text-stone-500">
                          Kelly stake: <strong className="font-mono text-stone-900 dark:text-stone-100">{money(r.recommended_stake_inr, r.recommended_stake_usd)}</strong>
                          {' '}· Edge score <strong className="font-mono">{r.contrarian_edge_score.toFixed(0)}/100</strong>
                        </span>
                        {!r.public_trap_flag && r.betdoc_ev_pct > 0 && (
                          <Button size="sm" variant="primary" icon="add_task" onClick={() => setDraft({
                            matchId: r.match_id, selection: r.selection, odds: r.best_odds, trueProbability: r.betdoc_true_prob,
                            label: `${r.home_team} v ${r.away_team} · ${r.selection_label}`, source: `KARNA live intel · ${r.best_bookmaker}`,
                          })}>
                            Stage @ {formatOdds(r.best_odds)} ({r.best_bookmaker})
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Async>
        </div>

        <div>
          <h3 className="mb-2.5 text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">3. The crowd's pick vs. BetDoc's verdict (public trap radar)</h3>
          <Async resource={signals} skeletonRows={2} isEmpty={(rows) => rows.length === 0}
                 empty={<EmptyState icon="groups" title="No crowd pick to grade" detail="A pick is graded once a splits feed measured the crowd on a fixture, or an administrator records one from a named source." />}>
            {(rows) => (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                {rows.map((sig) => (
                  <div key={sig.id} className="flex flex-col justify-between gap-2.5 rounded-2xl border border-stone-200/80 bg-white p-3.5 dark:border-white/[0.08] dark:bg-stone-900/60">
                    <div>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[11px] font-bold uppercase text-stone-400">{sig.source_site} · {sig.sport}{sig.origin === 'RECORDED' ? ' · recorded' : ''}</span>
                        <Pill tone={VERDICT_TONE[sig.betdoc_verdict]}>{label(sig.betdoc_verdict)}</Pill>
                      </div>
                      <p className="mt-1 text-sm font-bold text-stone-900 dark:text-stone-100">{sig.match_title}</p>
                      <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">Crowd pick ({sig.public_backing_pct.toFixed(0)}%): <strong>{sig.public_pick}</strong></p>
                      <p className="mt-0.5 text-xs text-emerald-700 dark:text-emerald-300">BetDoc: <strong>{sig.betdoc_recommended_pick}</strong>{sig.best_bookmaker ? ` (${sig.best_bookmaker})` : ''}</p>
                      <p className="mt-1.5 text-[11px] leading-relaxed text-stone-500 dark:text-stone-400">{sig.plain_english_rationale}</p>
                    </div>
                    <div className="flex items-center justify-between border-t border-stone-100 pt-2 text-[11px] text-stone-500 dark:border-white/[0.05]">
                      <span>Stake: {money(sig.stake_inr, sig.stake_usd)}</span>
                      {sig.best_odds !== null && <span className="font-mono font-semibold">Best odds {formatOdds(sig.best_odds)}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Async>
        </div>

        <p className="text-right text-[11px] text-stone-400">Engineered by Amit Ashok Kumar Patnaik · Commanders KARNA & PANINI · Primary INR (₹) / secondary USD ($)</p>
      </div>
    </Panel>
  );
};
