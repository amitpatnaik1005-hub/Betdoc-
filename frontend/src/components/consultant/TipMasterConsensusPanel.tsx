/**
 * TipMaster consensus against BetDoc's mathematical verdict (Group 105, point b.13b: TipMaster merged into the
 * Consultant). Each leg's measured ticket and money shares (a named feed's or an administrator's splits, else BetDoc's
 * own recorded bets) against Ashoka's fair probability and the EV at the book, classified by KARNA's consensus rules:
 * public trap, contrarian alpha, sharp consensus, or neutral. A leg with no measured share says so; nothing is estimated.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { type ConsultantReport, DIVERGENCE_STYLE } from "../../lib/consultant";
import { Panel, Pill } from "../../ui/kit";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);

const Bar = ({ value, color, label }: { value: number | null; color: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span className="w-28 shrink-0 text-[11px] text-stone-500 dark:text-stone-400">{label}</span>
    <div className="h-2 flex-1 overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800" role="meter" aria-label={label} aria-valuenow={value === null ? undefined : Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      {value !== null && <div className="h-full rounded-full" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, background: color }} />}
    </div>
    <span className="w-14 shrink-0 text-right font-mono text-[11px]">{value === null ? "—" : pct(value, 0)}</span>
  </div>
);

export const TipMasterConsensusPanel = ({ report }: { report: ConsultantReport }) => {
  const tip = report.tipmaster;
  const style = DIVERGENCE_STYLE[tip.verdict];
  return (
    <Panel title="TipMaster vs BetDoc" icon="groups" subtitle="the crowd against the maths" className="lg:col-span-6"
           actions={<Pill tone={style.tone} icon={style.icon}>{tip.label}</Pill>}>
      <div className="flex flex-col gap-3">
        <p className="rounded-xl bg-stone-50 p-3 text-xs leading-relaxed text-stone-700 dark:bg-white/[0.03] dark:text-stone-300">{tip.explanation}</p>
        <ul className="flex flex-col gap-3">
          {tip.legs.map((leg) => {
            const s = DIVERGENCE_STYLE[leg.verdict];
            return (
              <li key={leg.leg_id} className="flex flex-col gap-1.5 rounded-xl border border-stone-200 p-3 dark:border-white/[0.08]">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-xs font-semibold">{leg.label}</span>
                  <span className="flex items-center gap-2">
                    {leg.ev_pct !== null && <span className={`font-mono text-[11px] ${leg.ev_pct >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>EV {leg.ev_pct >= 0 ? "+" : ""}{leg.ev_pct.toFixed(1)}%</span>}
                    <Pill tone={s.tone} icon={s.icon}>{leg.verdict.toLowerCase().replace("_", " ")}</Pill>
                  </span>
                </div>
                <Bar label="Public tickets" value={leg.tickets_pct === null ? null : leg.tickets_pct / 100} color="#3B82F6" />
                <Bar label="Money" value={leg.money_pct === null ? null : leg.money_pct / 100} color="#8B5CF6" />
                <Bar label="BetDoc fair chance" value={leg.fair_probability} color="#10B981" />
                <Bar label="Price implies" value={leg.implied_probability} color="#A8A29E" />
                <span className="text-[11px] text-stone-500 dark:text-stone-400">
                  {leg.source ? `Shares: ${leg.source}` : "No public share measured for this selection"}
                  {leg.rlm ? " · reverse line movement" : ""}{leg.edge_score !== null && leg.tickets_pct !== null ? ` · KARNA edge score ${leg.edge_score.toFixed(0)}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </Panel>
  );
};
