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

// Blend a commander colour 24% toward warm stone (#78716c): matte, never neon.
const mute = (hex: string, amount = 0.24): string =>
  '#' +
  [1, 3, 5]
    .map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16);
      const stone = parseInt('78716c'.slice(i - 1, i + 1), 16);
      return Math.round(c + (stone - c) * amount).toString(16).padStart(2, '0');
    })
    .join('');

// Text colour for an accent fill: white or near-black, whichever has the higher WCAG contrast.
const INK_DARK = '#1c1917';
const inkOn = (hex: string): string => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const vsWhite = 1.05 / (lum + 0.05);
  const vsDark = (lum + 0.05) / 0.06; // relative luminance of #1c1917 ≈ 0.010
  return vsDark > vsWhite ? INK_DARK : '#ffffff';
};

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
        cx="20" cy="7" r="1.4" fill={BRAND.violet}
        initial={reduceMotion ? false : { scale: 0, opacity: 0 }}
        animate={
          reduceMotion
            ? { scale: 1, opacity: 1 }
            : { scale: [0, 1, 1.08, 1], opacity: [0, 1, 0.8, 1] }
        }
        transition={
          reduceMotion
            ? { duration: 0 }
            : { duration: 3.2, delay: 0.9, repeat: Infinity, ease: 'easeInOut' as any }
        }
        style={{ originX: '20px', originY: '7px' }}
      />
    </svg>
  );
};

