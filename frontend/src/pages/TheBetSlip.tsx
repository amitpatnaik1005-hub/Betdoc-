/**
 * The Bet-Slip (Group 106, ASHVATTHAMA, points b.7 and b.12): the collectible ticket of a slip the BetDoc Consultant
 * approved, and its six delivery channels. A ticket is issued only from a gold-grade verdict whose seal still holds;
 * its figures are the signed ones. Opens on `?report=<verdict id>` (the Consultant links here), else the newest
 * approval. Nothing is placed from here: the ticket is placed by hand at the book. Developed for Amit Ashok Kumar Patnaik.
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
import { CommanderHero } from "../ui/hero";
import { MOTIFS } from "../ui/motifs";
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

  const headline = t
    ? `Ticket ${t.ticket_no}: ${t.legs.length}-leg ${t.kind.replace(/_/g, " ").toLowerCase()} at ${t.book_label}, ${inr(t.stake_inr)} to return ${inr(t.payout_inr)}.`
    : tickets.data && issuable.length === 0
      ? "ASHVATTHAMA holds no approved slip yet. The Consultant issues a ticket only for a gold-grade verdict."
      : "ASHVATTHAMA preparing the ticket…";

  return (
    <Page>
      <CommanderHero
        commander="ASHVATTHAMA"
        headline={headline}
        motif={MOTIFS.stack}
        detail={t ? <>Slip Score {Math.round(t.slip_score)} ({t.tier}) · signed {t.signed_at_local} · sealed {t.seal.slice(0, 12)}…{t.booking_code ? ` · booking ${t.booking_code}` : ""}</> : undefined}
        actions={<Button icon="medical_services" onClick={() => navigate("/consultant")}>BetDoc Consultant</Button>}
      />
      <ComponentGuideBanner
        className="lg:col-span-12"
        moduleName="Bet-Slip"
        purpose="The collectible ticket of a slip the Consultant approved: every figure is the signed verdict's (the book's own selection codes, KUMBHA's Kelly stake, the 30% TDS, the stop-loss floor) under its seal, with six ways to take it with you."
        steps={[
          "Approve a slip in the BetDoc Consultant: only a gold-grade verdict whose seal holds gets a ticket",
          "Find each leg at the book by its fixture and the book's own code, check the prices are still the ticket's, place it by hand",
          "Record the booking code the book gives you, then copy, print, export or send the ticket",
        ]}
        kpiCodes={["SLIP_SCORE", "KELLY_STAKE", "EV", "OVERROUND"]}
      />
      <div className="min-w-0 lg:col-span-12">
      <Async resource={tickets} isEmpty={(d) => !d.tickets.some((row) => row.issuable)}
             empty={<EmptyState icon="confirmation_number" title="No approved slip yet"
                                detail="Consult a slip in the BetDoc Consultant. A gold-grade approval becomes a ticket here; a slip approved only with a prescription gets its ticket once the cure is applied."
                                action={<Button variant="primary" icon="medical_services" onClick={() => navigate("/consultant")}>Open the Consultant</Button>} />}>
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
                            <span className="font-mono" style={{ color: TIER_STYLE[row.tier].color }}>{Math.round(row.slip_score)} {row.tier}</span>
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
