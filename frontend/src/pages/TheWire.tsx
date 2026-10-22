/**
 * The Wire (Commander VIDUR). Group 108 (point b.14b) put the four-column intelligence board first: each item of
 * news, absences, weather and steam against its fixture's measured market move and best edge, one click from the
 * Oracle and the neural chain (point b.9). Below it, the Wire's working panels: scores, venue weather, absences and
 * ratings, referees, and the Omni ingestion stream. Group 78 made the Wire the twin fortress's feed: what this page
 * shows is what the pillars read. Architect: Amit Ashok Kumar Patnaik.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { useLiveOdds } from '../lib/api';
import { formatTime } from '../lib/format';
import { invalidate } from '../lib/resource';
import { scanWire, useWireDashboard, useWireFixtures, useWireStream, type WireFrame } from '../lib/the_wire';
import { subscribeChannel } from '../services/realtime';
import { useAuthStore } from '../store/useAuthStore';
import { LineupImpactPanel } from '../components/wire/LineupImpactPanel';
import { LiveNewsTicker } from '../components/wire/LiveNewsTicker';
import { MatchWeatherCard } from '../components/wire/MatchWeatherCard';
import { RefereeBoard } from '../components/wire/RefereeBoard';
import { TacticalAudioAlert } from '../components/wire/TacticalAudioAlert';
import { WireIntelligenceBoard } from '../components/wire/WireIntelligenceBoard';
import { useTacticalAudio } from '../lib/the_wire_audio';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, ComponentGuideBanner, EmptyState, Page, Panel, Pill, StatusBadge } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface OmniMessage { topic?: string; provider?: string; provider_name?: string; category?: string; payload?: unknown; at: string }

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE WIRE
// ---------------------------------------------------------------------------
export const TheWire = () => {
  const odds = useLiveOdds();
  const matchIds = useMemo(() => (odds.data ?? []).slice(0, 50).map((m) => m.id), [odds.data]);
  const idsKey = matchIds.join(',');
  const wire = useWireDashboard(idsKey);
  const fixtures = useWireFixtures();
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');
  const audio = useTacticalAudio();
  const [scanning, setScanning] = useState(false);
  const refreshWire = wire.refresh;
  const announce = audio.announce;
  const onFrame = useCallback((frame: WireFrame) => {
    announce(frame);
    if (frame.type === 'news' || frame.type === 'catalyst') {
      void refreshWire();
      invalidate('the-wire:board');
    }
  }, [announce, refreshWire]);
  const stream = useWireStream(onFrame);
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

  const news = wire.data?.news ?? [];
  const scores = wire.data?.scores ?? [];
  const catalysts = wire.data?.catalysts ?? [];
  const tracked = fixtures.data?.fixtures ?? [];
  const weathered = tracked.filter((f) => f.weather !== null);
  const credit = wire.data?.developer_credit ?? stream.developer ?? 'Amit Ashok Kumar Patnaik';
  const live = scores.filter((s) => s.status === 'LIVE');

  return (
    <Page>
      <CommanderHero
        commander="VIDUR"
        headline={`VIDUR ACTIVE. ${news.length} headlines intercepted, ${tracked.length} fixture${tracked.length === 1 ? '' : 's'} tracked, ${live.length} in play, ${catalysts.length} catalyst${catalysts.length === 1 ? '' : 's'} today.`}
        motif={MOTIFS.waves}
        detail={`News from RSS and newsapi.org scored for impact and credibility; scores, team sheets and injuries from ESPN; venue weather from Open-Meteo; steam from the odds ticks; all of it written to the twin's fortress and handed down the neural chain. Architect: ${credit}.`}
        actions={
          <>
            <Button variant="primary" icon="refresh" busy={wire.loading && wire.data !== undefined} onClick={() => { void wire.refresh(); void fixtures.refresh(); invalidate('the-wire:board'); }}>Refresh intercepts</Button>
            <TacticalAudioAlert armed={audio.armed} toggle={audio.toggle} supported={audio.supported} />
            {isAdmin && (
              <Button icon="radar" busy={scanning} onClick={async () => {
                setScanning(true);
                try {
                  await scanWire();
                } finally {
                  setScanning(false);
                  void wire.refresh();
                  void fixtures.refresh();
                  invalidate('the-wire:board', 'neural-bus');
                }
              }}>Scan now</Button>
            )}
          </>
        }
      />

      <ComponentGuideBanner
        className="lg:col-span-12"
        moduleName="The Wire · Intelligence board"
        purpose="Every story, absence, forecast and steam move the Wire holds on a tracked fixture, beside what the market measurably did and the fixture's best leg priced above fair. A breaking story or a catalyst also runs the neural chain: Score Predictor, Oracle, Consultant, Vault."
        steps={[
          "Filter by sport (Cricket and Football first) and by intelligence: lineup and injury, weather and pitch, odds and steam, tactical rotation",
          "Read column 3: the consensus since the story, the lineup model's delta, the friction factor or the steam move, and the best edge now (EV)",
          "Open the match in the Oracle, or Audit my slip: the chain re-scores your slip and shows each step's report on the pipeline bar",
        ]}
        kpiCodes={["EV", "SLIP_SCORE"]}
      />

      <LiveNewsTicker items={news} live={stream.connected} />

      <WireIntelligenceBoard className="lg:col-span-12" />

      <Panel title="Scoreboard" icon="scoreboard" className="lg:col-span-5" updatedAt={wire.updatedAt} subtitle={`${scores.length} tracked fixtures`}>
        <Async resource={wire} isEmpty={() => scores.length === 0} empty={<EmptyState icon="scoreboard" title="No scores for tracked fixtures" detail="Scores come from The Odds API for fixtures in the odds store (needs ODDS_API_KEY)." />}>
          {() => (
            <ul className="flex max-h-[420px] flex-col gap-1.5 overflow-y-auto">
              {[...scores].sort((a, b) => Number(b.status === 'LIVE') - Number(a.status === 'LIVE')).map((s) => (
                <li key={s.match_id} className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2 text-sm dark:bg-white/[0.03]">
                  <span className="min-w-0 truncate text-stone-700 dark:text-stone-200">{s.home_team} v {s.away_team}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    {s.status === 'LIVE' && (s.detail || s.clock) && <span className="font-mono text-[11px] text-emerald-600">{s.detail ?? s.clock}</span>}
                    {s.status !== 'SCHEDULED' && <span className="font-mono font-semibold tabular-nums">{s.home_score}-{s.away_score}</span>}
                    <StatusBadge status={s.status === 'FT' ? 'COMPLETED' : s.status === 'LIVE' ? 'RUNNING' : 'QUEUED'} label={s.status} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Async>
        <p className="mt-3 text-[11px] text-stone-400">The clock comes from ESPN where the Wire paired the fixture; otherwise The Odds API's score.</p>
      </Panel>

      <Panel title="Venue weather" icon="partly_cloudy_day" className="lg:col-span-7" updatedAt={fixtures.updatedAt} subtitle="Open-Meteo · friction factor Π">
        <Async resource={fixtures} isEmpty={() => weathered.length === 0} empty={<EmptyState icon="cloud_off" title="No forecasts yet" detail="The weather scan runs every 30 minutes. A fixture whose venue is unknown gets no weather, never a default climate." />}>
          {() => (
            <div className="grid max-h-[560px] grid-cols-1 gap-3 overflow-y-auto pr-1 2xl:grid-cols-2">
              {weathered.map((f) => f.weather && <MatchWeatherCard key={f.fixture_id} weather={f.weather} title={`${f.home} v ${f.away}`} />)}
            </div>
          )}
        </Async>
      </Panel>

      <Panel title="Absences & lineup delta" icon="personal_injury" className="lg:col-span-6" updatedAt={fixtures.updatedAt} subtitle="ESPN injury lists · operator ratings">
        <LineupImpactPanel fixtures={tracked} />
      </Panel>

      <Panel title="Referees" icon="sports" className="lg:col-span-6" subtitle="tendencies from officiating records">
        <RefereeBoard fixtures={tracked} />
      </Panel>

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
