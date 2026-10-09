/**
 * Control Panel, Active portfolio: every open book marked to the live books, five times a second.
 *
 * - Positions: one row per market the user holds. A row pulses green while its balanced hedge
 *   locks in a profit; "Hedge" opens the hedge modal.
 * - Hedge modal: a slider from Unbalanced (Free Bet: nothing lost if the bet fails, all profit on
 *   it) to Balanced (Equal Profit on every outcome), with the projected P&L per outcome.
 * - Arbitrage scanner: commission- and FX-adjusted arbitrages as they appear (new ones flash).
 *   Execution fires Leg A first; a partial fill re-sizes the legs after it.
 */
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ApiError, apiClient } from "../../api/client";
import { REFUSALS } from "../../lib/cfo";
import { formatINR, formatOdds, formatTime, humanize } from "../../lib/format";
import {
  type ArbOpportunity,
  type HedgePlan,
  LEG_REFUSALS,
  type LegStatus,
  type MarketBook,
  type MultiLegReceipt,
  interpolatePlan,
  n,
  useAge,
  useLivePortfolio,
} from "../../lib/portfolio";
import { invalidate } from "../../lib/resource";
import { newIdempotencyKey } from "../../store/useExecutionStore";
import { Button, CARD_VARIANTS, ConfirmButton, EmptyState, LiveDot, Panel, Pill, SPRING, SURFACE, Skeleton } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const signed = (value: string | number | null | undefined): string => {
  const v = n(value);
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${formatINR(Math.abs(v))}`;
};
const pnlTone = (value: string | number | null | undefined): string =>
  n(value) > 0 ? "text-emerald-700 dark:text-emerald-300" : n(value) < 0 ? "text-rose-600 dark:text-rose-300" : "text-stone-700 dark:text-stone-200";
const pct = (rate: string): string => `${(n(rate) * 100).toFixed(n(rate) * 100 % 1 ? 1 : 0)}%`;
const label = (outcome: string, book: { home: string; away: string }): string =>
  outcome === "HOME" && book.home ? book.home : outcome === "AWAY" && book.away ? book.away : humanize(outcome);

const BLOCKED: Record<string, string> = {
  UNCONFIRMED_BET: "Unconfirmed bet: resolve before hedging",
  NO_HEDGE: "No live price covers every outcome",
  NO_MARKET: "Market not on the board",
  NOTHING_TO_HEDGE: "Nothing to hedge",
  UNPRICEABLE: "Can't be priced",
};
const LEG_TONE: Record<LegStatus, "good" | "warning" | "critical" | "neutral"> = {
  FILLED: "good",
  PARTIAL: "warning",
  UNCONFIRMED: "warning",
  FAILED: "critical",
  ABORTED: "neutral",
  SKIPPED: "neutral",
};

function refusal(err: unknown): { title: string; message: string; reason: string | null } {
  if (err instanceof ApiError) {
    const reason = err.reason;
    return { title: reason ? LEG_REFUSALS[reason] ?? REFUSALS[reason] ?? humanize(reason) : "Refused", message: err.message, reason };
  }
  return { title: "No answer from the server", message: "Some legs may have gone through. Check the positions before trying again.", reason: null };
}

// ---------------------------------------------------------------- summary
const Summary = ({ status, receivedAt, totals }: { status: string; receivedAt: number | null; totals: NonNullable<ReturnType<typeof useLivePortfolio>["portfolio"]>["totals"] | undefined }) => {
  const age = useAge(receivedAt);
  const cells = [
    { k: "Open bets", v: totals ? String(totals.open_bets) : "—" },
    { k: "Staked", v: totals ? formatINR(n(totals.staked_inr)) : "—" },
    { k: "Cash-out now", v: totals?.cash_out != null ? signed(totals.cash_out) : "—", tone: totals?.cash_out != null ? pnlTone(totals.cash_out) : undefined, hint: "balanced hedges" },
    { k: "Worst case", v: totals?.worst_case != null ? signed(totals.worst_case) : "—", tone: totals?.worst_case != null ? pnlTone(totals.worst_case) : undefined },
    { k: "Best case", v: totals?.best_case != null ? signed(totals.best_case) : "—", tone: totals?.best_case != null ? pnlTone(totals.best_case) : undefined },
    { k: "Profitable hedges", v: totals ? String(totals.profitable_hedges) : "—", tone: totals?.profitable_hedges ? "text-emerald-700 dark:text-emerald-300" : undefined },
  ];
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 lg:grid-cols-6">
        {cells.map((c) => (
          <div key={c.k} className="min-w-0">
            <p className="text-[11px] text-stone-400">
              {c.k}
              {c.hint && <span className="ml-1 text-stone-300 dark:text-stone-600">· {c.hint}</span>}
            </p>
            <p className={cx("mt-1 truncate font-mono text-lg font-semibold tabular-nums", c.tone ?? "text-stone-900 dark:text-stone-50")}>{c.v}</p>
          </div>
        ))}
      </div>
      <p className="inline-flex items-center gap-2 text-[11px] text-stone-400" role="status">
        <LiveDot active={status === "open"} tone={status === "open" ? "good" : "warning"} />
        {status === "open" ? "Live · 5 updates a second" : status === "paused" ? "Paused while the tab is hidden · polling" : `Socket ${status} · polling every 3s`}
        {age !== null && <span className="font-mono tabular-nums">· {age}s ago</span>}
      </p>
    </div>
  );
};

// ---------------------------------------------------------------- one book
const OutcomeCell = ({ outcome, book }: { outcome: string; book: MarketBook }) => {
  const live = book.live[outcome];
  const profit = book.profits?.[outcome];
  return (
    <div className="min-w-0 rounded-2xl bg-stone-50/80 px-3 py-2.5 dark:bg-white/[0.03]">
      <p className="truncate text-[11px] text-stone-400">{label(outcome, book)}</p>
      <p className={cx("mt-0.5 font-mono text-sm font-semibold tabular-nums", pnlTone(profit))}>{profit != null ? signed(profit) : "—"}</p>
      <p className="mt-0.5 truncate font-mono text-[11px] tabular-nums text-stone-400" title={live ? `${live.bookmaker_id}: ${live.odds} raw, ${live.true_odds} after ${pct(live.commission)} commission` : undefined}>
        {live ? `${formatOdds(n(live.odds))} ${humanize(live.bookmaker_id)}${n(live.commission) > 0 ? ` · −${pct(live.commission)}` : ""}` : "no live price"}
      </p>
    </div>
  );
};

const PositionRow = ({ book, onHedge }: { book: MarketBook; onHedge: (book: MarketBook) => void }) => {
  const reduce = useReducedMotion();
  const pulse = book.profitable && !reduce;
  return (
    <motion.article
      layout
      variants={CARD_VARIANTS}
      className={cx(SURFACE, "relative flex min-w-0 flex-col gap-4 p-5 sm:p-6", book.profitable && "ring-1 ring-emerald-400/40 dark:ring-emerald-400/30")}
      animate={
        pulse
          ? { boxShadow: ["0 0 0 0 rgba(16,185,129,0)", "0 0 0 6px rgba(16,185,129,0.18)", "0 0 0 0 rgba(16,185,129,0)"] }
          : { boxShadow: "0 0 0 0 rgba(16,185,129,0)" }
      }
      transition={pulse ? { duration: 1.8, repeat: Infinity, ease: "easeInOut" } : SPRING}
      aria-label={`${book.home || book.fixture_id} v ${book.away}${book.profitable ? ", profitable hedge available" : ""}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-semibold text-stone-900 dark:text-stone-50">
            {book.home && book.away ? `${book.home} v ${book.away}` : book.fixture_id}
          </h3>
          <p className="mt-0.5 truncate text-xs text-stone-400">
            {book.market}
            {book.commence_time && ` · ${formatTime(book.commence_time)}`}
            {book.in_play && " · in play"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {book.profitable && (
            <Pill tone="good" icon="trending_up">
              Lock in {signed(book.cash_out)}
            </Pill>
          )}
          {book.blocked && <Pill tone={book.blocked === "UNCONFIRMED_BET" ? "warning" : "neutral"}>{BLOCKED[book.blocked] ?? humanize(book.blocked)}</Pill>}
          <Button size="sm" variant={book.profitable ? "primary" : "secondary"} icon="balance" disabled={!book.hedge} onClick={() => onHedge(book)}>
            Hedge
          </Button>
        </div>
      </header>

      <ul className="flex flex-wrap gap-2">
        {book.bets.map((bet) => (
          <li key={bet.id} className="inline-flex items-center gap-1.5 rounded-full bg-stone-100/80 px-3 py-1 text-xs text-stone-600 dark:bg-white/[0.05] dark:text-stone-300">
            <span className="font-medium text-stone-800 dark:text-stone-100">{label(bet.selection, book)}</span>
            <span className="font-mono tabular-nums">
              {formatINR(n(bet.stake_inr))} @ {formatOdds(n(bet.odds))}
            </span>
            <span className="text-stone-400">{humanize(bet.bookmaker_id)}</span>
            {bet.requested_stake_inr && <span className="text-amber-700 dark:text-amber-300" title={`asked ${formatINR(n(bet.requested_stake_inr))}`}>partial</span>}
            {bet.strategy && <span className="text-stone-400">· {bet.strategy}</span>}
            {bet.unconfirmed && <span className="text-amber-700 dark:text-amber-300">· unconfirmed</span>}
          </li>
        ))}
      </ul>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,9rem),1fr))] gap-2">
        {book.outcomes.map((o) => (
          <OutcomeCell key={o} outcome={o} book={book} />
        ))}
      </div>

      {book.notes.length > 0 && <p className="text-[11px] leading-relaxed text-stone-400">{book.notes.join(" · ")}</p>}
    </motion.article>
  );
};

