import { useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { apiClient } from '../api/client';
import { useLiveOdds } from '../lib/api';
import { formatAgo, formatOdds, formatPct, formatTime } from '../lib/format';
import { useResource } from '../lib/resource';
import { subscribeChannel } from '../services/realtime';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, EmptyState, Page, Panel, Pill, StatusBadge } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface NewsItem { id: string; source: string; title: string; summary: string; url: string; published_at: string }
interface MatchScore { match_id: string; home_team: string; away_team: string; home_score: number; away_score: number; status: string; clock: string | null }
interface WireDashboard { news: NewsItem[]; scores: MatchScore[]; weather: Record<string, unknown> }
interface SteamAlert { match_id: string; selection_id: string; market_type: string; opening_odds: number; current_odds: number; implied_prob_delta_pct: number; triggering_bookmakers: string[]; detected_at: string }
interface OmniMessage { topic?: string; provider?: string; provider_name?: string; category?: string; payload?: unknown; at: string }

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE WIRE
// ---------------------------------------------------------------------------
export const TheWire = () => {
  const odds = useLiveOdds();
  const matchIds = useMemo(() => (odds.data ?? []).slice(0, 50).map((m) => m.id), [odds.data]);
  const idsKey = matchIds.join(',');
  const wire = useResource('the-wire:dashboard', () => apiClient.get<WireDashboard>('/the-wire/dashboard', { match_ids: idsKey }), { intervalMs: 60_000 });
  const refreshWire = wire.refresh;
  useEffect(() => {
    if (idsKey) void refreshWire(); // fixtures arrived or changed: fetch their scores
  }, [idsKey, refreshWire]);
  const steam = useResource('signals:steam', () => apiClient.get<SteamAlert[]>('/signals/steam-moves', { window_minutes: 120 }), { intervalMs: 30_000 });
  const [omni, setOmni] = useState<OmniMessage[]>([]);

  // Omni ingestion stream: normalised provider payloads relayed from Redis.
  useEffect(
    () =>
      subscribeChannel('/omni/ws/stream', (data) => {
        if (!data || typeof data !== 'object' || (data as { type?: string }).type === 'pong') return;
        setOmni((prev) => [{ ...(data as OmniMessage), at: new Date().toISOString() }, ...prev].slice(0, 25));
      }, 'topic'),
    [],
  );

  const teamsByMatch = useMemo(() => new Map((odds.data ?? []).map((m) => [m.id, `${m.home_team} v ${m.away_team}`])), [odds.data]);
  const news = wire.data?.news ?? [];
  const scores = wire.data?.scores ?? [];
  const live = scores.filter((s) => s.status === 'LIVE');
  const sentiment = useMemo(() => {
    const rows = [...(steam.data ?? [])].sort((a, b) => Math.abs(b.implied_prob_delta_pct) - Math.abs(a.implied_prob_delta_pct));
    const backed = rows.filter((r) => r.current_odds < r.opening_odds).length;
    return { rows: rows.slice(0, 10), backed, drifting: rows.length - backed };
  }, [steam.data]);
  const mood = sentiment.rows.length === 0 ? 'NEUTRAL' : sentiment.backed >= sentiment.drifting ? 'BULLISH' : 'BEARISH';

  return (
    <Page>
      <CommanderHero
        commander="VIDUR"
        headline={`VIDUR ACTIVE. ${news.length} headlines intercepted, ${live.length} match${live.length === 1 ? '' : 'es'} in play, ${sentiment.rows.length} steam signals.`}
        motif={MOTIFS.waves}
        detail="News from public sports RSS feeds, scores from The Odds API for the fixtures you price, market sentiment from steam moves."
        actions={
          <>
            <Button variant="primary" icon="refresh" busy={wire.loading && wire.data !== undefined} onClick={() => void wire.refresh()}>Refresh intercepts</Button>
            <Button icon="sensors" onClick={() => void steam.refresh()}>Refresh sentiment</Button>
          </>
        }
      />

      <Panel title="Intercept feed" icon="wifi_tethering" className="lg:col-span-7" updatedAt={wire.updatedAt} subtitle="public RSS">
        <Async resource={wire} skeletonRows={6} isEmpty={() => news.length === 0} empty={<EmptyState icon="newspaper" title="No headlines" detail="The news feeds are unreachable from the API server right now." />}>
          {() => (
            <ul className="flex max-h-[640px] flex-col gap-3 overflow-y-auto pr-1">
              {news.map((n) => (
                <li key={n.id}>
                  <a href={n.url} target="_blank" rel="noopener noreferrer" className="group block rounded-2xl bg-stone-50 p-4 transition-[background-color,transform] duration-300 hover:-translate-y-0.5 hover:bg-stone-100/80 active:scale-[0.99] dark:bg-white/[0.03] dark:hover:bg-white/[0.05]">
                    <div className="flex items-center justify-between gap-2 text-[11px] text-stone-400">
                      <span className="font-semibold ">{n.source}</span>
                      <span>{formatAgo(n.published_at)}</span>
                    </div>
                    <p className="mt-1 text-sm font-semibold leading-snug text-stone-800 group-hover:underline dark:text-stone-100">{n.title}</p>
                    {n.summary && <p className="mt-1 line-clamp-2 text-xs text-stone-500 dark:text-stone-400">{n.summary}</p>}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Async>
      </Panel>

      <div className="flex flex-col gap-6 lg:col-span-5">
        <Panel title="Scoreboard" icon="scoreboard" updatedAt={wire.updatedAt} subtitle={`${scores.length} tracked fixtures`}>
          <Async resource={wire} isEmpty={() => scores.length === 0} empty={<EmptyState icon="scoreboard" title="No scores for tracked fixtures" detail="Scores come from The Odds API for fixtures in the odds store (needs ODDS_API_KEY)." />}>
            {() => (
              <ul className="flex max-h-[300px] flex-col gap-1.5 overflow-y-auto">
                {[...scores].sort((a, b) => Number(b.status === 'LIVE') - Number(a.status === 'LIVE')).map((s) => (
                  <li key={s.match_id} className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2 text-sm dark:bg-white/[0.03]">
                    <span className="min-w-0 truncate text-stone-700 dark:text-stone-200">{s.home_team} v {s.away_team}</span>
                    <span className="flex shrink-0 items-center gap-2">
                      {s.status !== 'SCHEDULED' && <span className="font-mono font-semibold tabular-nums">{s.home_score}-{s.away_score}</span>}
                      <StatusBadge status={s.status === 'FT' ? 'COMPLETED' : s.status === 'LIVE' ? 'RUNNING' : 'QUEUED'} label={s.status} />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Async>
          <p className="mt-3 text-[11px] text-stone-400">Venue weather is not shown: none of the ingested feeds carry venue locations.</p>
        </Panel>

        <Panel title="Sentiment radar" icon="sensors" updatedAt={steam.updatedAt} actions={<StatusBadge status={mood === 'BULLISH' ? 'ONLINE' : mood === 'BEARISH' ? 'WARNING' : 'IDLE'} label={`Market ${mood.toLowerCase()}`} />}>
          <Async resource={steam} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="sensors" title="Calm market" detail="No steam moves in the last two hours." />}>
            {() => (
              <ul className="flex flex-col gap-2.5">
                {sentiment.rows.map((r) => {
                  const backed = r.current_odds < r.opening_odds;
                  const width = Math.min(100, Math.abs(r.implied_prob_delta_pct) * 8);
                  return (
                    <li key={`${r.match_id}-${r.selection_id}`}>
                      <div className="flex items-center justify-between gap-2 text-xs">
                        <span className="truncate text-stone-700 dark:text-stone-200">{teamsByMatch.get(r.match_id) ?? r.match_id} · {r.selection_id}</span>
                        <span className="shrink-0 tabular-nums text-stone-500">{formatOdds(r.opening_odds)} → {formatOdds(r.current_odds)}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-stone-900/[0.06] dark:bg-white/[0.07]">
                          <div className="h-full rounded-full" style={{ width: `${width}%`, background: backed ? 'var(--viz-positive)' : 'var(--viz-negative)' }} />
                        </div>
                        <span className="w-24 text-right text-[10px] font-semibold text-stone-600 dark:text-stone-300">{backed ? '▲ backed' : '▼ drifting'} {formatPct(Math.abs(r.implied_prob_delta_pct))}</span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </Async>
        </Panel>
      </div>

      <Panel title="Omni ingestion stream" icon="stream" className="lg:col-span-12" bodyClassName="p-0" subtitle="Redis → WebSocket relay">
        {omni.length === 0 ? (
          <EmptyState icon="stream" title="No provider payloads yet" detail="Normalised payloads appear here as the Omni Celery workers ingest configured providers (Control Panel → Omni admin)." />
        ) : (
          <ul className="max-h-[260px] flex flex-col gap-0.5 overflow-y-auto p-2.5">
            <AnimatePresence initial={false}>
              {omni.map((m, i) => (
                <motion.li key={`${m.at}-${i}`} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} className="flex items-center justify-between gap-3 px-3.5 py-3 text-sm rounded-2xl transition-colors duration-300 hover:bg-stone-50 dark:hover:bg-white/[0.025]">
                  <span className="flex min-w-0 items-center gap-2">
                    <Pill tone="accent">{m.topic ?? 'payload'}</Pill>
                    <span className="truncate font-mono text-stone-500">{m.provider_name ?? m.provider ?? ''} {JSON.stringify(m.payload ?? m).slice(0, 120)}</span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-stone-400 dark:text-stone-500">{formatTime(m.at)}</span>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        )}
      </Panel>
    </Page>
  );
};
