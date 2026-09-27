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
interface BacktestResult {
  id: string;
  strategyName: string;
  dataset: string;
  roi: number;
  maxDrawdown: number;
  sharpeRatio: number;
}

interface StorageNode {
  id: string;
  name: string;
  type: 'glacier' | 'postgres' | 'redis';
  capacityPct: number;
  status: 'indexing' | 'idle' | 'archiving';
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const BACKTEST_RESULTS: BacktestResult[] = [
  {
    id: 'bt-001',
    strategyName: 'Asian Handicap Scalper',
    dataset: 'EPL 2018–2023',
    roi: 14.2,
    maxDrawdown: 4.1,
    sharpeRatio: 2.14,
  },
  {
    id: 'bt-002',
    strategyName: 'IPL Bayesian Win Model',
    dataset: 'IPL 2015–2024',
    roi: 9.7,
    maxDrawdown: 7.8,
    sharpeRatio: 1.82,
  },
  {
    id: 'bt-003',
    strategyName: 'Tennis Surface ELO',
    dataset: 'ATP/WTA 2019–2024',
    roi: -2.3,
    maxDrawdown: 18.4,
    sharpeRatio: 0.61,
  },
  {
    id: 'bt-004',
    strategyName: 'Kelly Compound Arb',
    dataset: 'Multi-Sport 2020–2024',
    roi: 22.8,
    maxDrawdown: 11.2,
    sharpeRatio: 3.07,
  },
];

const STORAGE_NODES: StorageNode[] = [
  {
    id: 'sn-001',
    name: 'AWS S3 Glacier',
    type: 'glacier',
    capacityPct: 71,
    status: 'archiving',
  },
  {
    id: 'sn-002',
    name: 'Postgres Cold Storage',
    type: 'postgres',
    capacityPct: 48,
    status: 'indexing',
  },
  {
    id: 'sn-003',
    name: 'Redis Hot Cache',
    type: 'redis',
    capacityPct: 23,
    status: 'idle',
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const NODE_TYPE_CONFIG: Record<
  StorageNode['type'],
  { icon: string; color: string }
> = {
  glacier:  { icon: 'ac_unit',    color: '#64748B' },
  postgres: { icon: 'storage',    color: '#B45309' },
  redis:    { icon: 'bolt',       color: '#F59E0B' },
};

const NODE_STATUS_CONFIG: Record<
  StorageNode['status'],
  { icon: string; label: string; spin: boolean }
> = {
  indexing:  { icon: 'sync',    label: 'INDEXING',  spin: true  },
  archiving: { icon: 'archive', label: 'ARCHIVING', spin: false },
  idle:      { icon: 'pause',   label: 'IDLE',      spin: false },
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#B45309' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#B45309' }}
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
// SUB-COMPONENT: DEEP STORAGE CONSOLE
// ---------------------------------------------------------------------------

// LED blink configs — pre-defined to avoid inline random calls on every render
const LED_CONFIGS = [
  [
    { dur: 1.2, delay: 0.0 }, { dur: 2.1, delay: 0.4 }, { dur: 1.7, delay: 0.9 },
    { dur: 0.9, delay: 0.2 }, { dur: 1.5, delay: 0.7 }, { dur: 2.4, delay: 0.1 },
  ],
  [
    { dur: 1.8, delay: 0.6 }, { dur: 1.1, delay: 0.3 }, { dur: 2.2, delay: 0.8 },
    { dur: 1.4, delay: 0.0 }, { dur: 0.8, delay: 0.5 }, { dur: 1.9, delay: 1.1 },
  ],
  [
    { dur: 2.0, delay: 0.2 }, { dur: 1.3, delay: 0.7 }, { dur: 1.6, delay: 0.4 },
    { dur: 2.3, delay: 0.9 }, { dur: 1.0, delay: 0.1 }, { dur: 1.7, delay: 0.6 },
  ],
];

// LED color alternates between amber and bronze per rack
const LED_COLORS = ['#F59E0B', '#B45309', '#F59E0B', '#64748B', '#F59E0B', '#B45309'];

const DeepStorageConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(180,83,9,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(180,83,9,0.15)',
    }}
  >
    {/* Server Rack SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-8 top-0 h-full w-[420px] opacity-[0.18]"
      viewBox="0 0 400 400"
      fill="none"
    >
      {/* Rack chassis outline */}
      <rect
        x="140" y="80" width="200" height="240" rx="6"
        stroke="#B45309" strokeWidth="1" fill="none"
      />
      {/* Rack mounting rails */}
      <line x1="148" y1="80"  x2="148" y2="320" stroke="#B45309" strokeWidth="0.5" opacity="0.5" />
      <line x1="332" y1="80"  x2="332" y2="320" stroke="#B45309" strokeWidth="0.5" opacity="0.5" />

      {/* 3 server units */}
      {[100, 160, 220].map((y, rackIdx) => (
        <g key={y}>
          {/* Server chassis */}
          <rect
            x="150" y={y} width="180" height="40" rx="4"
            stroke="#B45309" strokeWidth="1.5" fill="rgba(180,83,9,0.06)"
          />
          {/* Vent slots */}
          {[0, 1, 2].map((slot) => (
            <rect
              key={slot}
              x={158 + slot * 8} y={y + 14} width="4" height="12" rx="1"
              fill="#B45309" fillOpacity="0.3"
            />
          ))}
          {/* Drive bay indicator */}
          <rect
            x="190" y={y + 10} width="60" height="20" rx="2"
            stroke="#64748B" strokeWidth="0.75" fill="none"
          />
          {/* LED indicators */}
          {LED_CONFIGS[rackIdx].map((cfg, ledIdx) => (
            <motion.circle
              key={ledIdx}
              cx={270 + ledIdx * 9}
              cy={y + 20}
              r={3}
              fill={LED_COLORS[ledIdx]}
              animate={{ opacity: [0.2, 1, 0.3, 0.9, 0.1, 0.8, 0.2] }}
              transition={{
                duration: cfg.dur,
                repeat: Infinity,
                delay: cfg.delay,
                ease: 'linear',
              }}
            />
          ))}
        </g>
      ))}

      {/* Cable management — bottom */}
      {[160, 185, 210, 235, 260, 285, 310].map((x, i) => (
        <motion.path
          key={i}
          d={`M${x},320 C${x},340 ${x + 10},345 ${x + 5},360`}
          stroke="#64748B"
          strokeWidth="1"
          fill="none"
          animate={{ opacity: [0.2, 0.5, 0.2] }}
          transition={{ duration: 2 + i * 0.3, repeat: Infinity, delay: i * 0.2 }}
        />
      ))}

      {/* Power indicator top */}
      <motion.circle
        cx="320" cy="90" r="5"
        fill="#22C55E"
        animate={{ opacity: [0.6, 1, 0.6] }}
        transition={{ duration: 1.8, repeat: Infinity }}
      />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#B45309' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.6, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#B45309' }}
          >
            Data Lake · THE ARCHIVE
          </span>
        </div>

        <TypewriterLine text="ARCHIVE ACTIVE. 4.2 Billion historical market states indexed. Ready for backtesting." />

        <p className="max-w-xl text-sm text-slate-400">
          4 backtest strategies on record. AWS Glacier at 71% capacity —
          archiving in progress. Postgres cold storage indexing new EPL dataset.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Run Backtest',  icon: 'play_circle',  accent: '#F59E0B' },
          { label: 'Query Ledger', icon: 'manage_search', accent: '#B45309' },
          { label: 'Export CSV',   icon: 'download',      accent: '#64748B' },
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
// SUB-COMPONENT: BACKTEST CARD
// ---------------------------------------------------------------------------
const BacktestCard = ({ result, index }: { result: BacktestResult; index: number }) => {
  const roiPositive    = result.roi > 0;
  const drawdownDanger = result.maxDrawdown > 15;

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      whileHover={{ y: -2, boxShadow: '0 0 20px rgba(180,83,9,0.12)' }}
      transition={SPRING}
      className="rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 items-center">

        {/* Col 1: Strategy + Dataset */}
        <div className="col-span-2 md:col-span-1 flex flex-col gap-0.5">
          <span className="text-sm font-bold text-slate-100 leading-tight">
            {result.strategyName}
          </span>
          <span className="text-[11px] text-slate-500">{result.dataset}</span>
        </div>

        {/* Col 2: ROI */}
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            ROI
          </span>
          <span
            className="font-mono text-xl font-bold tabular-nums"
            style={{ color: roiPositive ? '#22C55E' : '#EF4444' }}
          >
            {roiPositive ? '+' : ''}{result.roi}%
          </span>
        </div>

        {/* Col 3: Max Drawdown */}
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Max DD
          </span>
          <span
            className="font-mono text-xl font-bold tabular-nums"
            style={{ color: drawdownDanger ? '#EF4444' : '#94A3B8' }}
          >
            {result.maxDrawdown}%
          </span>
        </div>

        {/* Col 4: Sharpe Ratio */}
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Sharpe
          </span>
          <span
            className="font-mono text-2xl font-black tabular-nums"
            style={{
              color: result.sharpeRatio >= 2
                ? '#F59E0B'
                : result.sharpeRatio >= 1
                ? '#94A3B8'
                : '#EF4444',
            }}
          >
            {result.sharpeRatio.toFixed(2)}
          </span>
        </div>
      </div>

      {/* Bottom accent bar — ROI-colored */}
      <div className="mt-4 h-0.5 w-full rounded-full overflow-hidden bg-white/5">
        <motion.div
          className="h-full rounded-full"
          style={{
            background: roiPositive ? '#22C55E' : '#EF4444',
            width: `${Math.min(100, Math.abs(result.roi) * 4)}%`,
          }}
          initial={{ width: '0%' }}
          animate={{ width: `${Math.min(100, Math.abs(result.roi) * 4)}%` }}
          transition={{ ...SPRING, delay: 0.2 + index * 0.07 }}
        />
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: BACKTEST LEDGER
// ---------------------------------------------------------------------------
const BacktestLedger = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="history" label="Backtest Strategy Ledger" />

    <motion.div
      className="grid grid-cols-1 gap-4"
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {BACKTEST_RESULTS.map((result, i) => (
          <BacktestCard key={result.id} result={result} index={i} />
        ))}
      </AnimatePresence>
    </motion.div>

    {/* Summary footer */}
    <div
      className="flex items-center justify-between rounded-xl px-4 py-3"
      style={{
        background: 'rgba(245,158,11,0.06)',
        border: '1px solid rgba(245,158,11,0.15)',
      }}
    >
      <span className="text-xs text-slate-500">
        Best strategy · Kelly Compound Arb
      </span>
      <span
        className="font-mono text-sm font-bold tabular-nums"
        style={{ color: '#22C55E' }}
      >
        +22.8% ROI · Sharpe 3.07
      </span>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: STORAGE NODE CARD
// ---------------------------------------------------------------------------
const StorageNodeCard = ({
  node,
  index,
}: {
  node: StorageNode;
  index: number;
}) => {
  const typeCfg   = NODE_TYPE_CONFIG[node.type];
  const statusCfg = NODE_STATUS_CONFIG[node.status];

  const capacityColor =
    node.capacityPct > 80
      ? '#EF4444'
      : node.capacityPct > 60
      ? '#F59E0B'
      : '#B45309';

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      whileHover={{ backgroundColor: 'rgba(255,255,255,0.05)' }}
      transition={SPRING}
      className="flex flex-col gap-3 rounded-xl p-4"
      style={{
        background: 'rgba(255,255,255,0.03)',
        border: '1px solid rgba(255,255,255,0.06)',
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2.5">
          <div
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
            style={{
              background: `${typeCfg.color}15`,
              border: `1px solid ${typeCfg.color}30`,
            }}
          >
            <span
              className="material-symbols-outlined text-base"
              style={{ color: typeCfg.color }}
            >
              {typeCfg.icon}
            </span>
          </div>
          <span className="text-sm font-bold text-slate-200 leading-tight">
            {node.name}
          </span>
        </div>

        {/* Status */}
        <div className="flex items-center gap-1.5 shrink-0">
          {statusCfg.spin ? (
            <motion.span
              className="material-symbols-outlined text-base"
              style={{ color: '#F59E0B' }}
              animate={{ rotate: 360 }}
              transition={{ duration: 1.4, repeat: Infinity, ease: 'linear' }}
            >
              {statusCfg.icon}
            </motion.span>
          ) : node.status === 'archiving' ? (
            <span
              className="material-symbols-outlined text-base"
              style={{ color: '#F59E0B' }}
            >
              {statusCfg.icon}
            </span>
          ) : (
            <span
              className="material-symbols-outlined text-base"
              style={{ color: '#64748B' }}
            >
              {statusCfg.icon}
            </span>
          )}
          <span
            className="font-mono text-[10px] font-bold uppercase tracking-wider"
            style={{
              color: node.status === 'idle' ? '#64748B' : '#F59E0B',
            }}
          >
            {statusCfg.label}
          </span>
        </div>
      </div>

      {/* Capacity bar */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-widest text-slate-600">
            Capacity
          </span>
          <span
            className="font-mono text-xs font-bold tabular-nums"
            style={{ color: capacityColor }}
          >
            {node.capacityPct}%
          </span>
        </div>
        <div className="h-1.5 w-full rounded-full overflow-hidden bg-white/5">
          <motion.div
            className="h-full rounded-full"
            style={{ background: '#B45309' }}
            initial={{ width: '0%' }}
            animate={{ width: `${node.capacityPct}%` }}
            transition={{ ...SPRING, delay: 0.2 + index * 0.1 }}
          />
        </div>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: STORAGE NODE TELEMETRY
// ---------------------------------------------------------------------------
const StorageNodeTelemetry = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="dns" label="Storage Node Telemetry" />

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
        <span className="text-xs font-bold text-slate-400">3 Nodes Active</span>
        <span
          className="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: 'rgba(180,83,9,0.12)',
            color: '#B45309',
            border: '1px solid rgba(180,83,9,0.3)',
          }}
        >
          <motion.span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: '#B45309' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.6, repeat: Infinity }}
          />
          WRITING
        </span>
      </div>

      {/* Total storage summary */}
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-bold tabular-nums text-slate-100">
          4.2B
        </span>
        <span className="text-xs text-slate-500">market states indexed</span>
      </div>

      {/* Node cards */}
      <motion.div
        className="flex flex-col gap-3"
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {STORAGE_NODES.map((node, i) => (
            <StorageNodeCard key={node.id} node={node} index={i} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Footer action */}
      <motion.button
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="mt-1 flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold"
        style={{
          background: 'rgba(180,83,9,0.08)',
          border: '1px solid rgba(180,83,9,0.25)',
          color: '#B45309',
        }}
      >
        <span className="material-symbols-outlined text-base">storage</span>
        Manage Storage Nodes
      </motion.button>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE ARCHIVE
// ---------------------------------------------------------------------------
export const TheArchive = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <DeepStorageConsole />
    <BacktestLedger />
    <StorageNodeTelemetry />
  </motion.div>
);