type Tone = 'neutral' | 'positive' | 'negative' | 'caution';
const TONE_STYLE: Record<Tone, { readonly icon: string; readonly value: string }> = {
  neutral: { icon: 'text-stone-400 dark:text-stone-500', value: 'text-stone-800 dark:text-stone-300' },
  positive: { icon: 'text-emerald-500/80', value: 'text-emerald-700 dark:text-emerald-300/90' },
  negative: { icon: 'text-rose-400', value: 'text-rose-600 dark:text-rose-300/90' },
  caution: { icon: 'text-amber-500/80', value: 'text-amber-700 dark:text-amber-200/90' },
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
    <div className="flex min-w-0 flex-1 items-center gap-2 px-3 py-3 sm:min-w-[10rem] sm:flex-none sm:gap-3 sm:px-5 sm:py-3.5">
      <AnimatedGlyph
        icon={icon}
        motionPreset={motionPreset}
        className={`hidden shrink-0 text-[20px] transition-colors duration-300 sm:inline-block ${t.icon}`}
      />
      <div className="min-w-0">
        <p className="text-xs font-medium text-stone-400 dark:text-stone-500">
          {label}
        </p>
        <motion.p
          key={changeKey}
          initial={reduceMotion ? false : { opacity: 0.4, y: -3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={glide}
          className={`mt-1.5 truncate font-mono text-[13px] font-medium leading-none tracking-tight tabular-nums transition-colors duration-500 sm:text-[15px] ${t.value}`}
        >
          {value}
        </motion.p>
      </div>
    </div>
  );
};

type SocketStatus = 'connecting' | 'live' | 'stalled' | 'offline';
const SOCKET_STYLE = {
  live: { dot: 'bg-emerald-400 animate-breathe', halo: '', text: 'text-emerald-700 dark:text-emerald-300/90', label: 'Live' },
  connecting: { dot: 'bg-amber-400 animate-breathe', halo: '', text: 'text-amber-700 dark:text-amber-200/90', label: 'Connecting' },
  stalled: { dot: 'bg-amber-400', halo: '', text: 'text-amber-700 dark:text-amber-200/90', label: 'Feed stalled' },
  offline: { dot: 'bg-stone-300 dark:bg-stone-600', halo: '', text: 'text-stone-400 dark:text-stone-500', label: 'Offline' },
} as const satisfies Record<SocketStatus, { readonly dot: string; readonly halo: string; readonly text: string; readonly label: string }>;

const StageHeader = (): ReactElement => {
  const summary = useDashboardSummary();
  const halted = useSystemStore((s) => s.halted);
  const busStatus = useSystemStore((s) => s.busStatus);
  const oddsLive = useMarketStore((s) => s.isConnected);
  const oddsTransport = useMarketStore((s) => s.transport);
  const activeCommander = useCommanderStore((s) => s.activeCommander);
  const isCompact = useUIStore((s) => s.isCompact);
  const toggleLeft = useUIStore((s) => s.toggleLeft);
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
    <header className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 bg-[#F8F6F0]/70 px-4 py-3.5 backdrop-blur-xl backdrop-saturate-150 sm:gap-4 sm:px-10 sm:py-5 dark:bg-stone-950/70">
      <div className="flex min-w-0 items-center gap-3.5">
        {isCompact && (
          <button type="button" onClick={toggleLeft} aria-label="Open navigation" className="-ml-1 grid size-10 shrink-0 place-items-center rounded-2xl text-stone-600 transition-transform duration-300 hover:bg-stone-900/5 active:scale-95 dark:text-stone-300 dark:hover:bg-white/5">
            <span className="material-symbols-outlined text-2xl">menu</span>
          </button>
        )}
        <span className="relative grid size-11 shrink-0 place-items-center rounded-2xl bg-white shadow-soft dark:bg-stone-900 dark:shadow-none">
          <AnimatedGlyph
            icon="dns"
            motionPreset={socketStatus === 'live' ? 'pulse' : 'none'}
            className="text-[20px] text-[#C89B3C] dark:text-[#E3BE63]"
            filled
          />
        </span>

        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h1 className="truncate font-display text-xl font-bold tracking-[-0.02em] text-stone-900 dark:text-stone-100">
              Quantitative Desk
            </h1>
            <span
              className="hidden items-center gap-1 rounded-full bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-2.5 py-0.5 text-[11px] font-medium text-[var(--accent-text)] sm:inline-flex"
              title={activeCommander.domain}
            >
              {activeCommander.name}
            </span>
            {halted && (
              <button
                type="button"
                onClick={() => window.dispatchEvent(new CustomEvent('betdoc:palette'))}
                className="inline-flex animate-breathe items-center gap-1 rounded-full bg-rose-50 px-2.5 py-0.5 text-[11px] font-semibold text-rose-700 dark:bg-rose-400/10 dark:text-rose-300"
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
              <span className="text-stone-300 dark:text-stone-700"> · </span>
              <span className="text-stone-400 dark:text-stone-500">Odds feed {oddsLive ? (oddsTransport === 'polling' ? 'polling' : 'streaming') : 'idle'}</span>
              <span className="text-stone-300 dark:text-stone-700"> · </span>
              <span className="text-stone-400 dark:text-stone-500">INR book</span>
            </p>
          </div>
        </div>
      </div>

      <div className="flex w-full divide-x divide-stone-100 rounded-3xl bg-white shadow-soft sm:w-auto dark:divide-white/[0.05] dark:bg-stone-900 dark:shadow-none">
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
        className="relative grid size-24 cursor-default place-items-center rounded-[28px] bg-white shadow-soft-lg dark:bg-stone-900 dark:shadow-none"
      >
        <BrandMark size={56} loop />
      </motion.div>

      <div className="flex flex-col items-center gap-3">
        <p className="font-display text-[30px] font-bold leading-none tracking-[-0.04em] text-stone-900 dark:text-stone-100">BetDoc</p>
        <div className="flex h-5 items-center overflow-hidden">
          <AnimatePresence mode="wait" initial={false}>
            <motion.p
              key={quip}
              initial={reduceMotion ? false : { opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, y: -10 }}
              transition={glide}
              className="text-[12px] font-medium tabular-nums text-stone-500 dark:text-stone-400"
            >
              {FALLBACK_QUIPS[quip]}
              <motion.span aria-hidden="true" animate={reduceMotion ? undefined : { opacity: [0, 1, 0] }} transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' as any }}>…</motion.span>
            </motion.p>
          </AnimatePresence>
        </div>
      </div>

      <div className="flex items-end gap-1.5" aria-hidden="true">
        {bars.map((color, i) => (
          <motion.span key={color} className="w-1.5 rounded-full" style={{ backgroundColor: color, height: 18 }} animate={reduceMotion ? undefined : { scaleY: [0.4, 1, 0.4] }} transition={{ duration: 1.4, repeat: Infinity, delay: i * 0.18, ease: 'easeInOut' as any }} />
        ))}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// NEW LAYOUT AND ROUTING FROM ASTRA
// ---------------------------------------------------------------------------
const SPRING = { type: 'spring', stiffness: 170, damping: 26, mass: 0.9 } as const;

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
  WORKING: 'bg-sky-400', ONLINE: 'bg-emerald-400', DEGRADED: 'bg-amber-400', FATAL: 'bg-rose-400', SLEEPING: 'bg-amber-400',
};

const SidebarNav = ({ collapsed }: { collapsed: boolean }) => {
  const { byId } = useCommanders();
  return (
    <nav className="flex-1 overflow-y-auto px-3 py-2 scrollbar-hide" aria-label="Sections">
      <ul className="flex flex-col gap-0.5">
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
                    'group relative flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm font-medium transition-[color,background-color,transform] duration-300 active:scale-[0.98]',
                    isActive
                      ? 'text-stone-900 dark:text-stone-50'
                      : 'text-stone-500 hover:bg-stone-900/[0.03] hover:text-stone-900 dark:text-stone-400 dark:hover:bg-white/[0.04] dark:hover:text-stone-100',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span
                        layoutId="nav-active-pill"
                        className="absolute inset-0 rounded-2xl bg-white shadow-soft dark:bg-stone-800/80 dark:shadow-none"
                        transition={SPRING}
                      />
                    )}
                    <span
                      className={[
                        'material-symbols-outlined relative shrink-0 text-xl transition-all',
                        collapsed ? 'mx-auto' : '',
                        isActive ? 'text-[var(--accent-text)]' : '',
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
                        STATUS_DOT[status] ?? 'bg-stone-300 dark:bg-stone-600',
                        '',
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
  const { isLeftCollapsed, toggleLeft, isCompact, closeLeftIfCompact } = useUIStore();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const { pathname } = useLocation();
  // Phones: the drawer is fully hidden when collapsed and floats over content when open.
  const hidden = isCompact && isLeftCollapsed;

  useEffect(() => closeLeftIfCompact(), [pathname, closeLeftIfCompact]);

  return (
    <>
    <AnimatePresence>
      {isCompact && !isLeftCollapsed && (
        <motion.div
          key="nav-backdrop"
          aria-hidden="true"
          onClick={toggleLeft}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-40 bg-stone-900/20 backdrop-blur-sm dark:bg-black/40"
        />
      )}
    </AnimatePresence>
    <motion.aside
      layout
      animate={{ width: hidden ? 0 : isLeftCollapsed ? 80 : 264 }}
      transition={SPRING}
      aria-hidden={hidden || undefined}
      className={`${isCompact ? 'fixed left-0 top-0 z-50 rounded-r-3xl bg-white/75 shadow-soft-lg backdrop-blur-xl backdrop-saturate-150 dark:bg-stone-900/80' : 'sticky top-0 z-40 bg-white/50 dark:bg-stone-900/40'} ${hidden ? 'invisible' : ''} flex h-screen shrink-0 flex-col overflow-hidden`}
    >
      <div className="flex items-center overflow-hidden px-4 pb-4 pt-6 sm:pb-6 sm:pt-8">
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

      <div className="flex flex-col gap-0.5 p-3 pb-5">
        <button onClick={() => window.dispatchEvent(new CustomEvent('betdoc:palette'))} className="flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm text-stone-500 transition-colors duration-300 hover:bg-stone-900/[0.03] hover:text-stone-800 dark:text-stone-400 dark:hover:bg-white/[0.04] dark:hover:text-stone-200" title="Command palette (Ctrl/⌘ K)">
          <span className="material-symbols-outlined shrink-0 text-xl">keyboard_command_key</span>
          <motion.span className="flex flex-1 items-center justify-between whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Commands
            <kbd className="rounded-lg bg-stone-900/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-stone-400 dark:bg-white/[0.06] dark:text-stone-500">Ctrl K</kbd>
          </motion.span>
        </button>
        <button onClick={onToggleTheme} className="flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm text-stone-500 transition-colors duration-300 hover:bg-stone-900/[0.03] hover:text-stone-800 dark:text-stone-400 dark:hover:bg-white/[0.04] dark:hover:text-stone-200" title="Toggle theme">
          <span className="material-symbols-outlined shrink-0 text-xl">{theme === 'dark' ? 'light_mode' : 'dark_mode'}</span>
          <motion.span className="whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            {theme === 'dark' ? 'Light mode' : 'Dark mode'}
          </motion.span>
        </button>
        <button onClick={toggleLeft} className="flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm text-stone-400 transition-colors duration-300 hover:bg-stone-900/[0.03] hover:text-stone-700 dark:hover:bg-white/[0.04] dark:hover:text-stone-200" title={isLeftCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
          <motion.span className="material-symbols-outlined shrink-0 text-xl" animate={{ rotate: isLeftCollapsed ? 180 : 0 }} transition={SPRING}>chevron_left</motion.span>
          <motion.span className="whitespace-nowrap overflow-hidden" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Collapse
          </motion.span>
        </button>
        <button onClick={logout} className="flex items-center gap-3 rounded-2xl px-3 py-2.5 text-sm text-stone-500 transition-colors duration-300 hover:bg-rose-50 hover:text-rose-600 dark:text-stone-400 dark:hover:bg-rose-400/10 dark:hover:text-rose-300" title="Sign out">
          <span className="material-symbols-outlined shrink-0 text-xl">logout</span>
          <motion.span className="min-w-0 whitespace-nowrap overflow-hidden text-left" animate={{ opacity: isLeftCollapsed ? 0 : 1, width: isLeftCollapsed ? 0 : 'auto' }} transition={SPRING}>
            Sign out{user ? <span className="text-stone-400 dark:text-stone-500"> · {user.username}</span> : null}
          </motion.span>
        </button>
      </div>
    </motion.aside>
    </>
  );
};

const ExecutionPanel = () => {
  const { isRightCollapsed, toggleRight, isCompact } = useUIStore();

  if (isCompact) {
    // Phones: a floating trigger, and a full-screen sheet when open.
    return (
      <AnimatePresence initial={false}>
        {isRightCollapsed ? (
          <motion.button
            key="slip-fab"
            type="button"
            onClick={toggleRight}
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.8, opacity: 0 }}
            whileTap={{ scale: 0.95 }}
            className="fixed bottom-5 right-5 z-30 inline-flex items-center gap-2 rounded-full bg-[var(--accent)] px-5 py-3.5 text-sm font-semibold text-[var(--accent-ink)] shadow-soft-lg"
          >
            <span className="material-symbols-outlined text-xl">receipt_long</span>
            Bet slip
          </motion.button>
        ) : (
          <motion.aside
            key="slip-sheet"
            role="dialog"
            aria-label="Execution panel"
            initial={{ y: '100%' }}
            animate={{ y: 0 }}
            exit={{ y: '100%' }}
            transition={SPRING}
            className="fixed inset-0 z-50 flex flex-col bg-[#FBFAF7] dark:bg-stone-900"
          >
            <div className="flex shrink-0 items-center justify-between px-5 pb-2 pt-4">
              <span className="font-display text-base font-semibold text-stone-900 dark:text-stone-100">Bet slip</span>
              <button onClick={toggleRight} title="Close panel" className="grid size-10 place-items-center rounded-full bg-stone-900/[0.04] text-stone-500 transition-transform duration-300 active:scale-95 dark:bg-white/[0.06]"><span className="material-symbols-outlined text-xl">close</span></button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <ExecutionTerminal />
              <div className="flex h-[70vh] flex-col"><EmbeddedScout /></div>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    );
  }

  return (
    <motion.aside
      layout
      animate={{ width: isRightCollapsed ? 80 : 380 }}
      transition={SPRING}
      className="z-20 flex h-screen shrink-0 flex-col overflow-hidden bg-[#FBFAF7] shadow-[-1px_0_0_rgba(41,37,36,0.04)] dark:bg-stone-900/60 dark:shadow-[-1px_0_0_rgba(255,255,255,0.03)]"
    >
      {isRightCollapsed ? (
        <div className="flex flex-col items-center gap-4 pt-6">
          <button onClick={toggleRight} title="Expand panel" className="flex h-10 w-10 items-center justify-center rounded-2xl text-stone-400 transition-[background-color,transform] duration-300 hover:bg-stone-900/[0.04] active:scale-95 dark:hover:bg-white/[0.05]"><span className="material-symbols-outlined text-xl">chevron_left</span></button>
          <button onClick={toggleRight} title="Open Betslip" className="flex h-10 w-10 items-center justify-center rounded-2xl text-stone-400 transition-[background-color,transform] duration-300 hover:bg-stone-900/[0.04] active:scale-95 dark:hover:bg-white/[0.05]"><span className="material-symbols-outlined text-xl">receipt_long</span></button>
          <button onClick={toggleRight} title="Open Scout Oracle" className="flex h-10 w-10 items-center justify-center rounded-2xl text-stone-400 transition-[background-color,transform] duration-300 hover:bg-stone-900/[0.04] active:scale-95 dark:hover:bg-white/[0.05]"><span className="material-symbols-outlined text-xl">chat</span></button>
        </div>
      ) : (
        <motion.div className="flex flex-col h-full w-[380px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }}>
          <div className="flex shrink-0 items-center justify-between px-5 pb-1 pt-5">
            <span className="font-display text-[15px] font-semibold text-stone-900 dark:text-stone-100">Bet slip</span>
            <button onClick={toggleRight} title="Collapse panel" className="flex h-8 w-8 items-center justify-center rounded-full text-stone-400 transition-[background-color,transform] duration-300 hover:bg-stone-900/[0.04] active:scale-95 dark:hover:bg-white/[0.05]"><span className="material-symbols-outlined text-base">chevron_right</span></button>
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
    const accent = mute(active.theme.primary);
    root.style.setProperty('--accent', accent);
    root.style.setProperty('--accent-glow', `${accent}33`);
    root.style.setProperty('--accent-ink', inkOn(accent));
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
      { id: 'act:betslip', label: 'Toggle bet slip', icon: 'receipt_long', group: 'Actions', run: toggleRight },
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
    <div className="flex h-screen w-full overflow-hidden bg-[#F8F6F0] text-stone-800 dark:bg-stone-950 dark:text-stone-200">
      <SessionEffects />
      <Sidebar theme={theme} onToggleTheme={toggle} />
      <motion.main
        layout
        transition={SPRING}
        className="relative flex min-w-0 flex-1 flex-col overflow-y-auto overflow-x-hidden"
      >
        <StageHeader />
        <HaltBanner />
        <div className="flex-1 px-4 pb-28 sm:px-10 sm:pb-14">
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
