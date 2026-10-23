/**
 * The official Bet-Slip (Group 106, points b.7 and b.12; Group 111's one-line header): the ticket of a slip the BetDoc
 * Consultant approved, with BetDoc's logo beside the book's own badge, and its six delivery channels. A ticket is issued
 * only from a gold-grade verdict whose seal still holds; its figures are the signed ones. Opens on `?report=<verdict id>`
 * (the Consultant links here), else the newest approval. Nothing is placed from here: the ticket is placed by hand at the
 * book. Developed for Amit Ashok Kumar Patnaik.
 */
import { useNavigate, useSearchParams } from "react-router-dom";
import { useState } from "react";
import { ApiError } from "../api/client";
import { ExportChannelsToolbar } from "../components/slip_ornament/ExportChannelsToolbar";
import { LuxuryBetCardOrnament } from "../components/slip_ornament/LuxuryBetCardOrnament";
import { TIER_STYLE } from "../lib/backtest_and_parlay";
import { formatDateTime } from "../lib/format";
import { invalidate } from "../lib/resource";
import { inr, recordBooking, useTicket, useTickets } from "../lib/slip_ornament";
import { toast } from "../store/useToastStore";
import { Async, Button, ComponentGuideBanner, EmptyState, Page, Panel, Pill } from "../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");

export const TheBetSlip = () => {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tickets = useTickets();
  const issuable = (tickets.data?.tickets ?? []).filter((t) => t.issuable);
  const chosen = params.get("report") ?? issuable[0]?.report_id ?? null;
  const ticket = useTicket(chosen);
  const [saving, setSaving] = useState(false);
  const t = ticket.data;

  const save = async (code: string | null) => {
    if (!t) return;
    setSaving(true);
    try {
      await recordBooking(t.report_id, code);
      invalidate(`bet-slip:ticket:${t.report_id}`, "bet-slip:tickets");
      toast.success(code ? "Booking code recorded" : "Booking code cleared", code ? `${t.book_label} · ${code}` : undefined);
    } catch (err) {
      toast.error("Booking code refused", refusal(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Page>
      {/* Group 111: one line for what the page is and where a slip comes from */}
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-200/80 pb-4 lg:col-span-12 dark:border-stone-800/80">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid size-11 shrink-0 place-items-center rounded-2xl border border-stone-200/60 bg-white shadow-soft dark:border-stone-800 dark:bg-stone-900" aria-hidden="true">
            <span className="material-symbols-outlined text-2xl text-[#C89B3C]">confirmation_number</span>
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-display text-xl font-bold tracking-tight text-stone-950 dark:text-stone-100">Official Bet-Slip</h1>
              <span className="rounded-full border border-[#C89B3C]/30 bg-[#C89B3C]/10 px-2.5 py-0.5 font-mono text-[10px] font-bold text-[#C89B3C]">Dual-branded</span>
            </div>
            <p className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">
              The signed ticket of an approved parlay, placed by hand at Parimatch or 1xBet
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" icon="travel_explore" onClick={() => navigate("/oracle")}>The Oracle</Button>
          <Button size="sm" variant="primary" icon="add_circle" onClick={() => navigate("/consultant")}>New parlay</Button>
        </div>
      </header>

      <ComponentGuideBanner
        className="lg:col-span-12"
        moduleName="Official Bet-Slip"
        purpose="The official ticket of an approved parlay verdict: every figure is the signed verdict's (the book's own selection codes, KUMBHA's Kelly stake, the 30% TDS, the stop-loss floor) under its HMAC-SHA256 seal, with six ways to take it with you."
        steps={[
          "Approve a slip in the BetDoc Consultant: only a gold-grade verdict whose seal holds gets a ticket",
          "Find each leg at the book by its fixture and the book's own code, check the prices are still the ticket's, place it by hand",
          "Record the booking code the book gives you and lock the stake in Todar Mal's vault; while it plays, weigh the book's cashout offer here",
        ]}
        kpiCodes={["SLIP_SCORE", "KELLY_STAKE", "EV", "OVERROUND"]}
      />

      <div className="min-w-0 lg:col-span-12">
        <Async resource={tickets} isEmpty={(d) => !d.tickets.some((row) => row.issuable)}
               empty={<EmptyState icon="confirmation_number" title="No approved bet-slip yet"
                                  detail="Build a parlay in The Oracle and consult it in the BetDoc Consultant. A gold-grade approval becomes a ticket here; a slip approved only with a prescription gets its ticket once the cure is applied."
                                  action={<Button variant="primary" icon="travel_explore" onClick={() => navigate("/oracle")}>Open the match board</Button>} />}>
          {() => (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
              <div className="flex min-w-0 flex-col gap-4 lg:col-span-8">
                {ticket.error && !t ? (
                  <EmptyState icon="gpp_bad" title="No ticket for this verdict" detail={ticket.error} action={<Button onClick={() => setParams({})}>Show the newest approval</Button>} />
                ) : t ? (
                  <LuxuryBetCardOrnament key={t.report_id} ticket={t} onSaveBooking={(code) => void save(code)} saving={saving} />
                ) : (
                  <div className="h-[560px] animate-pulse rounded-[28px] bg-stone-100 dark:bg-stone-800/50" />
                )}
              </div>
              <div className="flex min-w-0 flex-col gap-6 lg:col-span-4 print:hidden">
                {t && <ExportChannelsToolbar ticket={t} />}
                <Panel title="Approved slips" icon="confirmation_number" subtitle="newest first">
                  <ul className="flex flex-col gap-2">
                    {issuable.map((row) => {
                      const on = row.report_id === chosen;
                      return (
                        <li key={row.report_id}>
                          <button type="button" aria-current={on || undefined} onClick={() => setParams({ report: row.report_id })}
                                  className={`w-full rounded-xl border p-2.5 text-left text-xs transition ${on ? "border-[#C89B3C] bg-[#C89B3C]/[0.08]" : "border-stone-200 hover:border-stone-300 dark:border-white/[0.08]"}`}>
                            <span className="flex items-center justify-between gap-2">
                              <span className="font-mono font-bold">{row.ticket_no}</span>
                              <span className="font-mono text-[11px] font-bold" style={{ color: TIER_STYLE[row.tier].color }}>{Math.round(row.slip_score)} {row.tier}</span>
                            </span>
                            <span className="mt-0.5 block truncate text-stone-600 dark:text-stone-300">{(row.legs ?? []).join(" + ")}</span>
                            <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-stone-500 dark:text-stone-400">
                              {row.book_label} @ {row.odds ?? "—"} · {inr(row.stake_inr)} · {row.signed_at ? formatDateTime(row.signed_at) : ""}
                              {row.booking_code && <Pill tone="good" icon="confirmation_number">{row.booking_code}</Pill>}
                              {row.fresh === false && <Pill tone="warning">re-check prices</Pill>}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </Panel>
              </div>
            </div>
          )}
        </Async>
      </div>
    </Page>
  );
};

export default TheBetSlip;
