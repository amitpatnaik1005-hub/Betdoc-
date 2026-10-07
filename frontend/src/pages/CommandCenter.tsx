import { motion } from 'framer-motion';
import { BRAND } from '../ui/brand';
import { useUIStore } from '../store/useUIStore';
import MarketBoard from '../components/MarketBoard';

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
interface TelemetryMetric {
  id: string;
  label: string;
  value: string | number;
  status: 'optimal' | 'warning' | 'critical';
  icon: string;
}

interface GeneralStatus {
  botName: string;
  domain: string;
  currentTask: string;
  statusCode: 200 | 102 | 404;
  pingColor: string;
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const TELEMETRY_METRICS: TelemetryMetric[] = [
  {
    id: 'active-smallcases',
    label: 'Active Smallcases',
    value: 115,
    status: 'optimal',
    icon: 'hub',
  },
  {
    id: 'live-markets',
    label: 'Live Markets Tracked',
    value: '4,208',
    status: 'optimal',
    icon: 'monitoring',
  },
  {
    id: 'exec-latency',
    label: 'Execution Latency',
    value: '42ms',
    status: 'warning',
    icon: 'speed',
  },
];

const GENERALS: GeneralStatus[] = [
  {
    botName: 'BAJIRAO',
    domain: 'The Arena',
    currentTask: 'Scouting Live Odds',
    statusCode: 200,
    pingColor: '#EAB308',
  },
  {
    botName: 'PANINI',
    domain: 'The Lab',
    currentTask: 'Crunching Posterior Ranks',
    statusCode: 102,
    pingColor: '#3B82F6',
  },
  {
    botName: 'KUMBHA',
    domain: 'The Vault',
    currentTask: 'Auditing Cashflow',
    statusCode: 200,
    pingColor: '#22C55E',
  },
  {
    botName: 'VIDUR',
    domain: 'The Wire',
    currentTask: 'Parsing News Feeds',
    statusCode: 102,
    pingColor: '#A855F7',
  },
  {
    botName: 'PRATAP',
    domain: 'Core',
    currentTask: 'Engine Nominal',
    statusCode: 200,
    pingColor: '#F1F5F9',
  },
];

const STATUS_LABEL: Record<200 | 102 | 404, string> = {
  200: 'LIVE',
  102: 'PROCESSING',
  404: 'OFFLINE',
};

const TELEMETRY_STATUS_COLORS: Record<TelemetryMetric['status'], string> = {
  optimal:  '#22C55E',
  warning:  '#EAB308',
  critical: '#EF4444',
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: KAUTILYA HERO
// ---------------------------------------------------------------------------
const KautilyaHero = () => {
  const toggleRight = useUIStore((s) => s.toggleRight);
  return (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(255,255,255,0.04)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)',
    }}
  >
    {/* Neural Ring Background SVG */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-24 -top-24 h-[420px] w-[420px] opacity-20"
      viewBox="0 0 420 420"
      fill="none"
    >
      <defs>
        <radialGradient id="ring-grad" cx="50%" cy="50%" r="50%">
          <stop offset="0%"   stopColor={BRAND.azure}    stopOpacity="0.9" />
          <stop offset="60%"  stopColor={BRAND.dominant} stopOpacity="0.4" />
          <stop offset="100%" stopColor="transparent"    stopOpacity="0"   />
        </radialGradient>
        <mask id="ring-mask">
          <circle cx="210" cy="210" r="200" fill="white" />
          <circle cx="210" cy="210" r="130" fill="black" />
        </mask>
      </defs>
      <motion.circle
        cx="210" cy="210" r="200"
        fill="url(#ring-grad)"
        mask="url(#ring-mask)"
        animate={{ opacity: [0.6, 1, 0.6], scale: [1, 1.04, 1] }}
        transition={{ duration: 3.5, repeat: Infinity, ease: 'easeInOut' }}
      />
      {/* Inner decorative rings */}
      {[160, 110, 60].map((r, i) => (
        <motion.circle
          key={r}
          cx="210" cy="210" r={r}
          stroke={BRAND.azure}
          strokeWidth="0.75"
          strokeDasharray="6 10"
          fill="none"
          opacity={0.35 - i * 0.08}
          animate={{ rotate: i % 2 === 0 ? 360 : -360 }}
          transition={{ duration: 18 + i * 6, repeat: Infinity, ease: 'linear' }}
          style={{ transformOrigin: '210px 210px' }}
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
            style={{ background: BRAND.azure }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: BRAND.azure }}
          >
            Supreme Commander · KAUTILYA
          </span>
        </div>

        {/* Typewriter greeting */}
        <TypewriterLine
          text="KAUTILYA ACTIVE. Logic engine running 115 smallcase models. Awaiting directives."
        />

        <p className="max-w-xl text-sm text-slate-400">
          All subsystems nominal. Generals are deployed across their domains.
          Issue a directive or inspect the telemetry matrix below.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Deploy Oracle', icon: 'auto_awesome', onClick: toggleRight },
          { label: 'System Config', icon: 'settings', onClick: undefined },
          { label: 'View Architecture', icon: 'account_tree', onClick: undefined },
        ].map(({ label, icon, onClick }) => (
          <motion.button
            key={label}
            onClick={onClick}
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.97 }}
            transition={SPRING}
            className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold outline-none transition-colors"
            style={{
              background: 'rgba(255,255,255,0.06)',
              border: `1px solid rgba(255,255,255,0.1)`,
              color: '#F1F5F9',
            }}
            onFocus={(e) => {
              (e.currentTarget as HTMLButtonElement).style.boxShadow =
                `0 0 0 2px ${BRAND.azure}`;
            }}
            onBlur={(e) => {
              (e.currentTarget as HTMLButtonElement).style.boxShadow = 'none';
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
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TYPEWRITER LINE
// ---------------------------------------------------------------------------
const TypewriterLine = ({ text }: { text: string }) => {
  const words = text.split(' ');
  return (
    <h2 className="text-xl font-bold tracking-tight text-slate-100 lg:text-2xl">
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
// SUB-COMPONENT: TELEMETRY CARD
// ---------------------------------------------------------------------------
const TelemetryCard = ({ metric }: { metric: TelemetryMetric }) => {
  const color = TELEMETRY_STATUS_COLORS[metric.status];

  return (
    <motion.div
      variants={CARD_VARIANTS}
      whileHover={{
        boxShadow: '0 0 20px rgba(200,155,60,0.15)',
        y: -2,
      }}
      transition={SPRING}
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      <div className="flex items-start justify-between">
        <div
          className="flex h-10 w-10 items-center justify-center rounded-xl"
          style={{
            background: `${color}18`,
            border: `1px solid ${color}33`,
          }}
        >
          <span
            className="material-symbols-outlined text-xl"
            style={{ color }}
          >
            {metric.icon}
          </span>
        </div>

        {/* Status pill */}
        <span
          className="rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: `${color}18`,
            color,
            border: `1px solid ${color}33`,
          }}
        >
          {metric.status}
        </span>
      </div>

      <div>
        <p className="text-2xl font-bold text-slate-100">{metric.value}</p>
        <p className="mt-0.5 text-xs text-slate-500">{metric.label}</p>
      </div>

      {/* Bottom accent bar */}
      <div className="h-0.5 w-full rounded-full overflow-hidden bg-white/5">
        <motion.div
          className="h-full rounded-full"
          style={{ background: color }}
          initial={{ width: '0%' }}
          animate={{ width: metric.status === 'optimal' ? '88%' : metric.status === 'warning' ? '52%' : '18%' }}
          transition={{ ...SPRING, delay: 0.3 }}
        />
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: SYSTEM TELEMETRY
// ---------------------------------------------------------------------------
const SystemTelemetry = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="sensors" label="Live Telemetry Matrix" />
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      {TELEMETRY_METRICS.map((metric) => (
        <TelemetryCard key={metric.id} metric={metric} />
      ))}
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: GENERAL ROW
// ---------------------------------------------------------------------------
const GeneralRow = ({ general }: { general: GeneralStatus }) => (
  <motion.div
    variants={CARD_VARIANTS}
    className="flex items-center gap-3 rounded-xl px-4 py-3"
    style={{
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.06)',
    }}
    whileHover={{ background: 'rgba(255,255,255,0.06)' }}
    transition={SPRING}
  >
    {/* Ping indicator */}
    <div className="relative flex h-3 w-3 shrink-0 items-center justify-center">
      <span
        className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
        style={{ background: general.pingColor }}
      />
      <span
        className="relative inline-flex h-2 w-2 rounded-full"
        style={{ background: general.pingColor }}
      />
    </div>

    {/* Identity */}
    <div className="flex-1 min-w-0">
      <div className="flex items-baseline gap-2">
        <span className="text-sm font-bold text-slate-100">{general.botName}</span>
        <span className="text-[10px] text-slate-500">{general.domain}</span>
      </div>
      <p className="truncate text-xs text-slate-500">{general.currentTask}</p>
    </div>

    {/* Status code badge */}
    <span
      className="shrink-0 rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider"
      style={{
        background: `${general.pingColor}18`,
        color: general.pingColor,
        border: `1px solid ${general.pingColor}33`,
      }}
    >
      {STATUS_LABEL[general.statusCode]}
    </span>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: GENERALS STATUS PANEL
// ---------------------------------------------------------------------------
const GeneralsStatusPanel = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="groups" label="Bot Roster · Generals" />

    <div
      className="flex flex-col gap-2 rounded-2xl p-4"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {GENERALS.map((g) => (
        <GeneralRow key={g.botName} general={g} />
      ))}
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: BRAND.azure }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: BRAND.azure }}
    >
      {label}
    </span>
  </div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: COMMAND CENTER
// ---------------------------------------------------------------------------
export const CommandCenter = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <KautilyaHero />
    <SystemTelemetry />
    <GeneralsStatusPanel />
    <motion.div variants={CARD_VARIANTS} className="lg:col-span-12">
      <MarketBoard />
    </motion.div>
  </motion.div>
);
