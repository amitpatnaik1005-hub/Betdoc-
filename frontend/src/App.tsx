import { lazy, Suspense, useCallback, useEffect, useId, useState, type ReactElement, } from 'react';
import { AnimatePresence, LayoutGroup, motion, useReducedMotion } from 'framer-motion';
import { BRAND, AnimatedGlyph, type GlyphMotion, BetdocLogo as BrandLogo } from './ui/brand';
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
} from 'react-router-dom';

import { useBetStore as useBankrollStore } from './store/useBetStore';
import { formatINR, formatSignedINR, paiseToRupees } from './pages/BetHistory';
import { IdempotentBetslip } from './components/betslip/IdempotentBetslip';
import { ScoutDrawer as EmbeddedScout } from './components/oracle/ScoutDrawer';

const OddsGrid = lazy(() => import('./components/board/OddsGrid').then(m => ({ default: m.OddsGrid })));
const BetHistory = lazy(() => import('./pages/BetHistory'));
const TopModels = lazy(() =>
  import('./pages/TopModels').then((m) => ({ default: m.TopModels })),
);


const NAV_ITEMS = [
  { to: '/board', label: 'Odds Board', hint: 'LIVE PRICING', icon: 'dashboard' },
  { to: '/history', label: 'Trade Ledger', hint: 'SETTLED + OPEN', icon: 'receipt_long' },
  { to: '/models', label: 'Models', hint: 'POSTERIOR RANKS', icon: 'query_stats' },
] as const;

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
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      className={className}
      role="img"
      aria-label="BetDoc"
    >
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

      {/* Pulse line resolving into an ascending arrow, one continuous stroke */}
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

      {/* Signal dot at the arrow tip, ambient breath */}
      <motion.circle
        cx="20"
        cy="7"
        r="1.4"
        fill={BRAND.violet}
        filter={`url(#${glowId})`}
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


