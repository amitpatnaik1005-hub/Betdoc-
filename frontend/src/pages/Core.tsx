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
interface Subsystem {
  id: string;
  name: string;
  status: 'online' | 'degraded' | 'offline';
  latency: string;
  uptime: string;
  cpu: number;
}

interface RiskLimit {
  id: string;
  metric: string;
  currentValue: number;
  maxLimit: number;
  format: 'currency' | 'percentage';
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const SUBSYSTEMS: Subsystem[] = [
  {
    id: 'sys-001',
    name: 'WebSocket Feed',
    status: 'online',
    latency: '18ms',
    uptime: '99.97%',
    cpu: 34,
  },
  {
    id: 'sys-002',
    name: 'Postgres DB',
    status: 'online',
    latency: '4ms',
    uptime: '100%',
    cpu: 21,
  },
  {
    id: 'sys-003',
    name: 'Redis Cache',
    status: 'degraded',
    latency: '142ms',
    uptime: '98.41%',
    cpu: 67,
  },
  {
    id: 'sys-004',
    name: 'Execution Engine',
    status: 'online',
    latency: '42ms',
    uptime: '99.88%',
    cpu: 48,
  },
];

const RISK_LIMITS: RiskLimit[] = [
  {
    id: 'risk-001',
    metric: 'Max Exposure per Market',
    currentValue: 42000,
    maxLimit: 50000,
    format: 'currency',
  },
  {
    id: 'risk-002',
    metric: 'Drawdown Limit',
    currentValue: 13.4,
    maxLimit: 20,
    format: 'percentage',
  },
  {
    id: 'risk-003',
    metric: 'API Rate Limit',
    currentValue: 340,
    maxLimit: 500,
    format: 'percentage',
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const STATUS_CONFIG: Record<
  Subsystem['status'],
  { color: string; label: string; pingColor: string }
> = {
  online:   { color: '#22C55E', label: 'ONLINE',   pingColor: '#22C55E' },
  degraded: { color: '#F97316', label: 'DEGRADED', pingColor: '#F97316' },
  offline:  { color: '#EF4444', label: 'OFFLINE',  pingColor: '#EF4444' },
};

// ---------------------------------------------------------------------------
// HELPER: RISK COLOR
// ---------------------------------------------------------------------------
const getRiskColor = (percentage: number): string => {
  if (percentage > 80) return '#EF4444';
  if (percentage > 60) return '#F97316';
  return '#10B981';
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#F97316' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#F97316' }}
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
// SUB-COMPONENT: PRATAP CONSOLE
// ---------------------------------------------------------------------------
const PratapConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(249,115,22,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(249,115,22,0.15)',
    }}
  >
    {/* Reactor Core SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-12 -top-12 h-[400px] w-[400px]"
      viewBox="0 0 400 400"
      fill="none"
      style={{ opacity: 0.2 }}
    >
      {/* Outer orbit */}
      <motion.circle
        cx="200" cy="200" r="170"
        stroke="#F97316"
        strokeWidth="0.75"
        strokeDasharray="6 10"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 24, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />
      {/* Mid orbit */}
      <motion.circle
        cx="200" cy="200" r="120"
        stroke="#EF4444"
        strokeWidth="0.75"
        strokeDasharray="4 8"
        fill="none"
        animate={{ rotate: -360 }}
        transition={{ duration: 16, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />
      {/* Inner orbit */}
      <motion.circle
        cx="200" cy="200" r="70"
        stroke="#F97316"
        strokeWidth="0.5"
        strokeDasharray="3 6"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 10, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Orbiting satellite dots */}
      {[
        { orbit: 170, speed: 24, color: '#F97316', startDeg: 0   },
        { orbit: 120, speed: 16, color: '#EF4444', startDeg: 120 },
        { orbit: 70,  speed: 10, color: '#F97316', startDeg: 240 },
      ].map((sat, i) => (
        <motion.circle
          key={i}
          cx={200 + sat.orbit}
          cy={200}
          r={5}
          fill={sat.color}
          fillOpacity="0.7"
          animate={{ rotate: 360 }}
          transition={{
            duration: sat.speed,
            repeat: Infinity,
            ease: 'linear',
          }}
          style={{ transformOrigin: '200px 200px' }}
        />
      ))}

      {/* Central hexagon — reactor core */}
      <motion.polygon
        points="200,120 270,160 270,240 200,280 130,240 130,160"
        fill="#F97316"
        fillOpacity="0.08"
        stroke="#F97316"
        strokeWidth="1.5"
        animate={{
          scale: [1, 1.06, 1],
          opacity: [0.6, 1, 0.6],
        }}
        transition={{ duration: 2.5, repeat: Infinity, ease: 'easeInOut' }}
        style={{ transformOrigin: '200px 200px' }}
      />

      {/* Inner hexagon fill pulse */}
      <motion.polygon
        points="200,148 248,174 248,226 200,252 152,226 152,174"
        fill="#EF4444"
        fillOpacity="0.12"
        stroke="#EF4444"
        strokeWidth="1"
        animate={{ opacity: [0.4, 0.9, 0.4] }}
        transition={{ duration: 1.8, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* Core center dot */}
      <motion.circle
        cx="200" cy="200" r="8"
        fill="#F97316"
        animate={{ r: [8, 12, 8], opacity: [0.8, 1, 0.8] }}
        transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
      />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#F97316' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#F97316' }}
          >
            System Guardian · PRATAP
          </span>
        </div>

        <TypewriterLine text="PRATAP ACTIVE. System core stable. Hard risk limits strictly enforced." />

        <p className="max-w-xl text-sm text-slate-400">
          4 subsystems monitored. Redis Cache flagged as degraded — latency
          spike under investigation. All risk limits within mandate thresholds.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {/* Lock All Markets — danger red */}
        <motion.button
          whileHover={{
            scale: 1.04,
            backgroundColor: 'rgba(239,68,68,0.2)',
            borderColor: 'rgba(239,68,68,0.5)',
          }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-bold outline-none transition-colors"
          style={{
            background: 'rgba(239,68,68,0.1)',
            border: '1px solid rgba(239,68,68,0.3)',
            color: '#EF4444',
          }}
        >
          <span className="material-symbols-outlined text-base">lock</span>
          Lock All Markets
        </motion.button>

        {/* Restart Engine */}
        <motion.button
          whileHover={{ scale: 1.04 }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold outline-none transition-colors"
          style={{
            background: 'rgba(249,115,22,0.1)',
            border: '1px solid rgba(249,115,22,0.3)',
            color: '#F97316',
          }}
        >
          <span className="material-symbols-outlined text-base">restart_alt</span>
          Restart Engine
        </motion.button>

        {/* View Error Logs */}
        <motion.button
          whileHover={{ scale: 1.04 }}
          whileTap={{ scale: 0.97 }}
          transition={SPRING}
          className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold outline-none transition-colors"
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.1)',
            color: '#94A3B8',
          }}
        >
          <span className="material-symbols-outlined text-base">terminal</span>
          View Error Logs
        </motion.button>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SUBSYSTEM CARD
// ---------------------------------------------------------------------------
const SubsystemCard = ({
  subsystem,
}: {
  subsystem: Subsystem;
}) => {
  const statusCfg = STATUS_CONFIG[subsystem.status];
  const cpuColor  = getRiskColor(subsystem.cpu);

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      whileHover={{ y: -3, boxShadow: '0 0 20px rgba(249,115,22,0.1)' }}
      transition={SPRING}
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2.5">
          {/* Pulsing status dot */}
          <div className="relative flex h-3 w-3 shrink-0 items-center justify-center">
            <span
              className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-50"
              style={{ background: statusCfg.pingColor }}
            />
            <span
              className="relative inline-flex h-2 w-2 rounded-full"
              style={{ background: statusCfg.color }}
            />
          </div>
          <span className="text-sm font-bold text-slate-100">{subsystem.name}</span>
        </div>

        {/* Status badge */}
        <span
          className="shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: `${statusCfg.color}18`,
            color: statusCfg.color,
            border: `1px solid ${statusCfg.color}33`,
          }}
        >
          {statusCfg.label}
        </span>
      </div>

      {/* Telemetry row */}
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Latency
          </span>
          <span
            className="font-mono text-sm font-bold tabular-nums"
            style={{
              color: subsystem.status === 'degraded' ? '#F97316' : '#22C55E',
            }}
          >
            {subsystem.latency}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Uptime
          </span>
          <span className="font-mono text-sm font-bold tabular-nums text-slate-200">
            {subsystem.uptime}
          </span>
        </div>
      </div>

      {/* CPU load bar */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            CPU Load
          </span>
          <span
            className="font-mono text-xs font-bold tabular-nums"
            style={{ color: cpuColor }}
          >
            {subsystem.cpu}%
          </span>
        </div>
        <div className="h-1.5 w-full rounded-full overflow-hidden bg-white/5">
          <motion.div
            className="h-full rounded-full"
            style={{ background: cpuColor }}
            initial={{ width: '0%' }}
            animate={{ width: `${subsystem.cpu}%` }}
            transition={{ ...SPRING, delay: 0.25 }}
          />
        </div>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SUBSYSTEM DIAGNOSTICS
// ---------------------------------------------------------------------------
const SubsystemDiagnostics = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="developer_board" label="Subsystem Diagnostics" />

    <motion.div
      className="grid grid-cols-1 sm:grid-cols-2 gap-4"
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {SUBSYSTEMS.map((subsystem) => (
          <SubsystemCard key={subsystem.id} subsystem={subsystem} />
        ))}
      </AnimatePresence>
    </motion.div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: RISK LIMIT ROW
// ---------------------------------------------------------------------------
const RiskLimitRow = ({
  limit,
  index,
}: {
  limit: RiskLimit;
  index: number;
}) => {
  const pct   = Math.round((limit.currentValue / limit.maxLimit) * 100);
  const color = getRiskColor(pct);

  const formatValue = (val: number, fmt: RiskLimit['format']) => {
    if (fmt === 'currency') {
      return val >= 1000
        ? `₹${(val / 1000).toFixed(1)}K`
        : `₹${val}`;
    }
    return `${val}%`;
  };

  const formatMax = (val: number, fmt: RiskLimit['format']) => {
    if (fmt === 'currency') {
      return val >= 1000 ? `₹${(val / 1000).toFixed(0)}K` : `₹${val}`;
    }
    return `${val}%`;
  };

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      className="flex flex-col gap-2.5 rounded-xl p-4"
      style={{
        background: 'rgba(255,255,255,0.03)',
        border: `1px solid ${color}22`,
      }}
    >
      {/* Metric label + percentage */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-slate-200 leading-tight">
          {limit.metric}
        </span>
        <span
          className="shrink-0 font-mono text-xs font-bold tabular-nums"
          style={{ color }}
        >
          {pct}%
        </span>
      </div>

      {/* Progress bar */}
      <div className="h-2 w-full rounded-full overflow-hidden bg-white/5">
        <motion.div
          className="h-full rounded-full"
          style={{ background: color }}
          initial={{ width: '0%' }}
          animate={{ width: `${pct}%` }}
          transition={{ ...SPRING, delay: 0.2 + index * 0.1 }}
        />
      </div>

      {/* Current vs Max */}
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] font-bold tabular-nums" style={{ color }}>
          {formatValue(limit.currentValue, limit.format)}
        </span>
        <span className="font-mono text-[11px] text-slate-600 tabular-nums">
          max {formatMax(limit.maxLimit, limit.format)}
        </span>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: RISK LIMITS
// ---------------------------------------------------------------------------
const RiskLimits = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="shield" label="Risk Limits" />

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
        <span className="text-xs font-bold text-slate-400">Hard Limits · Live</span>
        <span
          className="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: 'rgba(249,115,22,0.12)',
            color: '#F97316',
            border: '1px solid rgba(249,115,22,0.3)',
          }}
        >
          <motion.span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: '#F97316' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          />
          ENFORCED
        </span>
      </div>

      {/* Risk rows */}
      <motion.div
        className="flex flex-col gap-3"
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {RISK_LIMITS.map((limit, i) => (
            <RiskLimitRow key={limit.id} limit={limit} index={i} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Overall risk summary */}
      <div
        className="flex flex-col gap-1.5 rounded-xl p-3"
        style={{
          background: 'rgba(239,68,68,0.06)',
          border: '1px solid rgba(239,68,68,0.15)',
        }}
      >
        <span className="text-[10px] uppercase tracking-widest text-slate-500">
          Highest Risk Metric
        </span>
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-300">
            Max Exposure per Market
          </span>
          <span
            className="font-mono text-sm font-bold"
            style={{ color: '#EF4444' }}
          >
            84%
          </span>
        </div>
        <p className="text-[10px] text-slate-500">
          Approaching hard ceiling. PRATAP monitoring closely.
        </p>
      </div>

      {/* Footer action */}
      <motion.button
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold"
        style={{
          background: 'rgba(249,115,22,0.08)',
          border: '1px solid rgba(249,115,22,0.25)',
          color: '#F97316',
        }}
      >
        <span className="material-symbols-outlined text-base">tune</span>
        Adjust Risk Limits
      </motion.button>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: CORE
// ---------------------------------------------------------------------------
export const Core = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <PratapConsole />
    <SubsystemDiagnostics />
    <RiskLimits />
  </motion.div>
);
