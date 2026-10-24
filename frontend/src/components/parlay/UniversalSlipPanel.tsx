/**
 * The universal betslip (Group 130): the manual bet builder's slip, filled at 1xBet or at Parimatch, with the combined
 * odds, the return and the profit at each for the stake typed (recomputed on every keystroke, and on every board refresh
 * through the slip's price sync). 1xBet's prices are the feeds' quotes; the feeds do not carry Parimatch, so its figures
 * are estimates by the operator's rule (lib/universal_slip.ts) and are marked "est." wherever they appear, including in
 * the copied ticket. Only the manual bet builder uses this; the slip scanner reads the bookmaker's own slip.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { bookPrice, type DeskBook, SOURCE_NOTE, universalSlip } from "../../lib/universal_slip";
import type { SlipLeg } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";

const LABEL: Record<DeskBook, string> = { "1xbet": "1xBet", parimatch: "Parimatch" };
const COLOR: Record<DeskBook, string> = { "1xbet": "#38BDF8", parimatch: "#FACC15" };
const inr = (x: number) => `₹${x.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const UniversalSlipPanel = ({ legs }: { legs: SlipLeg[] }) => {
  const [stake, setStake] = useState("500");
  const amount = Number(stake);
  const valid = Number.isFinite(amount) && amount > 0;
  const slip = universalSlip(legs, valid ? amount : 0);

  const copy = async () => {
    const lines = [
      "BETDOC UNIVERSAL SLIP (fill at 1xBet or Parimatch)",
      `Stake ${valid ? inr(amount) : "—"}`,
      ...(["1xbet", "parimatch"] as DeskBook[]).map((b) => {
        const t = slip.books[b];
        const est = t.estimatedLegs ? ` (estimated: ${t.estimatedLegs} of ${legs.length} legs not quoted there)` : "";
        return `${LABEL[b]}: ${t.odds ? `${t.odds.toFixed(2)}x, returns ${t.payout !== null ? inr(t.payout) : "—"}` : "not priced"}${est}`;
      }),
      ...legs.map((l, i) => {
        const x = bookPrice(l, "1xbet");
        const p = bookPrice(l, "parimatch");
        return `${i + 1}. ${l.fixture} | ${l.label} | 1xBet ${x ? `${x.source === "quoted" ? "" : "≈"}${x.price.toFixed(2)}` : "—"} | Parimatch ${p ? `${p.source === "quoted" ? "" : "≈"}${p.price.toFixed(2)}` : "—"}`;
      }),
      "≈ = estimate, not the book's quote: check the price at the book before you stake.",
    ].join("\n");
    try {
      await navigator.clipboard.writeText(lines);
      toast.success("Slip copied", "Fill it at 1xBet or Parimatch; check each ≈ price there");
    } catch {
      toast.error("Not copied", "The browser refused the clipboard");
    }
  };

  if (!legs.length) return null;
  return (
    <section aria-label="Universal betslip" className="col-span-full flex flex-col gap-2 rounded-xl p-3"
             style={{ background: "color-mix(in srgb, var(--wb-primary) 8%, var(--wb-card))", border: "1px solid color-mix(in srgb, var(--wb-primary) 25%, transparent)" }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider" style={{ color: "var(--wb-primary)" }}>
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">balance</span>Universal slip: 1xBet and Parimatch
        </span>
        <label className="flex items-center gap-1.5 text-xs">
          Stake ₹
          <input inputMode="decimal" value={stake} onChange={(e) => setStake(e.target.value)} aria-label="Stake to plan" aria-invalid={!valid}
                 className="w-24 rounded-lg bg-transparent px-2 py-1 font-mono" style={{ border: "1px solid color-mix(in srgb, var(--wb-primary) 40%, transparent)", color: "inherit" }} />
        </label>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {(["1xbet", "parimatch"] as DeskBook[]).map((b) => {
          const t = slip.books[b];
          const est = t.estimatedLegs > 0;
          return (
            <div key={b} className="flex items-center justify-between gap-2 rounded-lg p-2 text-xs" style={{ background: "rgba(0,0,0,0.18)" }}>
              <div className="min-w-0">
                <div className="font-semibold" style={{ color: COLOR[b] }}>
                  {LABEL[b]} {est && <span className="ml-1 rounded px-1 text-[9px] font-bold uppercase" style={{ background: "rgba(250,204,21,0.15)", color: "#FACC15" }}>est.</span>}
                </div>
                <div className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
                  {t.payout !== null && t.profit !== null ? <>returns <strong>{inr(t.payout)}</strong> · profit {t.profit >= 0 ? "+" : "−"}{inr(Math.abs(t.profit))}</> : t.odds === null ? "a leg is not priced here" : "type a stake"}
                </div>
                {est && <div className="text-[10px]" style={{ color: "var(--wb-muted)" }}>{t.estimatedLegs} of {legs.length} legs estimated: check at {LABEL[b]}</div>}
              </div>
              <span className="font-mono text-sm font-bold" style={{ color: COLOR[b] }}>{t.odds !== null ? `${est ? "≈" : ""}${t.odds.toFixed(2)}x` : "—"}</span>
            </div>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]" style={{ color: "var(--wb-muted)" }}>
        <span>
          {slip.gapPct !== null ? <>Gap between the books: <strong>{slip.gapPct.toFixed(1)}%</strong> of the 1xBet return. </> : null}
          {legs.some((l) => bookPrice(l, "parimatch")?.source !== "quoted") && <>Parimatch figures are {SOURCE_NOTE.from_1xbet}; the feeds do not carry Parimatch.</>}
        </span>
        <button type="button" onClick={() => void copy()} className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 font-medium"
                style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          <span className="material-symbols-outlined text-[13px]" aria-hidden="true">content_copy</span>Copy slip
        </button>
      </div>
    </section>
  );
};
