/**
 * The BetDoc Gazette (Group 116): the official operating procedure and testing manual, printed like a gazette notice on
 * white paper with a double gold rule. Every statement describes what the product does today: where each step lives,
 * which thresholds apply, and what BetDoc does not promise. The operator checklist is ticked by the operator (kept in
 * this browser), and two checks run live against the API. Its source text is docs/BETDOC_OFFICIAL_GAZETTE_TESTING_MANUAL.md.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { type ReactNode, useState } from "react";
import { apiClient, ApiError } from "../api/client";
import { BetdocLogo } from "../ui/brand";
import { ComponentGuideBanner } from "../ui/kit";

const CHECKS_KEY = "betdoc:manual:checks";
const CHECKLIST: { id: string; title: string; how: string }[] = [
  { id: "liveness", title: "System liveness", how: "The API answers http://localhost:8000/api/v1/health/ with postgres ok and redis ok, and the web app opens at http://localhost:5173. Use “Run live checks” below." },
  { id: "today", title: "Morning signal and /today", how: "Send /today to the BetDoc Telegram bot (its chat must be configured in Control Panel → Sentinel). It answers with today's score out of 100, the tier and the measured reasons." },
  { id: "synthesize", title: "Street-smart slip", how: "The Oracle → Bet builder → 5(d) Street-smart & splitter: synthesize toward a target. Every leg shows its price, fair probability and EV; a target the board cannot reach is reported, not padded." },
  { id: "split", title: "Three-ticket split", how: "Split the slip. Slip A shows “Capital shield on” only when its payout covers the whole session stake; otherwise it says the shield is off. The four outcomes add up to 100%." },
  { id: "placement", title: "Booking code locks the stake", how: "Record the three tickets (nothing is locked), place one at the book, then enter its booking code under “Your system tickets” (or under a Bet-Slip ticket on /bet-slip). The Vault's liquid cash falls and locked exposure rises by the stake." },
  { id: "cashout", title: "Cashout advice", how: "On a placed ticket, type the offer the book shows. The advice reads HOLD, CASHOUT ADVISED or STOP-LOSS BREACH, with the fair value and the source of its probability." },
  { id: "tds", title: "30% TDS on net winnings", how: "Record a cashout above the stake: TDS is 30% of (cashout − stake), the rest is credited. At or below the stake, TDS is ₹0.00. The Vault's ledger shows both rows." },
  { id: "status", title: "/status on Telegram", how: "Send /status from a linked chat: it lists the real vault (liquid, locked, SHIVAJI's state) beside the feeds and the kill switch." },
];

const readChecks = (): Record<string, boolean> => {
  try {
    return JSON.parse(window.localStorage.getItem(CHECKS_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
};

interface LiveResult { label: string; ok: boolean; detail: string }

const Part = ({ n, title, children }: { n: string; title: string; children: ReactNode }) => (
  <section className="mb-10 break-inside-avoid-page">
    <h2 className="mb-4 flex items-baseline gap-2 border-b border-stone-200 pb-1 text-lg font-bold uppercase tracking-wider text-stone-900">
      <span className="text-[#A47C26]">Part {n}</span> — {title}
    </h2>
    <div className="space-y-4 text-sm leading-relaxed text-stone-700">{children}</div>
  </section>
);

const Step = ({ n, title, children }: { n: number; title: string; children: ReactNode }) => (
  <div className="rounded border border-stone-200 bg-stone-50 p-4">
    <h3 className="mb-1 font-bold text-stone-900">Step {n}: {title}</h3>
    <div className="space-y-2 leading-relaxed">{children}</div>
  </div>
);

export const TestingManual = () => {
  const [checks, setChecks] = useState<Record<string, boolean>>(readChecks);
  const [live, setLive] = useState<LiveResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const toggle = (id: string) =>
    setChecks((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      try {
        window.localStorage.setItem(CHECKS_KEY, JSON.stringify(next));
      } catch {
        /* a private window: the ticks last for this visit only */
      }
      return next;
    });
  const runLive = async () => {
    setRunning(true);
    const out: LiveResult[] = [];
    try {
      const h = await apiClient.get<{ status: string; dependencies: Record<string, string> }>("/health/");
      out.push({ label: "API health", ok: h.status === "ok", detail: `status ${h.status} · postgres ${h.dependencies.postgres} · redis ${h.dependencies.redis}` });
    } catch (err) {
      out.push({ label: "API health", ok: false, detail: err instanceof ApiError ? err.message : "no answer" });
    }
    try {
      const m = await apiClient.get<{ health_score: number; advisory_tier: string; active_fixtures_count: number }>("/synthesizer/market-health");
      out.push({ label: "Market health", ok: true, detail: `${m.health_score}/100 · ${m.advisory_tier.replace("_", " ")} · ${m.active_fixtures_count} fixtures priced today` });
    } catch (err) {
      out.push({ label: "Market health", ok: false, detail: err instanceof ApiError ? err.message : "no answer" });
    }
    setLive(out);
    setRunning(false);
  };
  const done = CHECKLIST.filter((c) => checks[c.id]).length;

  return (
    <div className="min-h-screen bg-stone-100 px-4 py-8 text-stone-900 sm:px-6 lg:px-8">
      <div className="mx-auto mb-6 flex max-w-4xl flex-col gap-4 print:hidden">
        <ComponentGuideBanner
          className="lg:col-span-12"
          moduleName="The BetDoc Gazette"
          purpose="The official operating procedure and testing manual: the daily routine from the morning signal to the TDS ledger, and the checklist an operator ticks before trusting the desk."
          steps={[
            "Read Parts I and II once: what BetDoc does, and the six steps of a betting day",
            "Run the live checks, then tick each Part III item as you verify it on the real app",
            "Print or save the notice as PDF to circulate it to authorised operators",
          ]}
          kpiCodes={["SLIP_SCORE", "EV", "KELLY_STAKE"]}
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-xs font-semibold uppercase tracking-widest text-stone-600">Operating procedure · {done} of {CHECKLIST.length} checks ticked in this browser</span>
          <button type="button" onClick={() => window.print()}
                  className="inline-flex items-center gap-2 rounded bg-[#A47C26] px-4 py-2 text-xs font-semibold uppercase tracking-wider text-white shadow transition hover:bg-[#8A6A1F]">
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">print</span>Print notice / save PDF
          </button>
        </div>
      </div>

      <article id="betdoc-manual" data-print-root="" aria-label="The BetDoc Gazette: operating procedure and testing manual"
               className="mx-auto max-w-4xl border-4 border-double border-[#C89B3C] bg-white p-8 font-serif shadow-2xl sm:p-12 print:max-w-none print:shadow-none">
        <header className="mb-8 border-b-2 border-[#C89B3C] pb-6 text-center">
          <div className="mb-3 flex justify-center"><BetdocLogo variant="mark" /></div>
          <h1 className="text-2xl font-bold uppercase tracking-wider text-stone-900 sm:text-3xl">The Gazette of BetDoc Quantitative Systems</h1>
          <p className="mt-1 text-xs font-semibold uppercase tracking-widest text-[#A47C26] sm:text-sm">Extraordinary · Published by authority of the architect</p>
          <p className="mt-2 font-mono text-xs text-stone-500">
            Ref. No. <strong className="text-stone-800">BD/GAZ/2026/G116-SOP</strong> · Issued <strong className="text-stone-800">23 October 2026</strong>
          </p>
        </header>

        <div className="mb-8 border border-l-4 border-stone-200 border-l-[#C89B3C] bg-[#FFFDF9] p-4 text-sm leading-relaxed text-stone-800">
          <strong>PUBLIC NOTICE.</strong> This is BetDoc's own <em>standard operating procedure and testing manual</em>, issued for the guidance of authorised
          operators and testers under the authority of <strong>Amit Ashok Kumar Patnaik</strong>, chief architect. It is a product manual, not a government
          publication, and nothing in it is a promise of profit.
        </div>

        <Part n="I" title="The charter (the Krishna-Arjuna law)">
          <p><strong className="text-stone-900">1. What BetDoc is.</strong> An analytical advisor and an honest ledger. BetDoc does <strong>not</strong> hold your money,
            does <strong>not</strong> take bank deposits, does <strong>not</strong> place bets or take cashouts at any bookmaker, and does not read your
            bookmaker account. The rupees in its vault are your own record of what you did.</p>
          <ul className="list-disc space-y-2 pl-6">
            <li><strong>BetDoc is Krishna, the strategist and accountant.</strong> ASHOKA prices the live board, ASHVATTHAMA (the Consultant) seals each verdict,
              KUMBHA sizes the stake, SHIVAJI guards the losses, VIDUR reads the news, and TODAR MAL keeps every rupee and the 30% TDS to the paisa.</li>
            <li><strong>You are Arjuna, the warrior and the owner of the money.</strong> Your capital stays in your own Parimatch or 1xBet account. You read
              the advice, place each ticket with your own hands, copy the booking code back to BetDoc, and take any cashout at the book yourself.</li>
          </ul>
        </Part>

        <Part n="II" title="The six steps of a betting day">
          <Step n={1} title="Read the morning signal (07:55 IST)">
            <p>At 07:55 IST TODAR MAL scores the day out of 100 from what it can measure: Parimatch and 1xBet's margins on today's match odds, how many
              top-tier fixtures are priced, rotation news and depleted squads from the Wire, and how many legs are priced at or above fair. What it cannot
              measure is named, never guessed.</p>
            <div className="space-y-1 border-l-2 border-stone-300 pl-4">
              <p><strong className="text-red-700">Below 60, REST DAY:</strong> a warning goes to your Telegram and SMS with the reasons. Do not bet today.</p>
              <p><strong className="text-amber-700">60 to 79, SELECTIVE:</strong> nothing is sent. Light stakes, anchors only.</p>
              <p><strong className="text-emerald-700">80 and above, PRIME HARVEST:</strong> the best anchors go to your phone. Open BetDoc.</p>
            </div>
            <p>When the signal goes out, that morning's 08:00 hype stays silent. Send <code>/today</code> to the bot at any time for the current score.</p>
          </Step>
          <Step n={2} title="Build the street-smart slip on The Oracle">
            <p>The Oracle → Bet builder → <strong>5(d) Street-smart &amp; splitter</strong>. Give a stake and a target return and choose Parimatch or 1xBet.
              BetDoc takes the safest legs on the live board that the book prices at or above fair, one per match, spread across Football, Cricket,
              Tennis and Basketball as the board allows, until the target is reached. Each leg shows its price, its fair probability and its EV.
              If the target is out of reach, it says so. Player markets (wickets, goalscorers) are not on the board and are never used.</p>
          </Step>
          <Step n={3} title="Split it into three tickets">
            <p>The splitter turns the slip into three nested tickets, safest legs first:</p>
            <div className="overflow-x-auto font-sans text-xs">
              <table className="w-full border-collapse border border-stone-300 bg-white text-left">
                <thead><tr className="bg-stone-100 text-stone-900">
                  <th className="border border-stone-300 p-2">Ticket</th><th className="border border-stone-300 p-2">Legs</th>
                  <th className="border border-stone-300 p-2">Stake</th><th className="border border-stone-300 p-2">Purpose</th>
                </tr></thead>
                <tbody>
                  <tr><td className="border border-stone-300 p-2 font-bold text-emerald-800">Slip A · Fortress</td><td className="border border-stone-300 p-2">the fewest anchors (3 to 5) whose odds return the whole session stake</td>
                    <td className="border border-stone-300 p-2">40%</td><td className="border border-stone-300 p-2">the capital shield: on only when its payout covers the session stake</td></tr>
                  <tr><td className="border border-stone-300 p-2 font-bold text-sky-800">Slip B · Growth</td><td className="border border-stone-300 p-2">about two thirds of the legs, up to 9</td>
                    <td className="border border-stone-300 p-2">40%</td><td className="border border-stone-300 p-2">compounding across sports</td></tr>
                  <tr><td className="border border-stone-300 p-2 font-bold text-purple-800">Slip C · Moonshot</td><td className="border border-stone-300 p-2">every leg</td>
                    <td className="border border-stone-300 p-2">20%</td><td className="border border-stone-300 p-2">the full upside</td></tr>
                </tbody>
              </table>
            </div>
            <p><strong className="text-stone-900">Read the shield honestly.</strong> Slip A's anchors are on all three tickets. If a leg only B or C carries
              loses, Slip A can still return the session; if one of Slip A's anchors loses, all three tickets lose. The page shows the four outcomes with
              their chances and how often the session gets its stake back.</p>
          </Step>
          <Step n={4} title="Place each ticket by hand and enter its booking code">
            <ol className="list-decimal space-y-1 pl-5">
              <li>Press <strong>Record the 3 tickets</strong>: they are saved as ready to place; no money is locked.</li>
              <li>Open the book from the ticket's link and place it there, at the stake and odds shown or at what the book gives.</li>
              <li>Copy the booking code the book issues (for example <code>5X7A9B</code>).</li>
              <li>Under <strong>Your system tickets</strong>, enter the code and press <strong>Lock stake &amp; confirm</strong>. A Bet-Slip ticket from the
                Consultant is confirmed the same way on <code>/bet-slip</code>.</li>
            </ol>
            <p>TODAR MAL then moves the stake from liquid cash to locked exposure, the ticket's legs are tracked for settlement, and the in-play watch
              starts. SHIVAJI refuses the lock when cash is short, the 15% daily stop-loss is latched, a cool-down runs, or the kill switch is on.</p>
          </Step>
          <Step n={5} title="Follow the in-play cashout advisor">
            <p>BetDoc never sees the book's cashout offer: you read it in the app and type it on the placed ticket. The advice compares it with the
              ticket's fair value (its live win probability when the board prices every open leg) and its stop-loss floor:</p>
            <ul className="list-disc space-y-1 pl-5">
              <li><strong className="text-red-700">STOP-LOSS BREACH:</strong> the offer is at or under the floor shown on the ticket (15% to 40% under the stake): cash out to keep the rest.</li>
              <li><strong className="text-emerald-700">CASHOUT ADVISED:</strong> the offer is at least 85% of fair value, or its profit reaches the target.</li>
              <li><strong>HOLD:</strong> fair value is worth more than the offer.</li>
            </ul>
            <p>The in-play watch re-prices placed slips every few seconds when the live board covers them and pages your phone on a stop-loss, a
              collapse in the win probability or a profit target. Record the cashout you take with <strong>Record cashout taken</strong>.</p>
          </Step>
          <Step n={6} title="Review the rupee ledger and the 30% TDS">
            <p>On a win or a cashout above the stake, 30% TDS is withheld on the net winnings (Section 115BBJ's rate, deducted by the bookmaker under
              Section 194BA), computed to the paisa:</p>
            <p className="rounded bg-stone-100 p-2 font-mono text-xs">Net winnings = max(0, payout − stake) · TDS = 30% × net winnings · Credited = payout − TDS</p>
            <p>A loss, or a cashout at or under the stake, carries ₹0.00 TDS. Example: ₹1,000 at 3.00 pays ₹3,000; net winnings ₹2,000; TDS ₹600; ₹2,400
              credited. The Vault's TDS certificate is BetDoc's own summary of the year; the official certificate (Form 16A) comes from the bookmaker,
              and the 4% cess is settled when the return is filed.</p>
          </Step>
        </Part>

        <Part n="III" title="Operator verification protocol">
          <p>Tick each item only after verifying it on the running application. The ticks stay in this browser.</p>
          <div className="space-y-3">
            {CHECKLIST.map((c, i) => (
              <label key={c.id} className="flex cursor-pointer items-start gap-3">
                <input type="checkbox" checked={!!checks[c.id]} onChange={() => toggle(c.id)} className="mt-1 size-4 accent-[#A47C26]" />
                <span><strong className="text-stone-900">Item {i + 1}: {c.title}.</strong> {c.how}</span>
              </label>
            ))}
          </div>
          <div className="rounded border border-stone-200 p-3 print:hidden">
            <button type="button" onClick={() => void runLive()} disabled={running}
                    className="rounded bg-stone-900 px-3 py-1.5 font-sans text-xs font-semibold text-white disabled:opacity-50">{running ? "Checking…" : "Run live checks"}</button>
            {live && (
              <ul className="mt-2 space-y-1 font-mono text-xs">
                {live.map((r) => <li key={r.label} className={r.ok ? "text-emerald-700" : "text-red-700"}>{r.ok ? "✓" : "✗"} {r.label}: {r.detail}</li>)}
              </ul>
            )}
          </div>
        </Part>

        <Part n="IV" title="What BetDoc does not promise">
          <ul className="list-disc space-y-1 pl-5">
            <li>No ticket, split or signal guarantees a win or the return of a stake; probabilities are the market consensus's, and legs of different matches are treated as independent.</li>
            <li>Every price comes from the live odds feed. When the feed is off, the board is empty and BetDoc builds nothing rather than inventing a slate.</li>
            <li>The morning signal, /today and the cashout pages reach your phone only through the Telegram and SMS channels configured in the Sentinel.</li>
            <li>BetDoc never logs in to, scrapes or acts on a bookmaker site; the booking code and the cashout amount are the ones you type.</li>
          </ul>
        </Part>

        <footer className="flex flex-col items-start justify-between gap-6 border-t-2 border-[#C89B3C] pt-6 text-xs text-stone-600 sm:flex-row sm:items-end">
          <div>
            <div className="font-bold uppercase text-stone-900">Issued by authority of</div>
            <div className="mt-0.5 text-base font-bold text-[#A47C26]">Amit Ashok Kumar Patnaik</div>
            <div>Chief Quantitative Architect · BetDoc</div>
            <div className="mt-1 text-stone-500">Developed for Amit Ashok Kumar Patnaik</div>
          </div>
          <div className="rounded border-2 border-[#C89B3C] bg-[#FFFDF9] p-3 sm:text-right">
            <div className="text-[10px] font-bold uppercase tracking-wider text-[#A47C26]">Gazette seal</div>
            <div className="mt-0.5 text-sm font-bold text-stone-900">STANDARD OPERATING PROCEDURE</div>
            <div className="text-[10px] text-stone-500">Group 116 · 23 October 2026</div>
          </div>
        </footer>
      </article>
    </div>
  );
};
