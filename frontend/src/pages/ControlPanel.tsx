import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useCommandStore } from '../store/useCommandStore';

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
interface ExchangeLink {
  id: string;
  name: string;
  type: 'primary' | 'backup' | 'aggregator';
  ping: number;
  status: 'connected' | 'syncing' | 'offline';
}

interface GlobalOverride {
  id: string;
  label: string;
  description: string;
  defaultActive: boolean;
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const EXCHANGE_LINKS: ExchangeLink[] = [
  {
    id: 'ex-001',
    name: 'Pinnacle Primary',
    type: 'primary',
    ping: 18,
    status: 'connected',
  },
  {
    id: 'ex-002',
    name: 'Betfair Exchange',
    type: 'primary',
    ping: 24,
    status: 'connected',
  },
  {
    id: 'ex-003',
    name: 'Smarkets Aggregator',
    type: 'aggregator',
    ping: 41,
    status: 'syncing',
  },
  {
    id: 'ex-004',
    name: 'Bookmaker.eu Backup',
    type: 'backup',
    ping: 9999,
    status: 'offline',
  },
];

const GLOBAL_OVERRIDES: GlobalOverride[] = [
  {
    id: 'ov-001',
    label: 'Auto-Execution',
    description: 'Allow bots to place bets without manual confirmation.',
    defaultActive: true,
  },
  {
    id: 'ov-002',
    label: 'Dark Pool Routing',
    description: 'Route large stakes through Phantom micro-fragment engine.',
    defaultActive: true,
  },
  {
    id: 'ov-003',
    label: 'Strict Risk Limits',
    description: 'Hard-block any bet exceeding Pratap mandate thresholds.',
    defaultActive: false,
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const EXCHANGE_TYPE_CONFIG: Record<
  ExchangeLink['type'],
  { label: string; color: string }
> = {
  primary:    { label: 'PRIMARY',    color: '#06B6D4' },
  backup:     { label: 'BACKUP',     color: '#64748B' },
  aggregator: { label: 'AGGREGATOR', color: '#A855F7' },
};

const EXCHANGE_STATUS_CONFIG: Record<
  ExchangeLink['status'],
  { color: string; label: string; icon: string; spin: boolean }
> = {
  connected: { color: '#10B981', label: 'CONNECTED', icon: 'check_circle', spin: false },
  syncing:   { color: '#06B6D4', label: 'SYNCING',   icon: 'sync',         spin: true  },
  offline:   { color: '#F43F5E', label: 'OFFLINE',   icon: 'cancel',       spin: false },
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#06B6D4' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#06B6D4' }}
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
// SUB-COMPONENT: MASTER CONFIG CONSOLE
// ---------------------------------------------------------------------------
const MasterConfigConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(6,182,212,0.05)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(6,182,212,0.15)',
    }}
  >
    {/* Interlocking Gears SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-8 -top-8 h-[400px] w-[420px]"
      viewBox="0 0 400 400"
      fill="none"
      style={{ opacity: 0.15 }}
    >
      {/* Gear 1 — large, clockwise */}
      <motion.circle
        cx="160" cy="200" r="70"
        stroke="#06B6D4"
        strokeWidth="16"
        strokeDasharray="24 16"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 20, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '160px 200px' }}
      />
      {/* Gear 1 inner hub */}
      <circle
        cx="160" cy="200" r="40"
        stroke="#06B6D4"
        strokeWidth="2"
        fill="rgba(6,182,212,0.06)"
      />
      <motion.circle
        cx="160" cy="200" r="14"
        fill="#06B6D4"
        fillOpacity="0.3"
        animate={{ opacity: [0.3, 0.7, 0.3] }}
        transition={{ duration: 2.4, repeat: Infinity }}
      />

      {/* Gear 2 — small, counter-clockwise, meshed */}
      <motion.circle
        cx="290" cy="200" r="46"
        stroke="#06B6D4"
        strokeWidth="12"
        strokeDasharray="18 12"
        fill="none"
        animate={{ rotate: -360 }}
        transition={{ duration: 13.1, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '290px 200px' }}
      />
      {/* Gear 2 inner hub */}
      <circle
        cx="290" cy="200" r="20"
        stroke="#06B6D4"
        strokeWidth="1.5"
        fill="rgba(6,182,212,0.06)"
      />
      <motion.circle
        cx="290" cy="200" r="8"
        fill="#06B6D4"
        fillOpacity="0.3"
        animate={{ opacity: [0.3, 0.8, 0.3] }}
        transition={{ duration: 1.8, repeat: Infinity, delay: 0.6 }}
      />

      {/* Mesh point indicator */}
      <motion.circle
        cx="226" cy="200" r="4"
        fill="#10B981"
        animate={{ opacity: [0.4, 1, 0.4], r: [4, 6, 4] }}
        transition={{ duration: 1.2, repeat: Infinity }}
      />

      {/* Decorative axle lines */}
      <line x1="160" y1="130" x2="160" y2="270" stroke="#06B6D4" strokeWidth="0.5" opacity="0.4" />
      <line x1="90"  y1="200" x2="230" y2="200" stroke="#06B6D4" strokeWidth="0.5" opacity="0.4" />
      <line x1="290" y1="154" x2="290" y2="246" stroke="#06B6D4" strokeWidth="0.5" opacity="0.4" />
      <line x1="244" y1="200" x2="336" y2="200" stroke="#06B6D4" strokeWidth="0.5" opacity="0.4" />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#06B6D4' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#06B6D4' }}
          >
            Master Configuration · CONTROL PANEL
          </span>
        </div>

        <TypewriterLine text="SYSTEM CONTROL. Master configuration and global overrides active." />

        <p className="max-w-xl text-sm text-slate-400">
          4 exchange integrations monitored. 2 of 3 global overrides active.
          All subsystems reporting to PRATAP. Kill switch armed and ready.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Restart Engine', icon: 'restart_alt',    accent: '#06B6D4' },
          { label: 'Clear Cache',    icon: 'delete_sweep',   accent: '#A855F7' },
          { label: 'Audit Logs',     icon: 'policy',         accent: '#64748B' },
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
// SUB-COMPONENT: EXCHANGE CARD
// ---------------------------------------------------------------------------
const ExchangeCard = ({ link }: { link: ExchangeLink }) => {
  const typeCfg   = EXCHANGE_TYPE_CONFIG[link.type];
  const statusCfg = EXCHANGE_STATUS_CONFIG[link.status];
  const pingFast  = link.ping < 50;

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      whileHover={{ y: -2, boxShadow: '0 0 20px rgba(6,182,212,0.1)' }}
      transition={SPRING}
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.03)',
        backdropFilter: 'blur(12px)',
        border: `1px solid ${statusCfg.color}22`,
      }}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <span className="font-mono text-sm font-bold text-slate-100 leading-tight">
            {link.name}
          </span>
          {/* Type badge */}
          <span
            className="w-fit rounded-md px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider"
            style={{
              background: `${typeCfg.color}15`,
              color: typeCfg.color,
              border: `1px solid ${typeCfg.color}30`,
            }}
          >
            {typeCfg.label}
          </span>
        </div>

        {/* Status indicator */}
        <div className="flex shrink-0 items-center gap-1.5">
          {statusCfg.spin ? (
            <motion.span
              className="material-symbols-outlined text-base"
              style={{ color: statusCfg.color }}
              animate={{ rotate: 360 }}
              transition={{ duration: 1.2, repeat: Infinity, ease: 'linear' }}
            >
              {statusCfg.icon}
            </motion.span>
          ) : (
            <span
              className="material-symbols-outlined text-base"
              style={{ color: statusCfg.color }}
            >
              {statusCfg.icon}
            </span>
          )}
          <span
            className="font-mono text-[10px] font-bold uppercase tracking-wider"
            style={{ color: statusCfg.color }}
          >
            {statusCfg.label}
          </span>
        </div>
      </div>

      {/* Ping telemetry */}
      <div className="flex items-center justify-between border-t border-white/5 pt-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-slate-600">
          Ping
        </span>
        <span
          className="font-mono text-sm font-bold tabular-nums"
          style={{
            color: link.status === 'offline'
              ? '#F43F5E'
              : pingFast
              ? '#10B981'
              : '#F59E0B',
          }}
        >
          {link.status === 'offline' ? 'TIMEOUT' : `${link.ping}ms`}
        </span>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: EXCHANGE INTEGRATIONS
