/**
 * The Lawyer (Group 105, point b.6): the user is the client. The settlement rules this slip's legs fall under at its
 * book, the riskiest first. BetDoc holds no copy of any bookmaker's terms: each rule is the general settlement practice
 * (the ICC playing conditions for cricket's minimum overs), and a clause reference or maximum payout appears only when
 * the user recorded it from the book's own rules page. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import type { ConsultantReport, RiskLevel } from "../../lib/consultant";
import { Panel, Pill } from "../../ui/kit";

const RISK: Record<RiskLevel, "critical" | "warning" | "neutral"> = { HIGH: "critical", MEDIUM: "warning", LOW: "neutral" };

export const LawyerCard = ({ report }: { report: ConsultantReport }) => {
  const [open, setOpen] = useState<string | null>(null);
  const { clauses, bookmaker, references_on_file } = report.lawyer;
  const book = bookmaker ?? "the book";
  return (
    <Panel title="The Lawyer" icon="gavel" subtitle="to the client: you" className="lg:col-span-6"
           actions={<Pill tone="info" icon="menu_book">{references_on_file} reference{references_on_file === 1 ? "" : "s"} on file</Pill>}>
      <ul className="flex flex-col gap-2.5">
        {clauses.map((c) => {
          const expanded = open === c.code;
          return (
            <li key={c.code} className="rounded-xl border border-stone-200 bg-stone-50/60 dark:border-white/[0.08] dark:bg-white/[0.03]">
              <button type="button" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : c.code)}
                      className="flex w-full items-start justify-between gap-3 p-3 text-left">
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-bold text-stone-900 dark:text-stone-100">{c.title}</span>
                    <Pill tone={RISK[c.risk]}>{c.risk.toLowerCase()} risk</Pill>
                  </span>
                  <span className="mt-1 block text-xs leading-relaxed text-stone-600 dark:text-stone-300">{c.finding}</span>
                </span>
                <span className="material-symbols-outlined text-[18px] text-stone-400" aria-hidden="true">{expanded ? "expand_less" : "expand_more"}</span>
              </button>
              {expanded && (
                <div className="flex flex-col gap-2 border-t border-stone-200 p-3 text-xs leading-relaxed text-stone-600 dark:border-white/[0.08] dark:text-stone-300">
                  <p>{c.summary}</p>
                  <p><strong className="text-stone-800 dark:text-stone-100">Hedge:</strong> {c.hedge}</p>
                  <p className="text-[11px] text-stone-500 dark:text-stone-400">
                    Basis: {c.basis}. Clause: {c.reference ?? `not on file; confirm it in ${book}'s own rules before staking`}.
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
};
