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

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface MarketData {
  id: string;
  fixture: string;
  bookmaker: string;
  backOdds: string;
  layOdds: string;
  trend: 'drifting' | 'steaming' | 'stable';
  liquidity: string;
}

interface TacticalAlert {
  id: string;
  time: string;
  message: string;
  type: 'arb' | 'execution' | 'scan';
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const MARKET_DATA: MarketData[] = [
  {
    id: 'mkt-001',
    fixture: 'CSK vs MI · IPL T20',
    bookmaker: 'Betfair Exchange',
    backOdds: '1.94',
    layOdds: '1.96',
    trend: 'steaming',
    liquidity: '₹2.4L',
  },
  {
    id: 'mkt-002',
    fixture: 'RCB vs KKR · IPL T20',
    bookmaker: 'Smarkets',
    backOdds: '2.10',
    layOdds: '2.14',
    trend: 'drifting',
    liquidity: '₹1.1L',
  },
  {
    id: 'mkt-003',
    fixture: 'IND vs AUS · ODI',
    bookmaker: 'Betfair Exchange',
    backOdds: '1.72',
    layOdds: '1.74',
    trend: 'stable',
    liquidity: '₹5.8L',
  },
  {
    id: 'mkt-004',
    fixture: 'SRH vs PBKS · IPL T20',
    bookmaker: 'Matchbook',
    backOdds: '3.05',
    layOdds: '3.15',
    trend: 'drifting',
    liquidity: '₹0.7L',
  },
];

const TACTICAL_ALERTS: TacticalAlert[] = [
  {
    id: 'alert-001',
    time: '14:32:07',
    message: 'ARB detected · CSK/MI · 2.1% edge · Betfair vs Smarkets',
    type: 'arb',
  },
  {
    id: 'alert-002',
    time: '14:31:54',
    message: 'Order executed · IND ML · ₹12,500 @ 1.72 · Betfair',
    type: 'execution',
  },
  {
    id: 'alert-003',
    time: '14:31:22',
    message: 'Full scan complete · 4,208 markets · 3 edges flagged',
    type: 'scan',
  },
  {
    id: 'alert-004',
    time: '14:30:48',
    message: 'ARB detected · RCB/KKR · 1.4% edge · Matchbook vs Betfair',
    type: 'arb',
  },
  {
    id: 'alert-005',
    time: '14:30:11',
    message: 'Latency spike · 142ms · Betfair API · Auto-throttled',
    type: 'execution',
  },
];

const TREND_CONFIG: Record<
  MarketData['trend'],
  { label: string; color: string; icon: string }
> = {
  steaming: { label: 'STEAMING', color: '#22C55E', icon: 'trending_down'  },
  drifting: { label: 'DRIFTING', color: '#EF4444', icon: 'trending_up'    },
  stable:   { label: 'STABLE',   color: '#94A3B8', icon: 'trending_flat'  },
};

const ALERT_CONFIG: Record<
  TacticalAlert['type'],
  { color: string; icon: string; prefix: string }
> = {
  arb:       { color: '#F59E0B', icon: 'bolt',          prefix: 'ARB'  },
  execution: { color: '#3B82F6', icon: 'send',          prefix: 'EXEC' },
  scan:      { color: '#A855F7', icon: 'radar',         prefix: 'SCAN' },
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#EF4444' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#EF4444' }}
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
// SUB-COMPONENT: BAJIRAO CONSOLE
// ---------------------------------------------------------------------------
const BajiraoConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(255,255,255,0.04)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)',
    }}
  >
    {/* Radar / Targeting Reticle SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-16 -top-16 h-[380px] w-[380px] opacity-15"
      viewBox="0 0 380 380"
      fill="none"
    >
      {/* Outer rotating ring */}
      <motion.circle
        cx="190" cy="190" r="170"
        stroke="#EF4444"
        strokeWidth="1"
        strokeDasharray="4 12"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 8, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '190px 190px' }}
      />
      {/* Mid ring counter-rotating */}
      <motion.circle
        cx="190" cy="190" r="120"
        stroke="#F59E0B"
        strokeWidth="0.75"
        strokeDasharray="4 12"
        fill="none"
        animate={{ rotate: -360 }}
        transition={{ duration: 6, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '190px 190px' }}
      />
      {/* Inner ring */}
      <motion.circle
        cx="190" cy="190" r="70"
        stroke="#EF4444"
        strokeWidth="0.5"
        strokeDasharray="2 8"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 4, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '190px 190px' }}
      />
      {/* Crosshair lines */}
      <line x1="190" y1="20"  x2="190" y2="360" stroke="#EF4444" strokeWidth="0.4" opacity="0.4" />
      <line x1="20"  y1="190" x2="360" y2="190" stroke="#EF4444" strokeWidth="0.4" opacity="0.4" />
      {/* Center dot */}
      <motion.circle
        cx="190" cy="190" r="4"
        fill="#EF4444"
        animate={{ opacity: [1, 0.2, 1] }}
        transition={{ duration: 1.2, repeat: Infinity }}
      />
      {/* Sweep line */}
      <motion.line
        x1="190" y1="190" x2="190" y2="22"
        stroke="#F59E0B"
        strokeWidth="1.5"
        opacity="0.6"
        animate={{ rotate: 360 }}
        transition={{ duration: 3, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '190px 190px' }}
      />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full bg-red-500"
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.0, repeat: Infinity }}
          />
          <span className="text-xs font-bold uppercase tracking-[0.2em] text-red-400">
            Execution General · BAJIRAO
          </span>
        </div>

        <TypewriterLine text="BAJIRAO ACTIVE. Scouting 14 order books. Ready for execution." />

        <p className="max-w-xl text-sm text-slate-400">
          All exchange connections live. Arbitrage scanner running at 4,208 markets.
          Awaiting execution directive.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {/* Force Sync Odds */}
        <motion.button
          whileHover={{ scale: 1.04 }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-slate-200 outline-none transition-colors"
          style={{
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.1)',
          }}
        >
          <span className="material-symbols-outlined text-base">sync</span>
          Force Sync Odds
        </motion.button>

        {/* Scan Arbitrage */}
        <motion.button
          whileHover={{ scale: 1.04 }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-amber-400 outline-none transition-colors"
          style={{
            background: 'rgba(245,158,11,0.08)',
            border: '1px solid rgba(245,158,11,0.25)',
          }}
        >
          <span className="material-symbols-outlined text-base">radar</span>
          Scan Arbitrage
        </motion.button>

        {/* Halt Trading — red danger button */}
        <motion.button
          whileHover={{ scale: 1.04, backgroundColor: 'rgba(239,68,68,0.2)' }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-red-500 outline-none transition-colors"
          style={{
            background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.25)',
          }}
        >
          <span className="material-symbols-outlined text-base">emergency_stop</span>
          Halt Trading
        </motion.button>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: ODDS ROW
// ---------------------------------------------------------------------------
const OddsRow = ({ market, index }: { market: MarketData; index: number }) => {
  const trend = TREND_CONFIG[market.trend];

  return (
    <motion.div
      initial={{ opacity: 0, x: -12 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ ...SPRING, delay: 0.06 * index }}
      className="grid grid-cols-12 items-center gap-4 py-4 px-4"
    >
      {/* Fixture + Bookmaker — 4 cols */}
      <div className="col-span-12 sm:col-span-4 flex flex-col gap-0.5">
        <span className="text-sm font-semibold text-slate-100 leading-tight">
          {market.fixture}
        </span>
        <span className="text-[11px] text-slate-500">{market.bookmaker}</span>
      </div>

      {/* Back Odds — 2 cols */}
      <div className="col-span-3 sm:col-span-2 flex flex-col gap-0.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">Back</span>
        <motion.span
          className="font-mono tabular-nums text-lg font-bold text-emerald-400"
          animate={{
            backgroundColor: [
              'rgba(34,197,94,0.3)',
              'rgba(0,0,0,0)',
            ],
          }}
          transition={{ duration: 0.8, delay: 0.3 + 0.1 * index }}
        >
          {market.backOdds}
        </motion.span>
      </div>

      {/* Lay Odds — 2 cols */}
      <div className="col-span-3 sm:col-span-2 flex flex-col gap-0.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">Lay</span>
        <motion.span
          className="font-mono tabular-nums text-lg font-bold text-red-400"
          animate={{
            backgroundColor: [
              'rgba(239,68,68,0.3)',
              'rgba(0,0,0,0)',
            ],
          }}
          transition={{ duration: 0.8, delay: 0.4 + 0.1 * index }}
        >
          {market.layOdds}
        </motion.span>
      </div>

      {/* Trend — 2 cols */}
      <div className="col-span-3 sm:col-span-2 flex items-center gap-1.5">
        <span
          className="material-symbols-outlined text-base"
          style={{ color: trend.color }}
        >
          {trend.icon}
        </span>
        <span
          className="text-[10px] font-bold uppercase tracking-wider"
          style={{ color: trend.color }}
        >
          {trend.label}
        </span>
      </div>

      {/* Liquidity — 2 cols */}
      <div className="col-span-3 sm:col-span-2 flex flex-col gap-0.5 text-right">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">Liquidity</span>
        <span className="font-mono tabular-nums text-sm font-semibold text-slate-300">
          {market.liquidity}
        </span>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: LIVE ODDS MATRIX
// ---------------------------------------------------------------------------
const LiveOddsMatrix = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="table_rows" label="Live Odds Matrix" />

    <div
      className="rounded-2xl overflow-hidden"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Table header */}
      <div className="grid grid-cols-12 gap-4 border-b border-white/5 px-4 py-2.5">
        {['Fixture / Book', 'Back', 'Lay', 'Trend', 'Liquidity'].map((h) => (
          <span
            key={h}
            className={[
              'text-[10px] font-bold uppercase tracking-widest text-slate-500',
              h === 'Fixture / Book' ? 'col-span-4' : 'col-span-2',
              h === 'Liquidity' ? 'text-right' : '',
            ].join(' ')}
          >
            {h}
          </span>
        ))}
      </div>

      {/* Rows */}
      <div className="divide-y divide-white/5">
        {MARKET_DATA.map((market, i) => (
          <OddsRow key={market.id} market={market} index={i} />
        ))}
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TACTICAL FEED
// ---------------------------------------------------------------------------
const TacticalFeed = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="terminal" label="Tactical Feed" />

    <div
      className="flex flex-col rounded-2xl overflow-hidden"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Terminal header bar */}
      <div className="flex items-center gap-2 border-b border-white/5 px-4 py-2.5">
        <span className="h-2.5 w-2.5 rounded-full bg-red-500/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-amber-500/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-500/70" />
        <span className="ml-2 text-[10px] font-mono text-slate-500">
          bajirao@arena:~$ tail -f tactical.log
        </span>
      </div>

      {/* Log entries */}
      <ul className="flex flex-col divide-y divide-white/5">
        <AnimatePresence>
          {TACTICAL_ALERTS.map((alert, i) => {
            const cfg = ALERT_CONFIG[alert.type];
            return (
              <motion.li
                key={alert.id}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 10 }}
                transition={{ ...SPRING, delay: 0.08 * i }}
                className="flex flex-col gap-1 px-4 py-3"
              >
                <div className="flex items-center gap-2">
                  {/* Type badge */}
                  <span
                    className="flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                    style={{
                      background: `${cfg.color}18`,
                      color: cfg.color,
                      border: `1px solid ${cfg.color}33`,
                    }}
                  >
                    <span className="material-symbols-outlined text-[10px]">
                      {cfg.icon}
                    </span>
                    {cfg.prefix}
                  </span>

                  {/* Timestamp */}
                  <span className="font-mono text-[10px] text-slate-600">
                    {alert.time}
                  </span>
                </div>

                {/* Message */}
                <p className="font-mono text-[11px] leading-relaxed text-slate-400">
                  {alert.message}
                </p>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ARENA
// ---------------------------------------------------------------------------
export const TheArena = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <BajiraoConsole />
    <LiveOddsMatrix />
    <TacticalFeed />
  </motion.div>
);
