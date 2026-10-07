import React, { useCallback, useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useRegisterSW } from "virtual:pwa-register/react";
import { COMMANDER_LIST, primaryRoute } from "../../config/commanders.config";
import type { CommanderProfile } from "../../config/commanders.config";
import { useUIStore } from "../../store/useUIStore";
import type { ConnectionStatus, OddsQuote } from "../../store/useMarketStore";
import { BotAvatar } from "../bots/BotAvatar";
import type { BotStatus } from "../bots/BotAvatar";

const DEVELOPER_CREDIT = "Developed by Amit Ashok Kumar Patnaik";
const DESKTOP_MEDIA_QUERY = "(min-width: 1024px)"; // Tailwind `lg`
const DRAWER_ID = "betdoc-mobile-drawer";

const statusBadgeClassMap: Record<ConnectionStatus, string> = {
  idle: "bg-slate-700/60 text-slate-300",
  connecting: "bg-sky-500/20 text-sky-300",
  open: "bg-emerald-500/20 text-emerald-300",
  reconnecting: "bg-amber-500/20 text-amber-300",
  paused: "bg-slate-600/40 text-slate-300",
  offline: "bg-rose-500/20 text-rose-300",
  error: "bg-rose-500/20 text-rose-300",
};

const avatarStatusMap: Record<ConnectionStatus, BotStatus> = {
  idle: "idle",
  connecting: "active",
  open: "success",
  reconnecting: "active",
  paused: "idle",
  offline: "error",
  error: "error",
};

const cx = (...classes: Array<string | false | null | undefined>): string => classes.filter(Boolean).join(" ");

// ---------------------------------------------------------------- Navigation
interface CommanderNavProps {
  activeId: CommanderProfile["id"];
  onNavigate?: () => void;
}

const CommanderNavItem = React.memo(function CommanderNavItem({
  commander,
  isActive,
  onNavigate,
}: {
  commander: CommanderProfile;
  isActive: boolean;
  onNavigate?: () => void;
}): React.ReactNode {
  return (
    <Link
      to={primaryRoute(commander)}
      onClick={onNavigate}
      aria-label={`Open ${commander.name}, ${commander.domain}`}
      aria-current={isActive ? "page" : undefined}
      className={cx(
        "flex min-h-12 items-center gap-3 rounded-lg border p-3 transition-colors",
        "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-400",
        isActive ? "text-white" : "border-transparent text-slate-300 hover:bg-slate-800/60 hover:text-white",
      )}
      style={
        isActive
          ? {
              borderColor: commander.theme.primary,
              backgroundColor: `${commander.theme.primary}1F`,
              boxShadow: `0 0 16px ${commander.theme.glow}`,
            }
          : undefined
      }
    >
      <span
        aria-hidden="true"
        className="h-2.5 w-2.5 shrink-0 rounded-full"
        style={{ backgroundColor: commander.theme.primary, boxShadow: `0 0 8px ${commander.theme.glow}` }}
      />
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-sm font-semibold">{commander.name}</span>
        <span className="truncate text-xs text-slate-400">{commander.domain}</span>
      </span>
    </Link>
  );
});

const CommanderNav = React.memo(function CommanderNav({ activeId, onNavigate }: CommanderNavProps): React.ReactNode {
  return (
    <nav aria-label="Commanders" className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pr-1">
      {COMMANDER_LIST.map((commander) => (
        <CommanderNavItem key={commander.id} commander={commander} isActive={commander.id === activeId} onNavigate={onNavigate} />
      ))}
    </nav>
  );
});

const DeveloperCredit = React.memo(function DeveloperCredit(): React.ReactNode {
  return <p className="shrink-0 pt-4 text-center text-[11px] leading-snug text-slate-500">{DEVELOPER_CREDIT}</p>;
});

const Sidebar = React.memo(function Sidebar({
  commander,
  connection,
}: {
  commander: CommanderProfile;
  connection: ConnectionStatus;
}): React.ReactNode {
  return (
    <aside className="hidden w-72 shrink-0 flex-col border-r border-slate-800 bg-slate-950/80 p-4 lg:flex">
      <div className="mb-4 flex shrink-0 items-center gap-3">
        <BotAvatar botName={commander.name} status={avatarStatusMap[connection]} size="md" customHexColor={commander.theme.primary} />
        <span className="text-lg font-bold tracking-wide">BetDoc</span>
      </div>
      <CommanderNav activeId={commander.id} />
      <DeveloperCredit />
    </aside>
  );
});

