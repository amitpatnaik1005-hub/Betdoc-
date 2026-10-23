/**
 * The Bet-Slip ornament (Group 106, point b.7; Group 111's pure-white re-haul): the official ticket of an approved, sealed
 * verdict, made to read like a boarding pass. Pure white paper, a gold foil strip, BetDoc's quantitative seal beside the
 * book's own badge, notches and a perforation, each leg with its sport, fixture, selection (emerald), the book's code and
 * the price as signed, then the money: KUMBHA's Kelly stake, the gross return, the take-home after Section 194BA's TDS
 * and the stop-loss floor. The barcode is Code 39 of the ticket number (a scanner reads it back). Always light, in either
 * theme: it is a printed object. The booking code is the user's own note, kept outside the seal. Under it (Group 114) the
 * handcrafted desk locks the stake for the slip placed by hand and weighs the book's cashout once it is in play.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { TIER_STYLE } from "../../lib/backtest_and_parlay";
import { code39 } from "../../lib/barcode";
import { BetdocLogo } from "../../ui/brand";
import { inr, type Ticket } from "../../lib/slip_ornament";
import { HandcraftedPlacementDesk } from "./HandcraftedPlacementDesk";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);
const signedPct = (p: number | null | undefined) => (p === null || p === undefined ? "—" : `${p >= 0 ? "+" : ""}${pct(p)}`);
const CODE = /^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$/;
const SPORT_ICON: Record<string, string> = {
  cricket: "sports_cricket", football: "sports_soccer", soccer: "sports_soccer", tennis: "sports_tennis", basketball: "sports_basketball",
  "ice hockey": "sports_hockey", icehockey: "sports_hockey", mma: "sports_mma", baseball: "sports_baseball", "american football": "sports_football",
};
const sportIcon = (sport: string | null) => SPORT_ICON[(sport ?? "").toLowerCase()] ?? "sports";

const MoneyBox = ({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone: string }) => (
  <div className="flex min-w-0 flex-col rounded-2xl border border-stone-200/80 bg-white p-3">
    <span className="text-[10px] font-bold uppercase tracking-wider text-stone-500">{label}</span>
    <span className={`mt-1 truncate font-mono text-base font-black sm:text-lg ${tone}`}>{value}</span>
    {hint && <span className="mt-0.5 truncate text-[10px] text-stone-500">{hint}</span>}
  </div>
);

/** Code 39 of the ticket number, black on white so it scans; the number printed under it. */
const Barcode = ({ value }: { value: string }) => {
  const code = code39(value);
  if (!code) return null;
  return (
    <figure className="flex flex-col items-center gap-1" aria-label={`Barcode of ticket ${value}`}>
      <svg viewBox={`-10 0 ${code.width + 20} 40`} preserveAspectRatio="none" className="h-12 w-full max-w-sm" role="img" aria-hidden="true">
        {code.bars.map((bar, i) => <rect key={i} x={bar.x} y={0} width={bar.width} height={40} fill="#0C0A09" />)}
      </svg>
      <figcaption className="font-mono text-[11px] font-bold tracking-[0.35em] text-stone-800">{value}</figcaption>
    </figure>
  );
};

