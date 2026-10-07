import { lazy, Suspense, useCallback, useEffect, useId, useMemo, useState, type ReactElement } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom';

import { BRAND, AnimatedGlyph, type GlyphMotion, BetdocLogo as BrandLogo } from './ui/brand';
import { useUIStore } from './store/useUIStore';
import { formatINR, formatSignedINR } from './lib/format';
import ExecutionTerminal from './components/ExecutionTerminal';
import { ScoutDrawer as EmbeddedScout } from './components/oracle/ScoutDrawer';
import LoginForm from './components/LoginForm';
import { RouteErrorBoundary } from './components/RouteErrorBoundary';
import { Toaster } from './components/Toaster';
import { CommandPalette, type PaletteCommand, useNavigateCommands } from './components/CommandPalette';
import { MOCK_AUTH, useAuthStore } from './store/useAuthStore';
import { useMarketStore } from './store/useMarketStore';
import { useSystemStore } from './store/useSystemStore';
import { useCommanderStore } from './store/useCommanderStore';
import { COMMANDER_REGISTRY, type CommanderId } from './config/commanders.config';
import { useCommanders } from './lib/commanders';
import { emergencyStop, resumeTrading, useControls, useDashboardSummary } from './lib/api';
import { invalidate } from './lib/resource';
import { startRealtime } from './services/realtime';
import { toast } from './store/useToastStore';

const CommandCenter = lazy(() => import('./pages/CommandCenter').then(m => ({ default: m.CommandCenter })));
const TheArena = lazy(() => import('./pages/TheArena').then(m => ({ default: m.TheArena })));
const TheLab = lazy(() => import('./pages/TheLab').then(m => ({ default: m.TheLab })));
const TheVault = lazy(() => import('./pages/TheVault').then(m => ({ default: m.TheVault })));
const TheWire = lazy(() => import('./pages/TheWire').then(m => ({ default: m.TheWire })));
const Core = lazy(() => import('./pages/Core').then(m => ({ default: m.Core })));
const TheHive = lazy(() => import('./pages/TheHive').then(m => ({ default: m.TheHive })));
const TheOracle = lazy(() => import('./pages/TheOracle').then(m => ({ default: m.TheOracle })));
const Phantom = lazy(() => import('./pages/Phantom').then(m => ({ default: m.Phantom })));
const TheArchive = lazy(() => import('./pages/TheArchive').then(m => ({ default: m.TheArchive })));
const ControlPanel = lazy(() => import('./pages/ControlPanel').then(m => ({ default: m.ControlPanel })));

// ---------------------------------------------------------------------------
// UTILITIES PRESERVED FROM ORIGINAL
// ---------------------------------------------------------------------------
const glide = { type: 'spring', stiffness: 380, damping: 34, mass: 0.8 } as const;

type Theme = 'light' | 'dark';
const THEME_KEY = 'betdoc:theme';

const readInitialTheme = (): Theme => {
  if (typeof window === 'undefined') return 'light';
  const stored = window.localStorage.getItem(THEME_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
};

const useTheme = (): { readonly theme: Theme; readonly toggle: () => void } => {
  const [theme, setTheme] = useState<Theme>(readInitialTheme);

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.style.colorScheme = theme;
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);
  return { theme, toggle };
};

interface BrandMarkProps {
  readonly size?: number;
  readonly loop?: boolean;
  readonly className?: string;
}

