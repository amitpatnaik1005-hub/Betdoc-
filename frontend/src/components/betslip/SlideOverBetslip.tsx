/**
 * Slide-over betslip for Aryabhata signals: stake, pending risk, potential P&L, and a guarded
 * "Confirm Execution" that goes through the CFO ledger's two-phase execution.
 *
 * The slip follows the live signal: if the edge updates, the price shown (and sent) updates with
 * it; if the edge closes or its 15 seconds run out, confirming is disabled before the server has
 * to say so.
 */
import { type FormEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { type Bankroll, REFUSALS, potentialProfit, useBankroll } from "../../lib/cfo";
import { formatINR, formatOdds, humanize } from "../../lib/format";
import { useArenaStore } from "../../store/useArenaStore";
import { parseStake, useBetslipStore } from "../../store/useBetslipStore";
import { SPRING } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

/** Ticks four times a second while the slip is open, for the countdown. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

const Row = ({ label, value, tone, hint }: { label: string; value: string; tone?: "good" | "warn" | "bad"; hint?: string }) => (
  <div className="flex items-baseline justify-between gap-4 py-2">
    <dt className="text-[13px] text-stone-500 dark:text-stone-400">
      {label}
      {hint && <span className="ml-1.5 text-[11px] text-stone-400 dark:text-stone-500">{hint}</span>}
    </dt>
    <dd
      className={cx(
        "font-mono text-[15px] font-medium tabular-nums",
        tone === "good" ? "text-emerald-700 dark:text-emerald-300" : tone === "warn" ? "text-amber-700 dark:text-amber-300" : tone === "bad" ? "text-rose-600 dark:text-rose-300" : "text-stone-800 dark:text-stone-100",
      )}
    >
      {value}
    </dd>
  </div>
);

function riskView(bankroll: Bankroll | undefined, fixtureId: string, stake: number | null) {
  if (!bankroll) return null;
  const onFixture = bankroll.open_positions.filter((p) => p.fixture_id === fixtureId).reduce((acc, p) => acc + p.stake_inr, 0);
  const fixtureCap = (bankroll.equity * bankroll.limits.max_market_exposure_pct) / 100;
  const amount = stake ?? 0;
  return {
    onFixture,
    fixtureCap,
    fixtureAfter: onFixture + amount,
    exposureAfter: bankroll.exposure_balance + amount,
    availableAfter: bankroll.available_balance - amount,
    overFixture: onFixture + amount > fixtureCap + 1e-9,
    overAvailable: amount > bankroll.available_balance + 1e-9,
  };
}

export const SlideOverBetslip = () => {
  const { open, signal, stake, phase, receipt, error, setStake, close, confirm } = useBetslipStore();
  const live = useArenaStore((s) => (signal ? s.signals.find((x) => x.key === signal.key) : undefined));
  const bankroll = useBankroll();
  const now = useNow(open);
  const reduce = useReducedMotion();
  const stakeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  const current = live ?? signal;
  const amount = parseStake(stake);
  const odds = current?.odds ?? 0;
  const remaining = current && live ? Math.max(0, live.expiresAt - now) : 0;
  const expired = phase !== "executed" && phase !== "unknown" && (!live || remaining <= 0);
  const priceMoved = !!(signal && live && live.odds !== signal.odds);
  const busy = phase === "submitting";
  const done = phase === "executed" || phase === "unknown";
  // Once placed, the bankroll already holds the stake: show where it stands, not another projection
  const risk = current ? riskView(bankroll.data, current.fixture_id, done ? 0 : amount) : null;
  const profit = amount !== null ? potentialProfit(amount, odds) : 0;
  const halted = bankroll.data?.kill_switch ?? false;
  const blocked = halted || expired || amount === null || !!risk?.overAvailable || !!risk?.overFixture;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (blocked || busy || done || !live) return;
    void confirm(live.odds, live.expires_at);
  };

  const pick = current ? (current.selection === "HOME" ? current.home_team : current.selection === "AWAY" ? current.away_team : "Draw") : "";

  return createPortal(
    <AnimatePresence>
      {open && current && (
        <motion.div
          key="betslip"
          className="fixed inset-0 z-[90] flex justify-end bg-stone-900/25 backdrop-blur-sm dark:bg-black/45"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && close()}
        >
          <motion.form
            role="dialog"
            aria-modal="true"
            aria-label="Betslip"
            onSubmit={submit}
            initial={reduce ? { opacity: 0 } : { x: "100%" }}
            animate={reduce ? { opacity: 1 } : { x: 0 }}
            exit={reduce ? { opacity: 0 } : { x: "100%" }}
            transition={SPRING}
            onAnimationComplete={() => stakeRef.current?.focus()}
            className="flex h-full w-full max-w-md flex-col bg-[#FBFAF6]/95 shadow-soft-lg backdrop-blur-md dark:bg-stone-900/95 dark:ring-1 dark:ring-white/[0.06]"
          >
            <header className="flex items-start justify-between gap-4 px-7 pb-5 pt-8">
              <div className="min-w-0">
                <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-stone-400">Betslip · {bankroll.data?.execution_mode === "live" ? "live bookmaker" : "paper"}</p>
                <h2 className="mt-2 truncate text-xl font-semibold tracking-tight text-stone-900 dark:text-stone-50">{pick}</h2>
                <p className="mt-0.5 truncate text-sm text-stone-500 dark:text-stone-400">
                  {current.home_team} v {current.away_team} · {humanize(current.bookmaker_id)}
                </p>
              </div>
              <button type="button" onClick={close} disabled={busy} aria-label="Close betslip" className="grid size-9 shrink-0 place-items-center rounded-full text-stone-400 transition-colors hover:bg-stone-200/60 hover:text-stone-700 disabled:opacity-40 dark:hover:bg-white/10">
                <span className="material-symbols-outlined text-[20px]">close</span>
              </button>
            </header>

            <div className="flex-1 overflow-y-auto px-7 pb-6">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full bg-emerald-50 px-2.5 py-1 font-mono text-xs font-medium text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300">
                  +{current.ev_percent.toFixed(2)}% EV
                </span>
                {current.is_steam_move && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-orange-50 px-2.5 py-1 text-xs font-medium text-orange-700 dark:bg-orange-400/10 dark:text-orange-300">
                    <span className="material-symbols-outlined text-[14px]">local_fire_department</span>
                    Steam move
                  </span>
                )}
                <span className={cx("ml-auto font-mono text-xs tabular-nums", expired ? "text-rose-600 dark:text-rose-300" : "text-stone-400")} aria-live="polite">
                  {done ? "" : expired ? "Signal expired" : `${Math.ceil(remaining / 1000)}s left`}
                </span>
              </div>

              <div className="mt-6 grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-white p-4 shadow-soft dark:bg-stone-800/60 dark:shadow-none">
                  <p className="text-[11px] text-stone-400">Odds</p>
                  <p className="mt-1 font-mono text-2xl font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatOdds(odds)}</p>
                  {priceMoved && <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">was {formatOdds(signal?.odds)}</p>}
                </div>
                <label className="rounded-2xl bg-white p-4 shadow-soft focus-within:ring-2 focus-within:ring-[var(--accent)] dark:bg-stone-800/60 dark:shadow-none">
                  <span className="text-[11px] text-stone-400">Stake (₹)</span>
                  <input
                    ref={stakeRef}
                    inputMode="decimal"
                    autoComplete="off"
                    value={stake}
                    onChange={(e) => setStake(e.target.value)}
                    disabled={busy || done}
                    aria-invalid={amount === null}
                    className="mt-1 w-full bg-transparent font-mono text-2xl font-semibold tabular-nums text-stone-900 outline-none placeholder:text-stone-300 disabled:opacity-60 dark:text-stone-50"
                    placeholder="0"
                  />
                </label>
              </div>
              <p className="mt-2 text-[11px] text-stone-400 dark:text-stone-500">
                Recommended {formatINR(current.kelly_stake_inr)} · {(current.true_prob * 100).toFixed(1)}% fair · {current.books} books
              </p>

              <dl className="mt-6 divide-y divide-stone-900/[0.05] dark:divide-white/[0.06]">
                <Row label="Potential P&L" value={`+${formatINR(profit)}`} tone="good" hint="if it wins" />
                <Row label="Returns" value={formatINR((amount ?? 0) + profit)} />
                {risk && (
                  <>
                    <Row label={done ? "In exposure" : "Pending risk"} value={formatINR(risk.exposureAfter)} hint={done ? "all open bets" : "exposure after this bet"} />
                    <Row label={done ? "Available" : "Available after"} value={formatINR(risk.availableAfter)} tone={risk.overAvailable && !done ? "bad" : undefined} />
                    <Row
                      label="This fixture"
                      value={`${formatINR(risk.fixtureAfter)} / ${formatINR(risk.fixtureCap)}`}
                      tone={risk.overFixture ? "bad" : risk.fixtureAfter > risk.fixtureCap * 0.8 ? "warn" : undefined}
                      hint="open / cap"
                    />
                  </>
                )}
              </dl>

              <AnimatePresence mode="wait" initial={false}>
                {halted && phase === "editing" && (
                  <motion.p key="halted" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="mt-5 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
                    Trading is halted by the kill switch.
                  </motion.p>
                )}
                {phase === "executed" && receipt && (
                  <motion.div key="ok" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-5 rounded-2xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-200">
                    <p className="font-semibold">Placed{receipt.execution_mode === "paper" ? " (paper)" : ""}</p>
                    <p className="mt-1 text-[13px] opacity-80">
                      {formatINR(receipt.stake_inr)} moved to exposure · ref <span className="font-mono">{receipt.bookmaker_ref}</span>
                    </p>
                  </motion.div>
                )}
                {phase === "unknown" && receipt && (
                  <motion.div key="unknown" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-5 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:bg-amber-400/10 dark:text-amber-200">
                    <p className="font-semibold">Waiting on the bookmaker</p>
                    <p className="mt-1 text-[13px] opacity-80">{receipt.message}</p>
                  </motion.div>
                )}
                {phase === "failed" && error && (
                  <motion.div key="failed" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className={cx("mt-5 rounded-2xl px-4 py-3 text-sm", error.inFlight ? "bg-amber-50 text-amber-800 dark:bg-amber-400/10 dark:text-amber-200" : "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300")}>
                    <p className="font-semibold">{error.reason ? REFUSALS[error.reason] ?? humanize(error.reason) : error.inFlight ? "No answer from the server" : "Order refused"}</p>
                    <p className="mt-1 text-[13px] opacity-80">
                      {error.inFlight ? "The order may still have gone through. Check open positions before sending another." : error.message}
                    </p>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            <footer className="border-t border-stone-900/[0.05] px-7 pb-8 pt-5 dark:border-white/[0.06]">
              {done ? (
                <button type="button" onClick={close} className="w-full rounded-full bg-stone-900 px-5 py-3.5 text-sm font-semibold text-white transition-colors hover:bg-stone-800 dark:bg-stone-100 dark:text-stone-900">
                  Done
                </button>
              ) : (
                <motion.button
                  type="submit"
                  disabled={blocked || busy}
                  aria-busy={busy || undefined}
                  whileTap={blocked || busy || reduce ? undefined : { scale: 0.97 }}
                  transition={SPRING}
                  className="relative inline-flex w-full items-center justify-center gap-2 rounded-full bg-[var(--accent)] px-5 py-3.5 text-sm font-semibold text-[var(--accent-ink)] shadow-sm transition-[opacity,box-shadow] hover:shadow-md disabled:cursor-not-allowed disabled:opacity-45 disabled:shadow-none"
                >
                  <span className={cx("material-symbols-outlined text-[18px]", busy && "animate-spin")}>{busy ? "progress_activity" : "verified"}</span>
                  {busy ? "Executing…" : phase === "failed" ? "Try again" : "Confirm Execution"}
                </motion.button>
              )}
              <p className="mt-3 text-center text-[11px] text-stone-400 dark:text-stone-500">
                Risk guards run first; the stake is reserved under a lock and only kept if the bookmaker accepts.
              </p>
            </footer>
          </motion.form>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
};
