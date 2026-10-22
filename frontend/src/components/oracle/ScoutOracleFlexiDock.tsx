/**
 * The right-rail Scout Oracle dock (Group 100). Developed for Amit Ashok Kumar Patnaik.
 *
 * Four modes: collapsed (a 56px rail with an unread badge), docked (380px), expanded (600px) and max (half the
 * window). On a phone, any open mode is a full-screen sheet and collapsed is a floating button. Ctrl/Cmd+J toggles it
 * (wired in App.tsx). The conversation itself is the Scout drawer; this component is its frame.
 */
import { useEffect } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { type DockMode, type DockWidth, setScoutDockMode, useScoutDockMode, useScoutDockStore } from '../../store/useScoutDockStore';
import { useUIStore } from '../../store/useUIStore';
import { SPRING } from '../../ui/kit';
import { OracleMark, ScoutDrawer } from './ScoutDrawer';

const WIDTH_PX: Record<DockMode, number | string> = { collapsed: 56, docked: 380, expanded: 600, max: '50vw' };
const MODES: readonly { mode: DockWidth; icon: string; label: string; hint: string }[] = [
  { mode: 'docked', icon: 'dock_to_left', label: 'Docked', hint: 'Standard rail (380px)' },
  { mode: 'expanded', icon: 'width_wide', label: 'Expanded', hint: 'Wider rail for multi-leg slips (600px)' },
  { mode: 'max', icon: 'open_in_full', label: 'Max', hint: 'Research canvas (half the window)' },
];

const UnreadBadge = ({ count }: { count: number }) =>
  count > 0 ? (
    <span className="absolute -right-1.5 -top-1.5 grid min-w-[18px] place-items-center rounded-full bg-rose-500 px-1 text-[10px] font-bold leading-[18px] text-white" aria-label={`${count} unread`}>
      {count > 9 ? '9+' : count}
    </span>
  ) : null;

/** Width controls in the Scout's header; collapse closes the dock. */
const ModeToolbar = ({ mode, compact }: { mode: DockMode; compact: boolean }) => (
  <div role="group" aria-label="Scout Oracle width" className="flex items-center gap-0.5 rounded-lg bg-stone-100 p-0.5 dark:bg-white/[0.06]">
    {!compact && MODES.map(({ mode: m, icon, label, hint }) => (
      <button
        key={m}
        type="button"
        onClick={() => setScoutDockMode(m)}
        aria-pressed={mode === m}
        aria-label={label}
        title={hint}
        className={`grid size-7 place-items-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/50 ${
          mode === m ? 'bg-white text-stone-900 shadow-sm dark:bg-stone-700 dark:text-white' : 'text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-100'
        }`}
      >
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{icon}</span>
      </button>
    ))}
    <button
      type="button"
      onClick={() => setScoutDockMode('collapsed')}
      aria-label={compact ? 'Close Scout Oracle' : 'Collapse Scout Oracle'}
      title={compact ? 'Close' : 'Collapse (Ctrl+J)'}
      className="grid size-7 place-items-center rounded-md text-stone-500 transition-colors hover:text-stone-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/50 dark:text-stone-400 dark:hover:text-stone-100"
    >
      <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{compact ? 'close' : 'right_panel_close'}</span>
    </button>
  </div>
);

export const ScoutOracleFlexiDock = () => {
  const mode = useScoutDockMode();
  const isCompact = useUIStore((s) => s.isCompact);
  const unread = useScoutDockStore((s) => s.unreadInsights);
  const width = useScoutDockStore((s) => s.width);

  // However the dock was opened ("Ask the Scout", loading a bet, Ctrl+J), its answers are now in view
  useEffect(() => {
    if (mode !== 'collapsed') useScoutDockStore.getState().clearUnread();
  }, [mode]);

  if (isCompact) {
    return (
      <AnimatePresence initial={false}>
        {mode === 'collapsed' ? (
          <motion.button
            key="scout-fab"
            type="button"
            onClick={() => setScoutDockMode(width)}
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.8, opacity: 0 }}
            whileTap={{ scale: 0.95 }}
            className="fixed bottom-[calc(5rem+env(safe-area-inset-bottom))] right-4 z-30 inline-flex md:bottom-5 md:right-5 items-center gap-2 rounded-full bg-[var(--accent)] px-5 py-3.5 text-sm font-semibold text-[var(--accent-ink)] shadow-soft-lg"
            aria-label={unread ? `Open Scout Oracle, ${unread} unread` : 'Open Scout Oracle'}
          >
            <span className="relative"><OracleMark thinking={false} size={20} /><UnreadBadge count={unread} /></span>
            Scout Oracle
          </motion.button>
        ) : (
          <motion.aside
            key="scout-sheet"
            role="dialog"
            aria-label="Scout Oracle"
            initial={{ y: '100%' }}
            animate={{ y: 0 }}
            exit={{ y: '100%' }}
            transition={SPRING}
            className="fixed inset-0 z-50 flex flex-col bg-[#FBFAF7] dark:bg-stone-900"
          >
            <ScoutDrawer toolbar={<ModeToolbar mode={mode} compact />} />
          </motion.aside>
        )}
      </AnimatePresence>
    );
  }

  return (
    <motion.aside
      layout
      animate={{ width: WIDTH_PX[mode] }}
      transition={SPRING}
      aria-label="Scout Oracle"
      data-mode={mode}
      className="z-20 flex h-screen shrink-0 flex-col overflow-hidden bg-[#FBFAF7] shadow-[-1px_0_0_rgba(41,37,36,0.04)] dark:bg-stone-900/60 dark:shadow-[-1px_0_0_rgba(255,255,255,0.03)]"
    >
      {mode === 'collapsed' ? (
        <div className="flex flex-col items-center gap-3 pt-5">
          <button
            type="button"
            onClick={() => setScoutDockMode(width)}
            title="Open Scout Oracle (Ctrl+J)"
            aria-label={unread ? `Open Scout Oracle, ${unread} unread` : 'Open Scout Oracle'}
            className="relative grid size-10 place-items-center rounded-2xl text-stone-500 transition-[background-color,transform] duration-300 hover:bg-stone-900/[0.04] active:scale-95 dark:hover:bg-white/[0.05]"
          >
            <OracleMark thinking={false} size={22} />
            <UnreadBadge count={unread} />
          </button>
          <button
            type="button"
            onClick={() => setScoutDockMode(width)}
            title="Expand"
            aria-label="Expand Scout Oracle"
            className="grid size-8 place-items-center rounded-xl text-stone-400 transition-colors hover:bg-stone-900/[0.04] hover:text-stone-700 dark:hover:bg-white/[0.05] dark:hover:text-stone-200"
          >
            <span className="material-symbols-outlined text-xl" aria-hidden="true">chevron_left</span>
          </button>
        </div>
      ) : (
        <motion.div className="flex h-full min-w-0 flex-col" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }}>
          <ScoutDrawer toolbar={<ModeToolbar mode={mode} compact={false} />} />
        </motion.div>
      )}
    </motion.aside>
  );
};