const BrandMark = ({ size = 24, loop = false, className = '' }: BrandMarkProps): ReactElement => {
  const reduceMotion = useReducedMotion();
  const gradientId = useId();
  const glowId = useId();

  const draw = reduceMotion
    ? { pathLength: 1, opacity: 1 }
    : {
        pathLength: [0, 1, 1, 1],
        opacity: [0, 1, 1, loop ? 0 : 1],
      };

  const drawTransition = reduceMotion
    ? { duration: 0 }
    : loop
      ? { duration: 2.4, times: [0, 0.55, 0.85, 1], repeat: Infinity, ease: 'easeInOut' as any }
      : { duration: 1.1, times: [0, 0.8, 1, 1], ease: [0.22, 1, 0.36, 1] as any };

  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" className={className} role="img" aria-label="BetDoc">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor={BRAND.azure} />
          <stop offset="50%" stopColor={BRAND.dominant} />
          <stop offset="100%" stopColor={BRAND.violet} />
        </linearGradient>
        <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="1.2" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <motion.path
        d="M3 16h3l2.5-6 3 9 2.5-6 6-6"
        stroke={`url(#${gradientId})`}
        strokeWidth="2.25"
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }}
        animate={draw}
        transition={drawTransition}
      />
      <motion.path
        d="M14.5 7H20v5.5"
        stroke={`url(#${gradientId})`}
        strokeWidth="2.25"
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }}
        animate={draw}
        transition={{ ...drawTransition, delay: reduceMotion ? 0 : 0.25 }}
      />

      <motion.circle
        cx="20" cy="7" r="1.4" fill={BRAND.violet} filter={`url(#${glowId})`}
        initial={reduceMotion ? false : { scale: 0, opacity: 0 }}
        animate={
          reduceMotion
            ? { scale: 1, opacity: 1 }
            : { scale: [0, 1.15, 1, 1.25, 1], opacity: [0, 1, 0.75, 1, 0.75] }
        }
        transition={
          reduceMotion
            ? { duration: 0 }
            : { duration: 2.4, delay: 0.9, repeat: Infinity, ease: 'easeInOut' as any }
        }
        style={{ originX: '20px', originY: '7px' }}
      />
    </svg>
  );
};

type Tone = 'neutral' | 'positive' | 'negative' | 'caution';
const TONE_STYLE: Record<Tone, { readonly icon: string; readonly value: string }> = {
  neutral: { icon: 'text-slate-400 dark:text-slate-500', value: 'text-slate-900 dark:text-slate-50' },
  positive: { icon: 'text-emerald-500', value: 'text-emerald-600 dark:text-emerald-400' },
  negative: { icon: 'text-rose-500', value: 'text-rose-600 dark:text-rose-400' },
  caution: { icon: 'text-amber-500', value: 'text-amber-600 dark:text-amber-400' },
};

interface StatTileProps {
  readonly label: string;
  readonly value: string;
  readonly icon: string;
  readonly motionPreset: GlyphMotion;
  readonly tone?: Tone;
  readonly changeKey: number;
}

