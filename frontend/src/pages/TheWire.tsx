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

const FEED_VARIANTS = {
  hidden: { opacity: 0 },
  show:   { opacity: 1, transition: { staggerChildren: 0.08 } },
};

const ITEM_VARIANTS = {
  hidden: { opacity: 0, x: -14 },
  show:   { opacity: 1, x: 0, transition: SPRING },
};

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface IntelIntercept {
  id: string;
  source: 'twitter' | 'news' | 'insider' | 'weather';
  timestamp: string;
  content: string;
  impact: 'bullish' | 'bearish' | 'neutral';
  credibilityScore: number;
}

interface SentimentVector {
  id: string;
  fixture: string;
  bullish: number;
  bearish: number;
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const INTERCEPTS: IntelIntercept[] = [
  {
    id: 'int-001',
    source: 'twitter',
    timestamp: '14:38:02',
    content:
      'BREAKING: Rohit Sharma ruled out of tonight\'s IPL fixture with hamstring strain. Replacement yet to be confirmed. #CSKvMI',
    impact: 'bearish',
    credibilityScore: 91,
  },
  {
    id: 'int-002',
    source: 'weather',
    timestamp: '14:35:17',
    content:
      'Wankhede Stadium forecast: Clear skies, 28°C, wind 12 km/h NE. Pitch expected to be batting-friendly. Dew factor: LOW.',
    impact: 'bullish',
    credibilityScore: 98,
  },
  {
    id: 'int-003',
    source: 'news',
    timestamp: '14:29:44',
    content:
      'Betfair API latency spike detected across South Asian nodes. Arbitrage windows may be wider than usual for next 20 minutes.',
    impact: 'bullish',
    credibilityScore: 76,
  },
  {
    id: 'int-004',
    source: 'insider',
    timestamp: '14:21:09',
    content:
      'Source: RCB dressing room morale low after back-to-back losses. Captain reportedly in disagreement with coaching staff.',
    impact: 'bearish',
    credibilityScore: 54,
  },
  {
    id: 'int-005',
    source: 'news',
    timestamp: '14:10:33',
    content:
      'BCCI confirms pitch curator change at Eden Gardens ahead of KKR home fixture. New curator known for spin-friendly surfaces.',
    impact: 'neutral',
    credibilityScore: 83,
  },
];

const SENTIMENT_VECTORS: SentimentVector[] = [
  { id: 'sv-001', fixture: 'CSK vs MI · IPL T20',       bullish: 62, bearish: 38 },
  { id: 'sv-002', fixture: 'Man City vs Arsenal · EPL',  bullish: 44, bearish: 56 },
  { id: 'sv-003', fixture: 'IND vs AUS · 3rd ODI',       bullish: 71, bearish: 29 },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const SOURCE_CONFIG: Record<
  IntelIntercept['source'],
  { icon: string; label: string; color: string }
> = {
  twitter:  { icon: 'tag',            label: 'X / Twitter',    color: '#A855F7' },
  news:     { icon: 'newspaper',      label: 'News Wire',      color: '#06B6D4' },
  insider:  { icon: 'person_search',  label: 'Insider',        color: '#EC4899' },
  weather:  { icon: 'partly_cloudy_day', label: 'Weather',     color: '#3B82F6' },
};

const IMPACT_CONFIG: Record<
  IntelIntercept['impact'],
  { borderColor: string; label: string; color: string }
> = {
  bullish: { borderColor: '#06B6D4', label: 'BULLISH', color: '#06B6D4' },
  bearish: { borderColor: '#EC4899', label: 'BEARISH', color: '#EC4899' },
  neutral: { borderColor: '#475569', label: 'NEUTRAL', color: '#94A3B8' },
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#A855F7' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#A855F7' }}
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
// SUB-COMPONENT: VIDUR CONSOLE
// ---------------------------------------------------------------------------
const VidurConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(168,85,247,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(168,85,247,0.15)',
    }}
  >
    {/* Global Network Graph SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-10 -top-10 h-[380px] w-[420px] opacity-20"
      viewBox="0 0 420 380"
      fill="none"
    >
      {/* Node positions */}
      {/* N0: 210,190 (center)  N1: 80,80   N2: 340,70
          N3: 60,290           N4: 360,300              */}

      {/* Edges with flowing dash animation */}
      {[
        { x1: 210, y1: 190, x2: 80,  y2: 80,  color: '#A855F7' },
        { x1: 210, y1: 190, x2: 340, y2: 70,  color: '#EC4899' },
        { x1: 210, y1: 190, x2: 60,  y2: 290, color: '#A855F7' },
        { x1: 210, y1: 190, x2: 360, y2: 300, color: '#EC4899' },
        { x1: 80,  y1: 80,  x2: 340, y2: 70,  color: '#A855F7' },
        { x1: 60,  y1: 290, x2: 360, y2: 300, color: '#EC4899' },
      ].map((edge, i) => (
        <motion.line
          key={i}
          x1={edge.x1} y1={edge.y1}
          x2={edge.x2} y2={edge.y2}
          stroke={edge.color}
          strokeWidth="1"
          strokeDasharray="4 8"
          animate={{ strokeDashoffset: [0, -24] }}
          transition={{
            duration: 1.2,
            repeat: Infinity,
            ease: 'linear',
            delay: i * 0.15,
          }}
        />
      ))}

      {/* Outer nodes */}
      {[
        { cx: 80,  cy: 80,  r: 8,  color: '#A855F7' },
        { cx: 340, cy: 70,  r: 6,  color: '#EC4899' },
        { cx: 60,  cy: 290, r: 7,  color: '#EC4899' },
        { cx: 360, cy: 300, r: 8,  color: '#A855F7' },
      ].map((node, i) => (
        <motion.circle
          key={i}
          cx={node.cx} cy={node.cy} r={node.r}
          fill={node.color}
          fillOpacity="0.3"
          stroke={node.color}
          strokeWidth="1.5"
          animate={{ opacity: [0.5, 1, 0.5], r: [node.r, node.r + 2, node.r] }}
          transition={{
            duration: 2.5 + i * 0.4,
            repeat: Infinity,
            ease: 'easeInOut',
          }}
        />
      ))}

      {/* Center hub node */}
      <motion.circle
        cx="210" cy="190" r="14"
        fill="#A855F7"
        fillOpacity="0.2"
        stroke="#A855F7"
        strokeWidth="2"
        animate={{ r: [14, 18, 14], opacity: [0.8, 1, 0.8] }}
        transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
      />
      <motion.circle
        cx="210" cy="190" r="5"
        fill="#A855F7"
        animate={{ opacity: [1, 0.4, 1] }}
        transition={{ duration: 1.4, repeat: Infinity }}
      />

      {/* Ping rings emanating from center */}
      {[30, 55, 80].map((r, i) => (
        <motion.circle
          key={`ping-${i}`}
          cx="210" cy="190" r={r}
          stroke="#A855F7"
          strokeWidth="0.5"
          fill="none"
          animate={{ opacity: [0.4, 0, 0.4], scale: [1, 1.1, 1] }}
          transition={{
            duration: 3,
            repeat: Infinity,
            delay: i * 0.8,
            ease: 'easeOut',
          }}
          style={{ transformOrigin: '210px 190px' }}
        />
      ))}
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#A855F7' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#A855F7' }}
          >
            Intel General · VIDUR
          </span>
        </div>

        <TypewriterLine text="VIDUR ACTIVE. Global sentiment mapped. Intercepting alpha streams." />

        <p className="max-w-xl text-sm text-slate-400">
          5 intercepts queued for analysis. Sentiment vectors computed across
          3 live fixtures. Spider network nominal across 14 data sources.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Scan X/Twitter',       icon: 'tag',           accent: '#A855F7' },
          { label: 'Scrape Injury Reports', icon: 'personal_injury', accent: '#06B6D4' },
          { label: 'Deploy Spiders',        icon: 'travel_explore', accent: '#EC4899' },
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
// SUB-COMPONENT: INTERCEPT CARD
// ---------------------------------------------------------------------------
const InterceptCard = ({ intercept }: { intercept: IntelIntercept }) => {
  const sourceCfg = SOURCE_CONFIG[intercept.source];
  const impactCfg = IMPACT_CONFIG[intercept.impact];

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      className="flex flex-col gap-3 rounded-xl p-4 transition-colors"
      style={{
        background: 'rgba(255,255,255,0.03)',
        border: `1px solid ${impactCfg.borderColor}33`,
      }}
      whileHover={{ backgroundColor: 'rgba(255,255,255,0.05)' }}
      transition={SPRING}
    >
      {/* Header row */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {/* Source badge */}
          <div
            className="flex items-center gap-1.5 rounded-md px-2 py-0.5"
            style={{
              background: `${sourceCfg.color}15`,
              border: `1px solid ${sourceCfg.color}30`,
            }}
          >
            <span
              className="material-symbols-outlined text-[12px]"
              style={{ color: sourceCfg.color }}
            >
              {sourceCfg.icon}
            </span>
            <span
              className="text-[10px] font-bold uppercase tracking-wider"
              style={{ color: sourceCfg.color }}
            >
              {sourceCfg.label}
            </span>
          </div>

          {/* Impact badge */}
          <span
            className="rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider"
            style={{
              background: `${impactCfg.color}15`,
              color: impactCfg.color,
              border: `1px solid ${impactCfg.color}30`,
            }}
          >
            {impactCfg.label}
          </span>
        </div>

        {/* Timestamp */}
        <span className="font-mono text-[11px] text-slate-600 shrink-0">
          {intercept.timestamp}
        </span>
      </div>

      {/* Content */}
      <p className="font-mono text-[12px] leading-relaxed text-slate-300">
        {intercept.content}
      </p>

      {/* Credibility score bar */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-widest text-slate-600">
            Credibility
          </span>
          <span
            className="font-mono text-[10px] font-bold"
            style={{ color: impactCfg.color }}
          >
            {intercept.credibilityScore}%
          </span>
        </div>
        <div className="h-1 w-full rounded-full overflow-hidden bg-white/5">
          <motion.div
            className="h-full rounded-full"
            style={{ background: impactCfg.color }}
            initial={{ width: '0%' }}
            animate={{ width: `${intercept.credibilityScore}%` }}
            transition={{ ...SPRING, delay: 0.3 }}
          />
        </div>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: INTERCEPT FEED
// ---------------------------------------------------------------------------
const InterceptFeed = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="wifi_tethering" label="Intercept Feed" />

    <div
      className="rounded-2xl overflow-hidden p-4 flex flex-col gap-3"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Terminal bar */}
      <div className="flex items-center gap-2 border-b border-white/5 pb-3">
        <motion.span
          className="h-2 w-2 rounded-full"
          style={{ background: '#A855F7' }}
          animate={{ opacity: [1, 0.2, 1] }}
          transition={{ duration: 0.9, repeat: Infinity }}
        />
        <span className="font-mono text-[10px] text-slate-500">
          vidur@wire:~$ stream --sources=all --live
        </span>
      </div>

      {/* Feed */}
      <motion.div
        className="flex flex-col gap-3"
        variants={FEED_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {INTERCEPTS.map((intercept) => (
            <InterceptCard key={intercept.id} intercept={intercept} />
          ))}
        </AnimatePresence>
      </motion.div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SENTIMENT VECTOR ROW
// ---------------------------------------------------------------------------
const SentimentVectorRow = ({
  vector,
  index,
}: {
  vector: SentimentVector;
  index: number;
}) => (
  <motion.div
    variants={ITEM_VARIANTS}
    className="flex flex-col gap-2 rounded-xl p-4"
    style={{
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.06)',
    }}
  >
    {/* Fixture label */}
    <span className="text-xs font-semibold text-slate-200 leading-tight">
      {vector.fixture}
    </span>

    {/* Percentage labels */}
    <div className="flex items-center justify-between">
      <span
        className="font-mono text-[11px] font-bold"
        style={{ color: '#06B6D4' }}
      >
        {vector.bullish}% Bullish
      </span>
      <span
        className="font-mono text-[11px] font-bold"
        style={{ color: '#EC4899' }}
      >
        {vector.bearish}% Bearish
      </span>
    </div>

    {/* Tug of War bar */}
    <div className="flex h-2 w-full overflow-hidden rounded-full">
      <motion.div
        style={{ background: '#06B6D4' }}
        initial={{ width: '50%' }}
        animate={{ width: `${vector.bullish}%` }}
        transition={{ ...SPRING, delay: 0.2 + index * 0.1 }}
      />
      <motion.div
        style={{ background: '#EC4899' }}
        initial={{ width: '50%' }}
        animate={{ width: `${vector.bearish}%` }}
        transition={{ ...SPRING, delay: 0.2 + index * 0.1 }}
      />
    </div>

    {/* Legend */}
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-1.5">
        <div className="h-1.5 w-1.5 rounded-full" style={{ background: '#06B6D4' }} />
        <span className="text-[10px] text-slate-500">Bullish</span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] text-slate-500">Bearish</span>
        <div className="h-1.5 w-1.5 rounded-full" style={{ background: '#EC4899' }} />
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SENTIMENT RADAR
// ---------------------------------------------------------------------------
const SentimentRadar = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="sensors" label="Sentiment Radar" />

    <div
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Panel header */}
      <div className="flex items-center justify-between border-b border-white/5 pb-3">
        <span className="text-xs font-bold text-slate-400">Live Fixture Sentiment</span>
        <span
          className="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: 'rgba(168,85,247,0.12)',
            color: '#A855F7',
            border: '1px solid rgba(168,85,247,0.3)',
          }}
        >
          <motion.span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: '#A855F7' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
          LIVE
        </span>
      </div>

