import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { apiClient } from '../api/client';
import { bestPrices, emergencyStop, fairProbabilities, resumeTrading, type MatchOdds, type Side, useLiveOdds } from '../lib/api';
import { formatAgo, formatDateTime, formatINR, formatOdds, formatPct, formatSignedINR, formatTime } from '../lib/format';
import { invalidate, runMutation, useResource } from '../lib/resource';
import { MONEY_SECTIONS, useExecutionStore } from '../store/useExecutionStore';
import { useSystemStore } from '../store/useSystemStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, ConfirmButton, type Column, DataTable, EmptyState, Page, Panel, Pill, StatusBadge } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface ActiveBet {
  id: string;
  exchange: string;
  match_id: string;
  market_type: string;
  selection: string;
  stake: number;
  odds: number;
  placed_at: string;
  status: string;
  strategy_name: string | null;
}

interface CashOutQuote {
  bet_id: string;
  original_stake: number;
  original_odds: number;
  current_live_odds: number;
  fair_value: number;
  cash_out_offered: number;
  margin_applied: number;
}

interface SteamAlert {
  match_id: string;
  selection_id: string;
  market_type: string;
  opening_odds: number;
  current_odds: number;
  implied_prob_delta_pct: number;
  triggering_bookmakers: string[];
  detected_at: string;
}

interface Surebet {
  match_id: string;
  market_type: string;
  line: number | null;
  legs: { selection_id: string; bookmaker_id: string; odds: number; recommended_stake_pct: number }[];
  profit_pct: number;
}

interface StrategyNode {
  strategy_name: string;
  total_bets: number;
  bets_won: number;
  bets_lost: number;
  volume: number;
  net_profit: number;
  roi_pct: number;
}

const SIDES: readonly Side[] = ['HOME', 'DRAW', 'AWAY'];

