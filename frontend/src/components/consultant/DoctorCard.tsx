/**
 * The Doctor (Group 105, point b.6): the slip is the patient. Every pathology comes from the 15-pillar fortress or a
 * measurement (the leg's EV at the book, the book's margin, a recorded payout cap); pillars without evidence are listed
 * as not examined, never as healthy. The prescription is the auto-fix's best quoted alternative, offered only when the
 * fortress scored it higher. Developed for Amit Ashok Kumar Patnaik.
 */
import { TIER_STYLE } from "../../lib/backtest_and_parlay";
import type { ConsultantReport, Severity } from "../../lib/consultant";
import { Button, Panel, Pill } from "../../ui/kit";

const SEVERITY: Record<Severity, { tone: "critical" | "warning" | "neutral"; icon: string; ring: string }> = {
  CRITICAL: { tone: "critical", icon: "emergency", ring: "border-rose-200 bg-rose-50/50 dark:border-rose-900/50 dark:bg-rose-950/20" },
  WARNING: { tone: "warning", icon: "thermostat", ring: "border-amber-200 bg-amber-50/50 dark:border-amber-900/50 dark:bg-amber-950/20" },
  MILD: { tone: "neutral", icon: "healing", ring: "border-stone-200 bg-stone-50/60 dark:border-white/[0.08] dark:bg-white/[0.03]" },
};

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);

export const DoctorCard = ({ report, onApply, applying }: { report: ConsultantReport; onApply: () => void; applying: boolean }) => {
  const { pathologies, unexamined, prescription, clean_bill } = report.doctor;
  const critical = pathologies.filter((p) => p.severity === "CRITICAL").length;
  const warning = pathologies.filter((p) => p.severity === "WARNING").length;
  const rx = prescription?.available ? prescription : null;
  return (
    <Panel title="The Doctor" icon="medical_services" subtitle="to the patient: your slip" className="lg:col-span-6"
           actions={pathologies.length === 0 ? <Pill tone="good" icon="check_circle">No pathology found</Pill>
             : <span className="flex gap-1.5">{critical > 0 && <Pill tone="critical">{critical} critical</Pill>}{warning > 0 && <Pill tone="warning">{warning} warning</Pill>}</span>}>
      <div className="flex flex-col gap-3">
        {clean_bill && (
          <p className="rounded-xl border border-dashed border-emerald-300 bg-emerald-50/50 p-4 text-xs text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-300">
            Clean bill of health: every pillar was examined and none failed, no leg is priced under its fair value, and no margin is heavy.
          </p>
        )}
        {pathologies.map((p, i) => {
          const s = SEVERITY[p.severity];
          const treated = rx?.treats.includes(p.code);
          return (
            <article key={`${p.code}-${p.pillar ?? ""}-${i}`} className={`rounded-xl border p-3 ${s.ring}`}>
              <div className="flex items-start gap-2.5">
                <span className="material-symbols-outlined mt-0.5 text-[18px] text-stone-500 dark:text-stone-400" aria-hidden="true">{s.icon}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-xs font-bold uppercase tracking-wide text-stone-900 dark:text-stone-100">{p.title}</h3>
                    <Pill tone={s.tone}>{p.severity.toLowerCase()}</Pill>
                    {p.pillar !== null && <span className="text-[11px] text-stone-400">pillar {p.pillar}</span>}
                    {treated && <Pill tone="accent" icon="medication">the prescription treats this</Pill>}
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-stone-600 dark:text-stone-300">{p.diagnosis}</p>
                </div>
              </div>
            </article>
          );
        })}
        {rx && rx.replacement && rx.after && (
          <section aria-label="The Doctor's prescription" className="rounded-xl border border-[#C89B3C]/40 bg-[#C89B3C]/[0.06] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-[#9A7424] dark:text-[#E3BE63]">
                <span className="material-symbols-outlined text-[15px]" aria-hidden="true">medication</span> Prescription
              </span>
              <span className="font-mono text-xs">
                Slip Score {Math.round(rx.before.score)} <span style={{ color: TIER_STYLE[rx.before.tier].color }}>{rx.before.tier}</span> → {Math.round(rx.after.score)}{" "}
                <span style={{ color: TIER_STYLE[rx.after.tier].color }}>{rx.after.tier}</span>
              </span>
            </div>
            <p className="mt-2 text-xs text-stone-700 dark:text-stone-200">
              Swap <span className="line-through decoration-rose-400">{rx.replaced.label}</span> ({rx.replaced.reason}) for{" "}
              <strong>{rx.replacement.label}</strong>, {rx.replacement.fixture}: fair {pct(rx.replacement.fair_probability)}, EV {rx.replacement.ev === null ? "—" : `${rx.replacement.ev >= 0 ? "+" : ""}${pct(rx.replacement.ev)}`}.
            </p>
            <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">
              The best of {rx.tried.length} quoted alternative{rx.tried.length === 1 ? "" : "s"} on other fixtures, each run through the fortress.
              {rx.clean ? " The cured slip fails no pillar and keeps a positive joint EV." : " The cured slip still fails a pillar, so it would not be approved yet."}
            </p>
            <Button variant="primary" size="sm" icon="vaccines" className="mt-2.5" busy={applying} onClick={onApply}>Apply the 1-click prescription</Button>
          </section>
        )}
        {prescription && !prescription.available && (
          <p className="rounded-xl bg-stone-50 p-3 text-xs text-stone-500 dark:bg-white/[0.03] dark:text-stone-400">
            No prescription: {prescription.message} The leg to replace would be {prescription.replaced.label} ({prescription.replaced.reason}).
          </p>
        )}
        {unexamined.length > 0 && (
          <details className="rounded-xl bg-stone-50 p-3 text-xs dark:bg-white/[0.03]">
            <summary className="cursor-pointer font-semibold text-stone-600 dark:text-stone-300">Not examined: {unexamined.length} pillar{unexamined.length === 1 ? "" : "s"} without evidence</summary>
            <ul className="mt-2 flex flex-col gap-1 text-stone-500 dark:text-stone-400">
              {unexamined.map((u) => <li key={u.number}><strong>{u.number}. {u.title}</strong>: {u.reason}</li>)}
            </ul>
          </details>
        )}
      </div>
    </Panel>
  );
};
