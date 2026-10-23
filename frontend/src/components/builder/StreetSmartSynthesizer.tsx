/**
 * 5(d) The street-smart synthesizer and the system splitter (Group 115, points b.5, b.10, b.12 and b.16), in the skin of
 * the book the user bets from. Krishna advises, Arjuna places: BetDoc builds and measures, the user places every ticket
 * by hand at Parimatch or 1xBet and confirms it with the book's booking code.
 *
 * 1. TODAR MAL's market health for today: the score, the tier (rest, selective, prime), the measured deductions and
 *    what could not be measured.
 * 2. The synthesizer: a stake -> return target becomes a cross-sport slip of the board's anchors, each leg with its
 *    recipe; a target the anchors cannot reach is said so. The slip loads into the workbench (Slip Score, Consultant,
 *    the official Bet-Slip) or splits into three tickets.
 * 3. The splitter: Fortress, Growth and Moonshot, nested; the four exact outcomes, the chance the session returns its
 *    stake, and one ticket of every leg beside it. Recording them makes three ready-to-place tickets (nothing locked).
 * 4. The system tickets: confirm each placement with its booking code (the stake is locked then), cancel one not placed,
 *    and weigh the book's cashout offer once it plays.
 * Every figure is the server's. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { ApiError } from "../../api/client";
import type { Skin } from "../../lib/backtest_and_parlay";
import { inr } from "../../lib/slip_ornament";
import {
  cancelSystemTicket,
  confirmSystemTicket,
  type DeskBook,
  type MarketHealth,
  recordSystemTickets,
  splitSystemBets,
  type SplitSlipCard,
  type Synthesis,
  type SynthesizedLeg,
  synthesizeStreetSmart,
  type SystemSplit,
  type SystemTicket,
  useMarketHealth,
  useSystemTickets,
} from "../../lib/synthesizer";
import { useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Async } from "../../ui/kit";
import { InPlayDesk } from "../slip_ornament/HandcraftedPlacementDesk";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);
const signed = (p: number) => `${p >= 0 ? "+" : ""}${pct(p)}`;
const BOOKS: { value: DeskBook; label: string }[] = [{ value: "parimatch", label: "Parimatch" }, { value: "1xbet", label: "1xBet" }];
const SPORTS = ["Football", "Cricket", "Tennis", "Basketball"];
const PRESETS = [
  { stake: 250, back: 5000, label: "₹250 → ₹5,000 (20x)" },
  { stake: 500, back: 10000, label: "₹500 → ₹10,000 (20x)" },
  { stake: 100, back: 1000, label: "₹100 → ₹1,000 (10x)" },
  { stake: 250, back: 8750, label: "₹250 → ₹8,750 (35x)" },
];
const TIER: Record<MarketHealth["advisory_tier"], { label: string; icon: string; color: string }> = {
  REST_DAY: { label: "REST DAY", icon: "do_not_disturb_on", color: "#FB7185" },
  SELECTIVE: { label: "SELECTIVE", icon: "tune", color: "#FBBF24" },
  PRIME_HARVEST: { label: "PRIME HARVEST", icon: "local_fire_department", color: "#22C55E" },
};
const SLIP_COLOR: Record<SplitSlipCard["tier"], string> = { A: "#22C55E", B: "#38BDF8", C: "#C084FC" };
const fieldStyle = { background: "color-mix(in srgb, var(--wb-card) 80%, black)", color: "inherit" };
const fieldClass = "w-full rounded-xl border border-white/10 px-3 py-1.5 font-mono text-xs font-bold focus:outline-none focus:ring-2 focus:ring-[var(--wb-primary)]";

const refusal = (err: unknown): { code: string | null; message: string } =>
  err instanceof ApiError
    ? { code: typeof err.detail?.code === "string" ? err.detail.code : err.reason, message: err.message }
    : { code: null, message: "No answer from the server" };
const num = (raw: string): number | null => {
  const n = Number(raw);
  return raw.trim() !== "" && Number.isFinite(n) && n > 0 ? n : null;
};
const bookOf = (skin: Skin): DeskBook => (skin === "one_xbet" ? "1xbet" : "parimatch");

// ================================================================ 1. the day
const HealthStrip = ({ health }: { health: MarketHealth }) => {
  const tier = TIER[health.advisory_tier];
  return (
    <section aria-label="Daily market health" className="flex flex-col gap-2 rounded-2xl border p-3" style={{ background: "var(--wb-card)", borderColor: tier.color }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="material-symbols-outlined text-[26px]" style={{ color: tier.color }} aria-hidden="true">{tier.icon}</span>
          <span className="text-sm font-black">TODAR MAL · market health {health.health_score}/100</span>
          <span className="rounded-md px-2 py-0.5 font-mono text-[10px] font-black" style={{ background: tier.color, color: "#0C0A09" }}>{tier.label}</span>
        </span>
        <span className="font-mono text-[11px]" style={{ color: "var(--wb-muted)" }}>
          {health.active_fixtures_count} fixtures today · margin {health.average_bookmaker_margin_pct === null ? "not measured" : `${health.average_bookmaker_margin_pct}%`}
          {" "}· rotation {health.rotation_risks_count ?? "not measured"} · {health.anchors_count} anchors
        </span>
      </div>
      <p className="text-xs font-semibold">{health.advisory_message}</p>
      {health.deductions.length > 0 && (
        <ul className="flex flex-wrap gap-1.5 text-[10px]">
          {health.deductions.map((d) => <li key={d.code} className="rounded-md px-1.5 py-0.5" style={{ background: "color-mix(in srgb, #FB7185 18%, transparent)" }}>−{d.points} {d.reason}</li>)}
        </ul>
      )}
      {health.unmeasured.length > 0 && <p className="text-[10px]" style={{ color: "var(--wb-muted)" }}>Not measured: {health.unmeasured.join("; ")}.</p>}
      <p className="text-[10px]" style={{ color: "var(--wb-muted)" }}>
        {health.dispatched_at
          ? `Sent to the phone ${new Date(health.dispatched_at).toLocaleTimeString()}${health.dispatched_telegram ? " · Telegram ✓" : ""}${health.dispatched_sms ? " · SMS ✓" : ""}`
          : "A rest day or a prime day goes to Telegram and SMS once each morning; a selective day sends nothing."}
      </p>
    </section>
  );
};

// ================================================================ 2. the synthesized slip
const LegRow = ({ leg, i }: { leg: SynthesizedLeg; i: number }) => (
  <li className="flex flex-col gap-1 rounded-xl p-2.5 text-xs" style={{ background: "color-mix(in srgb, var(--wb-card) 70%, transparent)" }}>
    <div className="flex items-start justify-between gap-2">
      <span className="min-w-0">
        <span className="block truncate font-bold">{i}. {leg.fixture}</span>
        <span className="block truncate" style={{ color: "var(--wb-muted)" }}>{leg.sport}{leg.league ? ` · ${leg.league}` : ""} · {leg.label} [{leg.code}]</span>
      </span>
      <span className="shrink-0 text-right font-mono">
        <span className="block text-sm font-black" style={{ color: "var(--wb-primary)" }}>{leg.odds}</span>
        <span className="block text-[10px]" style={{ color: leg.ev >= 0 ? "#22C55E" : "#FB7185" }}>fair {pct(leg.fair_probability)} · EV {signed(leg.ev)}</span>
      </span>
    </div>
    <p className="text-[11px] leading-relaxed" style={{ color: "var(--wb-muted)" }}>{leg.recipe_rationale}</p>
  </li>
);

// ================================================================ 3. the split
const SlipCard = ({ slip, session }: { slip: SplitSlipCard; session: string }) => (
  <li className="flex flex-col gap-2 rounded-2xl border-2 p-3" style={{ background: "var(--wb-card)", borderColor: slip.tier === "A" && slip.covers_session_stake ? SLIP_COLOR.A : "transparent" }}>
    <div className="flex items-center justify-between gap-2">
      <span className="rounded-md px-2 py-0.5 font-mono text-[10px] font-black" style={{ background: SLIP_COLOR[slip.tier], color: "#0C0A09" }}>
        SLIP {slip.tier} · {slip.tier === "A" && !slip.covers_session_stake ? "FORTRESS" : slip.tag}
      </span>
      <span className="font-mono text-[11px]" style={{ color: "var(--wb-muted)" }}>{slip.leg_count} legs</span>
    </div>
    <h4 className="text-sm font-black">{slip.tier_name}</h4>
    {slip.tier === "A" && (
      <p className="text-[11px] font-semibold" style={{ color: slip.covers_session_stake ? SLIP_COLOR.A : "#FBBF24" }}>
        {slip.covers_session_stake ? `Capital shield on: alone it returns the ${inr(session)} session stake` : "Capital shield off: its odds do not cover the session stake"}
      </p>
    )}
    <ol className="flex flex-col gap-0.5 text-[11px]" style={{ color: "var(--wb-muted)" }}>
      {slip.legs.map((leg) => <li key={leg.leg_id} className="truncate">{leg.label} [{leg.code}] @ {leg.odds}</li>)}
    </ol>
    <div className="mt-auto grid grid-cols-3 gap-2 border-t border-white/10 pt-2 font-mono text-[11px]">
      <span><span className="block text-[9px] uppercase" style={{ color: "var(--wb-muted)" }}>Stake</span>{inr(slip.stake_inr)}</span>
      <span><span className="block text-[9px] uppercase" style={{ color: "var(--wb-muted)" }}>@ odds</span>{Number(slip.odds).toFixed(2)}x</span>
      <span className="text-right"><span className="block text-[9px] uppercase" style={{ color: "var(--wb-muted)" }}>Returns · wins</span>
        <strong style={{ color: SLIP_COLOR[slip.tier] }}>{inr(slip.projected_payout_inr)}</strong> · {pct(slip.win_probability)}</span>
    </div>
  </li>
);

const SplitView = ({ split, onRecord, recording }: { split: SystemSplit; onRecord: () => void; recording: boolean }) => (
  <section aria-label="System splitter" className="flex flex-col gap-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-1.5 text-sm font-black"><span className="material-symbols-outlined text-[18px]" style={{ color: "var(--wb-primary)" }} aria-hidden="true">shield</span>
        Three tickets · {inr(split.total_session_stake_inr)} session at {split.book_label}</h3>
      <button type="button" onClick={onRecord} disabled={recording} className="rounded-xl px-4 py-2 text-xs font-black disabled:opacity-50" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
        {recording ? "Recording…" : "Record the 3 tickets (nothing is locked)"}
      </button>
    </div>
    <p className="text-[11px] leading-relaxed" style={{ color: "var(--wb-muted)" }}>{split.formula_explanation}</p>
    <ul className="grid grid-cols-1 gap-3 md:grid-cols-3">
      {[split.slip_a_fortress, split.slip_b_growth, split.slip_c_moonshot].map((slip) => <SlipCard key={slip.tier} slip={slip} session={split.total_session_stake_inr} />)}
    </ul>
    <table className="w-full text-left text-[11px]">
      <caption className="pb-1 text-left text-xs font-bold">The four outcomes (the tickets are nested)</caption>
      <thead style={{ color: "var(--wb-muted)" }}><tr><th className="py-1 font-semibold">Outcome</th><th className="font-semibold">Chance</th><th className="font-semibold">Returns</th><th className="font-semibold">Net</th></tr></thead>
      <tbody>
        {split.scenarios.map((s) => (
          <tr key={s.code} className="border-t border-white/10">
            <td className="py-1 pr-2">{s.label}</td><td className="font-mono">{pct(s.probability)}</td><td className="font-mono">{inr(s.return_inr)}</td>
            <td className="font-mono" style={{ color: Number(s.net_inr) >= 0 ? "#22C55E" : "#FB7185" }}>{Number(s.net_inr) >= 0 ? "+" : ""}{inr(s.net_inr)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
      The session returns its stake {pct(split.recover_stake_probability)} of the time; expected return {inr(split.expected_session_return_inr)}. One ticket of all
      {" "}{split.single_ticket.legs} legs at the same stake: {Number(split.single_ticket.odds).toFixed(2)}x, wins {pct(split.single_ticket.probability)}, expected
      {" "}{inr(split.single_ticket.expected_return_inr)}.
    </p>
  </section>
);

// ================================================================ 4. the recorded tickets
const ConfirmForm = ({ ticket }: { ticket: SystemTicket }) => {
  const [code, setCode] = useState("");
  const [stake, setStake] = useState(String(ticket.recommended_stake));
  const [odds, setOdds] = useState(String(ticket.target_odds));
  const [override, setOverride] = useState(false);
  const [offerOverride, setOfferOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const stakeN = num(stake);
  const oddsN = num(odds);
  const ready = /^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$/.test(code) && stakeN !== null && oddsN !== null && oddsN > 1 && !busy;
  const confirm = async () => {
    if (!ready || stakeN === null || oddsN === null) return;
    setBusy(true);
    try {
      const placed = await confirmSystemTicket(ticket.ticket_reference, { booking_code: code, placed_stake_inr: stakeN, placed_odds: oddsN, override_odds_decay: override });
      toast.success("Stake locked · placement confirmed", `${placed.ticket.ticket_reference} · ${inr(placed.ticket.placed_stake)}${placed.monitor_note ? ` · ${placed.monitor_note}` : ""}`);
    } catch (err) {
      const r = refusal(err);
      setOfferOverride(r.code === "ODDS_DECAYED" || r.code === "STAKE_EXCEEDS_CEILING");
      toast.error("Placement not confirmed", r.message);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    try {
      await cancelSystemTicket(ticket.ticket_reference);
      toast.success("Ticket cancelled", `${ticket.ticket_reference}: nothing was locked.`);
    } catch (err) {
      toast.error("Not cancelled", refusal(err).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Booking code
          <input value={code} maxLength={32} placeholder="e.g. 5X7A9B" spellCheck={false} onChange={(e) => setCode(e.target.value.trim())} className={fieldClass} style={fieldStyle} /></label>
        <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Stake placed (₹)
          <input inputMode="decimal" value={stake} onChange={(e) => setStake(e.target.value)} className={fieldClass} style={fieldStyle} /></label>
        <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Odds the book gave
          <input inputMode="decimal" value={odds} onChange={(e) => setOdds(e.target.value)} className={fieldClass} style={fieldStyle} /></label>
        <div className="flex items-end gap-2">
          <button type="button" disabled={!ready} onClick={() => void confirm()} className="flex-1 rounded-xl px-3 py-1.5 text-xs font-black disabled:opacity-40" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
            Lock stake &amp; confirm
          </button>
          <button type="button" disabled={busy} onClick={() => void cancel()} className="rounded-xl border border-white/20 px-2.5 py-1.5 text-xs font-bold disabled:opacity-40" title="Cancel this ticket: nothing was locked">Cancel</button>
        </div>
      </div>
      {offerOverride && (
        <label className="flex items-center gap-2 text-[11px] font-semibold" style={{ color: "#FBBF24" }}>
          <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} className="size-4" />
          I placed it anyway: record it over SHIVAJI's refusal
        </label>
      )}
    </div>
  );
};

const TicketItem = ({ ticket }: { ticket: SystemTicket }) => {
  const label = ticket.bookmaker === "onexbet" ? "1xBet" : "Parimatch";
  return (
    <li className="flex flex-col gap-2 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
        <span className="font-mono font-black">{ticket.ticket_reference}</span>
        <span style={{ color: "var(--wb-muted)" }}>{ticket.legs.length} legs @ {Number(ticket.target_odds).toFixed(2)}x · {inr(ticket.recommended_stake)} · {ticket.status.replace(/_/g, " ").toLowerCase()}</span>
      </div>
      {ticket.status === "PENDING_PLACEMENT" ? (
        <ConfirmForm ticket={ticket} />
      ) : ticket.status === "CANCELLED" ? (
        <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>Cancelled before placement: nothing was locked.</p>
      ) : (
        <div className="rounded-xl bg-white p-3 text-stone-900"><InPlayDesk placement={ticket.placement} bookLabel={label} /></div>
      )}
    </li>
  );
};

// ================================================================ the mode
export const StreetSmartSynthesizer = ({ onLoad }: { onLoad: (title: string, legs: SynthesizedLeg[]) => void }) => {
  const skin = useParlaySlipStore((s) => s.skin);
  const health = useMarketHealth();
  const tickets = useSystemTickets();
  const [book, setBook] = useState<DeskBook>(() => bookOf(skin));
  const [stake, setStake] = useState("250");
  const [back, setBack] = useState("5000");
  const [maxLegs, setMaxLegs] = useState("15");
  const [sports, setSports] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [slip, setSlip] = useState<Synthesis | null>(null);
  const [session, setSession] = useState("250");
  const [split, setSplit] = useState<SystemSplit | null>(null);
  const [recording, setRecording] = useState(false);
  const stakeN = num(stake);
  const backN = num(back);
  const legsN = Math.round(Number(maxLegs));
  const ready = stakeN !== null && backN !== null && backN > stakeN && legsN >= 2 && legsN <= 20 && !busy;

  const run = async () => {
    if (!ready || stakeN === null || backN === null) return;
    setBusy(true);
    setSplit(null);
    try {
      const result = await synthesizeStreetSmart({ target_stake_inr: stakeN, target_return_inr: backN, bookmaker: book, max_legs: legsN,
                                                   preferred_sports: sports.length ? sports : undefined });
      setSlip(result);
      setSession(String(stakeN));
      if (result.legs.length === 0) toast.warning("Nothing to build from", result.shortfall ?? "No anchor on the board right now.");
      else toast.success(`${result.leg_count}-leg slip at ${Number(result.total_odds).toFixed(2)}x`, result.reached_target ? "The target is reached." : (result.shortfall ?? ""));
    } catch (err) {
      toast.error("Synthesis refused", refusal(err).message);
    } finally {
      setBusy(false);
    }
  };
  const runSplit = async () => {
    const sessionN = num(session);
    if (!slip || sessionN === null) return;
    setBusy(true);
    try {
      setSplit(await splitSystemBets(slip.legs.map((l) => l.leg_id), sessionN, slip.book));
    } catch (err) {
      toast.error("Split refused", refusal(err).message);
    } finally {
      setBusy(false);
    }
  };
  const record = async () => {
    const sessionN = num(session);
    if (!slip || sessionN === null) return;
    setRecording(true);
    try {
      const done = await recordSystemTickets(slip.legs.map((l) => l.leg_id), sessionN, slip.book);
      setSplit(done);
      toast.success("Three tickets recorded", done.message);
      window.setTimeout(() => document.getElementById("system-tickets")?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
    } catch (err) {
      toast.error("Not recorded", refusal(err).message);
    } finally {
      setRecording(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {health.data ? <HealthStrip health={health.data} /> : health.error ? (
        <p className="rounded-xl p-3 text-xs" style={{ background: "var(--wb-card)" }}>Market health unavailable: {health.error}</p>
      ) : null}

      <section aria-label="Synthesizer" className="flex flex-col gap-3 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
        <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
          The board's safest legs priced at or above fair, one per fixture, spread across sports, until the slip's odds reach your target. Probabilities come from the
          consensus of the books; nothing is filled in when the board is thin.
        </p>
        <div className="flex flex-wrap gap-2">
          {PRESETS.map((p) => {
            const on = stake === String(p.stake) && back === String(p.back);
            return (
              <button key={p.label} type="button" aria-pressed={on} onClick={() => { setStake(String(p.stake)); setBack(String(p.back)); }}
                      className="rounded-lg border px-3 py-1.5 font-mono text-xs" style={on ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)", borderColor: "var(--wb-primary)" } : { borderColor: "color-mix(in srgb, var(--wb-muted) 40%, transparent)" }}>
                {p.label}
              </button>
            );
          })}
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Stake (₹)<input inputMode="decimal" value={stake} onChange={(e) => setStake(e.target.value)} className={fieldClass} style={fieldStyle} /></label>
          <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Target return (₹)<input inputMode="decimal" value={back} onChange={(e) => setBack(e.target.value)} className={fieldClass} style={fieldStyle} /></label>
          <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Max legs<input inputMode="numeric" value={maxLegs} onChange={(e) => setMaxLegs(e.target.value)} className={fieldClass} style={fieldStyle} /></label>
          <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Book
            <select value={book} onChange={(e) => setBook(e.target.value as DeskBook)} className={fieldClass} style={fieldStyle}>
              {BOOKS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
            </select>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <span style={{ color: "var(--wb-muted)" }}>Sports:</span>
          {SPORTS.map((s) => {
            const on = sports.includes(s);
            return (
              <button key={s} type="button" aria-pressed={on} onClick={() => setSports((prev) => (on ? prev.filter((x) => x !== s) : [...prev, s]))}
                      className="rounded-full border px-2.5 py-0.5" style={on ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)", borderColor: "var(--wb-primary)" } : { borderColor: "color-mix(in srgb, var(--wb-muted) 40%, transparent)" }}>
                {s}
              </button>
            );
          })}
          <span style={{ color: "var(--wb-muted)" }}>{sports.length ? "only these" : "every sport on the board"}</span>
        </div>
        <button type="button" disabled={!ready} onClick={() => void run()} className="flex items-center justify-center gap-1.5 rounded-xl px-4 py-2.5 text-sm font-black disabled:opacity-40" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          <span className="material-symbols-outlined text-[18px]" aria-hidden="true">{busy ? "hourglass_top" : "auto_awesome"}</span>
          {busy ? "Reading the board…" : "Synthesize the street-smart slip"}
        </button>
      </section>

      {slip && (
        <section aria-label="Synthesized slip" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-black">{slip.leg_count}-leg slip at {slip.book_label} · {slip.sports_represented.join(" · ") || "no sport"}</h3>
            <span className="font-mono text-xs">
              <strong className="text-lg" style={{ color: "var(--wb-primary)" }}>{Number(slip.total_odds).toFixed(2)}x</strong>
              {" "}{inr(slip.target_stake_inr)} → {inr(slip.projected_return_inr)} · wins {pct(slip.joint_probability)} · EV {signed(slip.joint_ev)}
            </span>
          </div>
          {slip.shortfall && <p className="rounded-xl p-2.5 text-xs font-semibold" style={{ background: "color-mix(in srgb, #FBBF24 18%, transparent)" }}>{slip.shortfall}</p>}
          {slip.diversification_relaxed && <p className="text-[11px]" style={{ color: "#FBBF24" }}>The other sports ran out before the target: one sport fills more than its share of this slip.</p>}
          {slip.legs.length > 0 && (
            <>
              <ol className="grid grid-cols-1 gap-2 md:grid-cols-2">{slip.legs.map((leg, i) => <LegRow key={leg.leg_id} leg={leg} i={i + 1} />)}</ol>
              <p className="text-[10px]" style={{ color: "var(--wb-muted)" }}>{slip.independence_note}</p>
              <div className="flex flex-wrap items-end gap-2">
                <button type="button" onClick={() => onLoad(`Street-smart ${slip.leg_count}-leg slip`, slip.legs)} className="rounded-xl border px-4 py-2 text-xs font-black" style={{ borderColor: "var(--wb-primary)", color: "var(--wb-primary)" }}>
                  Load into the workbench (Slip Score → Consultant → Bet-Slip)
                </button>
                {slip.leg_count >= 5 && (
                  <>
                    <label className="flex flex-col gap-1 text-[10px] font-bold uppercase">Session stake (₹)
                      <input inputMode="decimal" value={session} onChange={(e) => setSession(e.target.value)} className={`${fieldClass} w-32`} style={fieldStyle} /></label>
                    <button type="button" disabled={busy || num(session) === null} onClick={() => void runSplit()} className="rounded-xl px-4 py-2 text-xs font-black disabled:opacity-40" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
                      Split into Fortress, Growth &amp; Moonshot
                    </button>
                  </>
                )}
              </div>
            </>
          )}
        </section>
      )}

      {split && <SplitView split={split} onRecord={() => void record()} recording={recording} />}

      <section id="system-tickets" aria-label="System tickets" className="flex flex-col gap-2">
        <h3 className="text-sm font-black">Your system tickets</h3>
        <Async resource={tickets} isEmpty={(d) => d.tickets.length === 0}
               empty={<p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>None recorded yet. Split a slip and record its three tickets; each is placed by hand, then confirmed here with its booking code.</p>}>
          {(d) => <ul className="flex flex-col gap-2">{d.tickets.map((t) => <TicketItem key={t.id} ticket={t} />)}</ul>}
        </Async>
      </section>
    </div>
  );
};
