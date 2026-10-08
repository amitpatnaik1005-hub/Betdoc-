/**
 * Control Panel: Aryabhata's stake cap. No recommended stake may exceed this share of the live
 * bankroll; the change applies to every open Arena the moment the slider is released.
 */
import { type KeyboardEvent, useRef, useState } from "react";
import { motion } from "framer-motion";
import { apiClient } from "../../api/client";
import { type ControlSettings, useDashboardSummary } from "../../lib/api";
import { formatINR } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import { AnimatedNumber, CARD_VARIANTS, SPRING, SURFACE } from "../../ui/kit";

const MIN_PCT = 1;
const MAX_PCT = 10;
const STEP = 0.5;
const KEY_COMMIT_DELAY_MS = 600;
const TICKS = Array.from({ length: MAX_PCT - MIN_PCT + 1 }, (_, i) => MIN_PCT + i);

const position = (pct: number): number => ((pct - MIN_PCT) / (MAX_PCT - MIN_PCT)) * 100;

export const StakeCapPanel = ({ settings }: { settings: ControlSettings }) => {
  const saved = settings.max_stake_pct;
  // The draft remembers which saved value it was dragged from: once the save lands (saved changes),
  // the server's value takes over again without an effect to reset it.
  const [draft, setDraft] = useState<{ value: number; base: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const keyTimer = useRef<number | null>(null);
  const summary = useDashboardSummary();

  const value = draft && draft.base === saved ? draft.value : saved;
  const bankroll = summary.data?.total_bankroll ?? null;
  const ceiling = bankroll !== null ? (bankroll * value) / 100 : null;
  const maxBetBinds = ceiling !== null && settings.max_bet_size < ceiling;

  const commit = async (next: number) => {
    if (keyTimer.current !== null) {
      window.clearTimeout(keyTimer.current);
      keyTimer.current = null;
    }
    if (next === saved || busy) return;
    setBusy(true);
    const result = await runMutation(() => apiClient.patch<ControlSettings>("/control-panel", { max_stake_pct: next }), {
      invalidate: ["system", "control-panel"],
      success: `Stakes now capped at ${next}% of bankroll`,
      errorTitle: "Stake cap rejected",
    });
    setBusy(false);
    if (result === undefined) setDraft(null); // rejected: back to the saved value
  };

  const onKeyUp = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) return;
    if (keyTimer.current !== null) window.clearTimeout(keyTimer.current);
    const next = Number(e.currentTarget.value);
    keyTimer.current = window.setTimeout(() => void commit(next), KEY_COMMIT_DELAY_MS);
  };

  return (
    <motion.section variants={CARD_VARIANTS} className="flex min-w-0 flex-col gap-4 lg:col-span-12">
      <div className="flex min-h-[32px] items-center gap-2.5 px-1">
        <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">shield</span>
        <h2 className="text-[15px] font-semibold text-stone-900 dark:text-stone-100">Stake cap</h2>
        <span className="hidden truncate text-xs text-stone-400 sm:inline dark:text-stone-500">Aryabhata never recommends more than this per signal</span>
      </div>
      <div className={`${SURFACE} grid grid-cols-1 items-center gap-8 p-6 sm:p-8 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]`}>
        <div className="min-w-0">
          <p className="text-[44px] font-semibold leading-none tracking-tight text-stone-900 dark:text-stone-50">
            <AnimatedNumber value={value} format={(n) => `${n.toFixed(1)}%`} />
          </p>
          <p className="mt-3 text-sm text-stone-500 dark:text-stone-400">
            {ceiling !== null ? (
              <>
                At most <span className="font-medium text-stone-700 dark:text-stone-200">{formatINR(ceiling)}</span> of your {formatINR(bankroll)} bankroll
              </>
            ) : (
              "of the live bankroll, per signal"
            )}
          </p>
          <p className="mt-1 text-xs text-stone-400 dark:text-stone-500">
            Sized at {settings.default_kelly_fraction}× Kelly{busy ? " · saving…" : ""}
          </p>
        </div>

        <div className="min-w-0">
          <div className="relative h-12">
            <div className="absolute inset-x-0 top-1/2 h-2 -mt-1 rounded-full bg-stone-100 dark:bg-stone-800" />
            <motion.div className="absolute left-0 top-1/2 h-2 -mt-1 rounded-full bg-[var(--accent)]" initial={false} animate={{ width: `${position(value)}%` }} transition={SPRING} />
            {TICKS.map((t) => (
              <span
                key={t}
                aria-hidden="true"
                className={`absolute top-1/2 -ml-[2px] -mt-[2px] size-1 rounded-full ${t <= value ? "bg-white/70" : "bg-stone-300 dark:bg-stone-600"}`}
                style={{ left: `${position(t)}%` }}
              />
            ))}
            <input
              type="range"
              min={MIN_PCT}
              max={MAX_PCT}
              step={STEP}
              value={value}
              aria-label="Maximum stake per signal, percent of bankroll"
              aria-valuetext={`${value}% of bankroll`}
              disabled={busy}
              onChange={(e) => setDraft({ value: Number(e.target.value), base: saved })}
              onPointerUp={(e) => void commit(Number(e.currentTarget.value))}
              onKeyUp={onKeyUp}
              onBlur={(e) => void commit(Number(e.currentTarget.value))}
              className="peer absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0 disabled:cursor-wait"
            />
            <motion.div
              aria-hidden="true"
              className="absolute top-1/2 -ml-3 -mt-3 grid size-6 place-items-center rounded-full bg-white shadow-md ring-1 ring-stone-900/5 peer-focus-visible:ring-2 peer-focus-visible:ring-[var(--accent)] dark:bg-stone-100"
              initial={false}
              animate={{ left: `${position(value)}%`, scale: busy ? 0.9 : 1 }}
              transition={SPRING}
            >
              <span className="size-2 rounded-full bg-[var(--accent)]" />
            </motion.div>
          </div>
          <div className="mt-1 flex justify-between font-mono text-[11px] tabular-nums text-stone-400 dark:text-stone-500">
            <span>{MIN_PCT}%</span>
            <span>{MAX_PCT}%</span>
          </div>
          {maxBetBinds && (
            <p className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-800 dark:bg-amber-400/10 dark:text-amber-200">
              Execution&apos;s max bet size ({formatINR(settings.max_bet_size)}) is lower than this cap, so every stake stops there. Raise it in Global
              overrides to let the cap decide.
            </p>
          )}
        </div>
      </div>
    </motion.section>
  );
};