// ---------------------------------------------------------------------------
const ExchangeIntegrations = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="cable" label="Exchange API Integrations" />

    <motion.div
      className="grid grid-cols-1 md:grid-cols-2 gap-4"
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
    >
      <AnimatePresence>
        {EXCHANGE_LINKS.map((link) => (
          <ExchangeCard key={link.id} link={link} />
        ))}
      </AnimatePresence>
    </motion.div>

    {/* Integration summary footer */}
    <div
      className="flex items-center justify-between rounded-xl px-4 py-3"
      style={{
        background: 'rgba(16,185,129,0.06)',
        border: '1px solid rgba(16,185,129,0.15)',
      }}
    >
      <div className="flex items-center gap-2">
        <motion.span
          className="h-2 w-2 rounded-full"
          style={{ background: '#10B981' }}
          animate={{ opacity: [1, 0.3, 1] }}
          transition={{ duration: 1.2, repeat: Infinity }}
        />
        <span className="text-xs text-slate-400">
          2 primary feeds live · avg latency
        </span>
      </div>
      <span
        className="font-mono text-sm font-bold tabular-nums"
        style={{ color: '#10B981' }}
      >
        21ms
      </span>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: OVERRIDE ROW (isolated state)
// ---------------------------------------------------------------------------
const OverrideRow = ({ override }: { override: GlobalOverride }) => {
  const [active, setActive] = useState(override.defaultActive);

  return (
    <motion.div
      variants={ITEM_VARIANTS}
      className="flex items-start justify-between gap-4 rounded-xl p-4"
      style={{
        background: 'rgba(255,255,255,0.03)',
        border: `1px solid ${active ? 'rgba(16,185,129,0.2)' : 'rgba(255,255,255,0.06)'}`,
      }}
      animate={{
        borderColor: active
          ? 'rgba(16,185,129,0.2)'
          : 'rgba(255,255,255,0.06)',
      }}
      transition={SPRING}
    >
      {/* Label + description */}
      <div className="flex flex-col gap-0.5 flex-1 min-w-0">
        <span
          className="text-sm font-bold leading-tight"
          style={{ color: active ? '#F1F5F9' : '#64748B' }}
        >
          {override.label}
        </span>
        <span className="text-[11px] text-slate-600 leading-relaxed">
          {override.description}
        </span>
      </div>

      {/* Custom Framer Motion toggle */}
      <button
        onClick={() => setActive((v) => !v)}
        className="shrink-0 outline-none focus:outline-none"
        aria-label={`Toggle ${override.label}`}
      >
        <motion.div
          className="relative flex h-6 w-11 cursor-pointer items-center rounded-full p-0.5"
          animate={{ backgroundColor: active ? '#10B981' : '#475569' }}
          transition={SPRING}
        >
          <motion.div
            className="h-5 w-5 rounded-full shadow-md"
            style={{ background: '#FFFFFF' }}
            animate={{ x: active ? 20 : 0 }}
            transition={SPRING}
          />
        </motion.div>
      </button>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: GLOBAL OVERRIDES
// ---------------------------------------------------------------------------
const GlobalOverrides = () => {
  const triggerKillSwitch = useCommandStore((s) => s.triggerKillSwitch);

  return (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="toggle_on" label="Global Overrides" />

    <div
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.03)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Panel header */}
      <div className="flex items-center justify-between border-b border-white/5 pb-3">
        <span className="text-xs font-bold text-slate-400">System Overrides</span>
        <span
          className="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: 'rgba(6,182,212,0.12)',
            color: '#06B6D4',
            border: '1px solid rgba(6,182,212,0.3)',
          }}
        >
          <motion.span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: '#06B6D4' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          LIVE
        </span>
      </div>

      {/* Override rows — each isolated with own useState */}
      <motion.div
        className="flex flex-col gap-3"
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {GLOBAL_OVERRIDES.map((override) => (
            <OverrideRow key={override.id} override={override} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Override summary */}
      <div
        className="flex items-center justify-between rounded-xl px-3 py-2.5"
        style={{
          background: 'rgba(255,255,255,0.03)',
          border: '1px solid rgba(255,255,255,0.06)',
        }}
      >
        <span className="text-[11px] text-slate-500">Active overrides</span>
        <span
          className="font-mono text-sm font-bold"
          style={{ color: '#10B981' }}
        >
          2 / 3
        </span>
      </div>

      {/* GLOBAL KILL SWITCH */}
      <motion.button
        onClick={triggerKillSwitch}
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="flex w-full items-center justify-center gap-2 rounded-xl py-3.5 text-sm font-black uppercase tracking-widest"
        style={{
          background: '#F43F5E',
          color: '#FFFFFF',
          boxShadow: '0 0 24px rgba(244,63,94,0.4)',
        }}
        animate={{
          boxShadow: [
            '0 0 16px rgba(244,63,94,0.3)',
            '0 0 32px rgba(244,63,94,0.55)',
            '0 0 16px rgba(244,63,94,0.3)',
          ],
        }}
      >
        <span className="material-symbols-outlined text-base">
          power_settings_new
        </span>
        GLOBAL KILL SWITCH
      </motion.button>

      <p className="text-center font-mono text-[9px] text-slate-700">
        Halts all execution · Disconnects all feeds · Logs event
      </p>
    </div>
  </motion.div>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: CONTROL PANEL
// ---------------------------------------------------------------------------
export const ControlPanel = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <MasterConfigConsole />
    <ExchangeIntegrations />
    <GlobalOverrides />
  </motion.div>
);