// ---------------------------------------------------------------------------
// LIVE ODDS MATRIX
// ---------------------------------------------------------------------------
const LiveOddsMatrix = ({ odds, steam }: { odds: ReturnType<typeof useLiveOdds>; steam: SteamAlert[] }) => {
  const setDraft = useExecutionStore((s) => s.setDraft);
  const draftMatchId = useExecutionStore((s) => s.draftMatchId);
  const steamByMatch = useMemo(() => new Map(steam.map((a) => [a.match_id, a])), [steam]);

  return (
    <Panel
      title="Live odds matrix"
      icon="table_rows"
      className="lg:col-span-8"
      updatedAt={odds.updatedAt}
      subtitle={odds.data ? `${odds.data.length} fixtures · best price across books` : undefined}
      bodyClassName="p-4"
    >
      <Async
        resource={odds}
        skeletonRows={6}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon="sports_soccer" title="No fixtures in the odds store" detail="The odds poller writes snapshots here when ODDS_API_KEY and ODDS_SPORT_KEYS are configured." />}
      >
        {(rows) => (
          <div className="-mx-4 -my-4 overflow-x-auto">
            <table className="w-full min-w-[620px] text-sm">
              <thead>
                <tr className="border-b border-slate-900/[0.06] text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400 dark:border-white/[0.06]">
                  <th className="px-4 py-2.5 text-left">Fixture</th>
                  {SIDES.map((s) => <th key={s} className="px-2 py-2.5 text-center">{s}</th>)}
                  <th className="px-4 py-2.5 text-right">Signal</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-900/[0.05] dark:divide-white/[0.05]">
                {rows.slice(0, 40).map((m: MatchOdds) => {
                  const best = bestPrices(m);
                  const fair = fairProbabilities(best);
                  const alert = steamByMatch.get(m.id);
                  const label = `${m.home_team} v ${m.away_team}`;
                  return (
                    <tr key={m.id} className={draftMatchId === m.id ? 'bg-[color-mix(in_srgb,var(--accent)_6%,transparent)]' : undefined}>
                      <td className="px-4 py-2.5">
                        <p className="font-medium text-slate-800 dark:text-slate-100">{label}</p>
                        <p className="text-[11px] text-slate-400">{formatDateTime(m.commence_time)} · {m.bookmakers.length} books</p>
                      </td>
                      {SIDES.map((side) => {
                        const price = best[side];
                        return (
                          <td key={side} className="px-2 py-2.5 text-center">
                            {price ? (
                              <button
                                type="button"
                                onClick={() => setDraft({ matchId: m.id, selection: side, odds: price.price, trueProbability: fair[side], label, source: 'Arena · best price' })}
                                aria-label={`Back ${side.toLowerCase()} in ${label} at ${formatOdds(price.price)}`}
                                title={`${price.bookmaker} · fair ${(100 * (fair[side] ?? 0)).toFixed(1)}%`}
                                className="rounded-lg px-2.5 py-1 font-mono text-sm font-semibold tabular-nums text-slate-800 ring-1 ring-inset ring-slate-900/10 transition-colors hover:bg-[var(--accent)] hover:text-[var(--accent-ink)] hover:ring-transparent dark:text-slate-100 dark:ring-white/10"
                              >
                                {formatOdds(price.price)}
                              </button>
                            ) : (
                              <span className="text-slate-300 dark:text-slate-600">—</span>
                            )}
                            {price && <p className="mt-0.5 truncate text-[10px] text-slate-400">{price.bookmaker}</p>}
                          </td>
                        );
                      })}
                      <td className="px-4 py-2.5 text-right">
                        {alert ? (
                          <Pill tone="warning" icon={alert.current_odds < alert.opening_odds ? 'trending_down' : 'trending_up'}>
                            Steam {formatPct(alert.implied_prob_delta_pct)}
                          </Pill>
                        ) : (
                          <Pill tone="neutral" icon="trending_flat">Stable</Pill>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// TACTICAL FEED (event bus + steam + surebets)
// ---------------------------------------------------------------------------
const TacticalFeed = ({ steam, surebets }: { steam: SteamAlert[]; surebets: Surebet[] }) => {
  const events = useSystemStore((s) => s.events);
  const lines = useMemo(() => {
    const out: { key: string; at: string; tag: string; tone: 'accent' | 'warning' | 'info' | 'good'; text: string }[] = [];
    for (const e of events) {
      if (e.type !== 'mutation' || !e.section || !['execution', 'arena', 'admin', 'capital', 'control-panel'].includes(e.section)) continue;
      out.push({ key: `bus-${e.at}-${e.path}`, at: e.at, tag: 'EXEC', tone: 'info', text: `${e.method} ${e.path?.replace('/api/v1/', '')}` });
    }
    for (const a of steam) out.push({ key: `steam-${a.match_id}-${a.selection_id}`, at: a.detected_at, tag: 'STEAM', tone: 'warning', text: `${a.match_id} · ${a.selection_id} ${formatOdds(a.opening_odds)} → ${formatOdds(a.current_odds)} (${a.triggering_bookmakers.join(', ')})` });
    for (const s of surebets) out.push({ key: `arb-${s.match_id}-${s.market_type}`, at: new Date().toISOString(), tag: 'ARB', tone: 'good', text: `${s.match_id} · ${s.market_type} · ${formatPct(s.profit_pct, 2)} locked across ${s.legs.length} books` });
    return out.sort((a, b) => b.at.localeCompare(a.at)).slice(0, 30);
  }, [events, steam, surebets]);

  return (
    <Panel title="Tactical feed" icon="terminal" className="lg:col-span-4" bodyClassName="p-0">
      <div className="flex items-center gap-2 border-b border-slate-900/[0.06] px-4 py-2.5 font-mono text-[10px] text-slate-400 dark:border-white/[0.06]">
        <span className="size-2 rounded-full bg-rose-500/70" />
        <span className="size-2 rounded-full bg-amber-500/70" />
        <span className="size-2 rounded-full bg-emerald-500/70" />
        <span className="ml-1">bajirao@arena:~$ tail -f tactical.log</span>
      </div>
      {lines.length === 0 ? (
        <EmptyState icon="terminal" title="Quiet tape" detail="Executions, settlements, steam moves and arbitrage hits stream here live." />
      ) : (
        <ul className="max-h-[440px] divide-y divide-slate-900/[0.05] overflow-y-auto dark:divide-white/[0.05]">
          <AnimatePresence initial={false}>
            {lines.map((l) => (
              <motion.li key={l.key} initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} className="flex flex-col gap-1 px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <Pill tone={l.tone}>{l.tag}</Pill>
                  <span className="font-mono text-[10px] text-slate-400">{formatTime(l.at)}</span>
                </div>
                <p className="break-words font-mono text-[11px] leading-relaxed text-slate-600 dark:text-slate-300">{l.text}</p>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// OPEN POSITIONS (cash-out quotes + manual grading)
// ---------------------------------------------------------------------------
const OpenPositions = ({ fairByMatch }: { fairByMatch: Map<string, Partial<Record<Side, number>>> }) => {
  const active = useResource('arena:active', () => apiClient.get<ActiveBet[]>('/arena/active'), { intervalMs: 20_000 });
  const [quotes, setQuotes] = useState<Record<string, CashOutQuote>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const quote = async (bet: ActiveBet) => {
    // Current probability: live consensus if the fixture is quoted, otherwise the price at placement.
    const p = fairByMatch.get(bet.match_id)?.[bet.selection as Side] ?? 1 / bet.odds;
    setBusy(bet.id);
    const q = await runMutation(() => apiClient.get<CashOutQuote>(`/arena/${bet.id}/cash-out`, { current_true_prob: p.toFixed(4) }), { errorTitle: 'Cash-out quote failed' });
    setBusy(null);
    if (q) setQuotes((prev) => ({ ...prev, [bet.id]: q }));
  };

  const settle = async (bet: ActiveBet, status: 'WON' | 'LOST' | 'VOID' | 'CASH_OUT', payout: number) => {
    setBusy(bet.id);
    await runMutation(() => apiClient.post(`/arena/${bet.id}/settle`, { status, payout, force_regrade: false }), {
      invalidate: MONEY_SECTIONS,
      success: `Position graded ${status.replace('_', ' ').toLowerCase()} · payout ${formatINR(payout)}`,
      errorTitle: 'Settlement failed',
    });
    setBusy(null);
  };

  const columns: Column<ActiveBet>[] = [
    { key: 'match', header: 'Position', render: (b) => (
      <div>
        <p className="font-medium text-slate-800 dark:text-slate-100">{b.selection} <span className="font-mono text-xs text-slate-400">{b.match_id}</span></p>
        <p className="text-[11px] text-slate-400">{b.exchange} · {b.market_type} · {formatAgo(b.placed_at)}{b.strategy_name ? ` · ${b.strategy_name}` : ''}</p>
      </div>
    ) },
    { key: 'stake', header: 'Stake @ odds', align: 'right', render: (b) => `${formatINR(b.stake)} @ ${formatOdds(b.odds)}` },
    { key: 'status', header: 'Status', render: (b) => <StatusBadge status={b.status} /> },
    { key: 'actions', header: 'Manage', align: 'right', render: (b) => {
      const q = quotes[b.id];
      return (
        <div className="flex flex-wrap items-center justify-end gap-1.5">
          {q ? (
            <ConfirmButton size="sm" variant="primary" confirmLabel={`Cash out ${formatINR(q.cash_out_offered)}?`} onConfirm={() => void settle(b, 'CASH_OUT', q.cash_out_offered)} busy={busy === b.id}>
              {formatINR(q.cash_out_offered)}
            </ConfirmButton>
          ) : (
            <Button size="sm" icon="request_quote" onClick={() => void quote(b)} busy={busy === b.id}>Quote</Button>
          )}
          <ConfirmButton size="sm" variant="ghost" confirmLabel="Won?" onConfirm={() => void settle(b, 'WON', b.stake * b.odds)}>Won</ConfirmButton>
          <ConfirmButton size="sm" variant="ghost" confirmLabel="Lost?" onConfirm={() => void settle(b, 'LOST', 0)}>Lost</ConfirmButton>
          <ConfirmButton size="sm" variant="ghost" confirmLabel="Void?" onConfirm={() => void settle(b, 'VOID', b.stake)}>Void</ConfirmButton>
        </div>
      );
    } },
  ];

  return (
    <Panel title="Open positions" icon="swords" className="lg:col-span-12" updatedAt={active.updatedAt} subtitle={active.data ? `${active.data.length} live` : undefined}>
      <Async
        resource={active}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon="swords" title="No open positions" detail="Click any price in the matrix (or an Oracle value bet) to stage an order in the execution terminal." />}
      >
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(b) => b.id} />}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// STRATEGY ANALYTICS
// ---------------------------------------------------------------------------
const StrategyAnalytics = () => {
  const strategies = useResource('arena:strategies', () => apiClient.get<StrategyNode[]>('/arena/analytics/strategies', { currency: 'INR' }), { intervalMs: 60_000 });
  const columns: Column<StrategyNode>[] = [
    { key: 'name', header: 'Strategy', render: (s) => <span className="font-medium">{s.strategy_name}</span> },
    { key: 'bets', header: 'W / L / total', align: 'right', render: (s) => `${s.bets_won} / ${s.bets_lost} / ${s.total_bets}` },
    { key: 'volume', header: 'Volume', align: 'right', render: (s) => formatINR(s.volume) },
    { key: 'pnl', header: 'Net', align: 'right', render: (s) => <span className={s.net_profit >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}>{formatSignedINR(s.net_profit)}</span> },
    { key: 'roi', header: 'ROI', align: 'right', render: (s) => `${s.roi_pct >= 0 ? '+' : ''}${formatPct(s.roi_pct)}` },
  ];
  return (
    <Panel title="Strategy analytics" icon="leaderboard" className="lg:col-span-12" updatedAt={strategies.updatedAt}>
      <Async resource={strategies} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="leaderboard" title="No settled strategies yet" detail="Performance by strategy appears once positions settle." />}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(s) => s.strategy_name} dense />}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ARENA
// ---------------------------------------------------------------------------
export const TheArena = () => {
  const odds = useLiveOdds();
  const steam = useResource('signals:steam', () => apiClient.get<SteamAlert[]>('/signals/steam-moves', { window_minutes: 120 }), { intervalMs: 30_000 });
  const surebets = useResource('signals:surebets', () => apiClient.get<Surebet[]>('/signals/surebets', { max_staleness_minutes: 30 }), { intervalMs: 60_000 });
  const active = useResource('arena:active', () => apiClient.get<ActiveBet[]>('/arena/active'), { intervalMs: 20_000 });
  const halted = useSystemStore((s) => s.halted);
  const [scanning, setScanning] = useState(false);

  const fairByMatch = useMemo(() => new Map((odds.data ?? []).map((m) => [m.id, fairProbabilities(bestPrices(m))])), [odds.data]);
  const openCount = active.data?.length ?? 0;
  const atRisk = (active.data ?? []).reduce((acc, b) => acc + b.stake, 0);
  const headline = halted
    ? 'BAJIRAO standing down. The emergency stop holds every order.'
    : `BAJIRAO ACTIVE. ${odds.data?.length ?? 0} fixtures priced, ${openCount} open position${openCount === 1 ? '' : 's'}, ${formatINR(atRisk)} at risk.`;

  return (
    <Page>
      <CommanderHero
        commander="BAJIRAO"
        headline={headline}
        motif={MOTIFS.radar}
        detail={`${steam.data?.length ?? 0} steam moves in the last 2h · ${surebets.data?.length ?? 0} surebets on fresh prices.`}
        actions={
          <>
            <Button icon="sync" busy={odds.loading && odds.data !== undefined} onClick={() => void odds.refresh()}>Sync odds</Button>
            <Button
              variant="primary"
              icon="radar"
              busy={scanning}
              onClick={async () => {
                setScanning(true);
                await Promise.all([steam.refresh(), surebets.refresh()]);
                setScanning(false);
                invalidate('signals');
              }}
            >
              Scan arbitrage
            </Button>
            {halted ? (
              <Button variant="danger" icon="play_circle" onClick={() => void resumeTrading(500)}>Resume trading</Button>
            ) : (
              <ConfirmButton variant="danger" icon="emergency_home" confirmLabel="Halt everything?" onConfirm={() => void emergencyStop()}>
                Halt trading
              </ConfirmButton>
            )}
          </>
        }
      />
      <LiveOddsMatrix odds={odds} steam={steam.data ?? []} />
      <TacticalFeed steam={steam.data ?? []} surebets={surebets.data ?? []} />
      <OpenPositions fairByMatch={fairByMatch} />
      <StrategyAnalytics />
    </Page>
  );
};
