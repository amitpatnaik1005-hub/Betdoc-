/**
 * Control Panel, Risk management: the CFO ledger's live state and the four guard limits.
 *
 * Each slider saves on release (keyboard: after a short pause) to `PUT /omni/risk-settings`; the
 * very next execution is checked against the new value.
 */
import { type KeyboardEvent, useRef, useState } from "react";
import { motion } from "framer-motion";
import { apiClient } from "../../api/client";
import { type Bankroll, type RiskSettings, useBankroll } from "../../lib/cfo";
import { formatINR } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import { Async, CARD_VARIANTS, LiveDot, Meter, Panel, SPRING, SURFACE } from "../../ui/kit";

const KEY_COMMIT_DELAY_MS = 600;

type Field = keyof RiskSettings;

interface SliderSpec {
  field: Field;
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  format: (v: number) => string;
}

const SLIDERS: readonly SliderSpec[] = [
  { field: "daily_drawdown_pct", label: "Daily drawdown", hint: "Trading pauses when the last 24h lose more than this share of peak bankroll.", min: 1, max: 50, step: 0.5, format: (v) => `${v.toFixed(1)}%` },
  { field: "max_market_exposure_pct", label: "Max market exposure", hint: "Open stakes on any one fixture, as a share of equity.", min: 1, max: 50, step: 0.5, format: (v) => `${v.toFixed(1)}%` },
  { field: "max_loss_streak", label: "Consecutive losses", hint: "Trading pauses after this many losses in a row; a win resets it.", min: 1, max: 20, step: 1, format: (v) => `${v}` },
  { field: "velocity_max_cv_pct", label: "Velocity lock", hint: "Blocks a bet while its price swings more than this (60s std dev ÷ mean).", min: 0.5, max: 20, step: 0.5, format: (v) => `${v.toFixed(1)}%` },
  { field: "max_slippage_pct", label: "Max slippage", hint: "How far below the asked price a fill may land. Never below the +0.5% EV floor.", min: 0, max: 5, step: 0.25, format: (v) => `${v.toFixed(2)}%` },
];

const position = (v: number, min: number, max: number): number => ((v - min) / (max - min)) * 100;

const GuardSlider = ({ spec, saved, preview }: { spec: SliderSpec; saved: number; preview?: string }) => {
  // Remembers the saved value it was dragged from: once the save lands, the server's value takes over
  const [draft, setDraft] = useState<{ value: number; base: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const keyTimer = useRef<number | null>(null);
  const value = draft && draft.base === saved ? draft.value : saved;

  const commit = async (next: number) => {
    if (keyTimer.current !== null) {
      window.clearTimeout(keyTimer.current);
      keyTimer.current = null;
    }
    if (next === saved || busy) return;
    setBusy(true);
    const result = await runMutation(() => apiClient.put<RiskSettings>("/omni/risk-settings", { [spec.field]: spec.step < 1 ? next.toFixed(2) : next }), {
      invalidate: ["cfo"],
      success: `${spec.label} set to ${spec.format(next)}`,
      errorTitle: `${spec.label} rejected`,
    });
    setBusy(false);
    if (result === undefined) setDraft(null);
  };

  const onKeyUp = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) return;
    if (keyTimer.current !== null) window.clearTimeout(keyTimer.current);
    const next = Number(e.currentTarget.value);
    keyTimer.current = window.setTimeout(() => void commit(next), KEY_COMMIT_DELAY_MS);
  };

  const at = position(value, spec.min, spec.max);
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm font-semibold text-stone-800 dark:text-stone-100">{spec.label}</p>
        <p className="font-mono text-lg font-semibold tabular-nums text-stone-900 dark:text-stone-50">
          {spec.format(value)}
          {busy && <span className="ml-1.5 text-[11px] font-normal text-stone-400">saving…</span>}
        </p>
      </div>
      <div className="relative h-10">
        <div className="absolute inset-x-0 top-1/2 -mt-1 h-2 rounded-full bg-stone-100 dark:bg-stone-800" />
        <motion.div className="absolute left-0 top-1/2 -mt-1 h-2 rounded-full bg-[var(--accent)]" initial={false} animate={{ width: `${at}%` }} transition={SPRING} />
        <input
          type="range"
          min={spec.min}
          max={spec.max}
          step={spec.step}
          value={value}
          aria-label={spec.label}
          aria-valuetext={spec.format(value)}
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
          animate={{ left: `${at}%`, scale: busy ? 0.9 : 1 }}
          transition={SPRING}
        >
          <span className="size-2 rounded-full bg-[var(--accent)]" />
        </motion.div>
      </div>
      <p className="text-xs leading-relaxed text-stone-500 dark:text-stone-400">
        {spec.hint}
        {preview && <span className="ml-1 font-medium text-stone-700 dark:text-stone-200">{preview}</span>}
      </p>
    </div>
  );
};

