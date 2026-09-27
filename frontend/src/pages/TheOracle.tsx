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

const ITEM_VARIANTS = {
  hidden: { opacity: 0, y: 14 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface MatchPrediction {
  id: string;
  fixture: string;
  league: string;
  homeWinPct: number;
  drawPct: number;
  awayWinPct: number;
  confidenceScore: number;
}

interface OracleBet {
  id: string;
  fixture: string;
  selection: string;
  impliedProbability: number;
  trueProbability: number;
  edge: number;
  recommendedStake: string;
}

// ---------------------------------------------------------------------------
// STATIC DATA  (homeWinPct + drawPct + awayWinPct === 100 for every row)
// ---------------------------------------------------------------------------
const MATCH_PREDICTIONS: MatchPrediction[] = [
  {
    id: 'pred-001',
    fixture: 'CSK vs MI',
    league: 'IPL T20',
    homeWinPct: 58,
    drawPct: 4,
    awayWinPct: 38,
    confidenceScore: 87,
  },
  {
    id: 'pred-002',
    fixture: 'Man City vs Arsenal',
    league: 'Premier League',
    homeWinPct: 47,
    drawPct: 26,
    awayWinPct: 27,
    confidenceScore: 74,
  },
  {
    id: 'pred-003',
    fixture: 'IND vs AUS',
    league: '3rd ODI',
    homeWinPct: 62,
    drawPct: 3,
    awayWinPct: 35,
    confidenceScore: 91,
  },
  {
    id: 'pred-004',
    fixture: 'Djokovic vs Alcaraz',
    league: 'ATP Finals',
    homeWinPct: 44,
    drawPct: 0,
    awayWinPct: 56,
    confidenceScore: 68,
  },
];

const ORACLE_BETS: OracleBet[] = [
  {
    id: 'ob-001',
    fixture: 'CSK vs MI',
    selection: 'CSK Match Winner',
    impliedProbability: 51.5,
    trueProbability: 58.0,
    edge: 14.2,
    recommendedStake: '₹18,500',
  },
  {
    id: 'ob-002',
    fixture: 'IND vs AUS · 3rd ODI',
    selection: 'India — Asian Handicap -1.5',
    impliedProbability: 44.8,
    trueProbability: 53.6,
    edge: 19.6,
    recommendedStake: '₹12,000',
  },
];

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#3B82F6' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#3B82F6' }}
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
// SUB-COMPONENT: ORACLE CORE (CONSOLE)
// ---------------------------------------------------------------------------
const OracleCore = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(59,130,246,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(59,130,246,0.15)',
    }}
  >
    {/* Astrolabe Cyber-Eye SVG */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-10 -top-10 h-[400px] w-[400px]"
      viewBox="0 0 400 400"
      fill="none"
      style={{ opacity: 0.22 }}
    >
      {/* Outer astrolabe ring 1 — rotateZ */}
      <motion.ellipse
        cx="200" cy="200" rx="160" ry="160"
        stroke="#3B82F6"
        strokeWidth="1"
        strokeDasharray="6 10"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 22, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Outer astrolabe ring 2 — tilted, counter-rotate */}
      <motion.ellipse
        cx="200" cy="200" rx="160" ry="60"
        stroke="#A855F7"
        strokeWidth="1"
        strokeDasharray="4 12"
        fill="none"
        animate={{ rotate: -360 }}
        transition={{ duration: 16, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Mid gyroscope ring */}
      <motion.ellipse
        cx="200" cy="200" rx="110" ry="110"
        stroke="#3B82F6"
        strokeWidth="0.75"
        strokeDasharray="3 8"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 12, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Inner eye — horizontal ellipse */}
      <ellipse
        cx="200" cy="200" rx="90" ry="40"
        fill="#3B82F6"
        fillOpacity="0.06"
        stroke="#3B82F6"
        strokeWidth="1.5"
      />

      {/* Iris — spinning dashed circle */}
      <motion.circle
        cx="200" cy="200" r="35"
        stroke="#A855F7"
        strokeWidth="1.5"
        strokeDasharray="4 12"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 5, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Iris inner fill glow */}
      <motion.circle
        cx="200" cy="200" r="35"
        fill="#A855F7"
        fillOpacity="0.08"
        animate={{ opacity: [0.08, 0.18, 0.08] }}
        transition={{ duration: 2.4, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* Pupil — pulsing solid */}
      <motion.circle
        cx="200" cy="200" r="12"
        fill="#3B82F6"
        fillOpacity="0.9"
        animate={{ scale: [1, 1.3, 1], opacity: [0.8, 1, 0.8] }}
        transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Orbital data points */}
      {[0, 72, 144, 216, 288].map((deg, i) => {
        const rad = (deg * Math.PI) / 180;
        const cx  = 200 + 110 * Math.cos(rad);
        const cy  = 200 + 110 * Math.sin(rad);
        return (
          <motion.circle
            key={i}
            cx={cx} cy={cy} r={4}
            fill="#3B82F6"
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 2, repeat: Infinity, delay: i * 0.35 }}
          />
        );
      })}
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
            Predictive Engine · THE ORACLE
          </span>
        </div>

        <TypewriterLine text="ORACLE ACTIVE. Calculating 14,000,605 future timelines. Identifying absolute value." />

        <p className="max-w-xl text-sm text-slate-400">
          4 fixtures under active simulation. 2 golden alpha bets identified
          with positive expected value. Monte Carlo sweep complete.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Run Monte Carlo',    icon: 'casino',        accent: '#3B82F6' },
          { label: 'Calibrate Models',   icon: 'tune',          accent: '#A855F7' },
          { label: 'Export Predictions', icon: 'download',      accent: '#94A3B8' },
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
// SUB-COMPONENT: PREDICTION CARD
// ---------------------------------------------------------------------------
const PredictionCard = ({
  pred,
  index,
}: {
  pred: MatchPrediction;
  index: number;
}) => (
  <motion.div
    variants={ITEM_VARIANTS}
    whileHover={{ y: -2, boxShadow: '0 0 20px rgba(59,130,246,0.1)' }}
    transition={SPRING}
    className="flex flex-col gap-4 rounded-2xl p-5"
    style={{
      background: 'rgba(255,255,255,0.04)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.07)',
    }}
  >
    {/* Header row */}
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-bold text-slate-100 leading-tight">
          {pred.fixture}
        </span>
        <span className="text-[11px] text-slate-500">{pred.league}</span>
      </div>

      {/* Confidence badge — glowing circle */}
      <div
        className="flex h-12 w-12 shrink-0 flex-col items-center justify-center rounded-full"
        style={{
          background: 'rgba(59,130,246,0.12)',
          border: '1.5px solid rgba(59,130,246,0.4)',
          boxShadow: `0 0 14px rgba(59,130,246,${pred.confidenceScore / 300})`,
        }}
      >
        <span
          className="font-mono text-sm font-bold tabular-nums leading-none"
          style={{ color: '#3B82F6' }}
        >
          {pred.confidenceScore}
        </span>
        <span className="text-[8px] uppercase tracking-wider text-slate-500">
          conf
        </span>
      </div>
    </div>

    {/* Tri-bar probability */}
    <div className="flex flex-col gap-2">
      <div className="flex h-2 w-full overflow-hidden rounded-full">
        {/* Home — Blue */}
        <motion.div
          style={{ background: '#3B82F6' }}
          initial={{ width: '33.33%' }}
          animate={{ width: `${pred.homeWinPct}%` }}
          transition={{ ...SPRING, delay: 0.2 + index * 0.06 }}
        />
        {/* Draw — Slate */}
        <motion.div
          style={{ background: '#475569' }}
          initial={{ width: '33.33%' }}
          animate={{ width: `${pred.drawPct}%` }}
          transition={{ ...SPRING, delay: 0.25 + index * 0.06 }}
        />
        {/* Away — Amethyst */}
        <motion.div
          style={{ background: '#A855F7' }}
          initial={{ width: '33.33%' }}
          animate={{ width: `${pred.awayWinPct}%` }}
          transition={{ ...SPRING, delay: 0.3 + index * 0.06 }}
        />
      </div>

      {/* Percentage labels */}
      <div className="flex items-center justify-between text-[10px] font-mono font-bold tabular-nums">
        <span style={{ color: '#3B82F6' }}>{pred.homeWinPct}% Home</span>
        {pred.drawPct > 0 && (
          <span style={{ color: '#94A3B8' }}>{pred.drawPct}% Draw</span>
        )}
        <span style={{ color: '#A855F7' }}>{pred.awayWinPct}% Away</span>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: MATCH PREDICTOR MATRIX
// ---------------------------------------------------------------------------
const MatchPredictorMatrix = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="query_stats" label="Match Predictor Matrix" />

    <motion.div
      className="grid grid-cols-1 gap-4"
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {MATCH_PREDICTIONS.map((pred, i) => (
          <PredictionCard key={pred.id} pred={pred} index={i} />
        ))}
      </AnimatePresence>
    </motion.div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: ORACLE BET CARD
// ---------------------------------------------------------------------------
const OracleBetCard = ({
  bet,
  index,
}: {
  bet: OracleBet;
  index: number;
}) => (
  <motion.div
    variants={ITEM_VARIANTS}
    className="flex flex-col gap-4 rounded-2xl p-5"
    style={{
      background: 'rgba(245,158,11,0.05)',
      border: '1px solid rgba(245,158,11,0.25)',
      boxShadow: '0 0 24px rgba(245,158,11,0.08)',
    }}
    animate={{
      boxShadow: [
        '0 0 16px rgba(245,158,11,0.06)',
        '0 0 28px rgba(245,158,11,0.16)',
        '0 0 16px rgba(245,158,11,0.06)',
      ],
    }}
    transition={{ duration: 2.5, repeat: Infinity, ease: 'easeInOut', delay: index * 0.6 }}
  >
    {/* Gold crown header */}
    <div className="flex items-center gap-2">
      <span
        className="material-symbols-outlined text-base"
        style={{ color: '#F59E0B' }}
      >
        workspace_premium
      </span>
      <span
        className="text-[10px] font-bold uppercase tracking-[0.2em]"
        style={{ color: '#F59E0B' }}
      >
        Golden Alpha · #{index + 1}
      </span>
    </div>

    {/* Fixture + selection */}
    <div className="flex flex-col gap-0.5">
      <span className="text-sm font-bold text-slate-100 leading-tight">
        {bet.fixture}
      </span>
      <span
        className="text-xs font-semibold"
        style={{ color: '#F59E0B' }}
      >
        {bet.selection}
      </span>
    </div>

    {/* Probability comparison */}
    <div
      className="grid grid-cols-2 gap-3 rounded-xl p-3"
      style={{ background: 'rgba(255,255,255,0.03)' }}
    >
      <div className="flex flex-col gap-0.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Implied Prob
        </span>
        <span className="font-mono text-lg font-bold tabular-nums text-slate-400">
          {bet.impliedProbability}%
        </span>
      </div>
      <div className="flex flex-col gap-0.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          True Prob
        </span>
        <span
          className="font-mono text-lg font-bold tabular-nums"
          style={{ color: '#22C55E' }}
        >
          {bet.trueProbability}%
        </span>
      </div>
    </div>

    {/* Edge highlight */}
    <div
      className="flex items-center justify-between rounded-xl px-4 py-2.5"
      style={{
        background: 'rgba(34,197,94,0.08)',
        border: '1px solid rgba(34,197,94,0.2)',
      }}
    >
      <span className="text-xs text-slate-400">Expected Edge</span>
      <span
        className="font-mono text-base font-bold tabular-nums"
        style={{ color: '#22C55E' }}
      >
        +{bet.edge}%
      </span>
    </div>

    {/* Stake */}
    <div className="flex items-center justify-between">
      <span className="text-xs text-slate-500">Kelly Stake</span>
      <span className="font-mono text-sm font-bold tabular-nums text-slate-200">
        {bet.recommendedStake}
      </span>
    </div>

    {/* Execute button */}
    <motion.button
      whileHover={{ scale: 1.03 }}
      whileTap={{ scale: 0.97 }}
      transition={SPRING}
      className="flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold"
      style={{
        background: 'linear-gradient(135deg, rgba(245,158,11,0.2), rgba(245,158,11,0.1))',
        border: '1px solid rgba(245,158,11,0.4)',
        color: '#F59E0B',
        boxShadow: '0 0 12px rgba(245,158,11,0.15)',
      }}
    >
      <span className="material-symbols-outlined text-base">bolt</span>
      Execute Oracle Bet
    </motion.button>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: GOLDEN ALPHA
// ---------------------------------------------------------------------------
const GoldenAlpha = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="workspace_premium" label="Golden Alpha" />

    <div className="flex flex-col gap-4">
      <motion.div
        className="flex flex-col gap-4"
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {ORACLE_BETS.map((bet, i) => (
            <OracleBetCard key={bet.id} bet={bet} index={i} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Panel footer summary */}
      <div
        className="flex flex-col gap-1.5 rounded-xl p-4"
        style={{
          background: 'rgba(59,130,246,0.06)',
          border: '1px solid rgba(59,130,246,0.15)',
        }}
      >
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Combined EV
        </span>
        <div className="flex items-baseline gap-2">
          <span
            className="font-mono text-xl font-bold tabular-nums"
            style={{ color: '#22C55E' }}
          >
            +16.9%
          </span>
          <span className="text-xs text-slate-500">avg edge across 2 bets</span>
        </div>
        <p className="text-[10px] text-slate-600">
          Oracle confidence: HIGH. Proceed with Kelly allocation.
        </p>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ORACLE
// ---------------------------------------------------------------------------
export const TheOracle = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <OracleCore />
    <MatchPredictorMatrix />
    <GoldenAlpha />
  </motion.div>
);
