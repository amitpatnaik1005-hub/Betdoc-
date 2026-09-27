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
  hidden: { opacity: 0, y: 12 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface ShadowBatch {
  id: string;
  targetFixture: string;
  totalTargetStake: number;
  executedStake: number;
  microBetsPlaced: number;
  status: 'routing' | 'executed' | 'ghosting';
}

interface StealthMetric {
  id: string;
  label: string;
  value: string;
  status: 'secure' | 'warning';
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const SHADOW_BATCHES: ShadowBatch[] = [
  {
    id: 'sb-001',
    targetFixture: 'CSK Match Winner',
    totalTargetStake: 50000,
    executedStake: 50000,
    microBetsPlaced: 42,
    status: 'executed',
  },
  {
    id: 'sb-002',
    targetFixture: 'IND vs AUS — Asian Handicap -1.5',
    totalTargetStake: 30000,
    executedStake: 18600,
    microBetsPlaced: 27,
    status: 'routing',
  },
  {
    id: 'sb-003',
    targetFixture: 'Man City — BTTS Yes',
    totalTargetStake: 20000,
    executedStake: 4800,
    microBetsPlaced: 9,
    status: 'ghosting',
  },
];

const STEALTH_METRICS: StealthMetric[] = [
  {
    id: 'sm-001',
    label: 'Digital Footprint',
    value: '0.00%',
    status: 'secure',
  },
  {
    id: 'sm-002',
    label: 'Proxy Rotation',
    value: '4m 12s ago',
    status: 'secure',
  },
  {
    id: 'sm-003',
    label: 'Slippage Avoided',
    value: '+₹1,240',
    status: 'secure',
  },
  {
    id: 'sm-004',
    label: 'Account Aging',
    value: 'FLAGGED',
    status: 'warning',
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const BATCH_STATUS_CONFIG: Record<
  ShadowBatch['status'],
  { color: string; label: string; icon: string }
> = {
  executed: { color: '#4ADE80', label: 'EXECUTED',  icon: 'check_circle'   },
  routing:  { color: '#FFFFFF', label: 'ROUTING',   icon: 'route'          },
  ghosting: { color: '#94A3B8', label: 'GHOSTING',  icon: 'blur_circular'  },
};

// ---------------------------------------------------------------------------
// UTILITY: FORMAT CURRENCY
// ---------------------------------------------------------------------------
const formatINR = (n: number): string =>
  n >= 1000 ? `₹${(n / 1000).toFixed(1)}K` : `₹${n}`;

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#94A3B8' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#94A3B8' }}
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
// SUB-COMPONENT: PHANTOM CONSOLE
// ---------------------------------------------------------------------------
const PhantomConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(255,255,255,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)',
    }}
  >
    {/* Glitching Fingerprint SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-10 -top-10 h-[400px] w-[400px]"
      viewBox="0 0 400 400"
      fill="none"
    >
      {/* 6 concentric glitching circles */}
      {[
        { r: 40,  duration: 2.1, delay: 0,    dashSeq: ['2 10', '15 5 5 15', '4 8',  '20 10'] },
        { r: 60,  duration: 3.4, delay: 0.3,  dashSeq: ['4 8',  '2 18',      '10 4', '6 12' ] },
        { r: 80,  duration: 1.8, delay: 0.7,  dashSeq: ['8 6',  '1 12 4 8',  '3 9',  '14 6' ] },
        { r: 100, duration: 2.7, delay: 0.15, dashSeq: ['3 9',  '12 4',      '2 14', '8 4'  ] },
        { r: 120, duration: 4.0, delay: 0.5,  dashSeq: ['6 10', '2 6 8 4',   '1 8',  '10 8' ] },
        { r: 140, duration: 2.4, delay: 0.9,  dashSeq: ['1 8',  '10 3 2 10', '5 5',  '16 4' ] },
      ].map((circle, i) => (
        <motion.circle
          key={i}
          cx="200"
          cy="200"
          r={circle.r}
          stroke="#FFFFFF"
          strokeWidth="1.5"
          fill="none"
          animate={{
            opacity:          [0, 0.6, 0.1, 0.8, 0],
            strokeDasharray:  circle.dashSeq,
            strokeDashoffset: [0, -12, -4, -20, 0],
          }}
          transition={{
            duration:   circle.duration,
            repeat:     Infinity,
            delay:      circle.delay,
            ease:       'linear',
          }}
        />
      ))}

      {/* Center void */}
      <motion.circle
        cx="200" cy="200" r="18"
        fill="#161514"
        stroke="#FFFFFF"
        strokeWidth="1"
        animate={{ opacity: [0.4, 1, 0.2, 0.9, 0.4] }}
        transition={{ duration: 1.6, repeat: Infinity }}
      />

      {/* Glitch horizontal scan lines */}
      {[160, 180, 200, 220, 240].map((y, i) => (
        <motion.line
          key={`scan-${i}`}
          x1="80" y1={y} x2="320" y2={y}
          stroke="#FFFFFF"
          strokeWidth="0.4"
          animate={{ opacity: [0, 0.3, 0, 0.15, 0], x1: [80, 85, 78, 82, 80] }}
          transition={{
            duration: 1.2 + i * 0.3,
            repeat: Infinity,
            delay: i * 0.18,
          }}
        />
      ))}
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full bg-white"
            animate={{ opacity: [1, 0.1, 0.8, 0.2, 1] }}
            transition={{ duration: 1.8, repeat: Infinity }}
          />
          <span className="text-xs font-bold uppercase tracking-[0.2em] text-slate-400">
            Stealth Protocol · PHANTOM
          </span>
        </div>

        <TypewriterLine text="PHANTOM PROTOCOL ACTIVE. Traces erased. Executing in dark pools." />

        <p className="max-w-xl text-sm text-slate-500">
          3 shadow batches active. 42 micro-fragments placed across 14 proxy
          nodes. Account aging flag detected — rotation queued.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {/* Scrub Footprint */}
        <motion.button
          whileHover={{ scale: 1.04, backgroundColor: 'rgba(255,255,255,0.08)' }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-slate-300 outline-none transition-colors"
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.12)',
          }}
        >
          <span className="material-symbols-outlined text-base">fingerprint</span>
          Scrub Footprint
        </motion.button>

        {/* Rotate VPN Nodes */}
        <motion.button
          whileHover={{ scale: 1.04, backgroundColor: 'rgba(255,255,255,0.08)' }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-slate-300 outline-none transition-colors"
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.12)',
          }}
        >
          <span className="material-symbols-outlined text-base">vpn_lock</span>
          Rotate VPN Nodes
        </motion.button>

        {/* Force Halt — danger */}
        <motion.button
          whileHover={{ scale: 1.04, backgroundColor: 'rgba(239,68,68,0.15)' }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-bold text-red-500 outline-none transition-colors"
          style={{
            background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.25)',
          }}
        >
          <span className="material-symbols-outlined text-base">emergency_stop</span>
          Force Halt
        </motion.button>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SHADOW BATCH CARD