const StatTile = ({ label, value, icon, motionPreset, tone = 'neutral', changeKey }: StatTileProps): ReactElement => {
  const reduceMotion = useReducedMotion();
  const t = TONE_STYLE[tone];

  return (
    <div className="flex min-w-[9.5rem] items-center gap-3 px-5 py-3">
      <AnimatedGlyph
        icon={icon}
        motionPreset={motionPreset}
        className={`shrink-0 text-[20px] transition-colors duration-300 ${t.icon}`}
      />
      <div className="min-w-0">
        <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
          {label}
        </p>
        <motion.p
          key={changeKey}
          initial={reduceMotion ? false : { opacity: 0.4, y: -3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={glide}
          className={`mt-1 truncate text-[15px] font-semibold leading-none tracking-tight tabular-nums transition-colors duration-300 ${t.value}`}
        >
          {value}
        </motion.p>
      </div>
    </div>
  );
};

type SocketStatus = 'connecting' | 'live' | 'stalled' | 'offline';
const SOCKET_STYLE = {
  live: { dot: 'bg-emerald-500', halo: 'bg-emerald-500/40 animate-ping', text: 'text-emerald-600 dark:text-emerald-400', label: 'Live feed' },
  connecting: { dot: 'bg-[#C89B3C] animate-pulse', halo: '', text: 'text-[#C89B3C] dark:text-[#E0B85A]', label: 'Connecting' },
  stalled: { dot: 'bg-[#C89B3C]', halo: '', text: 'text-[#C89B3C] dark:text-[#E0B85A]', label: 'Feed stalled' },
  offline: { dot: 'bg-slate-300 dark:bg-slate-600', halo: '', text: 'text-slate-400 dark:text-slate-500', label: 'Offline' },
} as const satisfies Record<SocketStatus, { readonly dot: string; readonly halo: string; readonly text: string; readonly label: string }>;

const StageHeader = (): ReactElement => {
  const summary = useDashboardSummary();
  const halted = useSystemStore((s) => s.halted);
  const busStatus = useSystemStore((s) => s.busStatus);
  const oddsLive = useMarketStore((s) => s.isConnected);
  const activeCommander = useCommanderStore((s) => s.activeCommander);
  // "Live" means the cross-section event bus is open; the odds feed is reported alongside it.
  const socketStatus: SocketStatus =
    MOCK_AUTH ? 'offline'
      : busStatus === 'open' ? 'live'
      : busStatus === 'connecting' || busStatus === 'reconnecting' || busStatus === 'idle' ? 'connecting'
      : busStatus === 'error' ? 'stalled' : 'offline';

  const bankroll = summary.data?.total_bankroll ?? 0;
  const exposure = summary.data?.current_exposure ?? 0;
  const dayPnl = summary.data?.daily_pnl ?? 0;
  const socket = SOCKET_STYLE[socketStatus];

  const pnlTone: Tone = dayPnl > 0 ? 'positive' : dayPnl < 0 ? 'negative' : 'neutral';
  const pnlIcon = dayPnl > 0 ? 'trending_up' : dayPnl < 0 ? 'trending_down' : 'trending_flat';

  return (
    <header className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-4 border-b border-slate-200/60 bg-[#F8F6F0]/80 px-8 py-4 backdrop-blur-md dark:border-white/[0.06] dark:bg-[#121110]/80">
      <div className="flex min-w-0 items-center gap-3.5">
        <span className="relative grid size-10 shrink-0 place-items-center rounded-xl bg-white ring-1 ring-inset ring-slate-900/[0.06] dark:bg-white/[0.04] dark:ring-white/[0.08]">
          {socketStatus === 'live' && (
            <motion.span
              aria-hidden="true"
              className="absolute inset-0 rounded-xl ring-1 ring-[#C89B3C]/40"
              animate={{ scale: [1, 1.25], opacity: [0.6, 0] }}
              transition={{ duration: 2, repeat: Infinity, ease: 'easeOut' }}
            />
          )}
          <AnimatedGlyph
            icon="dns"
            motionPreset={socketStatus === 'live' ? 'pulse' : 'none'}
            className="text-[20px] text-[#C89B3C] dark:text-[#E3BE63]"
            filled
          />
        </span>

        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h1 className="truncate text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              Quantitative Desk
            </h1>
            <span
              className="hidden items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.14em] ring-1 ring-inset sm:inline-flex"
              style={{ color: activeCommander.theme.primary, background: `${activeCommander.theme.primary}14`, boxShadow: `inset 0 0 0 1px ${activeCommander.theme.primary}40` }}
              title={activeCommander.domain}
            >
              {activeCommander.name}
            </span>
            {halted && (
              <button
                type="button"
                onClick={() => window.dispatchEvent(new CustomEvent('betdoc:palette'))}
                className="inline-flex animate-pulse items-center gap-1 rounded-full bg-rose-600 px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.14em] text-white"
                title="Trading halted by the emergency stop. Open the command palette to resume."
              >
                <span className="material-symbols-outlined text-[12px]">front_hand</span>
                Halted
              </button>
            )}
          </div>
          <div className="mt-1 flex items-center gap-2" role="status" aria-live="polite">
            <span className="relative flex size-2 shrink-0" aria-hidden="true">
              {socket.halo && <span className={`absolute inline-flex size-full rounded-full ${socket.halo}`} />}
              <span className={`relative inline-flex size-2 rounded-full ${socket.dot}`} />
            </span>
            <p className={`text-[11px] font-medium tracking-wide ${socket.text}`}>
              {MOCK_AUTH ? 'Mock session' : socket.label}
              <span className="text-slate-300 dark:text-slate-700"> · </span>
              <span className="text-slate-400 dark:text-slate-500">Odds feed {oddsLive ? 'streaming' : 'idle'}</span>
              <span className="text-slate-300 dark:text-slate-700"> · </span>
              <span className="text-slate-400 dark:text-slate-500">INR book</span>
            </p>
          </div>
        </div>
      </div>

      <div className="flex divide-x divide-slate-200/70 rounded-2xl bg-white ring-1 ring-slate-900/[0.06] dark:divide-white/[0.06] dark:bg-white/[0.04] dark:ring-white/[0.08]">
        <StatTile label="Bankroll" value={summary.data ? formatINR(bankroll) : '—'} icon="account_balance_wallet" motionPreset="breathe" tone="positive" changeKey={bankroll} />
        <StatTile label="Exposure" value={summary.data ? formatINR(exposure) : '—'} icon="inventory_2" motionPreset="sway" tone="caution" changeKey={exposure} />
        <StatTile label="Today" value={summary.data ? formatSignedINR(dayPnl) : '—'} icon={pnlIcon} motionPreset="drift" tone={pnlTone} changeKey={dayPnl} />
      </div>
    </header>
  );
};

const FALLBACK_QUIPS = [
  'Repricing the book', 'Sharpening the edge', 'Syncing the ledger', 'Warming the models', 'Reading the tape',
] as const;

const RouteFallback = (): ReactElement => {
  const reduceMotion = useReducedMotion();
  const [quip, setQuip] = useState(0);

  useEffect(() => {
    if (reduceMotion) return;
    const id = window.setInterval(() => setQuip((q) => (q + 1) % FALLBACK_QUIPS.length), 1500);
    return () => window.clearInterval(id);
  }, [reduceMotion]);

  const bars = [BRAND.azure, BRAND.dominant, BRAND.violet];

  return (
    <div role="status" aria-live="polite" aria-label="Loading" className="flex min-h-[60vh] w-full flex-col items-center justify-center gap-8 px-8">
      <motion.div
        initial={reduceMotion ? false : { opacity: 0, scale: 0.92, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={glide}
        whileHover={reduceMotion ? undefined : { scale: 1.04, rotate: -3 }}
        whileTap={reduceMotion ? undefined : { scale: 0.96, rotate: 3 }}
        className="relative grid size-24 cursor-default place-items-center rounded-[28px] bg-white ring-1 ring-inset ring-[#C89B3C]/10 dark:bg-white/[0.06] dark:ring-white/10"
      >
        <motion.span
          aria-hidden="true"
          className="pointer-events-none absolute -inset-1.5 rounded-[32px]"
          style={{
            background: `conic-gradient(from 0deg, ${BRAND.azure}00, ${BRAND.dominant}, ${BRAND.violet}, ${BRAND.azure}00)`,
            WebkitMaskImage: 'radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px))',
            maskImage: 'radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px))',
          }}
          animate={reduceMotion ? undefined : { rotate: 360 }}
          transition={{ duration: 2.6, repeat: Infinity, ease: 'linear' }}
        />
        <BrandMark size={56} loop />
      </motion.div>

      <div className="flex flex-col items-center gap-3">
        <p className={`text-[30px] font-extrabold leading-none tracking-[-0.04em] ${BRAND.gradientText}`}>BetDoc</p>
        <div className="flex h-5 items-center overflow-hidden">
          <AnimatePresence mode="wait" initial={false}>
            <motion.p
              key={quip}
              initial={reduceMotion ? false : { opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, y: -10 }}
              transition={glide}
              className="text-[12px] font-medium tabular-nums text-slate-500 dark:text-slate-400"
            >
              {FALLBACK_QUIPS[quip]}
              <motion.span aria-hidden="true" animate={reduceMotion ? undefined : { opacity: [0, 1, 0] }} transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' as any }}>…</motion.span>
            </motion.p>
          </AnimatePresence>
        </div>
      </div>

      <div className="flex items-end gap-1.5" aria-hidden="true">
        {bars.map((color, i) => (
          <motion.span key={color} className="w-1.5 rounded-full" style={{ backgroundColor: color, height: 18 }} animate={reduceMotion ? undefined : { scaleY: [0.4, 1, 0.4] }} transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.15, ease: 'easeInOut' as any }} />
        ))}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// NEW LAYOUT AND ROUTING FROM ASTRA
// ---------------------------------------------------------------------------
const SPRING = { type: 'spring', stiffness: 350, damping: 30 } as const;

const NAV_ITEMS: readonly { to: string; label: string; icon: string; bot: CommanderId }[] = [
  { to: '/command-center', label: 'Command Center', icon: 'dashboard',       bot: 'KAUTILYA' },
  { to: '/arena',          label: 'The Arena',      icon: 'sports_esports',  bot: 'BAJIRAO'  },
  { to: '/oracle',         label: 'Oracle',         icon: 'auto_awesome',    bot: 'ASHOKA'   },
  { to: '/lab',            label: 'The Lab',        icon: 'science',         bot: 'PANINI'   },
  { to: '/hive',           label: 'The Hive',       icon: 'hive',            bot: 'VIDUR'    },
  { to: '/vault',          label: 'The Vault',      icon: 'account_balance', bot: 'KUMBHA'   },
  { to: '/phantom',        label: 'Phantom',        icon: 'blur_on',         bot: 'GARUDA'   },
  { to: '/wire',           label: 'The Wire',       icon: 'newspaper',       bot: 'VIDUR'    },
  { to: '/archive',        label: 'Archive',        icon: 'inventory_2',     bot: 'TODAR MAL'},
  { to: '/core',           label: 'Core',           icon: 'memory',          bot: 'PRATAP'   },
  { to: '/control-panel',  label: 'Control Panel',  icon: 'settings',        bot: 'KAUTILYA' },
];

const STATUS_DOT: Record<string, string> = {
  WORKING: 'bg-sky-500', ONLINE: 'bg-emerald-500', DEGRADED: 'bg-amber-500', FATAL: 'bg-rose-500', SLEEPING: 'bg-amber-500',
};

const SidebarNav = ({ collapsed }: { collapsed: boolean }) => {
  const { byId } = useCommanders();
  return (
    <nav className="flex-1 overflow-y-auto px-3 py-2 scrollbar-hide" aria-label="Sections">
      <ul className="flex flex-col gap-1">
        {NAV_ITEMS.map(({ to, label, icon, bot }) => {
          const commander = byId.get(bot);
          const status = commander?.status ?? 'NO SIGNAL';
          return (
            <li key={to}>
              <NavLink
                to={to}
                title={`${label} · ${COMMANDER_REGISTRY[bot].name} · ${status}`}
                className={({ isActive }) =>
                  [
                    'group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors duration-150',
                    isActive
                      ? 'text-white'
                      : 'text-slate-500 hover:bg-slate-100 dark:hover:bg-white/5 hover:text-slate-900 dark:hover:text-white',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span
                        layoutId="nav-active-pill"
                        className="absolute inset-0 rounded-xl"
                        style={{
                          background: `linear-gradient(135deg, ${BRAND.dominant}, ${BRAND.azure})`,
                          boxShadow: `0 2px 12px ${BRAND.azure}55`,
                        }}
                        transition={SPRING}
                      />
                    )}
                    <span
                      className={[
                        'material-symbols-outlined relative shrink-0 text-xl transition-all',
                        collapsed ? 'mx-auto' : '',
                        isActive ? 'text-white' : '',
                      ].join(' ')}
                    >
                      {icon}
                    </span>
                    <motion.span
                      className="relative flex-1 whitespace-nowrap overflow-hidden leading-none"
                      animate={{
                        opacity: collapsed ? 0 : 1,
                        width: collapsed ? 0 : 'auto',
                        marginLeft: collapsed ? 0 : undefined,
                      }}
                      transition={SPRING}
                    >
                      {label}
                    </motion.span>
                    <span
                      aria-label={`${COMMANDER_REGISTRY[bot].name} ${status}`}
                      className={[
                        'relative size-1.5 shrink-0 rounded-full',
                        collapsed ? 'absolute right-2 top-2' : '',
                        STATUS_DOT[status] ?? 'bg-slate-300 dark:bg-slate-600',
                        isActive ? 'ring-2 ring-white/70' : '',
                      ].join(' ')}
                    />
                  </>
                )}
              </NavLink>
            </li>
          );
        })}
      </ul>
    </nav>
  );
};

const Sidebar = ({ theme, onToggleTheme }: { theme: string; onToggleTheme: () => void; }) => {
  const { isLeftCollapsed, toggleLeft } = useUIStore();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  return (
    <motion.aside
      layout
      animate={{ width: isLeftCollapsed ? 80 : 264 }}
      transition={SPRING}
      className="sticky top-0 z-40 flex h-screen shrink-0 flex-col border-r border-slate-200 dark:border-white/10 bg-[#FDFCFB] dark:bg-[#161514] overflow-hidden"
    >
      <div className="flex items-center px-4 pb-6 pt-8 overflow-hidden">
        <AnimatePresence mode="wait">
          {!isLeftCollapsed ? (
            <motion.div key="full-logo" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
              <BrandLogo variant="full" />
            </motion.div>
          ) : (
            <motion.div key="mark-logo" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="mx-auto">
              <BrandLogo variant="mark" />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <SidebarNav collapsed={isLeftCollapsed} />

      <div className="flex flex-col gap-1 border-t border-slate-200 dark:border-white/10 p-3">
        <button onClick={() => window.dispatchEvent(new CustomEvent('betdoc:palette'))} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-500 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors" title="Command palette (Ctrl/⌘ K)">
          <span className="material-symbols-outlined shrink-0 text-xl">keyboard_command_key</span>
          <motion.span className="flex flex-1 items-center justify-between whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Commands
            <kbd className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-500 dark:bg-white/10 dark:text-slate-400">Ctrl K</kbd>
          </motion.span>
        </button>
        <button onClick={onToggleTheme} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-500 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors" title="Toggle theme">
          <span className="material-symbols-outlined shrink-0 text-xl">{theme === 'dark' ? 'light_mode' : 'dark_mode'}</span>
          <motion.span className="whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
          </motion.span>
        </button>
        <button onClick={toggleLeft} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors" title={isLeftCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
          <motion.span className="material-symbols-outlined shrink-0 text-xl" animate={{ rotate: isLeftCollapsed ? 180 : 0 }} transition={SPRING}>chevron_left</motion.span>
          <motion.span className="whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Collapse
          </motion.span>
        </button>
        <button onClick={logout} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-slate-500 hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-500/10 dark:hover:text-rose-300 transition-colors" title="Sign out">
          <span className="material-symbols-outlined shrink-0 text-xl">logout</span>
          <motion.span className="min-w-0 whitespace-nowrap overflow-hidden text-left" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Sign out{user ? <span className="text-slate-400 dark:text-slate-500"> · {user.username}</span> : null}
          </motion.span>
        </button>
      </div>
    </motion.aside>
  );
};

const ExecutionPanel = () => {
  const { isRightCollapsed, toggleRight } = useUIStore();

  return (
    <motion.aside
      layout
      animate={{ width: isRightCollapsed ? 80 : 380 }}
      transition={SPRING}
      className="z-20 flex shrink-0 flex-col border-l border-slate-200 dark:border-white/10 bg-white dark:bg-[#161514] h-screen overflow-hidden"
    >
      {isRightCollapsed ? (
        <div className="flex flex-col items-center gap-4 pt-6">
          <button onClick={toggleRight} title="Expand panel" className="flex h-10 w-10 items-center justify-center rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"><span className="material-symbols-outlined text-xl">chevron_left</span></button>
          <button onClick={toggleRight} title="Open Betslip" className="flex h-10 w-10 items-center justify-center rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"><span className="material-symbols-outlined text-xl">receipt_long</span></button>
          <button onClick={toggleRight} title="Open Scout Oracle" className="flex h-10 w-10 items-center justify-center rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"><span className="material-symbols-outlined text-xl">chat</span></button>
        </div>
      ) : (
        <motion.div className="flex flex-col h-full w-[380px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }}>
          <div className="flex items-center justify-between border-b border-slate-200 dark:border-white/10 px-4 py-3 shrink-0">
            <span className="text-xs font-bold uppercase tracking-widest" style={{ color: BRAND.azure }}>Execution Panel</span>
            <button onClick={toggleRight} title="Collapse panel" className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"><span className="material-symbols-outlined text-base">chevron_right</span></button>
          </div>
          <div className="flex-none max-h-[55%] overflow-y-auto"><ExecutionTerminal /></div>
          <div className="flex-1 min-h-0 flex flex-col"><EmbeddedScout /></div>
        </motion.div>
      )}
    </motion.aside>
  );
};

const HaltBanner = () => {
  const halted = useSystemStore((s) => s.halted);
  const [busy, setBusy] = useState(false);
  return (
    <AnimatePresence>
      {halted && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          className="overflow-hidden"
        >
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 border-b border-rose-600/20 bg-rose-50 px-8 py-2.5 text-sm text-rose-800 dark:bg-rose-500/10 dark:text-rose-200">
            <span className="flex items-center gap-2 font-medium">
              <span className="material-symbols-outlined text-[18px]">front_hand</span>
              Emergency stop is active. Every order is refused until trading is resumed.
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                await resumeTrading(500);
                setBusy(false);
              }}
              className="rounded-lg bg-rose-600 px-3 py-1 text-xs font-bold text-white hover:bg-rose-500 disabled:opacity-60"
            >
              {busy ? 'Resuming…' : 'Resume trading (₹500 daily exposure)'}
            </button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

/** Session-wide realtime channels, commander sync and accent theming. Renders nothing. */
const SessionEffects = () => {
  const token = useAuthStore((s) => s.token);
  const loadProfile = useAuthStore((s) => s.loadProfile);
  const { pathname } = useLocation();
  const syncCommander = useCommanderStore((s) => s.syncCommander);
  const active = useCommanderStore((s) => s.activeCommander);
  const setCommanderStatus = useCommanderStore((s) => s.setCommanderStatus);
  const { commanders } = useCommanders();
  useControls(); // keeps the kill-switch state current for every section

  useEffect(() => {
    void loadProfile();
  }, [loadProfile]);

  useEffect(() => {
    if (!token || MOCK_AUTH) return;
    return startRealtime(token);
  }, [token]);

  useEffect(() => syncCommander(pathname), [pathname, syncCommander]);

  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty('--accent', active.theme.primary);
    root.style.setProperty('--accent-glow', active.theme.glow);
  }, [active]);

  useEffect(() => {
    for (const c of commanders) {
      setCommanderStatus(c.profile.id, c.status === 'WORKING' ? 'ENGAGED' : c.status === 'ONLINE' ? 'ACTIVE' : 'STANDBY');
    }
  }, [commanders, setCommanderStatus]);

  return null;
};

const usePaletteCommands = (): PaletteCommand[] => {
  const nav = useNavigateCommands(NAV_ITEMS);
  const toggleRight = useUIStore((s) => s.toggleRight);
  const halted = useSystemStore((s) => s.halted);
  return useMemo(
    () => [
      ...nav,
      { id: 'act:betslip', label: 'Toggle execution panel', icon: 'receipt_long', group: 'Actions', run: toggleRight },
      {
        id: 'act:refresh',
        label: 'Refresh every section',
        icon: 'refresh',
        group: 'Actions',
        run: () => {
          invalidate('dashboard', 'vault', 'arena', 'capital', 'hive', 'lab', 'core', 'commanders', 'system', 'signals', 'archive', 'the-wire', 'phantom', 'oracle');
          toast.info('Refreshing all sections');
        },
      },
      halted
        ? { id: 'act:resume', label: 'Resume trading', icon: 'play_circle', group: 'Actions', hint: '₹500 daily exposure', run: () => void resumeTrading(500) }
        : { id: 'act:halt', label: 'Emergency stop: halt all trading', icon: 'front_hand', group: 'Actions', run: () => void emergencyStop() },
    ],
    [nav, toggleRight, halted],
  );
};

const AppShell = () => {
  const { theme, toggle } = useTheme();
  const { pathname } = useLocation();
  const commands = usePaletteCommands();

  return (
    <div className="flex h-screen w-full overflow-hidden bg-[#F8F6F0] text-slate-900 antialiased selection:bg-[#C89B3C]/30 dark:bg-[#121110] dark:text-[#E8E6E3]">
      <SessionEffects />
      <Sidebar theme={theme} onToggleTheme={toggle} />
      <motion.main
        layout
        transition={SPRING}
        className="relative flex min-w-0 flex-1 flex-col overflow-y-auto overflow-x-hidden"
      >
        <StageHeader />
        <HaltBanner />
        <div className="flex-1 px-8 pb-10">
          <RouteErrorBoundary key={pathname}>
            <Suspense fallback={<RouteFallback />}>
              <Outlet />
            </Suspense>
          </RouteErrorBoundary>
        </div>
      </motion.main>
      <ExecutionPanel />
      <CommandPalette commands={commands} />
      <Toaster />
    </div>
  );
};

const AnimatedRoute = ({ children }: { children: React.ReactNode }) => {
  const location = useLocation();
  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={location.pathname}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -10 }}
        transition={{ duration: 0.18 }}
        className="h-full"
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
};

const App = () => {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  if (!isAuthenticated) {
    return <LoginForm />;
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate replace to="/command-center" />} />
        
        <Route path="/command-center" element={
          <AnimatedRoute>
            <CommandCenter />
          </AnimatedRoute>
        } />
        <Route path="/arena" element={
          <AnimatedRoute>
            <TheArena />
          </AnimatedRoute>
        } />
        <Route path="/lab" element={
          <AnimatedRoute>
            <TheLab />
          </AnimatedRoute>
        } />
        <Route path="/vault" element={
          <AnimatedRoute>
            <TheVault />
          </AnimatedRoute>
        } />
        <Route path="/wire" element={
          <AnimatedRoute>
            <TheWire />
          </AnimatedRoute>
        } />
        <Route path="/core" element={
          <AnimatedRoute>
            <Core />
          </AnimatedRoute>
        } />
        <Route path="/hive" element={
          <AnimatedRoute>
            <TheHive />
          </AnimatedRoute>
        } />
        <Route path="/oracle" element={
          <AnimatedRoute>
            <TheOracle />
          </AnimatedRoute>
        } />
        <Route path="/phantom" element={
          <AnimatedRoute>
            <Phantom />
          </AnimatedRoute>
        } />
        <Route path="/archive" element={
          <AnimatedRoute>
            <TheArchive />
          </AnimatedRoute>
        } />
        <Route path="/control-panel" element={
          <AnimatedRoute>
            <ControlPanel />
          </AnimatedRoute>
        } />
        
        <Route path="*" element={<Navigate replace to="/command-center" />} />
      </Route>
    </Routes>
  </BrowserRouter>
  );
};

export default App;
