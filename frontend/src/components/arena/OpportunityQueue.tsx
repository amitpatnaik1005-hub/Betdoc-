/**
 * The Arena's opportunity queue: Aryabhata's live +EV lines as frosted cards.
 *
 * Each card carries a hairline ring around its edge that drains over the signal's 15s life; an
 * in-place update (new price or stake for the same fixture and selection) refills it and softly
 * flashes the numbers that changed. At most five cards: a sixth pushes out the one closest to expiry.
 */
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, animate, motion, useMotionValue, useReducedMotion } from "framer-motion";
import { formatDateTime, formatINR, formatOdds, humanize } from "../../lib/format";
import { type ArenaSignal, MAX_VISIBLE, type RiskConfig, type StakeBinding, useArenaStore } from "../../store/useArenaStore";
import { isSelection, useExecutionStore } from "../../store/useExecutionStore";
import { CARD_VARIANTS, LiveDot, SPRING } from "../../ui/kit";

const PRUNE_INTERVAL_MS = 250;
const EASE_OUT = [0.22, 1, 0.36, 1] as const;

/** Hold the signals socket open while mounted, and sweep expired cards on a short interval. */
function useArenaFeed(): void {
  const connect = useArenaStore((s) => s.connect);
  const prune = useArenaStore((s) => s.prune);
  useEffect(() => connect(), [connect]);
  useEffect(() => {
    const id = window.setInterval(() => prune(Date.now()), PRUNE_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [prune]);
}

// ---------------------------------------------------------------- countdown ring
/** A rounded-rect stroke on the card's edge, drained linearly to the signal's local expiry. */
const TtlRing = ({ expiresAt, ttlMs }: { expiresAt: number; ttlMs: number }) => {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const offset = useMotionValue(0);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize((prev) => (prev && prev.w === width && prev.h === height ? prev : { w: width, h: height }));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const remaining = Math.max(0, expiresAt - Date.now());
    offset.set(1 - remaining / ttlMs);
    const controls = animate(offset, 1, { duration: remaining / 1000, ease: "linear" });
    return () => controls.stop();
  }, [expiresAt, ttlMs, offset]);

  return (
    <div ref={box} className="pointer-events-none absolute inset-0" aria-hidden="true">
      {size && size.w > 4 && (
        <svg width={size.w} height={size.h} className="absolute inset-0 overflow-visible">
          <rect x={1} y={1} width={size.w - 2} height={size.h - 2} rx={15} fill="none" strokeWidth={1.5} className="stroke-stone-900/[0.05] dark:stroke-white/[0.06]" />
          <motion.rect
            x={1}
            y={1}
            width={size.w - 2}
            height={size.h - 2}
            rx={15}
            fill="none"
            pathLength={1}
            strokeDasharray="1 1"
            strokeWidth={2}
            strokeLinecap="round"
            style={{ strokeDashoffset: offset, stroke: "var(--accent)" }}
          />
        </svg>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- one card
const BINDING: Record<StakeBinding, (risk: RiskConfig | null, s: ArenaSignal) => string> = {
  kelly: (risk) => `${risk ? risk.kelly_multiplier : "¼"}× Kelly`,
  pct_cap: (risk) => `capped at ${risk ? risk.max_stake_pct : "—"}% of bankroll`,
  max_bet: (risk) => `max bet ${formatINR(risk?.max_bet_size)}`,
  halted: () => "trading halted",
  no_bankroll: () => "no bankroll to stake",
  no_edge: () => "no stake",
};

const pickLabel = (s: ArenaSignal): string => (s.selection === "HOME" ? s.home_team : s.selection === "AWAY" ? s.away_team : s.selection === "DRAW" ? "Draw" : humanize(s.selection));

/** Re-keyed on every in-place update: the value fades in over a soft wash of the change colour. */
const Flash = ({ revision, children, tone = "accent" }: { revision: number; children: ReactNode; tone?: "accent" | "good" }) => {
  const reduce = useReducedMotion();
  return (
    <motion.span
      key={revision}
      initial={revision === 0 || reduce ? false : { backgroundColor: tone === "good" ? "rgba(16,185,129,0.16)" : "rgba(200,155,60,0.18)" }}
      animate={{ backgroundColor: "rgba(0,0,0,0)" }}
      transition={{ duration: 1.4, ease: EASE_OUT }}
      className="-mx-1 rounded-md px-1"
    >
      {children}
    </motion.span>
  );
};

const OpportunityCard = ({ signal, risk, onLoad }: { signal: ArenaSignal; risk: RiskConfig | null; onLoad: (s: ArenaSignal) => void }) => {
  const reduce = useReducedMotion();
  const fixture = `${signal.home_team} v ${signal.away_team}`;
  return (
    <motion.li
      layout
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: -16, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={reduce ? { opacity: 0 } : { opacity: 0, x: 56, scale: 0.96, transition: { duration: 0.4, ease: EASE_OUT } }}
      transition={SPRING}
      className="relative isolate flex flex-col rounded-2xl bg-white/65 p-5 shadow-soft ring-1 ring-inset ring-stone-900/[0.04] backdrop-blur-md sm:p-6 dark:bg-stone-900/55 dark:shadow-none dark:ring-white/[0.06]"
      aria-label={`${pickLabel(signal)} at ${formatOdds(signal.odds)}, plus ${signal.ev_percent.toFixed(2)} percent expected value`}
    >
      <TtlRing expiresAt={signal.expiresAt} ttlMs={signal.ttlMs} />

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-stone-500 dark:text-stone-400" title={fixture}>
            {fixture}
          </p>
          <p className="mt-0.5 text-[11px] text-stone-400 dark:text-stone-500">
            {signal.commence_time ? formatDateTime(signal.commence_time) : signal.market_type}
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-stone-900/[0.04] px-2.5 py-1 text-[11px] font-medium text-stone-600 dark:bg-white/[0.06] dark:text-stone-300">
          {humanize(signal.bookmaker_id)}
        </span>
      </div>

      <p className="mt-5 truncate text-lg font-semibold tracking-tight text-stone-900 dark:text-stone-50">
        {pickLabel(signal)}
        <span className="ml-2 text-sm font-normal text-stone-400 dark:text-stone-500">{signal.selection === "DRAW" ? "" : "to win"}</span>
      </p>

      <dl className="mt-5 grid grid-cols-2 gap-3">
        <div className="min-w-0">
          <dt className="text-[11px] text-stone-400 dark:text-stone-500">Odds</dt>
          <dd className="mt-1 font-mono text-[17px] font-medium tabular-nums text-stone-800 dark:text-stone-100">
            <Flash revision={signal.revision}>{formatOdds(signal.odds)}</Flash>
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-[11px] text-stone-400 dark:text-stone-500">Edge</dt>
          <dd className="mt-1 font-mono text-[17px] font-medium tabular-nums text-emerald-700/80 dark:text-emerald-300/80">
            <Flash revision={signal.revision} tone="good">
              +{signal.ev_percent.toFixed(2)}%
            </Flash>
          </dd>
        </div>
        <div className="col-span-2 mt-1 flex items-baseline justify-between gap-3 rounded-xl bg-stone-900/[0.03] px-3 py-2.5 dark:bg-white/[0.04]">
          <dt className="text-[11px] text-stone-500 dark:text-stone-400">Recommended stake</dt>
          <dd className="min-w-0 truncate font-mono text-[17px] font-semibold tabular-nums text-stone-900 dark:text-stone-50">
            <Flash revision={signal.revision}>{formatINR(signal.kelly_stake_inr)}</Flash>
          </dd>
        </div>
      </dl>

      <p className="mb-6 mt-3 text-[11px] leading-relaxed text-stone-400 dark:text-stone-500">
        Fair {(signal.true_prob * 100).toFixed(1)}% · {signal.books} books · {signal.devig_method === "mpo" ? "MPO" : humanize(signal.devig_method)} · {BINDING[signal.stake_binding](risk, signal)}
      </p>

      <motion.button
        type="button"
        onClick={() => onLoad(signal)}
        whileHover={reduce ? undefined : { y: -1 }}
        whileTap={reduce ? undefined : { scale: 0.97 }}
        transition={SPRING}
        className="mt-auto inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-[var(--accent-ink)] shadow-sm transition-shadow hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-stone-900"
      >
        <span className="material-symbols-outlined text-[18px]">add_shopping_cart</span>
        Load into Betslip
      </motion.button>
    </motion.li>
  );
};

// ---------------------------------------------------------------- the queue
const STATUS: Record<string, [string, "good" | "warning" | "critical", boolean]> = {
  open: ["Live", "good", true],
  connecting: ["Connecting", "warning", true],
  reconnecting: ["Reconnecting", "warning", true],
  paused: ["Paused", "warning", false],
  error: ["Offline", "critical", true],
  closed: ["Offline", "critical", false],
  idle: ["Idle", "warning", false],
};

export const OpportunityQueue = () => {
  useArenaFeed();
  const signals = useArenaStore((s) => s.signals);
  const status = useArenaStore((s) => s.status);
  const risk = useArenaStore((s) => s.risk);
  const bankroll = useArenaStore((s) => s.bankroll);
  const setDraft = useExecutionStore((s) => s.setDraft);
  const [label, tone, active] = STATUS[status] ?? STATUS.idle;

  const load = (s: ArenaSignal) => {
    if (!isSelection(s.selection)) return;
    setDraft({
      matchId: s.fixture_id,
      selection: s.selection,
      odds: s.odds,
      trueProbability: s.true_prob,
      label: `${s.home_team} v ${s.away_team}`,
      source: `Aryabhata · ${humanize(s.bookmaker_id)} · +${s.ev_percent.toFixed(2)}% EV`,
      stake: s.kelly_stake_inr >= 1 ? Math.floor(s.kelly_stake_inr) : undefined, // whole rupees, rounded down: never above the cap
    });
  };

  const visible = signals.slice(0, MAX_VISIBLE);
  return (
    <motion.section variants={CARD_VARIANTS} className="flex min-w-0 flex-col gap-4 lg:col-span-12" aria-label="Aryabhata signals">
      <div className="flex min-h-[32px] flex-wrap items-center justify-between gap-3 px-1">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">bolt</span>
          <h2 className="truncate text-[15px] font-semibold text-stone-900 dark:text-stone-100">Aryabhata signals</h2>
          <span className="hidden truncate text-xs text-stone-400 sm:inline dark:text-stone-500">
            +EV against the market consensus · 15s life
            {risk ? ` · stakes capped at ${risk.max_stake_pct}%` : ""}
            {bankroll !== null ? ` of ${formatINR(bankroll)}` : ""}
          </span>
        </div>
        <span className="inline-flex items-center gap-2 text-xs font-medium text-stone-500 dark:text-stone-400" role="status">
          <LiveDot active={active} tone={tone} />
          {label}
        </span>
      </div>

      <div className="relative isolate overflow-hidden rounded-3xl bg-gradient-to-br from-amber-50/80 via-[#F8F6F0] to-emerald-50/50 p-4 sm:p-8 dark:from-stone-900 dark:via-stone-950 dark:to-emerald-950/25">
        <div aria-hidden="true" className="pointer-events-none absolute -left-16 -top-24 -z-10 size-72 rounded-full bg-[var(--accent-glow)] opacity-30 blur-3xl" />
        <div aria-hidden="true" className="pointer-events-none absolute -bottom-28 right-0 -z-10 size-80 rounded-full bg-emerald-200/40 blur-3xl dark:bg-emerald-500/10" />

        <motion.ul layout className="relative grid grid-cols-[repeat(auto-fill,minmax(min(100%,17rem),1fr))] gap-5">
          <AnimatePresence mode="popLayout" initial={false}>
            {visible.map((s) => (
              <OpportunityCard key={s.key} signal={s} risk={risk} onLoad={load} />
            ))}
          </AnimatePresence>
        </motion.ul>

        <AnimatePresence initial={false}>
          {visible.length === 0 && (
            <motion.div
              key="quiet"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.4, ease: EASE_OUT }}
              className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center"
            >
              <span className="mb-1 grid size-12 place-items-center rounded-2xl bg-white/70 backdrop-blur-md dark:bg-stone-800/70">
                <span className="material-symbols-outlined text-[22px] text-stone-400 dark:text-stone-500">radar</span>
              </span>
              <p className="text-sm font-semibold text-stone-700 dark:text-stone-200">No edges right now</p>
              <p className="max-w-md text-xs leading-relaxed text-stone-500 dark:text-stone-400">
                A card appears the moment a bookmaker&apos;s price beats the de-vigged consensus of every other book by more than 0.5%,
                and leaves when the line closes or after 15 seconds.
              </p>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.section>
  );
};