const LedgerState = ({ bank }: { bank: Bankroll }) => {
  const loss = Math.max(0, -bank.pnl_24h);
  const used = bank.drawdown_limit ? loss / bank.drawdown_limit : 0;
  const streak = bank.loss_streak;
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
      <div>
        <p className="text-[11px] text-stone-400">Available</p>
        <p className="mt-1 font-mono text-lg font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatINR(bank.available_balance)}</p>
      </div>
      <div>
        <p className="text-[11px] text-stone-400">In exposure</p>
        <p className="mt-1 font-mono text-lg font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatINR(bank.exposure_balance)}</p>
      </div>
      <div>
        <p className="text-[11px] text-stone-400">Peak equity</p>
        <p className="mt-1 font-mono text-lg font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatINR(bank.peak_balance)}</p>
      </div>
      <div>
        <p className="text-[11px] text-stone-400">Kill switch</p>
        <p className="mt-1.5 inline-flex items-center gap-2 text-sm font-semibold text-stone-800 dark:text-stone-100">
          <LiveDot active tone={bank.kill_switch ? "critical" : "good"} />
          {bank.kill_switch ? "Engaged" : "Clear"}
        </p>
      </div>
      <div className="col-span-2">
        <div className="flex items-baseline justify-between text-[11px] text-stone-400">
          <span>24h drawdown</span>
          <span className="font-mono tabular-nums">
            {formatINR(loss)} / {bank.drawdown_limit !== null ? formatINR(bank.drawdown_limit) : "—"}
          </span>
        </div>
        <div className="mt-2">
          <Meter value={Math.min(1, used)} tone={used >= 1 ? "critical" : used >= 0.8 ? "warning" : "good"} label="24h drawdown used" />
        </div>
      </div>
      <div className="col-span-2">
        <div className="flex items-baseline justify-between text-[11px] text-stone-400">
          <span>Loss streak</span>
          <span className="font-mono tabular-nums">
            {streak ?? "?"} / {bank.limits.max_loss_streak}
          </span>
        </div>
        <div className="mt-2">
          <Meter value={streak === null ? 0 : Math.min(1, streak / bank.limits.max_loss_streak)} tone={streak !== null && streak >= bank.limits.max_loss_streak ? "critical" : "accent"} label="Loss streak" />
        </div>
      </div>
    </div>
  );
};

export const RiskManagement = () => {
  const bank = useBankroll();
  return (
    <>
      <Panel
        title="CFO ledger"
        icon="account_balance"
        className="lg:col-span-12"
        updatedAt={bank.updatedAt}
        subtitle={bank.data ? `${bank.data.execution_mode === "live" ? "live bookmaker" : "paper execution"} · ${bank.data.open_positions.length} open` : undefined}
      >
        <Async resource={bank} skeletonRows={2}>
          {(data) => <LedgerState bank={data} />}
        </Async>
      </Panel>
      <motion.section variants={CARD_VARIANTS} className="flex min-w-0 flex-col gap-4 lg:col-span-12">
        <div className="flex min-h-[32px] items-center gap-2.5 px-1">
          <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">shield_lock</span>
          <h2 className="text-[15px] font-semibold text-stone-900 dark:text-stone-100">Risk guards</h2>
          <span className="hidden truncate text-xs text-stone-400 sm:inline dark:text-stone-500">Every execution passes all of them, kill switch first</span>
        </div>
        <div className={`${SURFACE} p-6 sm:p-8`}>
          <Async resource={bank} skeletonRows={3}>
            {(data) => (
              <div className="grid grid-cols-1 gap-x-12 gap-y-10 md:grid-cols-2">
                {SLIDERS.map((spec) => {
                  const saved = data.limits[spec.field];
                  const preview =
                    spec.field === "daily_drawdown_pct"
                      ? `= ${formatINR((data.peak_balance * saved) / 100)} today.`
                      : spec.field === "max_market_exposure_pct"
                        ? `= ${formatINR((data.equity * saved) / 100)} per fixture.`
                        : undefined;
                  return <GuardSlider key={spec.field} spec={spec} saved={saved} preview={preview} />;
                })}
              </div>
            )}
          </Async>
        </div>
      </motion.section>
    </>
  );
};