// ---------------------------------------------------------------------------
const ShadowBatchCard = ({ batch }: { batch: ShadowBatch }) => {
  const cfg = BATCH_STATUS_CONFIG[batch.status];
  const pct = Math.min(
    100,
    Math.round((batch.executedStake / batch.totalTargetStake) * 100)
  );

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      whileHover={{ backgroundColor: 'rgba(255,255,255,0.04)' }}
      transition={SPRING}
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.02)',
        backdropFilter: 'blur(12px)',
        border: `1px solid ${cfg.color === '#4ADE80' ? 'rgba(74,222,128,0.2)' : 'rgba(255,255,255,0.07)'}`,
      }}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-sm font-bold text-slate-100 leading-tight">
            {batch.targetFixture}
          </span>
          <span className="font-mono text-[10px] text-slate-600">
            BATCH · {batch.id.toUpperCase()}
          </span>
        </div>

        {/* Status badge */}
        <div
          className="flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5"
          style={{
            background: `${cfg.color}12`,
            border: `1px solid ${cfg.color}30`,
          }}
        >
          {batch.status === 'routing' ? (
            <motion.span
              className="material-symbols-outlined text-[12px]"
              style={{ color: cfg.color }}
              animate={{ rotate: 360 }}
              transition={{ duration: 1.5, repeat: Infinity, ease: 'linear' }}
            >
              {cfg.icon}
            </motion.span>
          ) : (
            <span
              className="material-symbols-outlined text-[12px]"
              style={{ color: cfg.color }}
            >
              {cfg.icon}
            </span>
          )}
          <span
            className="font-mono text-[10px] font-bold uppercase tracking-widest"
            style={{ color: cfg.color }}
          >
            {cfg.label}
          </span>
        </div>
      </div>

      {/* Stake progress bar */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-600">
            Execution Progress
          </span>
          <span
            className="font-mono text-xs font-bold tabular-nums"
            style={{ color: cfg.color }}
          >
            {pct}%
          </span>
        </div>
        <div className="h-1 w-full rounded-full overflow-hidden bg-white/5">
          <motion.div
            className="h-full rounded-full"
            style={{ background: cfg.color }}
            initial={{ width: '0%' }}
            animate={{ width: `${pct}%` }}
            transition={{ ...SPRING, delay: 0.25 }}
          />
        </div>
      </div>

      {/* Telemetry grid */}
      <div className="grid grid-cols-3 gap-3 border-t border-white/5 pt-3">
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">
            Executed
          </span>
          <span
            className="font-mono text-sm font-bold tabular-nums"
            style={{ color: cfg.color }}
          >
            {formatINR(batch.executedStake)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">
            Target
          </span>
          <span className="font-mono text-sm font-bold tabular-nums text-slate-400">
            {formatINR(batch.totalTargetStake)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">
            Fragments
          </span>
          <span className="font-mono text-sm font-bold tabular-nums text-slate-300">
            {batch.microBetsPlaced}
          </span>
        </div>
      </div>

      {/* Micro-bet label */}
      <p className="font-mono text-[10px] text-slate-600">
        {batch.microBetsPlaced} micro-fragments placed across proxy network
      </p>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SHADOW LEDGER
// ---------------------------------------------------------------------------
const ShadowLedger = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="blur_on" label="Shadow Execution Ledger" />

    <motion.div
      className="grid grid-cols-1 gap-4"
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {SHADOW_BATCHES.map((batch) => (
          <ShadowBatchCard key={batch.id} batch={batch} />
        ))}
      </AnimatePresence>
    </motion.div>

    {/* Ledger footer */}
    <div
      className="flex items-center justify-between rounded-xl px-4 py-3"
      style={{
        background: 'rgba(74,222,128,0.04)',
        border: '1px solid rgba(74,222,128,0.12)',
      }}
    >
      <span className="font-mono text-[11px] text-slate-600">
        Total executed across 3 batches
      </span>
      <span
        className="font-mono text-sm font-bold tabular-nums"
        style={{ color: '#4ADE80' }}
      >
        {formatINR(73400)} / {formatINR(100000)}
      </span>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: STEALTH METRIC ROW
// ---------------------------------------------------------------------------
const StealthMetricRow = ({ metric }: { metric: StealthMetric }) => (
  <motion.div
    variants={ITEM_VARIANTS}
    className="flex items-center justify-between gap-3 py-3"
    style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}
  >
    <div className="flex items-center gap-2.5">
      {/* Status dot */}
      <div className="relative flex h-2.5 w-2.5 shrink-0 items-center justify-center">
        {metric.status === 'secure' ? (
          <>
            <span
              className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-40"
              style={{ background: '#4ADE80' }}
            />
            <span
              className="relative inline-flex h-1.5 w-1.5 rounded-full"
              style={{ background: '#4ADE80' }}
            />
          </>
        ) : (
          <span
            className="relative inline-flex h-1.5 w-1.5 rounded-full"
            style={{ background: '#475569' }}
          />
        )}
      </div>
      <span className="font-mono text-xs text-slate-400">{metric.label}</span>
    </div>

    <span
      className="font-mono text-xs font-bold tabular-nums"
      style={{
        color: metric.status === 'secure' ? '#4ADE80' : '#94A3B8',
      }}
    >
      {metric.value}
    </span>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: INCOGNITO TELEMETRY
// ---------------------------------------------------------------------------
const IncognitoTelemetry = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="security" label="Incognito Telemetry" />

    <div
      className="flex flex-col rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.02)',
        backdropFilter: 'blur(12px)',
        border: '1px dashed rgba(255,255,255,0.2)',
      }}
    >
      {/* Panel header */}
      <div className="flex items-center justify-between border-b border-white/5 pb-3 mb-1">
        <span className="font-mono text-xs font-bold text-slate-500">
          STEALTH STATUS
        </span>
        <motion.span
          className="font-mono text-[10px] font-bold"
          style={{ color: '#4ADE80' }}
          animate={{ opacity: [1, 0.3, 1] }}
          transition={{ duration: 1.4, repeat: Infinity }}
        >
          ● INCOGNITO
        </motion.span>
      </div>

      {/* Metric rows */}
      <motion.div
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {STEALTH_METRICS.map((metric) => (
            <StealthMetricRow key={metric.id} metric={metric} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* System integrity summary */}
      <div
        className="mt-4 flex flex-col gap-1.5 rounded-xl p-3"
        style={{
          background: 'rgba(74,222,128,0.05)',
          border: '1px solid rgba(74,222,128,0.12)',
        }}
      >
        <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">
          System Integrity
        </span>
        <div className="flex items-baseline gap-2">
          <span
            className="font-mono text-xl font-bold tabular-nums"
            style={{ color: '#4ADE80' }}
          >
            94.2%
          </span>
          <span className="font-mono text-[10px] text-slate-600">
            1 warning active
          </span>
        </div>
      </div>

      {/* ENGAGE SHADOW MODE — high contrast CTA */}
      <motion.button
        whileHover={{ scale: 1.02, backgroundColor: '#E2E8F0' }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl py-3 text-sm font-black uppercase tracking-widest"
        style={{
          background: '#FFFFFF',
          color: '#000000',
          boxShadow: '0 0 24px rgba(255,255,255,0.15)',
        }}
      >
        <span className="material-symbols-outlined text-base">
          visibility_off
        </span>
        ENGAGE SHADOW MODE
      </motion.button>

      {/* Disclaimer */}
      <p className="mt-3 text-center font-mono text-[9px] text-slate-700">
        All activity encrypted · Zero logs retained
      </p>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: PHANTOM
// ---------------------------------------------------------------------------
export const Phantom = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <PhantomConsole />
    <ShadowLedger />
    <IncognitoTelemetry />
  </motion.div>
);
