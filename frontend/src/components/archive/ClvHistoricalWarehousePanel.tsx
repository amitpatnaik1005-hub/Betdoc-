/**
 * Group 93: TODAR MAL's historical match, score & CLV warehouse. Architect: Amit Ashok Kumar Patnaik.
 * The signed-in user's own settled real slips: closing line value, P&L and ROI in ₹ (and $ only when a fresh
 * INR/USD rate is measured), model calibration, and the final-score archive. A slip with no recorded closing
 * price is outside the CLV sample; the edge tier is flagged provisional until enough slips are measured.
 * Recording a score settles every pending slip it decides, exactly as the Oracle's score entry does.
 */
import { useState, type FormEvent } from 'react';
import { apiClient } from '../../api/client';
import { downloadCsv, formatAgo, formatDate, formatINR, formatSignedINR } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { useAuthStore } from '../../store/useAuthStore';
import { Async, Button, EmptyState, Field, NumberInput, Panel, Pill, Select, TextInput, type Tone } from '../../ui/kit';

type EdgeTier = 'SHARP_ALPHA' | 'POSITIVE_CLV' | 'NEUTRAL_LINE' | 'ADVERSE_DRIFT' | 'UNMEASURED';

interface BookRow {
  bookmaker: string;
  settled_bets: number;
  win_rate_pct: number | null;
  total_staked_inr: number;
  total_staked_usd: number | null;
  net_pnl_inr: number;
  net_pnl_usd: number | null;
  roi_pct: number | null;
  clv_sample_count: number;
  avg_clv_pct: number | null;
  beat_close_rate_pct: number | null;
}

interface SportRow {
  sport_key: string;
  settled_bets: number;
  win_rate_pct: number | null;
  net_pnl_inr: number;
  net_pnl_usd: number | null;
  roi_pct: number | null;
  clv_sample_count: number;
  avg_clv_pct: number | null;
}

interface SettledSlip {
  bet_id: string;
  booking_code: string | null;
  bookmaker: string;
  structure: string;
  status: string;
  sport_key: string;
  fixture_summary: string;
  market_summary: string;
  placed_odds: number | null;
  closing_odds: number | null;
  clv_pct: number | null;
  clv_sharp_pct: number | null;
  stake_inr: number;
  stake_usd: number | null;
  return_inr: number;
  return_usd: number | null;
  pnl_inr: number;
  pnl_usd: number | null;
  settlement_source: string | null;
  root_cause_tag: string | null;
  placed_at: string;
  settled_at: string | null;
}

interface FinalScore {
  fixture_id: string;
  home: string;
  away: string;
  sport_key: string | null;
  kickoff: string | null;
  home_goals: number | null;
  away_goals: number | null;
  status: string;
  source: string;
  recorded_at: string;
}

interface Warehouse {
  settled_bets_count: number;
  pending_bets_count: number;
  won_bets_count: number;
  lost_bets_count: number;
  void_bets_count: number;
  win_rate_pct: number | null;
  total_staked_inr: number;
  total_staked_usd: number | null;
  total_return_inr: number;
  total_return_usd: number | null;
  net_pnl_inr: number;
  net_pnl_usd: number | null;
  roi_pct: number | null;
  clv_sample_count: number;
  avg_clv_pct: number | null;
  avg_clv_sharp_pct: number | null;
  beat_close_rate_pct: number | null;
  edge_tier: EdgeTier;
  edge_tier_provisional: boolean;
  edge_tier_min_sample: number;
  calibration_sample_count: number;
  mean_brier_score: number | null;
  mean_log_loss: number | null;
  archived_scores_count: number;
  inr_per_usd_rate: number | null;
  architect: string;
  by_bookmaker: BookRow[];
  by_sport: SportRow[];
  settled_bets: SettledSlip[];
  recent_scores: FinalScore[];
}

interface ScoreRecorded {
  fixture_id: string;
  settled_legs: number;
  settled_bets: number;
}

