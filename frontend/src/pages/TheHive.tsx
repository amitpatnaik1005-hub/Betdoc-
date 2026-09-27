import { motion, AnimatePresence } from 'framer-motion';

// ---------------------------------------------------------------------------
// PHYSICS
// ---------------------------------------------------------------------------
const SPRING = { type: 'spring', stiffness: 350, damping: 30 } as const;

const CARD_VARIANTS = {
  hidden: { opacity: 0, y: 18 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

const GRID_VARIANTS = {
  hidden: { opacity: 0 },
  show:   { opacity: 1, transition: { staggerChildren: 0.1 } },
};

const SWARM_VARIANTS = {
  hidden: { opacity: 0 },
  show:   { opacity: 1, transition: { staggerChildren: 0.09 } },
};

const FIREHOSE_VARIANTS = {
  hidden: { opacity: 0 },
  show:   { opacity: 1, transition: { staggerChildren: 0.08 } },
};

const FIREHOSE_ITEM = {
  hidden: { opacity: 0, y: -20 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

const SWARM_ITEM = {
  hidden: { opacity: 0, y: 14 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface ScraperNode {
  id: string;
  bookmaker: string;
  status: 'crawling' | 'blocked' | 'idle';
  rpm: number;
  latencyMs: number;
}

interface RawOddsPacket {
  id: string;
  bookmaker: string;
  fixture: string;
  market: string;
  value: string;
  timestamp: string;
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const SCRAPER_NODES: ScraperNode[] = [
  {
    id: 'scr-001',
    bookmaker: 'Pinnacle async_1',
    status: 'crawling',
    rpm: 840,
    latencyMs: 22,
  },
  {
    id: 'scr-002',
    bookmaker: 'Betfair Firehose',
    status: 'crawling',
    rpm: 1240,
    latencyMs: 18,
  },
  {
    id: 'scr-003',
    bookmaker: 'DraftKings Poller',
    status: 'blocked',
    rpm: 0,
    latencyMs: 9999,
  },
  {
    id: 'scr-004',
    bookmaker: 'Smarkets Stream',
    status: 'idle',
    rpm: 120,
    latencyMs: 64,
  },
];

const RAW_ODDS_PACKETS: RawOddsPacket[] = [
  {
    id: 'pkt-001',
    bookmaker: 'Pinnacle',
    fixture: 'CSK vs MI',
    market: 'Match Odds',
    value: '1.94',
    timestamp: '14:38:02.441',
  },
  {
    id: 'pkt-002',
    bookmaker: 'Betfair',
    fixture: 'IND vs AUS',
    market: 'Over 2.5 Goals',
    value: '2.10',
    timestamp: '14:38:02.389',
  },
  {
    id: 'pkt-003',
    bookmaker: 'Smarkets',
    fixture: 'RCB vs KKR',
    market: 'Asian Handicap -1',
    value: '1.87',
    timestamp: '14:38:01.912',
  },
  {
    id: 'pkt-004',
    bookmaker: 'Pinnacle',
    fixture: 'Man City vs Arsenal',
    market: 'BTTS — Yes',
    value: '1.72',
    timestamp: '14:38:01.774',
  },
  {
    id: 'pkt-005',
    bookmaker: 'Betfair',
    fixture: 'SRH vs PBKS',
    market: 'Match Odds',
    value: '3.05',
    timestamp: '14:38:01.603',
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const STATUS_CONFIG: Record<
  ScraperNode['status'],
  { color: string; label: string; icon: string; spin: boolean }
> = {
  crawling: { color: '#F59E0B', label: 'CRAWLING', icon: 'sync',          spin: true  },
  blocked:  { color: '#EF4444', label: 'BLOCKED',  icon: 'block',         spin: false },
  idle:     { color: '#94A3B8', label: 'IDLE',     icon: 'pause_circle',  spin: false },
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#F59E0B' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#F59E0B' }}
    >
      {label}
    </span>
  </div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TYPEWRITER LINE
// ---------------------------------------------------------------------------
const TypewriterLine = ({ text }: { text: string }) => {
  const words = text.split(' ');
  return (
    <h2 className="max-w-2xl text-xl font-bold tracking-tight text-slate-100 lg:text-2xl">
      {words.map((word, i) => (
        <motion.span
          key={i}
          className="inline-block mr-[0.3em]"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ ...SPRING, delay: 0.04 * i }}
        >
          {word}
        </motion.span>
      ))}
    </h2>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: HIVE MIND CONSOLE
// ---------------------------------------------------------------------------
const HiveMindConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(245,158,11,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(245,158,11,0.15)',
    }}
  >
    {/* Honeycomb SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute right-0 top-0 h-full w-[420px] opacity-20"
      viewBox="0 0 400 400"
      fill="none"
    >
      {/* Tessellated hexagons with crawling dash animation */}
      {[
        'M170,150 L185,176 L215,176 L230,150 L215,124 L185,124 Z',
        'M215,176 L230,202 L260,202 L275,176 L260,150 L230,150 Z',
        'M170,202 L185,228 L215,228 L230,202 L215,176 L185,176 Z',
      ].map((d, i) => (
        <motion.path
          key={i}
          d={d}
          stroke="#F59E0B"
          strokeWidth="1.5"
          strokeDasharray="4 8"
          fill="#F59E0B"
          fillOpacity="0.04"
          animate={{ strokeDashoffset: [0, -24] }}
          transition={{
            duration: 1.4,
            repeat: Infinity,
            ease: 'linear',
            delay: i * 0.3,
          }}
        />
      ))}

      {/* Extended honeycomb — outer ring of partial hexes for depth */}
      {[
        'M125,124 L140,150 L170,150 L185,124 L170,98 L140,98 Z',
        'M260,124 L275,150 L305,150 L320,124 L305,98 L275,98 Z',
        'M125,202 L140,228 L170,228 L185,202 L170,176 L140,176 Z',
        'M260,202 L275,228 L305,228 L320,202 L305,176 L275,176 Z',
        'M170,254 L185,280 L215,280 L230,254 L215,228 L185,228 Z',
        'M215,98  L230,124 L260,124 L275,98  L260,72  L230,72  Z',
      ].map((d, i) => (
        <motion.path
          key={`outer-${i}`}
          d={d}
          stroke="#F59E0B"
          strokeWidth="0.75"
          strokeDasharray="3 10"
          fill="none"
          opacity={0.35}
          animate={{ strokeDashoffset: [0, -26] }}
          transition={{
            duration: 2,
            repeat: Infinity,
            ease: 'linear',
            delay: i * 0.2,
          }}
        />
      ))}

      {/* Node dots at hex vertices */}
      {[
        [200, 124], [230, 150], [230, 202], [200, 228],
        [170, 202], [170, 150], [260, 150], [260, 202],
      ].map(([cx, cy], i) => (
        <motion.circle
          key={`node-${i}`}
          cx={cx} cy={cy} r={3}
          fill="#F59E0B"
          animate={{ opacity: [0.4, 1, 0.4] }}
          transition={{
            duration: 1.8,
            repeat: Infinity,
            delay: i * 0.22,
          }}
        />
      ))}

      {/* Central glow */}
      <motion.circle
        cx="215" cy="176" r="28"
        fill="#F59E0B"
        fillOpacity="0.06"
        stroke="#F59E0B"
        strokeWidth="0.75"
        animate={{ r: [28, 34, 28], opacity: [0.5, 1, 0.5] }}
        transition={{ duration: 2.4, repeat: Infinity, ease: 'easeInOut' }}
      />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#F59E0B' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#F59E0B' }}
          >
            Autonomous Scraper Network · THE HIVE
          </span>
        </div>

        <TypewriterLine text="HIVE ACTIVE. 409 bookmakers indexed. 12,400 markets scanning asynchronously." />

        <p className="max-w-xl text-sm text-slate-400">
          4 scraper nodes deployed. DraftKings node blocked — proxy rotation
          queued. Firehose ingesting 1,240 req/min from Betfair exchange.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Spawn New Crawler', icon: 'add_circle',    accent: '#F59E0B' },
          { label: 'Rotate Proxies',    icon: 'vpn_lock',      accent: '#06B6D4' },
          { label: 'Flush Cache',       icon: 'delete_sweep',  accent: '#EF4444' },
        ].map(({ label, icon, accent }) => (
          <motion.button
            key={label}
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.97 }}
            transition={SPRING}
            className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold outline-none transition-colors"
            style={{
              background: `${accent}10`,
              border: `1px solid ${accent}30`,
              color: accent,
            }}
          >
            <span className="material-symbols-outlined text-base">{icon}</span>
            {label}
          </motion.button>
        ))}
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SCRAPER CARD
// ---------------------------------------------------------------------------
const ScraperCard = ({ node }: { node: ScraperNode }) => {
  const cfg = STATUS_CONFIG[node.status];

  return (
    <motion.div
      variants={SWARM_ITEM}
      whileHover={{ y: -3, boxShadow: '0 0 20px rgba(245,158,11,0.12)' }}
      transition={SPRING}
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: `1px solid ${cfg.color}22`,
      }}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2.5">
          {/* Status icon — spinning if crawling */}
          {cfg.spin ? (
            <motion.span
              className="material-symbols-outlined text-xl shrink-0"
              style={{ color: cfg.color }}
              animate={{ rotate: 360 }}
              transition={{ duration: 1.2, repeat: Infinity, ease: 'linear' }}
            >
              {cfg.icon}
            </motion.span>
          ) : (
            <span
              className="material-symbols-outlined text-xl shrink-0"
              style={{ color: cfg.color }}
            >
              {cfg.icon}
            </span>
          )}
          <span className="text-sm font-bold text-slate-100 leading-tight">
            {node.bookmaker}
          </span>
        </div>

        {/* Status badge */}
        <span
          className="shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: `${cfg.color}18`,
            color: cfg.color,
            border: `1px solid ${cfg.color}33`,
          }}
        >
          {cfg.label}
        </span>
      </div>

      {/* RPM — hero metric */}
      <div className="flex flex-col gap-0.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Requests / Min
        </span>
        <span
          className="font-mono text-3xl font-bold tabular-nums leading-none"
          style={{ color: node.status === 'blocked' ? '#EF4444' : '#06B6D4' }}
        >
          {node.rpm.toLocaleString()}
        </span>
      </div>

      {/* Latency */}
      <div className="flex items-center justify-between border-t border-white/5 pt-3">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Latency
        </span>
        <span
          className="font-mono text-sm font-bold tabular-nums"
          style={{
            color:
              node.latencyMs > 1000
                ? '#EF4444'
                : node.latencyMs > 50
                ? '#F59E0B'
                : '#22C55E',
          }}
        >
          {node.latencyMs > 1000 ? 'TIMEOUT' : `${node.latencyMs}ms`}
        </span>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SCRAPER SWARM
// ---------------------------------------------------------------------------
const ScraperSwarm = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-7 flex flex-col gap-4"
  >
    <SectionLabel icon="hub" label="Scraper Swarm Matrix" />

    <motion.div
      className="grid grid-cols-1 sm:grid-cols-2 gap-4"
      variants={SWARM_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {SCRAPER_NODES.map((node) => (
          <ScraperCard key={node.id} node={node} />
        ))}
      </AnimatePresence>
    </motion.div>

    {/* Swarm summary footer */}
    <div
      className="flex items-center justify-between rounded-xl px-4 py-3"
      style={{
        background: 'rgba(245,158,11,0.06)',
        border: '1px solid rgba(245,158,11,0.15)',
      }}
    >
      <div className="flex items-center gap-2">
        <motion.span
          className="h-2 w-2 rounded-full"
          style={{ background: '#F59E0B' }}
          animate={{ opacity: [1, 0.3, 1] }}
          transition={{ duration: 1, repeat: Infinity }}
        />
        <span className="text-xs text-slate-400">
          Total throughput
        </span>
      </div>
      <span
        className="font-mono text-sm font-bold tabular-nums"
        style={{ color: '#06B6D4' }}
      >
        2,200 req/min
      </span>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: FIREHOSE PACKET CARD
// ---------------------------------------------------------------------------
const FirehoseCard = ({ packet }: { packet: RawOddsPacket }) => (
  <motion.div
    variants={FIREHOSE_ITEM}
    className="rounded-xl p-3 font-mono text-[10px] leading-relaxed"
    style={{
      background: 'rgba(6,182,212,0.04)',
      border: '1px solid rgba(6,182,212,0.15)',
    }}
    whileHover={{ backgroundColor: 'rgba(6,182,212,0.08)' }}
    transition={SPRING}
  >
    {/* Timestamp + bookmaker header */}
    <div className="flex items-center justify-between mb-1.5">
      <span className="text-slate-600">{packet.timestamp}</span>
      <span
        className="rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider"
        style={{
          background: 'rgba(245,158,11,0.12)',
          color: '#F59E0B',
          border: '1px solid rgba(245,158,11,0.25)',
        }}
      >
        {packet.bookmaker}
      </span>
    </div>

    {/* Raw packet body */}
    <div className="text-slate-400 space-y-0.5">
      <div>
        <span className="text-slate-600">fixture: </span>
        <span className="text-slate-300">"{packet.fixture}"</span>
      </div>
      <div>
        <span className="text-slate-600">market:  </span>
        <span className="text-slate-300">"{packet.market}"</span>
      </div>
      <div>
        <span className="text-slate-600">value:   </span>
        <span
          className="font-bold"
          style={{ color: '#06B6D4' }}
        >
          {packet.value}
        </span>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: DATA FIREHOSE
// ---------------------------------------------------------------------------
const DataFirehose = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-5 flex flex-col gap-4"
  >
    <SectionLabel icon="bolt" label="Data Firehose" />

    <div
      className="flex flex-col rounded-2xl overflow-hidden"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Terminal header */}
      <div className="flex items-center gap-2 border-b border-white/5 px-4 py-2.5">
        <motion.span
          className="h-2 w-2 rounded-full"
          style={{ background: '#06B6D4' }}
          animate={{ opacity: [1, 0.2, 1] }}
          transition={{ duration: 0.7, repeat: Infinity }}
        />
        <span className="font-mono text-[10px] text-slate-500">
          hive@firehose:~$ stream --raw --live --bookmakers=all
        </span>
      </div>

      {/* Packet feed */}
      <motion.div
        className="flex flex-col gap-2 p-4"
        variants={FIREHOSE_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {RAW_ODDS_PACKETS.map((packet) => (
            <FirehoseCard key={packet.id} packet={packet} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Footer */}
      <div
        className="flex items-center justify-between border-t border-white/5 px-4 py-2.5"
        style={{ background: 'rgba(6,182,212,0.04)' }}
      >
        <span className="font-mono text-[10px] text-slate-600">
          5 packets · live stream
        </span>
        <motion.span
          className="font-mono text-[10px] font-bold"
          style={{ color: '#06B6D4' }}
          animate={{ opacity: [1, 0.4, 1] }}
          transition={{ duration: 1.1, repeat: Infinity }}
        >
          ● STREAMING
        </motion.span>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE HIVE
// ---------------------------------------------------------------------------
export const TheHive = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <HiveMindConsole />
    <ScraperSwarm />
    <DataFirehose />
  </motion.div>
);