      {/* Vector rows */}
      <motion.div
        className="flex flex-col gap-3"
        variants={FEED_VARIANTS}
        initial="hidden"
        animate="show"
      >
        {SENTIMENT_VECTORS.map((vector, i) => (
          <SentimentVectorRow key={vector.id} vector={vector} index={i} />
        ))}
      </motion.div>

      {/* Global sentiment summary */}
      <div
        className="flex flex-col gap-2 rounded-xl p-3 border-t border-white/5 pt-4"
      >
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Global Market Mood
        </span>
        <div className="flex items-center gap-3">
          <motion.span
            className="text-2xl font-bold"
            style={{ color: '#06B6D4' }}
            animate={{ opacity: [0.7, 1, 0.7] }}
            transition={{ duration: 2, repeat: Infinity }}
          >
            BULLISH
          </motion.span>
          <span className="text-xs text-slate-500">59% consensus across 3 fixtures</span>
        </div>
      </div>

      {/* Footer action */}
      <motion.button
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold"
        style={{
          background: 'rgba(168,85,247,0.08)',
          border: '1px solid rgba(168,85,247,0.25)',
          color: '#A855F7',
        }}
      >
        <span className="material-symbols-outlined text-base">refresh</span>
        Refresh Vectors
      </motion.button>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE WIRE
// ---------------------------------------------------------------------------
export const TheWire = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <VidurConsole />
    <InterceptFeed />
    <SentimentRadar />
  </motion.div>
);
