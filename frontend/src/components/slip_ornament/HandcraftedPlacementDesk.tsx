/**
 * The handcrafted placement and in-play cashout desk (Group 114, points a.4, a.7, b.7 and FA-6), under the ticket's
 * booking code. BetDoc is Krishna, the strategist and the ledger; the user is Arjuna, who alone places the slip and takes
 * the cashout at the book. Before placement: lock the stake in TODAR MAL's vault against the booking code (the seal, the
 * funds and SHIVAJI's breakers are checked on the server). Once placed: the live badge, the stop-loss floor, the book's
 * cashout offer weighed against fair value (HOLD, CASHOUT ADVISED, STOP-LOSS BREACH), and the cashout taken recorded with
 * its 30% TDS. Every figure is the server's; nothing is computed here. Never printed. Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  type CashoutAdvice,
  type CashoutEvaluation,
  confirmHandcraftedPlacement,
  evaluateCashoutOffer,
  executeCashout,
  useRealBankroll,
} from "../../lib/real_money";
import { invalidate } from "../../lib/resource";
import { inr, type Ticket, type TicketPlacement } from "../../lib/slip_ornament";
import { toast } from "../../store/useToastStore";

const DESK_BOOKS = new Set(["parimatch", "1xbet"]);
// a refusal the trader may knowingly override: the odds under the floor, the stake over the 5% ceiling
const OVERRIDABLE = new Set(["ODDS_DECAYED", "STAKE_EXCEEDS_CEILING"]);
const ADVICE: Record<CashoutAdvice, { label: string; icon: string; tone: string }> = {
  HOLD: { label: "HOLD", icon: "pan_tool", tone: "border-stone-300 bg-stone-100 text-stone-800" },
  CASHOUT_ADVISED: { label: "CASHOUT ADVISED", icon: "savings", tone: "border-emerald-300 bg-emerald-50 text-emerald-800" },
  STOP_LOSS_BREACH: { label: "STOP-LOSS BREACH", icon: "e911_emergency", tone: "border-rose-300 bg-rose-50 text-rose-800" },
};
const SOURCE: Record<CashoutEvaluation["probability_source"], string> = {
  LIVE: "live win probability, every open leg priced now",
  LAST_TICK: "the in-play watch's last priced tick (no live price now)",
  ENTRY: "the probability at placement (no live price yet)",
};
const SETTLED: Record<string, string> = {
  WON: "Won", HALF_WON: "Half won", LOST: "Lost", HALF_LOST: "Half lost", PUSH: "Push", VOID: "Void", CANCELLED: "Cancelled", CASHED_OUT: "Cashed out",
};

const refusal = (err: unknown): { code: string | null; message: string } =>
  err instanceof ApiError
    ? { code: typeof err.detail?.code === "string" ? err.detail.code : err.reason, message: err.message }
    : { code: null, message: "No answer from the server" };
const amount = (raw: string): number | null => {
  const n = Number(raw);
  return raw.trim() !== "" && Number.isFinite(n) && n > 0 ? n : null;
};
const fieldClass = "w-full rounded-xl border border-stone-200 bg-stone-50 px-3 py-1.5 font-mono text-xs font-bold text-stone-900 focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/50";

const Fig = ({ label, value, tone = "text-stone-950" }: { label: string; value: string; tone?: string }) => (
  <div className="flex min-w-0 flex-col rounded-xl border border-stone-200/80 bg-white px-3 py-2">
    <span className="text-[10px] font-bold uppercase tracking-wider text-stone-500">{label}</span>
    <span className={`truncate font-mono text-sm font-black ${tone}`}>{value}</span>
  </div>
);

/** Lock the stake for a slip placed by hand: the booking code is the one typed above. */
const PlaceForm = ({ ticket, code, codeValid }: { ticket: Ticket; code: string; codeValid: boolean }) => {
  const bankroll = useRealBankroll();
  const [stake, setStake] = useState(ticket.stake_inr);
  const [odds, setOdds] = useState(ticket.odds ?? "");
  const [override, setOverride] = useState(false);
  const [offerOverride, setOfferOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stakeN = amount(stake);
  const oddsN = amount(odds);
  const cash = bankroll.data?.cash_balance ?? null;
  const short = cash !== null && stakeN !== null && stakeN > cash;
  const ready = code !== "" && codeValid && stakeN !== null && oddsN !== null && oddsN > 1 && !busy;

  const place = async () => {
    if (!ready || stakeN === null || oddsN === null) return;
    setBusy(true);
    setError(null);
    try {
      const placed = await confirmHandcraftedPlacement({ report_id: ticket.report_id, booking_code: code, placed_stake_inr: stakeN, placed_odds: oddsN,
                                                         override_odds_decay: override });
      invalidate(`bet-slip:ticket:${ticket.report_id}`, "bet-slip:tickets");
      toast.success(placed.replayed ? "Already placed" : "Stake locked · placement confirmed",
                    `${placed.ticket.ticket_reference} · ${inr(placed.ticket.placed_stake)} at ${placed.ticket.placed_odds ?? "—"}${placed.monitor_note ? ` · ${placed.monitor_note}` : ""}`);
    } catch (err) {
      const r = refusal(err);
      setError(r.message);
      setOfferOverride(r.code !== null && OVERRIDABLE.has(r.code));
      toast.error("Placement not confirmed", r.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[11px] text-stone-500">
        Placed it at {ticket.book_label}? Lock the stake in Todar Mal's vault against the booking code above. The seal, your liquid cash and SHIVAJI's
        breakers are checked first; BetDoc never places the bet.
      </p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <label className="flex flex-col gap-1 text-[10px] font-bold uppercase tracking-wider text-stone-500">
          Stake placed (₹)
          <input inputMode="decimal" value={stake} onChange={(e) => setStake(e.target.value)} className={fieldClass} aria-invalid={stakeN === null} />
        </label>
        <label className="flex flex-col gap-1 text-[10px] font-bold uppercase tracking-wider text-stone-500">
          Odds the book gave
          <input inputMode="decimal" value={odds} onChange={(e) => setOdds(e.target.value)} className={fieldClass} aria-invalid={oddsN === null || oddsN <= 1} />
        </label>
        <Fig label="Liquid cash" value={cash === null ? (bankroll.error ? "unavailable" : "…") : inr(cash)} tone={short ? "text-rose-700" : "text-stone-950"} />
        <Fig label="Locked exposure" value={bankroll.data ? inr(bankroll.data.locked_exposure) : "…"} />
      </div>
      {short && <p className="text-[11px] font-semibold text-rose-700">The stake exceeds your liquid cash. Please deposit funds into Todar Mal's Vault first.</p>}
      {bankroll.data && !bankroll.data.can_bet && (
        <p className="text-[11px] font-semibold text-amber-800">
          {bankroll.data.stop_loss_triggered ? "SHIVAJI's daily stop-loss is latched until midnight IST." : "SHIVAJI's cool-down is active after consecutive losses."}
        </p>
      )}
      {error && <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] font-semibold text-rose-800" role="alert">{error}</p>}
      {offerOverride && (
        <label className="flex items-center gap-2 text-[11px] font-semibold text-amber-900">
          <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} className="size-4 accent-amber-600" />
          I placed it anyway: record it over SHIVAJI's refusal
        </label>
      )}
      <button type="button" disabled={!ready} onClick={() => void place()}
              className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-gradient-to-r from-[#B8892E] via-[#C89B3C] to-[#E3BE63] px-4 py-2.5 text-sm font-black text-stone-950 shadow-sm transition hover:brightness-105 disabled:opacity-40">
        <span className="material-symbols-outlined text-[18px]" aria-hidden="true">{busy ? "hourglass_top" : "bolt"}</span>
        Lock stake &amp; confirm placement{stakeN !== null ? ` (${inr(stakeN)})` : ""}
      </button>
      {code === "" && <p className="text-center text-[11px] text-stone-500">Type the booking code the book gave you above to confirm the placement.</p>}
    </div>
  );
};

/** The cashout taken at the book, recorded: the server withholds the TDS and credits the vault. */
const CashoutModal = ({ placement, offer, advice, onClose }: { placement: TicketPlacement; offer: string; advice: CashoutAdvice | null; onClose: () => void }) => {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const [value, setValue] = useState(offer);
  const [busy, setBusy] = useState(false);
  const n = amount(value);
  useEffect(() => {
    box.current?.querySelector<HTMLInputElement>("input")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const submit = async () => {
    if (n === null) return;
    setBusy(true);
    try {
      const done = await executeCashout(placement.ticket_reference, n, advice ?? "MANUAL_USER_REQUEST");
      invalidate("bet-slip");
      toast.success(`Cashout recorded · ${inr(done.net_payout)} credited`,
                    `Gross ${inr(done.gross_cashout)} · TDS ${inr(done.tds_deducted)} · P&L ${done.net_profit >= 0 ? "+" : ""}${inr(done.net_profit)}`);
      onClose();
    } catch (err) {
      toast.error("Cashout not recorded", refusal(err).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/40 p-4 sm:items-center print:hidden" role="presentation"
         onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-md rounded-3xl bg-white p-6 text-stone-900 shadow-soft-lg">
        <h3 id={titleId} className="text-base font-black">Record the cashout taken</h3>
        <p className="mt-1 text-xs text-stone-500">
          {placement.ticket_reference} · stake {inr(placement.placed_stake)}. Enter the gross amount the book paid. Section 115BBJ's 30% TDS is withheld on the
          net winnings (above the stake); the rest joins your liquid cash and the ticket closes as cashed out.
        </p>
        <label className="mt-4 flex flex-col gap-1 text-[10px] font-bold uppercase tracking-wider text-stone-500">
          Cashout received (₹, gross)
          <input inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} className={fieldClass} aria-invalid={n === null} />
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-xl px-3.5 py-1.5 text-xs font-bold text-stone-600 hover:bg-stone-100">Cancel</button>
          <button type="button" disabled={n === null || busy} onClick={() => void submit()}
                  className="inline-flex items-center gap-1 rounded-xl bg-stone-950 px-3.5 py-1.5 text-xs font-bold text-white transition hover:bg-stone-800 disabled:opacity-40">
            <span className="material-symbols-outlined text-[15px]" aria-hidden="true">{busy ? "hourglass_top" : "account_balance_wallet"}</span>Record &amp; credit the vault
          </button>
        </div>
      </div>
    </div>
  );
};

/** A placed slip: the live badge, the stop-loss floor, the cashout quote and the cashout taken. */
const InPlayDesk = ({ placement, bookLabel }: { placement: TicketPlacement; bookLabel: string }) => {
  const [offer, setOffer] = useState("");
  // the server's answer for one offer; shown only while that offer is still the one typed
  const [answer, setAnswer] = useState<{ offer: number; evaluation: CashoutEvaluation | null; problem: string | null } | null>(null);
  const [recording, setRecording] = useState(false);
  const offerN = amount(offer);
  const live = placement.status === "PLACED";

  useEffect(() => {
    if (!live || offerN === null) return;
    let current = true;
    const timer = window.setTimeout(() => {
      evaluateCashoutOffer(placement.ticket_reference, offerN)
        .then((evaluation) => current && setAnswer({ offer: offerN, evaluation, problem: null }))
        .catch((err: unknown) => current && setAnswer({ offer: offerN, evaluation: null, problem: refusal(err).message }));
    }, 350);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [live, offerN, placement.ticket_reference]);
  const shown = answer !== null && answer.offer === offerN ? answer : null;
  const evaluation = shown?.evaluation ?? null;
  const problem = shown?.problem ?? null;

  if (!live) {
    const profit = Number(placement.net_profit);
    return (
      <div className="flex flex-col gap-3">
        <span className="inline-flex w-fit items-center gap-1.5 rounded-full border border-stone-300 bg-stone-100 px-3 py-1 font-mono text-[11px] font-black uppercase tracking-wider text-stone-800">
          <span className="material-symbols-outlined text-[15px]" aria-hidden="true">task_alt</span>
          {SETTLED[placement.status] ?? placement.status} · {placement.ticket_reference}
        </span>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fig label="Gross return" value={inr(placement.gross_return)} />
          <Fig label="TDS withheld" value={inr(placement.tds_deducted)} />
          <Fig label="Credited" value={inr(placement.settlement_payout)} tone="text-emerald-700" />
          <Fig label="P&L" value={`${profit >= 0 ? "+" : ""}${inr(placement.net_profit)}`} tone={profit >= 0 ? "text-emerald-700" : "text-rose-700"} />
        </div>
      </div>
    );
  }
  const advice = evaluation ? ADVICE[evaluation.advice] : null;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-600 px-3 py-1 font-mono text-[11px] font-black uppercase tracking-wider text-white">
          <span className="size-2 animate-pulse rounded-full bg-white" aria-hidden="true" />LIVE IN-PLAY · PLACED ON BOOKMAKER
        </span>
        <span className="font-mono text-[11px] font-bold text-stone-500">{placement.ticket_reference} · {placement.booking_code ?? "—"}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Fig label="Stake locked" value={inr(placement.placed_stake)} />
        <Fig label="Odds placed" value={placement.placed_odds ?? "—"} />
        <Fig label="Stop-loss floor" value={inr(placement.stop_loss_floor_inr)} tone="text-rose-700" />
        <Fig label="In-play watch" value={placement.monitor_active ? `win ${placement.current_win_prob === null ? "—" : `${(placement.current_win_prob * 100).toFixed(1)}%`}` : "not watching"} />
      </div>
      <p className="text-[11px] text-stone-500">
        Stop-Loss Floor: {inr(placement.stop_loss_floor_inr)} ({Math.round(placement.stop_loss_pct * 100)}% threshold). Read the cashout offer {bookLabel} shows you now:
      </p>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label className="flex min-w-0 flex-1 items-center gap-2 text-xs font-bold text-stone-800">
          <span className="shrink-0">Live cashout offer (₹)</span>
          <input inputMode="decimal" value={offer} placeholder="e.g. 1450" onChange={(e) => setOffer(e.target.value)} className={fieldClass} aria-describedby="cashout-advice" />
        </label>
        <button type="button" onClick={() => setRecording(true)}
                className="inline-flex shrink-0 items-center justify-center gap-1 rounded-xl bg-stone-950 px-3.5 py-1.5 text-xs font-bold text-white transition hover:bg-stone-800">
          <span className="material-symbols-outlined text-[15px]" aria-hidden="true">payments</span>Record cashout taken
        </button>
      </div>
      <div id="cashout-advice" aria-live="polite">
        {problem && <p className="text-[11px] font-semibold text-rose-700">{problem}</p>}
        {evaluation && advice && (
          <div className={`flex flex-col gap-1.5 rounded-xl border px-3 py-2.5 ${advice.tone}`}>
            <span className="inline-flex items-center gap-1.5 font-mono text-xs font-black tracking-wider">
              <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{advice.icon}</span>{advice.label}
            </span>
            <p className="text-xs font-semibold">{evaluation.message}</p>
            <p className="font-mono text-[10px] opacity-80">
              fair value {inr(evaluation.fair_value)} · offer/fair {evaluation.hold_ratio === null ? "—" : `${(evaluation.hold_ratio * 100).toFixed(1)}%`} · floor {inr(evaluation.stop_loss_floor)}
              {" "}· win {(evaluation.win_probability * 100).toFixed(1)}% from {SOURCE[evaluation.probability_source]}
            </p>
            {evaluation.twin_call && <p className="text-[10px] font-semibold opacity-90">The twin's watch: {evaluation.twin_call.action}</p>}
          </div>
        )}
      </div>
      {recording && <CashoutModal placement={placement} offer={offer} advice={evaluation?.advice ?? null} onClose={() => setRecording(false)} />}
    </div>
  );
};

export const HandcraftedPlacementDesk = ({ ticket, code, codeValid }: { ticket: Ticket; code: string; codeValid: boolean }) => (
  <section aria-label="Handcrafted placement and in-play cashout" className="border-t border-dashed border-stone-300 bg-[#FBFAF6] px-5 py-4 sm:px-6 print:hidden">
    <h3 className="mb-3 flex items-center gap-1.5 text-xs font-black uppercase tracking-wider text-stone-800">
      <span className="material-symbols-outlined text-[16px] text-[#C89B3C]" aria-hidden="true">swords</span>
      {ticket.placement ? "In-play cashout desk" : "Handcrafted placement"}
    </h3>
    {ticket.placement ? (
      <InPlayDesk placement={ticket.placement} bookLabel={ticket.book_label} />
    ) : DESK_BOOKS.has(ticket.book ?? "") ? (
      <PlaceForm ticket={ticket} code={code} codeValid={codeValid} />
    ) : (
      <p className="text-[11px] text-stone-500">
        This slip was priced at {ticket.book_label}; the real-money desk keeps Parimatch and 1xBet accounts only, so its stake is not locked here.
      </p>
    )}
  </section>
);