const MobileDrawer = React.memo(function MobileDrawer({ activeId }: { activeId: CommanderProfile["id"] }): React.ReactNode {
  const isOpen = useUIStore((s) => s.isMobileMenuOpen);
  const closeMobileMenu = useUIStore((s) => s.closeMobileMenu);
  const reduceMotion = useReducedMotion();
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") closeMobileMenu();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus();
    };
  }, [isOpen, closeMobileMenu]);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(DESKTOP_MEDIA_QUERY);
    const onChange = (event: MediaQueryListEvent): void => {
      if (event.matches) closeMobileMenu();
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [closeMobileMenu]);

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div key="mobile-drawer-root" className="fixed inset-0 z-50 lg:hidden" initial={{ opacity: 1 }} animate={{ opacity: 1 }} exit={{ opacity: 1 }}>
          <motion.button
            key="mobile-drawer-backdrop"
            type="button"
            aria-label="Close navigation menu"
            className="absolute inset-0 h-full w-full cursor-default bg-black/60 backdrop-blur-sm"
            onClick={closeMobileMenu}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduceMotion ? 0 : 0.2 }}
          />
          <motion.aside
            key="mobile-drawer-panel"
            id={DRAWER_ID}
            role="dialog"
            aria-modal="true"
            aria-label="Commander navigation"
            className="absolute inset-y-0 left-0 flex w-[min(20rem,85vw)] flex-col border-r border-slate-800 bg-slate-950 p-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-[max(1rem,env(safe-area-inset-top))] shadow-2xl"
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 380, damping: 36 }}
            style={{ willChange: "transform" }}
          >
            <div className="mb-4 flex shrink-0 items-center justify-between">
              <span className="text-lg font-bold tracking-wide">BetDoc</span>
              <button
                ref={closeButtonRef}
                type="button"
                onClick={closeMobileMenu}
                aria-label="Close navigation menu"
                className="flex min-h-12 min-w-12 items-center justify-center rounded-lg p-3 text-slate-300 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
              >
                <svg viewBox="0 0 24 24" preserveAspectRatio="xMidYMid meet" className="h-6 w-6" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <CommanderNav activeId={activeId} onNavigate={closeMobileMenu} />
            <DeveloperCredit />
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>
  );
});

// ---------------------------------------------------------------- Header
const StageHeader = React.memo(function StageHeader({
  commander,
  connection,
  error,
  onReconnect,
}: {
  commander: CommanderProfile;
  connection: ConnectionStatus;
  error: string | null;
  onReconnect: () => void;
}): React.ReactNode {
  const isMobileMenuOpen = useUIStore((s) => s.isMobileMenuOpen);
  const toggleMobileMenu = useUIStore((s) => s.toggleMobileMenu);
  const canReconnect = connection === "error" || connection === "offline";

  return (
    <header
      className="flex items-center gap-3 border-b bg-slate-950/60 px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] backdrop-blur lg:px-6"
      style={{ borderColor: `${commander.theme.primary}40` }}
    >
      <button
        type="button"
        onClick={toggleMobileMenu}
        aria-label={isMobileMenuOpen ? "Close navigation menu" : "Open navigation menu"}
        aria-expanded={isMobileMenuOpen}
        aria-controls={DRAWER_ID}
        className="flex min-h-12 min-w-12 items-center justify-center rounded-lg p-3 text-slate-200 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 lg:hidden"
      >
        <svg viewBox="0 0 24 24" preserveAspectRatio="xMidYMid meet" className="h-6 w-6" aria-hidden="true">
          <path d="M4 7h16M4 12h16M4 17h16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>

      <div className="lg:hidden">
        <BotAvatar botName={commander.name} status={avatarStatusMap[connection]} size="sm" customHexColor={commander.theme.primary} />
      </div>

      <h1 className="min-w-0 flex-1 truncate text-base font-semibold lg:text-lg">
        <span style={{ color: commander.theme.primary }}>{commander.name}</span>
        <span className="text-slate-400"> · {commander.domain}</span>
      </h1>

      <span
        role="status"
        aria-live="polite"
        className={cx("rounded-full px-3 py-1 font-mono text-[11px] uppercase tracking-widest", statusBadgeClassMap[connection])}
        title={error ?? undefined}
      >
        {commander.networkProtocol === "REST" ? "rest" : connection}
      </span>

      {canReconnect && (
        <button
          type="button"
          onClick={onReconnect}
          aria-label="Reconnect live stream"
          className="min-h-12 rounded-lg p-3 text-xs font-semibold text-sky-300 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
        >
          Retry
        </button>
      )}
    </header>
  );
});

