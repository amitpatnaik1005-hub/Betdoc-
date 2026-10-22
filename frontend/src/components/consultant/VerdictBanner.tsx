/**
 * The Consultant's final signed verdict (Group 105): APPROVED — GOLD GRADE, APPROVED WITH PRESCRIPTION or REJECTED —
 * CAPITAL PROTECTION, with the Slip Score, the prescription's score, and the HMAC-SHA256 seal the server recorded with
 * the report (re-checked on every read of the history). Developed for Amit Ashok Kumar Patnaik.
 */
import { motion } from "framer-motion";
import { TIER_STYLE } from "../../lib/backtest_and_parlay";
import { type ConsultantReport, VERDICT_STYLE } from "../../lib/consultant";
import { formatDateTime } from "../../lib/format";
import { toast } from "../../store/useToastStore";
import { Button } from "../../ui/kit";

export const VerdictBanner = ({ report, onRecord, onTicket }: { report: ConsultantReport; onRecord: () => void; onTicket?: () => void }) => {
  const style = VERDICT_STYLE[report.verdict.status];
  const rx = report.doctor.prescription;
  const tier = TIER_STYLE[report.rating.tier];
  const copySeal = () => {
    void navigator.clipboard?.writeText(report.seal).then(() => toast.success("Seal copied", report.seal.slice(0, 16) + "…"), () => undefined);
  };
  return (
    <motion.section initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} aria-label="Final signed verdict"
                    className="rounded-2xl border-2 p-5 lg:col-span-12" style={{ borderColor: style.color, background: `color-mix(in srgb, ${style.color} 7%, transparent)` }}>
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-stone-500 dark:text-stone-400">
            <span>{formatDateTime(report.signed_at)}</span>
            <span>· {report.signed_by}</span>
            <button type="button" onClick={copySeal} title={`${report.seal_algorithm} seal: ${report.seal}`}
                    className="rounded-md bg-stone-900/5 px-1.5 py-0.5 font-bold text-stone-600 hover:bg-stone-900/10 dark:bg-white/10 dark:text-stone-300">
              <span className="material-symbols-outlined mr-0.5 align-[-2px] text-[13px]" aria-hidden="true">verified_user</span>
              seal {report.seal.slice(0, 12)}…
            </button>
          </div>
          <h2 className="mt-1.5 flex items-center gap-2 text-lg font-black text-stone-900 dark:text-stone-50">
            <span className="material-symbols-outlined text-[22px]" style={{ color: style.color }} aria-hidden="true">{style.icon}</span>
            {report.verdict.title}
          </h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-stone-600 dark:text-stone-300">{report.verdict.summary}</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-3">
          <div className="rounded-xl border bg-white/80 px-3 py-2 text-center dark:bg-stone-900/80" style={{ borderColor: tier.color }}>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-stone-400">Slip Score</div>
            <div className="flex items-center gap-1 font-mono text-lg font-black">
              <span style={{ color: tier.color }}>{Math.round(report.rating.score)}</span>
              {rx?.available && rx.after && rx.after.score > report.rating.score && (
                <>
                  <span className="material-symbols-outlined text-[16px] text-emerald-500" aria-hidden="true">arrow_forward</span>
                  <span style={{ color: TIER_STYLE[rx.after.tier].color }} title="after the prescription">{Math.round(rx.after.score)}</span>
                </>
              )}
            </div>
            <div className="text-[10px] text-stone-500">{tier.label}</div>
          </div>
          {report.verdict.status === "APPROVED_GOLD" && onTicket && <Button variant="primary" icon="confirmation_number" onClick={onTicket}>Open the Bet-Slip ticket</Button>}
          <Button variant={report.verdict.status === "APPROVED_GOLD" ? "secondary" : "primary"} icon="receipt_long" onClick={onRecord}>Inspect and record in the workbench</Button>
        </div>
      </div>
    </motion.section>
  );
};