// ---------------------------------------------------------------- hedge modal
const HedgeModal = ({ book, onClose }: { book: MarketBook; onClose: () => void }) => {
  const reduce = useReducedMotion();
  const [fraction, setFraction] = useState(1);
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<MultiLegReceipt | null>(null);
  const [error, setError] = useState<ReturnType<typeof refusal> | null>(null);
  const key = useRef(newIdempotencyKey());
  const close = useRef<HTMLButtonElement>(null);

  useEffect(() => close.current?.focus(), []);
  const hedge = book.hedge;
  const plan: HedgePlan | null = useMemo(() => (hedge ? interpolatePlan(hedge.free_bet, hedge.balanced, fraction) : null), [hedge, fraction]);

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && !busy) onClose();
  };

  const confirm = async () => {
    if (!plan || !hedge) return;
    setBusy(true);
    setError(null);
    try {
      const result = await apiClient.post<MultiLegReceipt>("/omni/portfolio/hedge", {
        idempotency_key: key.current,
        fixture_id: book.fixture_id,
        market: book.market,
        fraction: fraction.toFixed(4),
        anchor: hedge.anchor,
        expected_legs: plan.legs.map((leg) => ({ selection: leg.selection, bookmaker_id: leg.bookmaker_id, odds: leg.odds, stake_inr: n(leg.stake_inr).toFixed(2) })),
      });
      setReceipt(result);
      invalidate("cfo");
    } catch (err) {
      const why = refusal(err);
      setError(why);
      if (why.reason) key.current = newIdempotencyKey(); // refused: nothing fired, a retry is a new order
    } finally {
      setBusy(false);
    }
  };

  const at = fraction * 100;
  return createPortal(
    <motion.div
      className="fixed inset-0 z-[90] grid place-items-center bg-stone-900/25 p-4 backdrop-blur-sm dark:bg-black/45"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}
      onKeyDown={onKey}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={`Hedge ${book.home} v ${book.away}`}
        initial={reduce ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.98 }}
        animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
        exit={reduce ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.98 }}
        transition={SPRING}
        className="flex max-h-[min(44rem,calc(100vh-2rem))] w-full max-w-xl flex-col overflow-hidden rounded-3xl bg-[#FBFAF6]/95 shadow-soft-lg backdrop-blur-md dark:bg-stone-900/95 dark:ring-1 dark:ring-white/[0.06]"
      >
        <header className="flex items-start justify-between gap-4 px-7 pb-4 pt-7">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-stone-400">Advanced hedge</p>
            <h2 className="mt-1.5 truncate text-xl font-semibold tracking-tight text-stone-900 dark:text-stone-50">
              {book.home && book.away ? `${book.home} v ${book.away}` : book.fixture_id}
            </h2>
            <p className="mt-0.5 text-sm text-stone-500 dark:text-stone-400">
              Keeps the upside on <span className="font-medium text-stone-700 dark:text-stone-200">{hedge ? label(hedge.anchor, book) : "—"}</span>
            </p>
          </div>
          <button ref={close} type="button" onClick={onClose} disabled={busy} aria-label="Close hedge" className="grid size-9 shrink-0 place-items-center rounded-full text-stone-400 transition-colors hover:bg-stone-200/60 hover:text-stone-700 disabled:opacity-40 dark:hover:bg-white/10">
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-7 pb-6">
          {!hedge || !plan ? (
            <EmptyState icon="balance" title="No hedge right now" detail={book.blocked ? BLOCKED[book.blocked] ?? humanize(book.blocked) : undefined} />
          ) : receipt ? (
            <ReceiptView receipt={receipt} outcomes={book.outcomes} book={book} />
          ) : (
            <>
              <div className="flex items-baseline justify-between text-xs">
                <span className={cx("font-medium", fraction < 0.5 ? "text-stone-800 dark:text-stone-100" : "text-stone-400")}>Unbalanced (Free Bet)</span>
                <span className={cx("font-medium", fraction >= 0.5 ? "text-stone-800 dark:text-stone-100" : "text-stone-400")}>Balanced (Equal Profit)</span>
              </div>
              <div className="relative mt-2 h-10">
                <div className="absolute inset-x-0 top-1/2 -mt-1 h-2 rounded-full bg-stone-200/70 dark:bg-stone-800" />
                <motion.div className="absolute left-0 top-1/2 -mt-1 h-2 rounded-full bg-emerald-500/80" initial={false} animate={{ width: `${at}%` }} transition={SPRING} />
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={Math.round(at)}
                  onChange={(e) => setFraction(Number(e.target.value) / 100)}
                  disabled={busy}
                  aria-label="Hedge balance, from free bet to equal profit"
                  aria-valuetext={fraction === 0 ? "Free bet" : fraction === 1 ? "Balanced" : `${Math.round(at)}% of the balanced profit locked`}
                  className="peer absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0"
                />
                <motion.div
                  aria-hidden="true"
                  className="absolute top-1/2 -ml-3 -mt-3 grid size-6 place-items-center rounded-full bg-white shadow-md ring-1 ring-stone-900/5 peer-focus-visible:ring-2 peer-focus-visible:ring-emerald-500 dark:bg-stone-100"
                  initial={false}
                  animate={{ left: `${at}%` }}
                  transition={SPRING}
                >
                  <span className="size-2 rounded-full bg-emerald-500" />
                </motion.div>
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-stone-400">
                {fraction === 0
                  ? "Nothing lost if the bet fails; every rupee of profit stays on it."
                  : fraction === 1
                    ? "The same profit whatever happens."
                    : `Locks ${Math.round(at)}% of the balanced profit on the other outcomes.`}
              </p>

              <h3 className="mt-6 text-[11px] font-medium uppercase tracking-[0.12em] text-stone-400">Projected P&L</h3>
              <dl className="mt-2 grid grid-cols-[repeat(auto-fill,minmax(min(100%,9rem),1fr))] gap-2">
                {book.outcomes.map((o) => (
                  <div key={o} className="rounded-2xl bg-white px-3 py-2.5 shadow-soft dark:bg-stone-800/60 dark:shadow-none">
                    <dt className="truncate text-[11px] text-stone-400">{label(o, book)}</dt>
                    <dd className={cx("mt-0.5 font-mono text-[15px] font-semibold tabular-nums", pnlTone(plan.after[o]))}>{signed(plan.after[o])}</dd>
                    <dd className="font-mono text-[11px] tabular-nums text-stone-400">now {signed(book.profits?.[o])}</dd>
                  </div>
                ))}
              </dl>

              <h3 className="mt-6 text-[11px] font-medium uppercase tracking-[0.12em] text-stone-400">Legs · fired one at a time</h3>
              <ul className="mt-2 divide-y divide-stone-900/[0.05] dark:divide-white/[0.06]">
                {plan.legs.map((leg) => (
                  <li key={leg.selection} className="flex items-baseline justify-between gap-3 py-2.5 text-sm">
                    <span className="min-w-0 truncate text-stone-700 dark:text-stone-200">
                      Back <span className="font-medium">{label(leg.selection, book)}</span> @ <span className="font-mono">{formatOdds(n(leg.odds))}</span>
                      <span className="text-stone-400">
                        {" "}
                        · {humanize(leg.bookmaker_id)}
                        {n(leg.commission) > 0 ? ` · ${pct(leg.commission)} commission (true ${n(leg.true_odds).toFixed(3)})` : ""}
                        {leg.currency !== "INR" ? ` · ${n(leg.stake_ccy).toFixed(2)} ${leg.currency}` : ""}
                      </span>
                    </span>
                    <span className="shrink-0 font-mono font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatINR(n(leg.stake_inr))}</span>
                  </li>
                ))}
              </ul>

              {error && (
                <div className="mt-5 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" role="alert">
                  <p className="font-semibold">{error.title}</p>
                  <p className="mt-1 text-[13px] opacity-80">{error.reason === "HEDGE_CHANGED" ? "Prices moved since this was shown. The plan above is live again: check it and confirm." : error.message}</p>
                </div>
              )}
            </>
          )}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-stone-900/[0.05] px-7 pb-7 pt-5 dark:border-white/[0.06]">
          <p className="text-[12px] text-stone-500 dark:text-stone-400">
            {plan && !receipt ? (
              <>
                Hedge stake <span className="font-mono font-semibold text-stone-800 dark:text-stone-100">{formatINR(n(plan.hedge_stake_inr))}</span>
                {" · worst "}
                <span className={cx("font-mono font-semibold", pnlTone(plan.worst_after))}>{signed(plan.worst_after)}</span>
              </>
            ) : receipt ? (
              receipt.message
            ) : null}
          </p>
          {receipt ? (
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          ) : (
            <ConfirmButton variant="primary" icon="balance" busy={busy} disabled={!plan || plan.legs.length === 0} confirmLabel={`Fire ${plan?.legs.length ?? 0} leg${plan?.legs.length === 1 ? "" : "s"}?`} onConfirm={() => void confirm()}>
              Confirm hedge
            </ConfirmButton>
          )}
        </footer>
      </motion.div>
    </motion.div>,
    document.body,
  );
};

