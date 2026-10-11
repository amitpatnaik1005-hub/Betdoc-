/**
 * Tax Estimation Drawer: Indian Section 115BBJ 30% TDS Net Yield Calculator (TODAR MAL, Group 80).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * The figures come from the backend's estimator (the same Section 115BBJ arithmetic the tax ledger books): 30% on the
 * net winnings of the one bet, max(0, payout - stake), with the 4% cess on the tax shown beside it.
 */
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useState } from "react";
import { estimateTax, type TaxEstimate } from "../../lib/paper_trading";
import { Button, ErrorState, KeyValues, Skeleton } from "../../ui/kit";
import { formatINR } from "../../utils/formatCurrency";
import { explainPaperError } from "../../store/usePaperTradingStore";

interface TaxEstimationDrawerProps {
  grossWin: number;
  stake: number;
  isOpen: boolean;
  onClose: () => void;
  title?: string;
}

export const TaxEstimationDrawer = ({ grossWin, stake, isOpen, onClose, title }: TaxEstimationDrawerProps) => {
  const reduce = useReducedMotion();
  const [attempt, setAttempt] = useState(0);
  const key = `${grossWin}|${stake}|${attempt}`;
  // Each answer is tagged with the request it answers: a stale one is never shown, and nothing is reset synchronously.
  const [answer, setAnswer] = useState<{ key: string; estimate: TaxEstimate | null; error: string | null } | null>(null);
  const estimate = answer?.key === key ? answer.estimate : null;
  const error = answer?.key === key ? answer.error : null;

  useEffect(() => {
    if (!isOpen) return;
    let live = true;
    estimateTax(grossWin, stake)
      .then((e) => live && setAnswer({ key, estimate: e, error: null }))
      .catch((err: unknown) => live && setAnswer({ key, estimate: null, error: explainPaperError(err) }));
    return () => {
      live = false;
    };
  }, [isOpen, grossWin, stake, key]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div className="fixed inset-0 z-50 flex justify-end bg-stone-950/40 backdrop-blur-sm" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
          <motion.aside
            role="dialog"
            aria-modal="true"
            aria-label="Section 115BBJ tax estimator"
            initial={reduce ? false : { x: 48, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={reduce ? undefined : { x: 48, opacity: 0 }}
            transition={{ type: "spring", stiffness: 260, damping: 30 }}
            onClick={(e) => e.stopPropagation()}
            className="flex h-full w-full max-w-md flex-col gap-5 overflow-y-auto bg-[#F8F6F0] p-6 shadow-soft-lg dark:bg-stone-950"
          >
            <header className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[#C89B3C] dark:text-[#E3BE63]">TODAR MAL · Tax ledger</p>
                <h2 className="mt-1 font-display text-lg font-bold text-stone-900 dark:text-stone-100">{title ?? "Section 115BBJ Tax Estimator"}</h2>
              </div>
              <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onClose} />
            </header>
            <p className="text-xs leading-relaxed text-stone-500 dark:text-stone-400">
              Under Indian tax law (Section 115BBJ), a flat 30% applies to the net winnings of each bet, with no deduction or set-off of losses;
              Section 194BA withholds it on withdrawal and at the financial year&apos;s end.
            </p>
            {error ? (
              <ErrorState message={error} onRetry={() => setAttempt((n) => n + 1)} />
            ) : !estimate ? (
              <Skeleton rows={5} />
            ) : (
              <div className="rounded-3xl bg-white p-5 shadow-soft dark:bg-stone-900 dark:shadow-none">
                <KeyValues
                  items={[
                    { label: "Gross payout", value: formatINR(estimate.gross_win) },
                    { label: "Stake", value: formatINR(estimate.stake) },
                    { label: "Net winnings", value: <span className="text-emerald-700 dark:text-emerald-300">{formatINR(estimate.gross_profit)}</span> },
                    { label: `${estimate.tds_rate_pct}% TDS`, value: <span className="text-rose-600 dark:text-rose-300">-{formatINR(estimate.tds_deducted)}</span> },
                    { label: "4% cess on the tax (on assessment)", value: formatINR(estimate.cess) },
                    { label: "Net take-home payout", value: <span className="font-semibold text-stone-900 dark:text-stone-50">{formatINR(estimate.net_take_home)}</span> },
                    { label: "Effective rate on winnings", value: `${estimate.effective_rate_pct.toFixed(1)}%` },
                    { label: "Jurisdiction", value: estimate.jurisdiction },
                  ]}
                />
              </div>
            )}
            <p className="mt-auto text-[11px] italic text-stone-400 dark:text-stone-500">
              Developer Credit: Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
            </p>
            <Button onClick={onClose}>Close estimator</Button>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>
  );
};