// The bookmakers a real slip can be recorded against (PlacedBookmaker on the server)
const BOOKMAKERS = ['ALL', 'PARIMATCH', '1XBET', 'STAKE', 'PINNACLE', 'BETFAIR', 'OTHER'] as const;
const SCORE_STATUSES = ['FINAL', 'ABANDONED', 'POSTPONED'] as const;

const TIER: Record<EdgeTier, { tone: Tone; label: string }> = {
  SHARP_ALPHA: { tone: 'good', label: 'Sharp alpha' },
  POSITIVE_CLV: { tone: 'info', label: 'Positive CLV' },
  NEUTRAL_LINE: { tone: 'neutral', label: 'Neutral line' },
  ADVERSE_DRIFT: { tone: 'critical', label: 'Adverse drift' },
  UNMEASURED: { tone: 'neutral', label: 'Unmeasured' },
};

const USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
const usd = (v: number | null): string | null => (v === null ? null : USD.format(v));
const pct = (v: number | null, digits = 1): string => (v === null ? '—' : `${v.toFixed(digits)}%`);
const signedPct = (v: number | null, digits = 2): string => (v === null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`);
const tone = (v: number | null): string =>
  v === null ? 'text-stone-400' : v > 0 ? 'text-emerald-600 dark:text-emerald-400' : v < 0 ? 'text-rose-600 dark:text-rose-400' : 'text-stone-600 dark:text-stone-300';
const sportLabel = (key: string): string => key.replace(/_/g, ' ');

const EMPTY_SCORE = { fixture_id: '', home: '', away: '', sport_key: '', home_goals: '', away_goals: '', status: 'FINAL' as (typeof SCORE_STATUSES)[number] };

export const ClvHistoricalWarehousePanel = () => {
  const [bookmaker, setBookmaker] = useState<(typeof BOOKMAKERS)[number]>('ALL');
  const [sport, setSport] = useState('ALL');
  const [knownSports, setKnownSports] = useState<string[]>([]);
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');

  const warehouse = useResource(
    `archive:clv-warehouse:${bookmaker}:${sport}`,
    async () => {
      const data = await apiClient.get<Warehouse>('/archive/clv-warehouse', { bookmaker, sport_key: sport });
      // Remember every sport seen, so filtering to one never shrinks the menu to that one
      setKnownSports((prev) => Array.from(new Set([...prev, ...data.by_sport.map((s) => s.sport_key)])).sort());
      return data;
    },
    { intervalMs: 60_000 },
  );

  return (
    <Panel
      title="Historical match, score & CLV warehouse"
      subtitle="TODAR MAL · real settled slips · closing line value · ₹ INR & $ USD"
      icon="analytics"
      className="lg:col-span-12"
      updatedAt={warehouse.updatedAt}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Select aria-label="Bookmaker" value={bookmaker} onChange={(e) => setBookmaker(e.target.value as (typeof BOOKMAKERS)[number])} className="h-8 py-0 text-xs">
            {BOOKMAKERS.map((b) => <option key={b} value={b}>{b === 'ALL' ? 'All bookmakers' : b}</option>)}
          </Select>
          <Select aria-label="Sport" value={sport} onChange={(e) => setSport(e.target.value)} className="h-8 py-0 text-xs">
            <option value="ALL">All sports</option>
            {knownSports.map((s) => <option key={s} value={s}>{sportLabel(s)}</option>)}
          </Select>
        </div>
      }
    >
      <Async resource={warehouse} skeletonRows={5}>
        {(d) => (
          <div className="flex flex-col gap-6">
            <Kpis d={d} />
            <BookStrip rows={d.by_bookmaker} />
            <SlipLedger d={d} />
            <div className="grid gap-5 lg:grid-cols-2">
              <ScoreArchive scores={d.recent_scores} total={d.archived_scores_count} />
              {isAdmin ? <ScoreForm /> : (
                <p className="self-center text-xs text-stone-400">Final scores are recorded by an administrator; each one settles the slips it decides.</p>
              )}
            </div>
            <p className="text-right text-[11px] text-stone-400">
              {d.inr_per_usd_rate !== null ? `USD at ₹${d.inr_per_usd_rate.toFixed(2)} per $ (measured)` : 'No fresh INR/USD rate: USD figures withheld'} · Architect: {d.architect}
            </p>
          </div>
        )}
      </Async>
    </Panel>
  );
};

const Kpi = ({ label, value, sub, valueClass }: { label: string; value: string; sub?: string | null; valueClass?: string }) => (
  <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
    <p className="text-xs text-stone-500 dark:text-stone-400">{label}</p>
    <p className={`mt-1 font-display text-lg font-semibold tabular-nums ${valueClass ?? 'text-stone-900 dark:text-stone-100'}`}>{value}</p>
    {sub && <p className="mt-0.5 text-[11px] tabular-nums text-stone-500 dark:text-stone-400">{sub}</p>}
  </div>
);

const Kpis = ({ d }: { d: Warehouse }) => {
  const tier = TIER[d.edge_tier];
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
      <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
        <p className="text-xs text-stone-500 dark:text-stone-400">CLV edge tier</p>
        <Pill className="mt-2" tone={tier.tone} icon="insights">{tier.label}</Pill>
        <p className="mt-1.5 text-[11px] text-stone-500 dark:text-stone-400">
          {d.clv_sample_count === 0 ? 'No slip has a recorded close yet'
            : d.edge_tier_provisional ? `Provisional: ${d.clv_sample_count} of ${d.edge_tier_min_sample} slips`
            : `${d.clv_sample_count} measured slips`}
        </p>
      </div>
      <Kpi
        label="Settled / pending"
        value={`${d.settled_bets_count} / ${d.pending_bets_count}`}
        sub={`${pct(d.win_rate_pct)} win rate · ${d.won_bets_count}W ${d.lost_bets_count}L${d.void_bets_count ? ` ${d.void_bets_count} void` : ''}`}
      />
      <Kpi label="Turnover" value={formatINR(d.total_staked_inr)} sub={usd(d.total_staked_usd)} />
      <Kpi
        label="Net P&L"
        value={formatSignedINR(d.net_pnl_inr)}
        valueClass={tone(d.net_pnl_inr)}
        sub={[usd(d.net_pnl_usd), d.roi_pct !== null ? `${signedPct(d.roi_pct)} ROI` : null].filter(Boolean).join(' · ') || null}
      />
      <Kpi
        label="Closing line value"
        value={signedPct(d.avg_clv_pct)}
        valueClass={tone(d.avg_clv_pct)}
        sub={d.beat_close_rate_pct !== null ? `beat the close ${pct(d.beat_close_rate_pct)}${d.avg_clv_sharp_pct !== null ? ` · sharp ${signedPct(d.avg_clv_sharp_pct)}` : ''}` : null}
      />
      <Kpi
        label="Model calibration"
        value={d.mean_brier_score !== null ? d.mean_brier_score.toFixed(4) : '—'}
        sub={d.mean_brier_score !== null ? `Brier over ${d.calibration_sample_count} legs · log loss ${d.mean_log_loss?.toFixed(3) ?? '—'}` : 'No settled legs fed back yet'}
      />
    </div>
  );
};

const BookStrip = ({ rows }: { rows: BookRow[] }) =>
  rows.length === 0 ? null : (
    <div>
      <h3 className="mb-2 text-xs font-medium text-stone-500 dark:text-stone-400">By bookmaker</h3>
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {rows.map((b) => (
          <li key={b.bookmaker} className="rounded-xl bg-stone-50 px-3 py-2.5 dark:bg-white/[0.03]">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-semibold text-stone-800 dark:text-stone-100">{b.bookmaker}</span>
              <span className={`text-sm font-semibold tabular-nums ${tone(b.net_pnl_inr)}`}>{formatSignedINR(b.net_pnl_inr)}</span>
            </div>
            <p className="mt-0.5 text-[11px] tabular-nums text-stone-500 dark:text-stone-400">
              {b.settled_bets} settled · ROI {signedPct(b.roi_pct)} · CLV {signedPct(b.avg_clv_pct)}
              {b.clv_sample_count > 0 && b.beat_close_rate_pct !== null ? ` (${pct(b.beat_close_rate_pct, 0)} beat)` : ''}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );

const SlipLedger = ({ d }: { d: Warehouse }) => {
  const exportCsv = () =>
    downloadCsv(
      'betdoc-clv-warehouse.csv',
      d.settled_bets.map((s) => ({
        placed_at: s.placed_at, settled_at: s.settled_at, bookmaker: s.bookmaker, booking_code: s.booking_code, structure: s.structure,
        sport: s.sport_key, fixtures: s.fixture_summary, markets: s.market_summary, status: s.status, placed_odds: s.placed_odds,
        closing_odds: s.closing_odds, clv_pct: s.clv_pct, clv_sharp_pct: s.clv_sharp_pct, stake_inr: s.stake_inr, return_inr: s.return_inr,
        pnl_inr: s.pnl_inr, stake_usd: s.stake_usd, pnl_usd: s.pnl_usd, settlement_source: s.settlement_source, root_cause: s.root_cause_tag,
      })),
    );

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-stone-500 dark:text-stone-400">
          Settled slips{d.settled_bets.length < d.settled_bets_count ? ` · newest ${d.settled_bets.length} of ${d.settled_bets_count}` : ''}
        </h3>
        <Button size="sm" variant="ghost" icon="download" disabled={d.settled_bets.length === 0} onClick={exportCsv}>Export CSV</Button>
      </div>
      {d.settled_bets.length === 0 ? (
        <EmptyState icon="receipt_long" title="No settled slips yet" detail="Record real slips from the Oracle; they appear here once they settle." />
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-xs">
            <thead className="text-[11px] text-stone-400">
              <tr>
                <th className="px-1 py-1.5 font-medium">Slip</th>
                <th className="px-1 py-1.5 font-medium">Status</th>
                <th className="px-1 py-1.5 text-right font-medium">Odds → close</th>
                <th className="px-1 py-1.5 text-right font-medium">CLV</th>
                <th className="px-1 py-1.5 text-right font-medium">Stake</th>
                <th className="px-1 py-1.5 text-right font-medium">P&L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 dark:divide-white/[0.05]">
              {d.settled_bets.map((s) => (
                <tr key={s.bet_id}>
                  <td className="max-w-[22rem] px-1 py-2">
                    <p className="truncate font-medium text-stone-800 dark:text-stone-100" title={s.fixture_summary}>{s.fixture_summary}</p>
                    <p className="truncate text-[11px] text-stone-400" title={s.market_summary}>
                      {s.bookmaker}{s.booking_code ? ` · ${s.booking_code}` : ''} · {s.structure.toLowerCase()} · {sportLabel(s.sport_key)} · {formatDate(s.placed_at)}
                    </p>
                  </td>
                  <td className="px-1 py-2">
                    <Pill tone={s.status.includes('WON') ? 'good' : s.status.includes('LOST') ? 'critical' : 'neutral'}>{s.status.replace('_', ' ').toLowerCase()}</Pill>
                  </td>
                  <td className="px-1 py-2 text-right font-mono tabular-nums text-stone-600 dark:text-stone-300">
                    {s.placed_odds?.toFixed(2) ?? '—'} → {s.closing_odds?.toFixed(2) ?? '—'}
                  </td>
                  <td className={`px-1 py-2 text-right tabular-nums ${tone(s.clv_pct)}`} title={s.clv_sharp_pct !== null ? `Sharp (de-vigged) CLV ${signedPct(s.clv_sharp_pct)}` : undefined}>
                    {signedPct(s.clv_pct)}
                  </td>
                  <td className="px-1 py-2 text-right tabular-nums text-stone-600 dark:text-stone-300">
                    {formatINR(s.stake_inr)}
                    {s.stake_usd !== null && <span className="block text-[10px] text-stone-400">{usd(s.stake_usd)}</span>}
                  </td>
                  <td className={`px-1 py-2 text-right tabular-nums ${tone(s.pnl_inr)}`}>
                    {formatSignedINR(s.pnl_inr)}
                    {s.pnl_usd !== null && <span className="block text-[10px] opacity-75">{usd(s.pnl_usd)}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

const ScoreArchive = ({ scores, total }: { scores: FinalScore[]; total: number }) => (
  <div>
    <h3 className="mb-2 text-xs font-medium text-stone-500 dark:text-stone-400">Final scores · {total} archived</h3>
    {scores.length === 0 ? (
      <p className="text-xs text-stone-400">No final scores recorded yet.</p>
    ) : (
      <ul className="flex max-h-72 flex-col divide-y divide-stone-100 overflow-y-auto dark:divide-white/[0.05]">
        {scores.map((s) => (
          <li key={s.fixture_id} className="flex items-center justify-between gap-3 py-2 text-xs">
            <span className="min-w-0">
              <span className="block truncate font-medium text-stone-800 dark:text-stone-100">{s.home} vs {s.away}</span>
              <span className="block truncate text-[11px] text-stone-400">
                {s.sport_key ? sportLabel(s.sport_key) : 'sport not set'} · {s.source === 'admin' ? 'entered by admin' : 'feed'} {formatAgo(s.recorded_at)}
              </span>
            </span>
            {s.status === 'FINAL' ? (
              <span className="shrink-0 font-mono text-sm font-semibold tabular-nums text-stone-900 dark:text-stone-100">{s.home_goals}–{s.away_goals}</span>
            ) : (
              <Pill tone="warning">{s.status.toLowerCase()}</Pill>
            )}
          </li>
        ))}
      </ul>
    )}
  </div>
);

const ScoreForm = () => {
  const [form, setForm] = useState(EMPTY_SCORE);
  const [busy, setBusy] = useState(false);
  const final = form.status === 'FINAL';
  const ready = form.home.trim().length > 0 && form.away.trim().length > 0 && (!final || (form.home_goals !== '' && form.away_goals !== ''));
  const set = (key: keyof typeof EMPTY_SCORE) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const body = {
        fixture_id: form.fixture_id.trim() || undefined,
        home: form.home.trim(),
        away: form.away.trim(),
        sport_key: form.sport_key.trim() || undefined,
        home_goals: final ? Number(form.home_goals) : undefined,
        away_goals: final ? Number(form.away_goals) : undefined,
        status: form.status,
      };
      const done = await runMutation(() => apiClient.post<ScoreRecorded>('/archive/clv-warehouse/scores', body), {
        invalidate: ['archive:clv-warehouse', 'oracle', 'dashboard'],
        success: (r) => `Score recorded · ${r.settled_bets} slip${r.settled_bets === 1 ? '' : 's'} settled`,
        errorTitle: 'Score not recorded',
      });
      if (done) setForm(EMPTY_SCORE);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
      <h3 className="text-xs font-medium text-stone-500 dark:text-stone-400">Record a final score (settles the slips it decides)</h3>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Home"><TextInput required maxLength={128} value={form.home} onChange={set('home')} /></Field>
        <Field label="Away"><TextInput required maxLength={128} value={form.away} onChange={set('away')} /></Field>
        <Field label="Home goals"><NumberInput min={0} step={1} disabled={!final} value={form.home_goals} onChange={set('home_goals')} /></Field>
        <Field label="Away goals"><NumberInput min={0} step={1} disabled={!final} value={form.away_goals} onChange={set('away_goals')} /></Field>
        <Field label="Status">
          <Select value={form.status} onChange={set('status')}>
            {SCORE_STATUSES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
          </Select>
        </Field>
        <Field label="Sport key" hint="optional, e.g. soccer_epl"><TextInput maxLength={64} value={form.sport_key} onChange={set('sport_key')} /></Field>
        <Field label="Fixture id" hint="optional: when blank, slips are matched by team names" className="col-span-2">
          <TextInput maxLength={128} value={form.fixture_id} onChange={set('fixture_id')} />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button type="submit" size="sm" variant="primary" icon="scoreboard" busy={busy} disabled={!ready}>Record score</Button>
      </div>
    </form>
  );
};