const ReceiptView = ({ receipt, outcomes, book }: { receipt: MultiLegReceipt; outcomes: string[]; book: { home: string; away: string } }) => (
  <div className="flex flex-col gap-4">
    <div
      className={cx(
        "rounded-2xl px-4 py-3 text-sm",
        receipt.status === "COMPLETE" ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-200" : receipt.status === "LEGGED" ? "bg-amber-50 text-amber-800 dark:bg-amber-400/10 dark:text-amber-200" : "bg-stone-100 text-stone-700 dark:bg-white/5 dark:text-stone-200",
      )}
      role="status"
    >
      <p className="font-semibold">
        {receipt.status === "COMPLETE" ? "Hedged" : receipt.status === "LEGGED" ? "Partly placed" : "Not placed"}
        {receipt.execution_mode === "paper" ? " (paper)" : ""}
      </p>
      <p className="mt-1 text-[13px] opacity-80">{receipt.message}</p>
    </div>
    <ul className="divide-y divide-stone-900/[0.05] dark:divide-white/[0.06]">
      {receipt.legs.map((leg) => (
        <li key={leg.selection} className="flex flex-wrap items-baseline justify-between gap-2 py-2.5 text-sm">
          <span className="min-w-0 text-stone-700 dark:text-stone-200">
            {label(leg.selection, book)} <span className="text-stone-400">@ {formatOdds(leg.matched_odds ?? leg.odds)} · {humanize(leg.bookmaker_id)}</span>
          </span>
          <span className="flex items-center gap-2">
            <span className="font-mono tabular-nums text-stone-900 dark:text-stone-50">
              {leg.filled_stake_inr !== null ? formatINR(leg.filled_stake_inr) : "—"}
              {leg.requested_stake_inr !== null && leg.filled_stake_inr !== null && leg.filled_stake_inr < leg.requested_stake_inr && (
                <span className="text-stone-400"> / {formatINR(leg.requested_stake_inr)}</span>
              )}
            </span>
            <Pill tone={LEG_TONE[leg.status]}>{humanize(leg.status)}</Pill>
          </span>
          {leg.reason && leg.status !== "FILLED" && <span className="w-full text-[11px] text-stone-400">{LEG_REFUSALS[leg.reason] ?? REFUSALS[leg.reason] ?? humanize(leg.reason)}</span>}
        </li>
      ))}
    </ul>
    <dl className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,9rem),1fr))] gap-2">
      {outcomes.map((o) => (
        <div key={o} className="rounded-2xl bg-white px-3 py-2.5 shadow-soft dark:bg-stone-800/60 dark:shadow-none">
          <dt className="truncate text-[11px] text-stone-400">{label(o, book)}</dt>
          <dd className={cx("mt-0.5 font-mono text-[15px] font-semibold tabular-nums", pnlTone(receipt.outcome_profits[o]))}>{signed(receipt.outcome_profits[o])}</dd>
        </div>
      ))}
    </dl>
  </div>
);