const SidebarNav = (): ReactElement => {
  const reduceMotion = useReducedMotion();

  return (
    <nav aria-label="Primary" className="flex-1 overflow-y-auto px-3 py-2 scrollbar-hide">
      <LayoutGroup id="sidebar-nav">
        <ul className="flex flex-col gap-1">
          {NAV_ITEMS.map(({ to, label, icon }) => (
            <li key={to}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  [
                    'group relative flex w-full items-center gap-4 rounded-xl px-4 py-3 outline-none',
                    'transition-colors duration-300',
                    isActive
                      ? 'bg-[#C89B3C]/10 text-[#C89B3C] dark:bg-[#C89B3C]/15 dark:text-[#E0B85A]'
                      : 'text-slate-500 hover:bg-slate-900/[0.04] hover:text-slate-900 dark:text-[#8A8783] dark:hover:bg-white/[0.04] dark:hover:text-[#E8E6E3]',
                    BRAND.ring,
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span
                        layoutId="sidebar-active-bar"
                        transition={reduceMotion ? { duration: 0 } : glide}
                        className={`absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full ${BRAND.gradient} shadow-[0_0_12px_rgba(200,155,60,0.8)]`}
                        aria-hidden="true"
                      />
                    )}

                    <span
                      className={[
                        'material-symbols-outlined relative z-10 shrink-0 text-[22px] leading-none',
                        'transition-[color,filter] duration-300',
                        isActive
                          ? 'drop-shadow-[0_0_8px_rgba(200,155,60,0.55)]'
                          : 'group-hover:drop-shadow-[0_0_6px_rgba(200,155,60,0.25)]',
                      ].join(' ')}
                      style={{ fontVariationSettings: `'FILL' ${isActive ? 1 : 0}, 'wght' 400` }}
                      aria-hidden="true"
                    >
                      {icon}
                    </span>

                    <span className="relative z-10 truncate text-sm font-semibold tracking-wide">
                      {label}
                    </span>
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
      </LayoutGroup>
    </nav>
  );
};

const SidebarFooter = ({
  theme,
  onToggleTheme,
}: {
  readonly theme: Theme;
  readonly onToggleTheme: () => void;
}): ReactElement => {
  const reduceMotion = useReducedMotion();
  const [, setHovered] = useState(false);
  const isDark = theme === 'dark';
  const label = isDark ? 'Light mode' : 'Dark mode';

  return (
    <div className="flex flex-col items-center gap-3 px-4 pb-7 pt-4">
      <span aria-hidden="true" className="h-px w-8 bg-slate-200 dark:bg-white/10" />

      <div
        className="relative"
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
      >
        <motion.button
          type="button"
          onClick={onToggleTheme}
          aria-label={label}
          aria-pressed={isDark}
          whileHover={reduceMotion ? undefined : { scale: 1.06 }}
          whileTap={{ scale: 0.92 }}
          transition={glide}
          className="group relative grid size-12 place-items-center overflow-hidden rounded-2xl bg-white dark:bg-white/[0.04] ring-1 ring-inset ring-slate-200 dark:ring-white/10 outline-none transition-colors hover:bg-slate-50 dark:hover:bg-white/[0.08] focus-visible:ring-2 focus-visible:ring-[#C89B3C]/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-slate-950"
        >
          <AnimatePresence mode="wait" initial={false}>
            <motion.span
              key={theme}
              initial={reduceMotion ? false : { rotate: -90, opacity: 0, y: 8 }}
              animate={{ rotate: 0, opacity: 1, y: 0 }}
              exit={reduceMotion ? undefined : { rotate: 90, opacity: 0, y: -8 }}
              transition={glide}
              className={[
                'material-symbols-outlined text-[22px] leading-none',
                isDark ? 'text-[#C89B3C]' : 'text-slate-400 group-hover:text-slate-600',
              ].join(' ')}
              style={{ fontVariationSettings: "'FILL' 1, 'wght' 400" }}
              aria-hidden="true"
            >
              {isDark ? 'light_mode' : 'dark_mode'}
            </motion.span>
          </AnimatePresence>
        </motion.button>
      </div>
    </div>
  );
};

const Sidebar = ({
  theme,
  onToggleTheme,
}: {
  readonly theme: Theme;
  readonly onToggleTheme: () => void;
}): ReactElement => (
  <aside className="sticky top-0 z-40 flex h-screen w-64 shrink-0 flex-col overflow-visible border-r border-slate-900/[0.06] bg-[#FDFCFB] dark:border-white/[0.06] dark:bg-[#161514]">
    <div className="flex items-center px-6 pb-6 pt-8">
      <BrandLogo variant="full" />
    </div>
    <SidebarNav />
    <SidebarFooter theme={theme} onToggleTheme={onToggleTheme} />
  </aside>
);

type SocketStatus = 'connecting' | 'live' | 'stalled' | 'offline';

const SOCKET_STYLE = {
  live: {
    dot: 'bg-emerald-500',
    halo: 'bg-emerald-500/40 animate-ping',
    text: 'text-emerald-600 dark:text-emerald-400',
    label: 'Live feed',
  },
  connecting: {
    dot: 'bg-[#C89B3C] animate-pulse',
    halo: '',
    text: 'text-[#C89B3C] dark:text-[#E0B85A]',
    label: 'Connecting',
  },
  stalled: {
    dot: 'bg-[#C89B3C]',
    halo: '',
    text: 'text-[#C89B3C] dark:text-[#E0B85A]',
    label: 'Feed stalled',
  },
  offline: {
    dot: 'bg-slate-300 dark:bg-slate-600',
    halo: '',
    text: 'text-slate-400 dark:text-slate-500',
    label: 'Offline',
  },
} as const satisfies Record<
  SocketStatus,
  { readonly dot: string; readonly halo: string; readonly text: string; readonly label: string }
>;


const StatTile = ({
  label,
  value,
  icon,
  motionPreset,
  tone = 'neutral',
  changeKey,
}: StatTileProps): ReactElement => {
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

const StageHeader = (): ReactElement => {
  const balancePaise = useBankrollStore((s: any) => s.bankroll * 100);
  const exposurePaise = useBankrollStore((s: any) => s.exposure * 100);
  const dayPnlPaise = useBankrollStore((s: any) => s.sessionPnl * 100);
  const socketStatus = useBankrollStore((s: any) => s.connectionState || 'live');

  const dayPnl = paiseToRupees(dayPnlPaise);
  const socket = SOCKET_STYLE[socketStatus as keyof typeof SOCKET_STYLE];

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
          <h1 className="truncate text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
            Quantitative Desk
          </h1>
          <div className="mt-1 flex items-center gap-2" role="status" aria-live="polite">
            <span className="relative flex size-2 shrink-0" aria-hidden="true">
              {socket.halo && (
                <span className={`absolute inline-flex size-full rounded-full ${socket.halo}`} />
              )}
              <span className={`relative inline-flex size-2 rounded-full ${socket.dot}`} />
            </span>
            <p className={`text-[11px] font-medium tracking-wide ${socket.text}`}>
              {socket.label}
              <span className="text-slate-300 dark:text-slate-700"> · </span>
              <span className="text-slate-400 dark:text-slate-500">INR book</span>
            </p>
          </div>
        </div>
      </div>

      <div className="flex divide-x divide-slate-200/70 rounded-2xl bg-white ring-1 ring-slate-900/[0.06] dark:divide-white/[0.06] dark:bg-white/[0.04] dark:ring-white/[0.08]">
        <StatTile
          label="Bankroll"
          value={formatINR(paiseToRupees(balancePaise))}
          icon="account_balance_wallet"
          motionPreset="breathe"
          tone="positive"
          changeKey={balancePaise}
        />
        <StatTile
          label="Exposure"
          value={formatINR(paiseToRupees(exposurePaise))}
          icon="inventory_2"
          motionPreset="sway"
          tone="caution"
          changeKey={exposurePaise}
        />
        <StatTile
          label="Session"
          value={formatSignedINR(dayPnl)}
          icon={pnlIcon}
          motionPreset="drift"
          tone={pnlTone}
          changeKey={dayPnlPaise}
        />
      </div>
    </header>
  );
};

const FALLBACK_QUIPS = [
  'Repricing the book',
  'Sharpening the edge',
  'Syncing the ledger',
  'Warming the models',
  'Reading the tape',
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
    <div
      role="status"
      aria-live="polite"
      aria-label="Loading"
      className="flex min-h-[60vh] w-full flex-col items-center justify-center gap-8 px-8"
    >
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
        <p className={`text-[30px] font-extrabold leading-none tracking-[-0.04em] ${BRAND.gradientText}`}>
          BetDoc
        </p>

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
              <motion.span
                aria-hidden="true"
                animate={reduceMotion ? undefined : { opacity: [0, 1, 0] }}
                transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' as any }}
              >
                …
              </motion.span>
            </motion.p>
          </AnimatePresence>
        </div>
      </div>

      <div className="flex items-end gap-1.5" aria-hidden="true">
        {bars.map((color, i) => (
          <motion.span
            key={color}
            className="w-1.5 rounded-full"
            style={{ backgroundColor: color, height: 18 }}
            animate={reduceMotion ? undefined : { scaleY: [0.4, 1, 0.4] }}
            transition={{
              duration: 0.9,
              repeat: Infinity,
              delay: i * 0.15,
              ease: 'easeInOut' as any,
            }}
          />
        ))}
      </div>
    </div>
  );
};

const ExecutionPanel = (): ReactElement => (
  <aside className="z-20 flex w-[380px] shrink-0 flex-col border-l border-slate-200/60 bg-white shadow-[-10px_0_40px_rgba(0,0,0,0.04)] h-screen dark:border-white/[0.06] dark:bg-[#161514]">
    <div className="flex-none max-h-[55%] overflow-y-auto">
      <IdempotentBetslip />
    </div>
    <div className="h-px shrink-0 bg-slate-200/60 dark:bg-white/[0.06]" />
    <div className="flex-1 min-h-0 flex flex-col">
      <EmbeddedScout />
    </div>
  </aside>
);

const AppShell = (): ReactElement => {
  const { theme, toggle } = useTheme();

  useEffect(() => {
    useBankrollStore.getState().fetchWallet();
  }, []);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-[#F8F6F0] text-slate-900 antialiased selection:bg-[#C89B3C]/30 dark:bg-[#121110] dark:text-[#E8E6E3]">
      <Sidebar theme={theme} onToggleTheme={toggle} />
      <div className="relative flex min-w-0 flex-1 flex-col overflow-y-auto">
        <StageHeader />
        <main className="flex-1 px-8 pb-10">
          <Suspense fallback={<RouteFallback />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
      <ExecutionPanel />
    </div>
  );
};

const NotFound = (): ReactElement => (
  <div className="mx-auto max-w-md rounded-3xl bg-white px-8 py-16 text-center shadow-[0_8px_30px_rgb(0,0,0,0.04)] dark:bg-white/[0.04]">
    <span className="mx-auto grid size-14 place-items-center rounded-2xl bg-rose-50 dark:bg-rose-950/30">
      <span
        className="material-symbols-outlined text-[26px] leading-none text-rose-600 dark:text-rose-400"
        aria-hidden="true"
      >
        report
      </span>
    </span>
    <p className="mt-4 font-mono text-[10px] font-bold uppercase tracking-[0.24em] text-rose-600 dark:text-rose-400">
      404
    </p>
    <h2 className="mt-2 text-xl font-black tracking-tighter text-slate-900 dark:text-slate-50">
      No such instrument
    </h2>
    <p className="mt-1.5 text-[13px] text-slate-500 dark:text-slate-400">
      That route is not mounted on this terminal.
    </p>
  </div>
);

const App = (): ReactElement => (
  <BrowserRouter>
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/board" replace />} />
        <Route path="/board" element={<OddsGrid />} />
        <Route path="/history" element={<BetHistory />} />
        <Route path="/models" element={<TopModels />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  </BrowserRouter>
);

export default App;
