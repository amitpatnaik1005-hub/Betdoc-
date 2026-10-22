/**
 * The Consultant (Group 105, point b.6): the bankroll is the company. KUMBHA's fractional-Kelly stake against the CFO
 * account, the payout and its Section 115BBJ / 194BA tax, the in-play stop-loss floor, the slip's simulated downside,
 * and USD only at TODAR MAL's fresh rate. The what-if reruns the same formulas at another stake; it is not part of the
 * signed verdict. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { type ConsultantReport, whatIf } from "../../lib/consultant";
import { formatINR } from "../../lib/format";
import { Field, NumberInput, Panel, Pill } from "../../ui/kit";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);
const usd = (v: number | null) => (v === null ? "no fresh USD rate" : `$${v.toFixed(2)}`);

const Figure = ({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) => (
  <div className="rounded-xl border border-stone-200 bg-stone-50/60 p-3 dark:border-white/[0.08] dark:bg-white/[0.03]">
    <div className="text-[11px] font-semibold text-stone-500 dark:text-stone-400">{label}</div>
    <div className={`mt-1 font-mono text-lg font-bold ${tone ?? "text-stone-900 dark:text-stone-100"}`}>{value}</div>
    {hint && <div className="mt-0.5 text-[11px] text-stone-500 dark:text-stone-400">{hint}</div>}
  </div>
);

export const CapitalAdvisorCard = ({ report }: { report: ConsultantReport }) => {
  const c = report.consultant;
  const kelly = Number(c.stake_inr);
  const [stake, setStake] = useState<string>("");
  const what = stake !== "" && Number(stake) >= 0 ? whatIf(c, Number(stake)) : null;
  const sized = kelly > 0;
  return (
    <Panel title="The Consultant" icon="payments" subtitle="to the company: your bankroll" className="lg:col-span-6"
           actions={c.bankroll_inr ? <Pill tone="neutral" icon="account_balance">Bankroll {formatINR(Number(c.bankroll_inr))}</Pill> : <Pill tone="warning">No bankroll</Pill>}>
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
          <Figure label={`KUMBHA's stake (${c.kelly_multiplier === 0.25 ? "quarter" : `${c.kelly_multiplier}×`} Kelly)`} value={sized ? formatINR(kelly) : "₹0"}
                  hint={sized ? `${pct(c.stake_pct_of_bankroll, 2)} of bankroll · ${c.stake_usd ? `$${c.stake_usd}` : "no fresh USD rate"}` : `no stake: ${c.sizing_reason ?? "sizing produced none"}`} />
          <Figure label="Stop-loss cashout floor" value={sized ? formatINR(Number(c.stop_loss.floor_inr)) : "—"} tone="text-rose-600 dark:text-rose-400"
                  hint={`cash out once the live value falls ${pct(c.stop_loss.pct, 0)} below the stake`} />
          <Figure label="Payout · after 30% TDS" value={sized && c.payout_inr ? formatINR(Number(c.payout_inr)) : "—"} tone="text-emerald-600 dark:text-emerald-400"
                  hint={sized && c.tax ? `TDS ${formatINR(c.tax.tds)} on ${formatINR(c.tax.net_winnings)} net · take home ${formatINR(c.tax.net_post_tax_payout)}` : "nothing staked, nothing paid"} />
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
          <div><dt className="text-stone-500 dark:text-stone-400">Full Kelly</dt><dd className="font-mono">{pct(c.full_kelly, 2)}</dd></div>
          <div><dt className="text-stone-500 dark:text-stone-400">Regime</dt><dd className="font-mono">{c.regime?.toLowerCase().replace("_", " ") ?? "—"} · drawdown {pct(c.drawdown)}</dd></div>
          <div><dt className="text-stone-500 dark:text-stone-400">Lose it all</dt><dd className="font-mono">{pct(c.downside.p_total_loss)}</dd></div>
          <div><dt className="text-stone-500 dark:text-stone-400">Expected profit</dt><dd className="font-mono">{c.expected_profit_inr ? formatINR(Number(c.expected_profit_inr)) : "—"}</dd></div>
        </dl>
        {c.max_payout_inr ? (
          <p className={`text-xs ${c.payout_over_cap ? "text-rose-600 dark:text-rose-400" : "text-stone-500 dark:text-stone-400"}`}>
            Maximum payout you recorded for {report.lawyer.bookmaker}: {formatINR(Number(c.max_payout_inr))}{c.payout_over_cap ? " (this slip pays more: the excess is not paid)" : ""}.
          </p>
        ) : null}
        <div className="rounded-xl border border-stone-200 p-3 dark:border-white/[0.08]">
          <Field label="What-if stake (₹), not part of the signed verdict" hint={`KUMBHA's ceiling is ${pct(c.what_if.max_fraction, 1)} of bankroll per slip.`}>
            <NumberInput min={0} value={stake} placeholder={sized ? String(kelly) : "e.g. 500"} onChange={(e) => setStake(e.target.value)} aria-label="What-if stake in rupees" />
          </Field>
          {what && (
            <p className="mt-2 font-mono text-xs text-stone-600 dark:text-stone-300">
              pays {what.payout === null ? "—" : formatINR(what.payout)} · TDS {what.tds === null ? "—" : formatINR(what.tds)} · take home {what.takeHome === null ? "—" : formatINR(what.takeHome)}
              {" "}· floor {formatINR(what.floor)} · {usd(what.usd)} · {pct(what.pctOfBankroll, 2)} of bankroll{what.overCap ? " · over the recorded payout cap" : ""}
            </p>
          )}
        </div>
        <p className="text-[11px] text-stone-500 dark:text-stone-400">{c.tax?.basis ?? "Section 115BBJ / 194BA"}. {c.stop_loss.basis}.</p>
      </div>
    </Panel>
  );
};