// ---------------------------------------------------------------- arbitrage scanner
const ArbCard = ({ arb, fresh }: { arb: ArbOpportunity; fresh: boolean }) => {
  const reduce = useReducedMotion();
  const [stake, setStake] = useState(arb.total_stake_inr);
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<MultiLegReceipt | null>(null);
  const [error, setError] = useState<ReturnType<typeof refusal> | null>(null);
  const amount = Number(stake);
  const valid = Number.isFinite(amount) && amount > 0 && /^\d+(\.\d{1,2})?$/.test(stake);
  const scale = valid ? amount / n(arb.total_stake_inr) : 0;
  const book = { home: arb.home, away: arb.away };

  const execute = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await apiClient.post<MultiLegReceipt>("/omni/arbitrage/execute", {
        idempotency_key: newIdempotencyKey(),
        fixture_id: arb.fixture_id,
        market: arb.market,
        total_stake_inr: amount.toFixed(2),
        legs: arb.legs.map((leg) => ({ selection: leg.selection, bookmaker_id: leg.bookmaker_id, odds: leg.odds })),
      });
      setReceipt(result);
      invalidate("cfo");
    } catch (err) {
      setError(refusal(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <motion.li
      layout
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={
        fresh && !reduce
          ? { opacity: 1, y: 0, backgroundColor: ["rgba(16,185,129,0.16)", "rgba(16,185,129,0)"] }
          : { opacity: 1, y: 0 }
      }
      exit={{ opacity: 0 }}
      transition={fresh ? { duration: 1.6 } : SPRING}
      className="flex min-w-0 flex-col gap-3 rounded-2xl bg-white/70 p-4 shadow-soft ring-1 ring-stone-900/[0.03] backdrop-blur-md dark:bg-stone-800/50 dark:shadow-none dark:ring-white/[0.05]"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-stone-900 dark:text-stone-50">{arb.home && arb.away ? `${arb.home} v ${arb.away}` : arb.fixture_id}</p>
          <p className="truncate text-[11px] text-stone-400">
            {arb.market}
            {arb.commence_time && ` · ${formatTime(arb.commence_time)}`} · Σ1/odds {n(arb.booksum).toFixed(4)}
          </p>
        </div>
        <span className="rounded-full bg-emerald-50 px-2.5 py-1 font-mono text-xs font-semibold text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300">+{n(arb.margin_pct).toFixed(2)}%</span>
      </div>
      <ul className="flex flex-col gap-1">
        {arb.legs.map((leg) => (
          <li key={leg.selection} className="flex items-baseline justify-between gap-3 text-[13px]">
            <span className="min-w-0 truncate text-stone-600 dark:text-stone-300">
              {label(leg.selection, book)} <span className="font-mono">{formatOdds(n(leg.odds))}</span>
              <span className="text-stone-400">
                {" "}
                · {humanize(leg.bookmaker_id)}
                {n(leg.commission) > 0 && ` · true ${n(leg.true_odds).toFixed(3)}`}
                {leg.currency !== "INR" && ` · ${leg.currency}`}
              </span>
            </span>
            <span className="shrink-0 font-mono tabular-nums text-stone-800 dark:text-stone-100">{formatINR(n(leg.stake_inr) * scale)}</span>
          </li>
        ))}
      </ul>
      {receipt ? (
        <ReceiptView receipt={receipt} outcomes={arb.legs.map((l) => l.selection)} book={book} />
      ) : (
        <div className="flex flex-wrap items-end justify-between gap-3">
          <label className="flex min-w-0 flex-col gap-1">
            <span className="text-[11px] text-stone-400">Total stake (₹)</span>
            <input
              inputMode="decimal"
              value={stake}
              onChange={(e) => setStake(e.target.value.replace(/[^\d.]/g, ""))}
              disabled={busy}
              aria-invalid={!valid}
              className="w-32 rounded-xl bg-stone-50 px-3 py-1.5 font-mono text-sm tabular-nums text-stone-900 outline-none ring-1 ring-stone-900/5 focus:ring-2 focus:ring-emerald-500 dark:bg-stone-900 dark:text-stone-50 dark:ring-white/10"
            />
          </label>
          <p className="text-[12px] text-stone-500 dark:text-stone-400">
            Guaranteed <span className="font-mono font-semibold text-emerald-700 dark:text-emerald-300">≈ {formatINR(n(arb.guaranteed_profit_inr) * scale)}</span>
          </p>
          <ConfirmButton size="sm" variant="primary" icon="bolt" busy={busy} disabled={!valid} confirmLabel={`Fire ${arb.legs.length} legs?`} onConfirm={() => void execute()}>
            Execute
          </ConfirmButton>
        </div>
      )}
      {error && (
        <p className="rounded-xl bg-rose-50 px-3 py-2 text-[12px] text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" role="alert">
          <span className="font-semibold">{error.title}.</span> {error.message}
        </p>
      )}
    </motion.li>
  );
};

const ArbScanner = ({ arbs }: { arbs: ArbOpportunity[] }) => {
  const seen = useRef<Set<string>>(new Set());
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  useEffect(() => {
    const appeared = arbs.map((a) => a.id).filter((id) => !seen.current.has(id));
    for (const id of appeared) seen.current.add(id);
    if (appeared.length === 0) return;
    setFresh(new Set(appeared));
    const timer = window.setTimeout(() => setFresh(new Set()), 1_800);
    return () => window.clearTimeout(timer);
  }, [arbs]);

  return (
    <Panel title="Arbitrage matrix scanner" icon="radar" className="lg:col-span-5" subtitle={`${arbs.length} live · after commission and FX`}>
      {arbs.length === 0 ? (
        <EmptyState icon="radar" title="No arbitrage right now" detail="Every pre-match market on the board is checked every second: Σ 1 / true odds < 1, with each exchange's commission taken out." />
      ) : (
        <ul className="flex flex-col gap-3">
          <AnimatePresence initial={false}>
            {arbs.map((arb) => (
              <ArbCard key={arb.id} arb={arb} fresh={fresh.has(arb.id)} />
            ))}
          </AnimatePresence>
        </ul>
      )}
    </Panel>
  );
};

// ---------------------------------------------------------------- the tab
export const ActivePortfolio = () => {
  const { status, portfolio, arbs, receivedAt, error, loading } = useLivePortfolio();
  const [hedging, setHedging] = useState<string | null>(null);
  const live = hedging ? portfolio?.markets.find((m) => m.market_key === hedging) : undefined;

  return (
    <>
      <Panel title="Active portfolio" icon="monitoring" className="lg:col-span-12" subtitle={portfolio ? `${portfolio.totals.markets} markets` : undefined}>
        {loading && !portfolio ? <Skeleton rows={2} /> : <Summary status={status} receivedAt={receivedAt} totals={portfolio?.totals} />}
        {portfolio && portfolio.fx_missing.length > 0 && (
          <p className="mt-4 text-[12px] text-amber-700 dark:text-amber-300">No live rate for {portfolio.fx_missing.join(", ")}: books in those currencies are left out until one is set.</p>
        )}
        {error && !portfolio && <p className="mt-4 text-[12px] text-rose-600 dark:text-rose-300">{error}</p>}
      </Panel>

      <motion.section variants={CARD_VARIANTS} className="flex min-w-0 flex-col gap-4 lg:col-span-7">
        <div className="flex min-h-[32px] items-center gap-2.5 px-1">
          <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">stacks</span>
          <h2 className="text-[15px] font-semibold text-stone-900 dark:text-stone-100">Live positions</h2>
          <span className="hidden truncate text-xs text-stone-400 sm:inline dark:text-stone-500">Green: a hedge locks in profit now</span>
        </div>
        {portfolio && portfolio.markets.length === 0 ? (
          <div className={cx(SURFACE, "p-6")}>
            <EmptyState icon="stacks" title="No open positions" detail="Open bets appear here, marked against every live book, with their hedges." />
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {(portfolio?.markets ?? []).map((book) => (
              <PositionRow key={book.market_key} book={book} onHedge={(b) => setHedging(b.market_key)} />
            ))}
          </div>
        )}
      </motion.section>

      <ArbScanner arbs={arbs} />

      <AnimatePresence>{live && <HedgeModal key={live.market_key} book={live} onClose={() => setHedging(null)} />}</AnimatePresence>
    </>
  );
};