export const LuxuryBetCardOrnament = ({ ticket, onSaveBooking, saving }: { ticket: Ticket; onSaveBooking: (code: string | null) => void; saving: boolean }) => {
  const [draft, setDraft] = useState<string | null>(null);
  const code = draft ?? ticket.booking_code ?? "";
  const dirty = draft !== null && draft !== (ticket.booking_code ?? "");
  const valid = code === "" || CODE.test(code);
  const tier = TIER_STYLE[ticket.tier];
  const tdsRate = ticket.tds_rate_pct ?? 30;
  return (
    <article id="betdoc-ticket" aria-label={`BetDoc official Bet-Slip ${ticket.ticket_no}`}
             className="relative mx-auto w-full max-w-2xl overflow-hidden rounded-[24px] border border-stone-200 bg-white text-stone-900 shadow-2xl shadow-stone-900/10 print:border-stone-300 print:shadow-none">
      {/* the ticket's notches: the page behind it, in either theme */}
      <span aria-hidden="true" className="absolute -left-3 top-[132px] z-10 size-6 rounded-full border border-stone-200 bg-[#F8F6F0] dark:bg-[#0c0a09] print:hidden" />
      <span aria-hidden="true" className="absolute -right-3 top-[132px] z-10 size-6 rounded-full border border-stone-200 bg-[#F8F6F0] dark:bg-[#0c0a09] print:hidden" />

      {/* gold foil strip */}
      <div className="flex flex-wrap items-center justify-between gap-2 bg-gradient-to-r from-[#B8892E] via-[#C89B3C] to-[#E3BE63] px-5 py-2 font-mono text-[10px] font-black uppercase tracking-[0.18em] text-stone-950 sm:px-6">
        <span className="flex items-center gap-1.5"><span className="material-symbols-outlined text-[15px]" aria-hidden="true">verified</span>BetDoc official Bet-Slip · {ticket.ticket_no}</span>
        <span className="tracking-[0.12em]">{ticket.kind.replace(/_/g, " ")} · {ticket.legs.length} leg{ticket.legs.length === 1 ? "" : "s"}</span>
      </div>

      {/* BetDoc's official logo and the book's badge (Parimatch and 1xBet in their own colours, any other book in the
          badge colours the ticket carries) */}
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-dashed border-stone-300 px-5 py-5 sm:px-6">
        <div className="flex min-w-0 items-center gap-3.5">
          <BetdocLogo variant="mark" className="ring-2 ring-[#C89B3C]/40" />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-display text-sm font-black tracking-tight text-stone-950 sm:text-base">BETDOC QUANTITATIVE DESK</span>
              <span className="rounded-full bg-[#C89B3C]/15 px-2 py-0.5 font-mono text-[9px] font-black uppercase tracking-wider text-[#8A6A1F]">Official seal</span>
            </div>
            <p className="mt-0.5 truncate text-sm font-bold text-emerald-700">{ticket.verdict.title}</p>
            <p className="truncate font-mono text-[11px] text-stone-500">Signed {ticket.signed_at_local}</p>
          </div>
        </div>
        {ticket.book_label.toLowerCase().includes("parimatch") ? (
          <span className="flex items-center gap-2 rounded-xl border border-[#F8FF13]/40 bg-[#111111] px-4 py-2 text-[#F8FF13] shadow-sm">
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">bolt</span>
            <span className="text-xs font-black italic tracking-wider">PARIMATCH</span>
          </span>
        ) : ticket.book_label.toLowerCase().includes("1xbet") ? (
          <span className="flex items-center gap-2 rounded-xl border border-white/30 bg-[#007ACC] px-4 py-2 text-white shadow-sm">
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">sports_soccer</span>
            <span className="text-xs font-black tracking-wider">1xBET</span>
          </span>
        ) : (
          <span className="flex items-center gap-1.5 rounded-xl px-4 py-2 font-mono text-xs font-black uppercase tracking-widest shadow-sm"
                style={{ color: ticket.badge.foreground, background: ticket.badge.background, border: `2px solid ${ticket.badge.foreground}` }}>
            <span className="material-symbols-outlined text-[17px]" aria-hidden="true">storefront</span>{ticket.book_label}
          </span>
        )}
      </header>

      {/* Slip Score, freshness, total odds */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-200 bg-stone-50/70 px-5 py-3 sm:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 font-mono text-xs font-black"
                style={{ color: "#1C1917", background: `color-mix(in srgb, ${tier.color} 22%, white)`, border: `1px solid ${tier.color}` }}>
            <span className="material-symbols-outlined text-[15px]" aria-hidden="true">shield</span>
            Slip Score {Math.round(ticket.slip_score)}/100 · {tier.label}
          </span>
          {ticket.fresh ? (
            <span className="rounded-md bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800">Fresh · signed {ticket.age_minutes} min ago</span>
          ) : (
            <span className="rounded-md bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-800">Signed {ticket.age_minutes} min ago · re-check prices at {ticket.book_label}</span>
          )}
        </div>
        <span className="flex items-baseline gap-1.5 font-mono font-black text-stone-950">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-stone-400">Total odds</span>
          <span className="text-lg">{ticket.odds ?? "—"}</span>
        </span>
      </div>

      {/* the legs */}
      <ol className="divide-y divide-stone-100 px-5 sm:px-6">
        {ticket.legs.map((leg) => (
          <li key={leg.leg_id} className="flex items-start justify-between gap-4 py-3.5">
            <div className="flex min-w-0 items-start gap-3">
              <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-stone-900 font-mono text-[11px] font-bold text-white">{leg.number}</span>
              <div className="min-w-0">
                <p className="flex min-w-0 items-center gap-1 text-[10px] font-bold uppercase tracking-wider text-stone-500">
                  <span className="material-symbols-outlined text-[14px] text-stone-400" aria-hidden="true">{sportIcon(leg.sport)}</span>
                  <span className="truncate">{[leg.sport, leg.league, leg.kickoff].filter(Boolean).join(" · ") || "—"}</span>
                </p>
                <h3 className="mt-0.5 text-sm font-black leading-snug text-stone-950">{leg.fixture}</h3>
                <p className="mt-1 flex flex-wrap items-center gap-2">
                  <span className="rounded-md bg-emerald-50 px-2 py-0.5 text-xs font-bold text-emerald-700">{leg.market}</span>
                  <span className="rounded-md bg-stone-100 px-1.5 py-0.5 font-mono text-[10px] font-bold text-stone-600" title={`${ticket.book_label}'s own code for this selection`}>{leg.code}</span>
                  <span className="font-mono text-[10px] text-stone-500">fair {pct(leg.fair_probability)} · EV {signedPct(leg.ev)}</span>
                </p>
              </div>
            </div>
            <div className="shrink-0 text-right">
              <div className="font-mono text-lg font-black text-stone-950">{leg.price ?? "—"}</div>
              <div className="text-[10px] font-semibold text-stone-400">at signing</div>
            </div>
          </li>
        ))}
      </ol>

      {/* the money */}
      <section aria-label="Stake and returns" className="border-t-2 border-stone-200 bg-stone-50/80 p-5 sm:px-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MoneyBox label="Kelly stake" value={inr(ticket.stake_inr)} hint={ticket.stake_usd ? `$${ticket.stake_usd}` : "no fresh USD rate at signing"} tone="text-stone-950" />
          <MoneyBox label="Gross return" value={inr(ticket.payout_inr)} hint={`wins ${pct(ticket.joint_probability)}`} tone="text-stone-950" />
          <MoneyBox label={`Net after ${tdsRate}% TDS`} value={inr(ticket.take_home_inr)} hint={ticket.tds_inr ? `TDS ${inr(ticket.tds_inr)}` : undefined} tone="text-emerald-700" />
          <MoneyBox label="Stop-loss floor" value={inr(ticket.stop_loss_floor_inr)} hint={`cash out ${Math.round(ticket.stop_loss_pct * 100)}% under the stake`} tone="text-rose-700" />
        </div>
        <p className="mt-3 text-center text-[10px] text-stone-500">
          Section 115BBJ / Section 194BA: {tdsRate}% TDS withheld on the net winnings{ticket.net_winnings_inr ? ` of ${inr(ticket.net_winnings_inr)}` : ""}; the 4% cess is due when the return is filed.
        </p>
      </section>

      {/* the booking code: the user's note, beside the seal */}
      <section aria-label="Booking code" className="border-t border-dashed border-stone-300 bg-white px-5 py-4 sm:px-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <label htmlFor="booking-code" className="min-w-0 text-xs">
            <span className="flex items-center gap-1.5 font-bold"><span className="material-symbols-outlined text-[16px] text-[#C89B3C]" aria-hidden="true">confirmation_number</span>{ticket.book_label} booking code</span>
            <span className="mt-0.5 block text-[11px] text-stone-500">Placed it at {ticket.book_label}? Record the code the book gave you. It is your note, kept beside the seal, never inside it.</span>
          </label>
          <div className="flex shrink-0 items-center gap-2 print:hidden">
            <input id="booking-code" value={code} maxLength={32} placeholder="e.g. 5X7A9B" spellCheck={false} aria-invalid={!valid}
                   onChange={(e) => setDraft(e.target.value.trim())}
                   className={`w-36 rounded-xl border bg-stone-50 px-3 py-1.5 font-mono text-xs font-bold tracking-wider text-stone-900 focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/50 ${valid ? "border-stone-200" : "border-rose-400"}`} />
            <button type="button" disabled={!dirty || !valid || saving} onClick={() => { onSaveBooking(code || null); setDraft(null); }}
                    className="inline-flex items-center gap-1 rounded-xl bg-stone-950 px-3.5 py-1.5 text-xs font-bold text-white transition hover:bg-stone-800 disabled:opacity-40">
              <span className="material-symbols-outlined text-[15px]" aria-hidden="true">{saving ? "hourglass_top" : "save"}</span>Save
            </button>
          </div>
          <span className="hidden font-mono text-sm font-black print:inline">{ticket.booking_code ?? "—"}</span>
        </div>
        {!valid && <p className="mt-1 text-[11px] text-rose-700">3 to 32 letters, digits, dashes or underscores, starting with a letter or digit.</p>}
      </section>

      {/* Group 114: lock the stake for the slip placed by hand, then the book's cashout weighed in play */}
      <HandcraftedPlacementDesk ticket={ticket} code={code} codeValid={valid} />

      {/* the barcode, then the seal */}
      <div className="border-t border-stone-200 bg-white px-5 pb-4 pt-5 sm:px-6">
        <Barcode value={ticket.ticket_no} />
      </div>
      <footer className="flex flex-col gap-1 bg-stone-950 px-5 py-3.5 text-center sm:px-6">
        <span className="flex items-center justify-center gap-1.5 font-mono text-[10px] tracking-wider text-stone-300" title={`HMAC-SHA256 seal over the signed report: ${ticket.seal}`}>
          <span className="material-symbols-outlined text-[14px] text-[#C89B3C]" aria-hidden="true">lock</span>
          SEAL {ticket.seal.slice(0, 24)}…
        </span>
        <span className="text-[10px] text-stone-400">Official Quantitative Slip · Developed for {ticket.architect || "Amit Ashok Kumar Patnaik"}</span>
      </footer>
    </article>
  );
};
