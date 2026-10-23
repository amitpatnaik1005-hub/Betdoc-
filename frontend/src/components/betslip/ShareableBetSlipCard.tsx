/**
 * The shareable bet card (Group 116): a recorded ticket as a white, high-contrast card to copy as text or print / save as
 * PDF. Every figure is the server's (the gross payout if every leg wins, Section 115BBJ's 30% TDS on the net winnings,
 * the take-home); nothing is computed here. Opened from a system ticket. Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useId, useRef } from "react";
import { inr } from "../../lib/slip_ornament";
import { toast } from "../../store/useToastStore";
import { BetdocLogo } from "../../ui/brand";

export interface ShareLeg {
  sport: string;
  event: string;
  market: string;
  selection: string;
  odds: string;
  code?: string;
}

export interface ShareableBetSlipCardProps {
  reference: string;
  book: string;
  status: string;
  legs: ShareLeg[];
  totalOdds: string;
  stakeInr: string;
  grossPayoutInr: string;
  netWinningsInr: string;
  tdsInr: string;
  takeHomeInr: string;
  onClose: () => void;
}

export const ShareableBetSlipCard = ({ reference, book, status, legs, totalOdds, stakeInr, grossPayoutInr, netWinningsInr, tdsInr, takeHomeInr, onClose }:
                                       ShareableBetSlipCardProps) => {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const placed = status !== "PENDING_PLACEMENT";
  useEffect(() => {
    box.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copy = async () => {
    const text = [
      `BETDOC SLIP · ${reference} · ${book}`,
      `${legs.length}-leg accumulator @ ${Number(totalOdds).toFixed(2)} · ${placed ? "stake" : "planned stake"} ${inr(stakeInr)}`,
      ...legs.map((leg, i) => `${i + 1}. [${leg.sport}] ${leg.event}: ${leg.selection}${leg.code ? ` [${leg.code}]` : ""} @ ${leg.odds}`),
      `If every leg wins: ${inr(grossPayoutInr)} gross · TDS 30% of ${inr(netWinningsInr)} = ${inr(tdsInr)} · take-home ${inr(takeHomeInr)}`,
      "Placed by hand at the book. Developed for Amit Ashok Kumar Patnaik",
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Slip copied", `${reference} · ${legs.length} legs`);
    } catch {
      toast.error("Not copied", "The browser refused the clipboard: select the card's text instead.");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-stone-950/60 p-4 backdrop-blur-sm print:bg-white print:p-0" role="presentation"
         onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} data-print-root=""
           className="relative my-8 w-full max-w-lg overflow-hidden rounded-xl border-2 border-[#C89B3C] bg-white font-sans text-stone-900 shadow-2xl print:my-0 print:shadow-none">
        <header className="flex items-center justify-between gap-3 border-b-2 border-[#C89B3C] bg-[#1C1917] p-5 text-white">
          <div className="flex min-w-0 items-center gap-3">
            <BetdocLogo variant="mark" />
            <div className="min-w-0">
              <h2 id={titleId} className="flex items-center gap-2 text-base font-bold uppercase tracking-wider">
                BetDoc slip
                <span className="rounded bg-[#C89B3C] px-1.5 py-0.5 text-[10px] font-extrabold text-black">{placed ? status.replace(/_/g, " ") : "TO PLACE"}</span>
              </h2>
              <p className="truncate font-mono text-[11px] text-stone-400">{reference} · {book}</p>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded p-1 text-stone-400 transition hover:text-white print:hidden">
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">close</span>
          </button>
        </header>

        <ol className="max-h-80 space-y-2 overflow-y-auto border-b border-stone-200 bg-stone-50 p-5 print:max-h-none" aria-label="Legs">
          {legs.map((leg, i) => (
            <li key={`${leg.event}-${i}`} className="flex items-center justify-between gap-3 rounded border border-stone-200 bg-white p-2.5">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-xs">
                  <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-stone-700">{leg.sport}</span>
                  <span className="truncate font-semibold">{leg.event}</span>
                </p>
                <p className="truncate text-[11px] text-stone-600"><strong className="text-stone-800">{leg.selection}</strong>{leg.code ? ` [${leg.code}]` : ""} · {leg.market}</p>
              </div>
              <span className="shrink-0 font-mono text-xs font-bold text-[#8A6A1F]">{leg.odds}</span>
            </li>
          ))}
        </ol>

        <div className="space-y-3 p-5">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="rounded border border-stone-200 bg-stone-100 p-2.5"><span className="block text-[10px] font-bold uppercase text-stone-500">Total odds</span>
              <span className="font-mono text-base font-bold">{Number(totalOdds).toFixed(2)}x</span></div>
            <div className="rounded border border-stone-200 bg-stone-100 p-2.5"><span className="block text-[10px] font-bold uppercase text-stone-500">{placed ? "Stake placed" : "Planned stake"}</span>
              <span className="font-mono text-base font-bold">{inr(stakeInr)}</span></div>
          </div>
          <div className="space-y-1 rounded border border-emerald-200 bg-emerald-50 p-3 text-xs">
            <p className="flex justify-between"><span>Gross payout if every leg wins</span><strong className="font-mono">{inr(grossPayoutInr)}</strong></p>
            <p className="flex justify-between text-[11px] text-stone-600"><span>30% TDS on {inr(netWinningsInr)} net winnings</span><span className="font-mono">−{inr(tdsInr)}</span></p>
            <p className="flex justify-between border-t border-emerald-200 pt-1 text-sm font-bold text-emerald-900"><span>Take-home</span><span className="font-mono">{inr(takeHomeInr)}</span></p>
          </div>
          <p className="border-t border-stone-100 pt-2 text-center text-[10px] text-stone-500">
            Placed by hand at the book; no result is promised.<br /><strong className="text-stone-700">Developed for Amit Ashok Kumar Patnaik</strong>
          </p>
        </div>

        <footer className="flex gap-3 border-t border-stone-200 bg-stone-100 p-4 print:hidden">
          <button type="button" onClick={() => void copy()} className="flex-1 rounded bg-stone-800 px-3 py-2 text-xs font-semibold text-white transition hover:bg-stone-900">Copy slip text</button>
          <button type="button" onClick={() => window.print()} className="flex-1 rounded bg-[#A47C26] px-3 py-2 text-xs font-semibold text-white transition hover:bg-[#8A6A1F]">Print / save PDF</button>
        </footer>
      </div>
    </div>
  );
};