// ---------------------------------------------------------------- Execution panel
const ExecutionPanel = React.memo(function ExecutionPanel({
  commander,
  quotes,
  lastUpdatedAt,
}: {
  commander: CommanderProfile;
  quotes: readonly OddsQuote[];
  lastUpdatedAt: number | null;
}): React.ReactNode {
  return (
    <aside aria-label="Execution panel" className="hidden w-80 shrink-0 flex-col border-l border-slate-800 bg-slate-950/80 p-4 xl:flex">
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-widest" style={{ color: commander.theme.accent }}>
        Live Quotes
      </h2>
      <p className="mb-4 text-xs text-slate-500">
        {lastUpdatedAt ? `Updated ${new Date(lastUpdatedAt).toLocaleTimeString()}` : "Awaiting stream"}
      </p>
      <ul className="flex flex-col gap-2 overflow-y-auto">
        {quotes.length === 0 && <li className="text-xs text-slate-500">No quotes yet.</li>}
        {quotes.map((quote) => (
          <li key={`${quote.eventId}-${quote.market}-${quote.selection}-${quote.bookmaker ?? ""}`} className="rounded-lg bg-slate-900 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm text-slate-200">{quote.selection}</span>
              <span className="font-mono text-sm" style={{ color: commander.theme.primary }}>
                {quote.odds.toFixed(2)}
              </span>
            </div>
            <div className="truncate text-[11px] text-slate-500">
              {quote.market} · {quote.eventId}
              {quote.bookmaker ? ` · ${quote.bookmaker}` : ""}
            </div>
          </li>
        ))}
      </ul>
    </aside>
  );
});

// ---------------------------------------------------------------- PWA update prompt
const UpdatePrompt = React.memo(function UpdatePrompt(): React.ReactNode {
  const intervalMs = Number(import.meta.env.VITE_SW_UPDATE_INTERVAL_MS);
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    offlineReady: [offlineReady, setOfflineReady],
    updateServiceWorker,
  } = useRegisterSW({
    onRegisteredSW(_swUrl, registration) {
      if (!registration || !Number.isFinite(intervalMs) || intervalMs <= 0) return;
      window.setInterval(() => {
        if (navigator.onLine && registration.installing === null) void registration.update();
      }, intervalMs);
    },
    onRegisterError(error: unknown) {
      console.error("Service worker registration failed", error);
    },
  });

  const dismiss = useCallback((): void => {
    setNeedRefresh(false);
    setOfflineReady(false);
  }, [setNeedRefresh, setOfflineReady]);

  return (
    <AnimatePresence>
      {(needRefresh || offlineReady) && (
        <motion.div
          key={needRefresh ? "sw-update-available" : "sw-offline-ready"}
          role="alert"
          className="fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-50 mx-auto flex max-w-md items-center gap-3 rounded-xl border border-slate-700 bg-slate-900/95 p-3 shadow-xl backdrop-blur sm:inset-x-auto sm:right-4"
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 24 }}
          transition={{ duration: 0.2 }}
        >
          <p className="flex-1 text-sm text-slate-200">
            {needRefresh ? "A new version of BetDoc is available." : "BetDoc is ready to work offline."}
          </p>
          {needRefresh && (
            <button
              type="button"
              onClick={() => void updateServiceWorker(true)}
              aria-label="Reload to update BetDoc"
              className="min-h-12 rounded-lg bg-sky-500 p-3 text-xs font-semibold text-slate-950 hover:bg-sky-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-300"
            >
              Update
            </button>
          )}
          <button
            type="button"
            onClick={dismiss}
            aria-label="Dismiss notification"
            className="min-h-12 rounded-lg p-3 text-xs text-slate-400 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
          >
            Dismiss
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
});

// ---------------------------------------------------------------- Shell
export interface AppShellProps {
  commander: CommanderProfile;
  connection: ConnectionStatus;
  error: string | null;
  onReconnect: () => void;
  lastUpdatedAt: number | null;
  quotes: readonly OddsQuote[];
  children: React.ReactNode;
}

export function AppShell({ commander, connection, error, onReconnect, lastUpdatedAt, quotes, children }: AppShellProps): React.ReactNode {
  return (
    <div className="flex h-dvh overflow-hidden bg-[#121110] text-slate-100">
      <Sidebar commander={commander} connection={connection} />
      <MobileDrawer activeId={commander.id} />
      <main className="flex min-w-0 flex-1 flex-col">
        <StageHeader commander={commander} connection={connection} error={error} onReconnect={onReconnect} />
        <section aria-label={commander.domain} className="flex-1 overflow-y-auto p-3 sm:p-4 lg:p-6">
          {children}
        </section>
      </main>
      <ExecutionPanel commander={commander} quotes={quotes} lastUpdatedAt={lastUpdatedAt} />
      <UpdatePrompt />
    </div>
  );
}
