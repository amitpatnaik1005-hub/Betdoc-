/**
 * The Bet-Slip ornament (Group 106, point b.7): the bright collectible ticket of an approved, sealed verdict. Ivory
 * paper, gold rules, emerald for what comes back, the book's own badge; always light (a printed object), in either
 * theme. Each leg reads as the book's slip does: the fixture, the market and selection, the book's own code, the price
 * as signed. The booking code is the user's own note, kept outside the seal. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { TIER_STYLE } from "../../lib/backtest_and_parlay";
import { inr, type Ticket } from "../../lib/slip_ornament";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);
const CODE = /^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$/;

const Money = ({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone: string }) => (
  <div className="min-w-0">
    <div className="text-[10px] font-bold uppercase tracking-wider text-stone-500">{label}</div>
    <div className={`mt-0.5 truncate font-mono text-lg font-black sm:text-xl ${tone}`}>{value}</div>
    {hint && <div className="text-[10px] text-stone-500">{hint}</div>}
  </div>
);

export const LuxuryBetCardOrnament = ({ ticket, onSaveBooking, saving }: { ticket: Ticket; onSaveBooking: (code: string | null) => void; saving: boolean }) => {
  const [draft, setDraft] = useState<string | null>(null);
  const code = draft ?? ticket.booking_code ?? "";
  const dirty = draft !== null && draft !== (ticket.booking_code ?? "");
  const valid = code === "" || CODE.test(code);
  const tier = TIER_STYLE[ticket.tier];
  return (
    <article id="betdoc-ticket" aria-label={`Bet-Slip ticket ${ticket.ticket_no}`}
             className="relative overflow-hidden rounded-[28px] border-2 border-[#C89B3C] bg-[#FDFCF7] text-stone-900 shadow-2xl shadow-[#C89B3C]/20">
      <span aria-hidden="true" className="absolute -left-3 top-[112px] size-6 rounded-full border-2 border-[#C89B3C] bg-[#F8F6F0] dark:bg-stone-950 print:hidden" />
      <span aria-hidden="true" className="absolute -right-3 top-[112px] size-6 rounded-full border-2 border-[#C89B3C] bg-[#F8F6F0] dark:bg-stone-950 print:hidden" />
      <div className="flex flex-wrap items-center justify-between gap-2 border-b-2 border-dashed border-[#C89B3C]/50 bg-[#C89B3C]/[0.08] px-5 py-2 font-mono text-[10px] font-bold tracking-[0.2em] text-[#9A7424] sm:px-6">
        <span>BETDOC BET-SLIP · {ticket.ticket_no}</span>
        <span className="tracking-normal">{ticket.kind.replace(/_/g, " ").toLowerCase()} · {ticket.legs.length} leg{ticket.legs.length === 1 ? "" : "s"}</span>
      </div>

      <header className="flex flex-wrap items-center justify-between gap-4 px-5 py-5 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid size-14 shrink-0 place-items-center rounded-full bg-[#C89B3C] text-stone-950 shadow-md ring-4 ring-[#C89B3C]/25">
            <span className="material-symbols-outlined text-[30px]" aria-hidden="true">workspace_premium</span>
          </div>
          <div className="min-w-0">
            <div className="font-mono text-[10px] font-black uppercase tracking-[0.18em] text-[#C89B3C]">BetDoc quantitative seal</div>
            <h2 className="truncate text-lg font-black tracking-tight sm:text-xl">{ticket.verdict.title}</h2>
            <p className="truncate font-mono text-[11px] text-stone-500">Signed {ticket.signed_at_local}</p>
          </div>
        </div>
        <span className="rounded-xl px-4 py-2 font-mono text-sm font-black tracking-widest shadow-sm"
              style={{ color: ticket.badge.foreground, background: ticket.badge.background, border: `2px solid ${ticket.badge.foreground}` }}>
          {ticket.book_label.toUpperCase()}
        </span>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-2 border-y border-stone-200 bg-stone-50 px-5 py-2.5 text-xs sm:px-6">
        <span className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 font-mono font-black" style={{ color: "#1C1917", background: `color-mix(in srgb, ${tier.color} 22%, white)`, border: `1px solid ${tier.color}` }}>
          <span className="material-symbols-outlined text-[15px]" aria-hidden="true">verified</span>
          Slip Score {Math.round(ticket.slip_score)}/100 · {tier.label}
        </span>
        <span className="font-mono font-bold">{ticket.odds ? `total odds ${ticket.odds}` : ""}</span>
      </div>

      {!ticket.fresh && (
        <p className="border-b border-orange-200 bg-orange-50 px-5 py-2 text-[11px] font-semibold text-orange-800 sm:px-6">
          Prices as signed {ticket.age_minutes} minutes ago: re-check them at {ticket.book_label} before placing.
        </p>
      )}

      <ol className="divide-y divide-stone-200 px-5 sm:px-6">
        {ticket.legs.map((leg) => (
          <li key={leg.leg_id} className="flex items-start justify-between gap-3 py-4">
            <div className="flex min-w-0 items-start gap-3">
              <span className="grid size-7 shrink-0 place-items-center rounded-full bg-stone-200 font-mono text-xs font-bold text-stone-700">{leg.number}</span>
              <div className="min-w-0">
                <p className="truncate text-[11px] text-stone-500">{[leg.sport, leg.league, leg.kickoff].filter(Boolean).join(" · ") || "—"}</p>
                <p className="text-sm font-black leading-snug">{leg.fixture}</p>
                <p className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-bold text-[#059669]">{leg.market}</span>
                  <span className="rounded-md bg-stone-200/80 px-1.5 py-0.5 font-mono text-[11px] font-bold text-stone-700" title={`${ticket.book_label}'s own code for this selection`}>{leg.code}</span>
                </p>
                <p className="mt-1 font-mono text-[10px] text-stone-500 sm:hidden">fair {pct(leg.fair_probability)} · EV {leg.ev === null ? "—" : `${leg.ev >= 0 ? "+" : ""}${pct(leg.ev)}`}</p>
              </div>
            </div>
            <div className="shrink-0 text-right">
              <div className="font-mono text-xl font-black">{leg.price ?? "—"}</div>
              <div className="hidden font-mono text-[10px] text-stone-500 sm:block">fair {pct(leg.fair_probability)} · EV {leg.ev === null ? "—" : `${leg.ev >= 0 ? "+" : ""}${pct(leg.ev)}`}</div>
            </div>
          </li>
        ))}
      </ol>

      <section aria-label="Stake and returns" className="border-t-2 border-stone-200 bg-stone-50 px-5 py-4 sm:px-6">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Money label="Kelly stake" value={inr(ticket.stake_inr)} hint={ticket.stake_usd ? `$${ticket.stake_usd}` : "no fresh USD rate at signing"} tone="text-stone-900" />
          <Money label="Returns" value={inr(ticket.payout_inr)} hint={`wins ${pct(ticket.joint_probability)}`} tone="text-stone-900" />
          <Money label="Take home after TDS" value={inr(ticket.take_home_inr)} hint={ticket.tds_inr ? `TDS ${inr(ticket.tds_inr)} (${ticket.tds_rate_pct ?? 30}%)` : undefined} tone="text-[#059669]" />
          <Money label="Stop-loss floor" value={inr(ticket.stop_loss_floor_inr)} hint={`cash out ${Math.round(ticket.stop_loss_pct * 100)}% under the stake`} tone="text-rose-700" />
        </div>
        <p className="mt-3 text-[10px] text-stone-500">Section 115BBJ / 194BA: 30% TDS on the net winnings{ticket.net_winnings_inr ? ` of ${inr(ticket.net_winnings_inr)}` : ""}; 4% cess when the return is filed.</p>
      </section>

      <section aria-label="Booking code" className="border-t border-dashed border-[#C89B3C]/50 bg-[#C89B3C]/[0.06] px-5 py-4 sm:px-6">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <label htmlFor="booking-code" className="min-w-0 text-xs">
            <span className="flex items-center gap-1.5 font-bold"><span className="material-symbols-outlined text-[16px] text-[#C89B3C]" aria-hidden="true">confirmation_number</span>{ticket.book_label} booking code</span>
            <span className="mt-0.5 block text-[11px] text-stone-500">Placed it at {ticket.book_label}? Record the code the book gave you. It is your note, kept beside the seal, never inside it.</span>
          </label>
          <div className="flex shrink-0 items-center gap-2 print:hidden">
            <input id="booking-code" value={code} maxLength={32} placeholder="e.g. 5X7A9B" spellCheck={false} aria-invalid={!valid}
                   onChange={(e) => setDraft(e.target.value.trim())}
                   className={`w-36 rounded-lg border bg-white px-3 py-1.5 font-mono text-xs font-bold tracking-wider text-stone-900 focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/50 ${valid ? "border-stone-300" : "border-rose-400"}`} />
            <button type="button" disabled={!dirty || !valid || saving} onClick={() => { onSaveBooking(code || null); setDraft(null); }}
                    className="inline-flex items-center gap-1 rounded-lg bg-stone-900 px-3 py-1.5 text-xs font-bold text-white transition hover:bg-stone-700 disabled:opacity-40">
              <span className="material-symbols-outlined text-[15px]" aria-hidden="true">{saving ? "hourglass_top" : "save"}</span>Save
            </button>
          </div>
          <span className="hidden font-mono text-sm font-black print:inline">{ticket.booking_code ?? "—"}</span>
        </div>
        {!valid && <p className="mt-1 text-[11px] text-rose-700">3 to 32 letters, digits, dashes or underscores, starting with a letter or digit.</p>}
      </section>

      <footer className="flex flex-col gap-1 bg-[#1C1917] px-5 py-3.5 text-center sm:px-6">
        <span className="flex items-center justify-center gap-1.5 font-mono text-[10px] tracking-wider text-stone-300" title={`HMAC-SHA256 seal over the signed report: ${ticket.seal}`}>
          <span className="material-symbols-outlined text-[14px] text-[#C89B3C]" aria-hidden="true">verified_user</span>
          SEAL {ticket.seal.slice(0, 24)}…
        </span>
        <span className="text-[10px] text-stone-400">{ticket.watermark}</span>
      </footer>
    </article>
  );
};
