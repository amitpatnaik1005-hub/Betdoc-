/**
 * ASHOKA's desk on The Oracle page (Group 69).
 *
 * - Scorecard: today / this week / this month P&L, win rate, streak; an optional tax estimate.
 * - Vetted slips: the "1000%" gate's survivors, each with Parimatch and 1xBet views side by side, the
 *   leg checklist (odds, fair probability, why), the Kelly stake, odds freshness, Copy and "I placed this bet".
 * - Cashout & hedge: running multiples; type the bookmaker's offer, get HOLD / CASH OUT / HEDGE LEG.
 * - Trends: sharp steam parlays, AI hybrids, public traps (a public share only when it was measured).
 * - My bets: active with live match status, settled with results; the betting twin's strengths and leaks.
 * - Group 72: "Run the fortress" on any slip sends it through the Digital Twin's fortress (DigitalTwinCard).
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ApiError, apiClient } from "../../api/client";
import { formatDateTime, humanize } from "../../lib/format";
import {
  BOOK_LABEL,
  PLACED_BOOKMAKERS,
  SYSTEM_KINDS,
  placementLegs,
  rupees,
  useBets,
  useScorecard,
  useSlips,
  useTrending,
  useTwin,
  type CashoutAdvice,
  type PeriodStats,
  type PlacedBet,
  type Slip,
  type SlipsPayload,
} from "../../lib/oracle";
import { invalidate } from "../../lib/resource";
import { vetSlip, type TwinAudit } from "../../lib/twin";
import { toast } from "../../store/useToastStore";
import { Async, Button, EmptyState, Field, NumberInput, Panel, Pill, Segmented, Select, Stat, StatGrid, TextInput, Toggle, type Tone } from "../../ui/kit";
import { DigitalTwinCard } from "./DigitalTwinCard";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
const signedPct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(digits)}%`);
const tone = (value: string | number | null | undefined): "positive" | "negative" | "neutral" => {
  const n = Number(value);
  return !Number.isFinite(n) || n === 0 ? "neutral" : n > 0 ? "positive" : "negative";
};

/** Seconds since a moment, ticking once a second. */
const useAge = (base: number, since: string | undefined) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);
  if (!since) return base;
  return base + Math.max(0, (now - new Date(since).getTime()) / 1000);
};

// ---------------------------------------------------------------- scorecard
export const ScorecardStrip = () => {
  const [tax, setTax] = useState(false);
  const card = useScorecard(tax);
  const shown = (p: PeriodStats) => (tax && p.net_after_tax_inr !== undefined ? p.net_after_tax_inr : p.pnl_inr);
  return (
    <Panel
      title="Your P&L"
      icon="account_balance_wallet"
      className="lg:col-span-12"
      subtitle="bets you placed and told Ashoka about, settled from final scores"
      updatedAt={card.updatedAt}
      actions={<Toggle label="After estimated tax" checked={tax} onChange={setTax} />}
    >
      <Async resource={card} skeletonRows={2}>
        {(c) => (
          <div className="flex flex-col gap-4">
            <StatGrid cols={4}>
              <Stat label="Today" icon="today" value={rupees(shown(c.periods.today), true)} hint={`${c.periods.today.bets} settled · ${rupees(c.periods.today.staked_inr)} staked`} tone={tone(shown(c.periods.today))} />
              <Stat label="This week" icon="date_range" value={rupees(shown(c.periods.week), true)} hint={`${c.periods.week.bets} settled · ROI ${pct(c.periods.week.roi)}`} tone={tone(shown(c.periods.week))} />
              <Stat label="This month" icon="calendar_month" value={rupees(shown(c.periods.month), true)} hint={`${c.periods.month.bets} settled · ROI ${pct(c.periods.month.roi)}`} tone={tone(shown(c.periods.month))} />
              <Stat label="Win rate" icon="emoji_events" value={pct(c.periods.all_time.win_rate)} hint={`streak ${c.streak.label} · all-time ${rupees(shown(c.periods.all_time), true)}`} />
            </StatGrid>
            <p className="text-xs text-stone-500 dark:text-stone-400">
              {c.pending.bets} pending ({rupees(c.pending.staked_inr)} at stake) · all-time staked {rupees(c.periods.all_time.staked_inr)}, returned {rupees(c.periods.all_time.returned_inr)}
              {tax && c.tax_rate !== null && ` · net figures deduct an estimated ${Math.round(c.tax_rate * 100)}% on positive winnings (an estimate, not tax advice)`} · {c.timezone}
            </p>
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------- "I placed this bet"
const PlaceBetDialog = ({ slip, book, onClose }: { slip: Slip; book: string; onClose: () => void }) => {
  const titleId = useId();
  const view = slip.books.find((b) => b.bookmaker === book);
  const isSystem = SYSTEM_KINDS.includes(slip.kind);
  const fallbackOdds = view?.odds ?? (isSystem ? "" : slip.odds.toFixed(3));
  const placed = PLACED_BOOKMAKERS.find((b) => b.book === book) ?? PLACED_BOOKMAKERS[0];
  const [bookmaker, setBookmaker] = useState<string>(placed.value);
  const [other, setOther] = useState("");
  const [stake, setStake] = useState(Number(slip.stake_inr) > 0 ? slip.stake_inr : slip.reference_stake_inr);
  const [odds, setOdds] = useState(fallbackOdds);
  const [at, setAt] = useState(() => {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  });
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.querySelector<HTMLSelectElement>("select")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const legBook = PLACED_BOOKMAKERS.find((b) => b.value === bookmaker)?.book || book;
  const submit = async () => {
    setBusy(true);
    try {
      await apiClient.post("/oracle/bets", {
        slip_id: slip.slip_id,
        source: "ASHOKA",
        bookmaker,
        bookmaker_name: bookmaker === "OTHER" ? other : null,
        structure: slip.kind,
        stake_inr: stake,
        placed_odds: isSystem || !odds ? null : odds,
        placed_at: new Date(at).toISOString(),
        legs: placementLegs(slip, legBook),
      });
      toast.success("Bet recorded", "Ashoka settles it from the final scores");
      invalidate("oracle:bets", "oracle:pnl", "oracle:twin");
      onClose();
    } catch (err) {
      toast.error("Not recorded", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/40 p-4 sm:items-center" role="presentation" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-md rounded-3xl bg-white p-6 shadow-soft-lg dark:bg-stone-900 dark:ring-1 dark:ring-white/10">
        <h3 id={titleId} className="text-base font-semibold text-stone-900 dark:text-stone-100">I placed this bet</h3>
        <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">{slip.title} · {slip.legs.length} leg{slip.legs.length === 1 ? "" : "s"}. Enter what you actually placed.</p>
        <div className="mt-4 grid grid-cols-2 gap-3">
          <Field label="Bookmaker" className="col-span-2 sm:col-span-1">
            <Select value={bookmaker} onChange={(e) => setBookmaker(e.target.value)}>
              {PLACED_BOOKMAKERS.map((b) => (
                <option key={b.value} value={b.value}>
                  {b.label}
                </option>
              ))}
            </Select>
          </Field>
          {bookmaker === "OTHER" && (
            <Field label="Which bookmaker" className="col-span-2 sm:col-span-1">
              <TextInput value={other} onChange={(e) => setOther(e.target.value)} placeholder="e.g. Bet365" />
            </Field>
          )}
          <Field label="Stake (₹)" hint={isSystem ? "the total across every line" : undefined}>
            <NumberInput value={stake} min={1} onChange={(e) => setStake(e.target.value)} />
          </Field>
          {!isSystem && (
            <Field label="Odds you got">
              <NumberInput value={odds} min={1.01} step="0.01" onChange={(e) => setOdds(e.target.value)} />
            </Field>
          )}
          <Field label="Placed at" className="col-span-2">
            <TextInput type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
          </Field>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" icon="check" busy={busy} disabled={!(Number(stake) > 0) || (bookmaker === "OTHER" && !other.trim())} onClick={() => void submit()}>
            Record bet
          </Button>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------- vetted slips
const CHECK_LABEL: Record<string, string> = {
  independent: "Independent legs",
  joint_ev: "Joint EV ≥ bar",
  joint_probability: "Probability ≥ bar",
  models_agree: "Every model agrees",
  fresh_odds: "Fresh odds",
  consensus: "Market consensus",
};

const SlipCard = ({ initial, generatedAt, bankroll }: { initial: Slip; generatedAt: string; bankroll: number | null }) => {
  // A re-check holds until the next generation of slips arrives; then the fresh one wins.
  const [override, setOverride] = useState<{ slip: Slip; at: string; from: string } | null>(null);
  const current = override !== null && override.from === generatedAt ? override : null;
  const slip = current?.slip ?? initial;
  const since = current?.at ?? initial.rechecked_at ?? generatedAt;
  const tabs = useMemo(() => {
    const priority = ["parimatch", "1xbet"].filter((b) => slip.books.some((v) => v.bookmaker === b));
    const best = slip.comparison?.best && !priority.includes(slip.comparison.best) ? [slip.comparison.best] : [];
    return [...priority, ...best];
  }, [slip]);
  const [tab, setTab] = useState<string>(() => slip.comparison?.recommended ?? tabs[0] ?? slip.book);
  const [placing, setPlacing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [audit, setAudit] = useState<TwinAudit | null>(null);
  const [vetting, setVetting] = useState(false);
  const age = useAge(slip.odds_age_seconds, since);
  const view = slip.books.find((b) => b.bookmaker === tab);
  const sim = slip.simulation;
  const vetted = slip.tier === "VETTED";
  const recheck = async () => {
    setBusy(true);
    try {
      const fresh = await apiClient.post<Slip>("/oracle/slips/recheck", { leg_ids: slip.legs.map((l) => l.leg_id), kind: slip.kind });
      setOverride({ slip: fresh, at: fresh.rechecked_at ?? new Date().toISOString(), from: generatedAt });
      toast.success("Odds re-checked", fresh.tier === slip.tier ? `${fresh.tier.toLowerCase()} · joint EV ${signedPct(fresh.simulation.joint_ev)}` : `now ${fresh.tier.toLowerCase()}`);
    } catch (err) {
      toast.error("Re-check failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const fortress = async () => {
    setVetting(true);
    try {
      const result = await vetSlip(slip, bankroll);
      setAudit(result);
      invalidate("twin:audits");
      toast.success(result.is_vetted ? `Ultra-vetted: ${result.pillars_passed}/${result.pillars.length} pillars` : `${result.pillars_passed}/${result.pillars.length} pillars passed`, result.is_vetted ? "Sent to your phone" : result.rejection_reasons[0]);
    } catch (err) {
      toast.error("Fortress run failed", refusal(err));
    } finally {
      setVetting(false);
    }
  };
  const copy = () =>
    void navigator.clipboard
      ?.writeText(slip.quick_copy)
      .then(() => toast.success("Slip copied", "Paste it into WhatsApp, Telegram or your notes"))
      .catch(() => toast.error("Copy failed", "The browser refused clipboard access"));
  return (
    <article className="flex min-w-0 flex-col gap-4 rounded-3xl bg-stone-50 p-5 dark:bg-stone-800/40">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold text-stone-900 dark:text-stone-100">{slip.title}</h3>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">
            {slip.leagues.join(" + ")} · {sim.paths.toLocaleString("en-IN")} simulated paths
          </p>
        </div>
        <span title="Passed every gate: joint EV, true joint probability, independence, model agreement, fresh odds. Not a guarantee: the probability below is the honest one.">
          <Pill tone={vetted ? "good" : "warning"} icon={vetted ? "verified" : "balance"}>
            {slip.badge}
          </Pill>
        </span>
      </header>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">True joint probability</p>
          <p className="font-mono text-lg text-stone-900 dark:text-stone-100">{pct(sim.joint_probability)}</p>
          {slip.multiple && <p className="text-[10px] text-stone-400">models {pct(sim.probability_band[0], 0)}–{pct(sim.probability_band[1], 0)}</p>}
        </div>
        <div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">Joint EV</p>
          <p className={cx("font-mono text-lg", sim.joint_ev >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-600")}>{signedPct(sim.joint_ev)}</p>
          <p className="text-[10px] text-stone-400">± {pct(sim.joint_ev_se * 2)} (2 SE)</p>
        </div>
        <div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">Kelly stake</p>
          <p className="font-mono text-lg text-stone-900 dark:text-stone-100">{Number(slip.stake_inr) > 0 ? rupees(slip.stake_inr) : "—"}</p>
          <p className="text-[10px] text-stone-400">{pct(slip.stake_fraction, 2)} of bankroll</p>
        </div>
        <div>
          <p className="text-[11px] text-stone-500 dark:text-stone-400">Loses everything</p>
          <p className="font-mono text-lg text-stone-900 dark:text-stone-100">{pct(sim.p_total_loss)}</p>
          <p className="text-[10px] text-stone-400">of paths</p>
        </div>
      </div>

      {tabs.length > 0 && !SYSTEM_KINDS.includes(slip.kind) && (
        <Segmented size="sm" label="Bookmaker view" value={tab} onChange={setTab} options={tabs.map((b) => ({ value: b, label: `${BOOK_LABEL[b] ?? b} view` }))} />
      )}

      <ol className="flex flex-col gap-2">
        {slip.legs.map((leg, i) => {
          const line = view?.legs[i];
          const price = view ? line?.odds : String(leg.prices[slip.book] ?? "");
          return (
            <li key={leg.leg_id} className="flex gap-3 rounded-2xl bg-white p-3 dark:bg-stone-900/60">
              <span className="material-symbols-outlined mt-0.5 text-[18px] text-emerald-600 dark:text-emerald-400" aria-hidden>
                check_circle
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                  <p className="text-sm font-semibold text-stone-900 dark:text-stone-100">{leg.fixture}</p>
                  <p className="font-mono text-sm text-stone-900 dark:text-stone-100">{price ? `@ ${price}` : <span className="text-xs text-stone-400">not quoted</span>}</p>
                </div>
                <p className="text-sm text-stone-700 dark:text-stone-300">
                  {leg.label}
                  {line?.search_code && <span className="ml-2 rounded bg-stone-100 px-1.5 py-0.5 font-mono text-[10px] text-stone-500 dark:bg-stone-800 dark:text-stone-400">{line.search_code}</span>}
                </p>
                <p className="mt-0.5 text-[11px] text-stone-500 dark:text-stone-400">
                  fair {pct(leg.fair_probability)} · {leg.league ?? "—"} · {leg.kickoff ? formatDateTime(leg.kickoff) : "kickoff unknown"}
                </p>
                <p className="mt-0.5 text-[11px] text-stone-400 dark:text-stone-500">{leg.rationale}</p>
              </div>
            </li>
          );
        })}
      </ol>

      {view && (
        <p className="text-sm text-stone-700 dark:text-stone-300">
          {view.available ? (
            <>
              <strong className="font-mono">{view.odds}</strong> on {view.label}: {rupees(slip.reference_stake_inr)} returns <strong>{rupees(view.payout_inr)}</strong>
            </>
          ) : (
            <span className="text-stone-500">{view.note}</span>
          )}
        </p>
      )}
      {slip.comparison?.recommendation && <p className="rounded-2xl bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-500/10 dark:text-amber-100">{slip.comparison.recommendation}</p>}

      <div className="flex flex-wrap gap-1.5">
        {Object.entries(slip.verdict.checks).map(([name, ok]) => (
          <Pill key={name} tone={ok ? "good" : "warning"} icon={ok ? "check" : "priority_high"}>
            {CHECK_LABEL[name] ?? humanize(name)}
          </Pill>
        ))}
      </div>
      {slip.verdict.reasons.length > 0 && <p className="text-[11px] text-stone-500 dark:text-stone-400">{slip.verdict.reasons.join(" · ")}</p>}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-stone-200/70 pt-3 dark:border-stone-700/60">
        <span className={cx("inline-flex items-center gap-1.5 text-xs", age > 120 ? "text-amber-700 dark:text-amber-300" : "text-stone-500 dark:text-stone-400")} aria-live="polite">
          <span className={cx("size-2 rounded-full", age > 120 ? "bg-amber-500" : "animate-pulse bg-emerald-500")} aria-hidden />
          Odds verified {age < 90 ? `${Math.round(age)}s` : `${Math.round(age / 60)} min`} ago
        </span>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" icon="refresh" busy={busy} onClick={() => void recheck()}>
            Re-check odds
          </Button>
          <Button size="sm" icon="content_copy" onClick={copy}>
            Copy slip
          </Button>
          <Button size="sm" icon="shield_person" busy={vetting} onClick={() => void fortress()}>
            Run the fortress
          </Button>
          <Button size="sm" variant="primary" icon="task_alt" onClick={() => setPlacing(true)}>
            I placed this bet
          </Button>
        </div>
      </footer>
      {audit && <DigitalTwinCard audit={audit} />}
      {placing && <PlaceBetDialog slip={slip} book={tab} onClose={() => setPlacing(false)} />}
    </article>
  );
};

export const VettedSlips = ({ slips }: { slips: ReturnType<typeof useSlips> }) => {
  const [filter, setFilter] = useState<"ALL" | "VETTED" | "VALUE">("ALL");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      await apiClient.get<SlipsPayload>("/oracle/slips", { refresh: true });
      await slips.refresh();
    } catch (err) {
      toast.error("Refresh failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title="Vetted bet generator"
      icon="workspace_premium"
      className="lg:col-span-12"
      subtitle="Monte Carlo, the anti-correlation gate and the 1000% filter on every slip"
      updatedAt={slips.updatedAt}
      actions={
        <Button size="sm" variant="ghost" icon="refresh" busy={busy} onClick={() => void refresh()}>
          Re-scan market
        </Button>
      }
    >
      <Async resource={slips} skeletonRows={4}>
        {(p) => {
          const shown = p.slips.filter((s) => filter === "ALL" || s.tier === filter);
          return (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <Segmented
                  size="sm"
                  label="Tier"
                  value={filter}
                  onChange={setFilter}
                  options={[
                    { value: "ALL", label: `All (${p.slips.length})` },
                    { value: "VETTED", label: `Vetted (${p.slips.filter((s) => s.tier === "VETTED").length})` },
                    { value: "VALUE", label: `Value (${p.slips.filter((s) => s.tier === "VALUE").length})` },
                  ]}
                />
                <p className="text-[11px] text-stone-500 dark:text-stone-400">
                  {p.read.fixtures} fixtures · {p.read.legs} priced legs · {p.scanned} slips scanned · bar: EV ≥ {pct(p.thresholds.min_joint_ev)} and ≥ {pct(p.thresholds.min_joint_probability, 0)} likely
                  {p.bankroll_inr && ` · bankroll ${rupees(p.bankroll_inr)}`}
                </p>
              </div>
              {shown.length === 0 ? (
                <EmptyState
                  icon="search_off"
                  title="Nothing clears the bar right now"
                  detail={
                    p.read.legs === 0
                      ? "No priced legs: the odds feeds have nothing upcoming in Redis."
                      : `Every candidate failed a gate: ${Object.entries(p.rejected).map(([why, n]) => `${n}× ${why}`).join(", ") || "none scanned"}. That is the filter working.`
                  }
                />
              ) : (
                <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                  {shown.map((slip) => (
                    <SlipCard key={slip.slip_id} initial={slip} generatedAt={p.generated_at} bankroll={p.bankroll_inr ? Number(p.bankroll_inr) : null} />
                  ))}
                </div>
              )}
            </div>
          );
        }}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------- cashout & hedge
const ADVICE_TONE: Record<CashoutAdvice["advice"], Tone> = { HOLD: "info", CASH_OUT: "good", HEDGE_LEG: "accent" };

const CashoutRow = ({ bet }: { bet: PlacedBet }) => {
  const [offer, setOffer] = useState("");
  const [probabilities, setProbabilities] = useState<Record<number, string>>({});
  const [needs, setNeeds] = useState<number | null>(null);
  const [advice, setAdvice] = useState<CashoutAdvice | null>(null);
  const [busy, setBusy] = useState(false);
  const won = bet.legs.filter((l) => l.result !== "PENDING").length;
  const ask = async () => {
    setBusy(true);
    try {
      const probs = Object.fromEntries(Object.entries(probabilities).filter(([, v]) => v).map(([k, v]) => [k, Number(v) / 100]));
      setAdvice(await apiClient.post<CashoutAdvice>(`/oracle/bets/${bet.id}/cashout-advice`, { offer_inr: offer || null, probabilities: probs }));
      setNeeds(null);
    } catch (err) {
      const detail = err instanceof ApiError ? (err.detail as { reason?: string; position?: number } | undefined) : undefined;
      if (detail?.reason === "NO_LIVE_PRICE" && detail.position !== undefined) setNeeds(detail.position);
      toast.error("No advice yet", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const take = async () => {
    if (!offer) return;
    try {
      await apiClient.post(`/oracle/bets/${bet.id}/cashout`, { cashout_inr: offer });
      toast.success("Cashout recorded");
      invalidate("oracle:bets", "oracle:pnl", "oracle:twin");
    } catch (err) {
      toast.error("Not recorded", refusal(err));
    }
  };
  return (
    <li className="flex flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-semibold text-stone-900 dark:text-stone-100">
          {humanize(bet.structure)} · {rupees(bet.stake_inr)} @ {bet.placed_odds ?? "—"}
        </p>
        <p className="text-[11px] text-stone-500">
          {won}/{bet.legs.length} legs in · {bet.bookmaker_name ?? humanize(bet.bookmaker)}
        </p>
      </div>
      <ul className="flex flex-col gap-1 text-xs">
        {bet.legs.map((leg) => (
          <li key={leg.id} className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-stone-700 dark:text-stone-300">
              {leg.home} vs {leg.away}: {leg.market} {leg.selection} @ {leg.odds}
            </span>
            <span className="flex items-center gap-2">
              {needs === leg.position && (
                <NumberInput
                  aria-label="Current win probability %"
                  className="!w-24 !py-1 text-xs"
                  placeholder="win %"
                  value={probabilities[leg.position] ?? ""}
                  onChange={(e) => setProbabilities((p) => ({ ...p, [leg.position]: e.target.value }))}
                />
              )}
              <Pill tone={leg.result === "PENDING" ? (leg.match_status === "LIVE" ? "warning" : "neutral") : leg.result.includes("WON") ? "good" : leg.result === "VOID" ? "neutral" : "critical"}>
                {leg.result === "PENDING" ? humanize(leg.match_status) : `${humanize(leg.result)}${leg.score ? ` ${leg.score}` : ""}`}
              </Pill>
            </span>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Bookmaker's cashout offer (₹)">
          <NumberInput value={offer} min={0} placeholder="as shown on your slip" onChange={(e) => setOffer(e.target.value)} />
        </Field>
        <Button size="sm" icon="calculate" busy={busy} onClick={() => void ask()}>
          Advise
        </Button>
        {advice?.advice === "CASH_OUT" && offer && (
          <Button size="sm" variant="ghost" icon="payments" onClick={() => void take()}>
            I cashed out
          </Button>
        )}
      </div>
      {advice && (
        <div className="flex flex-col gap-2 rounded-2xl bg-white p-3 text-sm dark:bg-stone-900/60">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={ADVICE_TONE[advice.advice]} icon={advice.advice === "HOLD" ? "front_hand" : advice.advice === "CASH_OUT" ? "payments" : "shield"}>
              {advice.advice.replace("_", " ")}
            </Pill>
            <span className="text-xs text-stone-500 dark:text-stone-400">
              fair value {rupees(advice.fair_value_inr)} · offer {advice.offer_ratio !== null ? `${pct(advice.offer_ratio)} of fair` : "—"} · full payout {rupees(advice.potential_payout_inr)} at {pct(advice.win_probability)}
            </span>
          </div>
          {advice.hedge && (
            <p className="text-stone-700 dark:text-stone-300">
              {advice.hedge.instruction}. Locks <strong>{rupees(advice.hedge.locked_profit_inr, true)}</strong> whatever happens.
            </p>
          )}
          <p className="text-xs text-stone-500 dark:text-stone-400">{advice.reasons.join(" · ")}</p>
        </div>
      )}
    </li>
  );
};

export const CashoutPanel = () => {
  const bets = useBets("active");
  return (
    <Panel title="Cashout & hedge advisor" icon="shield" className="lg:col-span-6" subtitle="running multiples with a leg already in" updatedAt={bets.updatedAt}>
      <Async resource={bets} skeletonRows={2}>
        {(rows) => {
          const running = rows.filter((b) => ["DOUBLE", "TREBLE", "ACCUMULATOR"].includes(b.structure) && b.legs.some((l) => l.result !== "PENDING"));
          return running.length === 0 ? (
            <EmptyState icon="shield" title="No running multiples" detail="When a leg of a double, treble or accumulator you placed has won, it shows here for cashout and hedge advice." />
          ) : (
            <ul className="flex flex-col gap-3">
              {running.map((bet) => (
                <CashoutRow key={bet.id} bet={bet} />
              ))}
            </ul>
          );
        }}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------- trends
const TREND: Record<string, { tone: Tone; icon: string; label: string }> = {
  SHARP_STEAM: { tone: "good", icon: "bolt", label: "Sharp steam" },
  AI_HYBRID: { tone: "info", icon: "auto_awesome", label: "AI vetted hybrid" },
  PUBLIC_TRAP: { tone: "critical", icon: "warning", label: "Public trap" },
};

export const TrendingFeed = () => {
  const trends = useTrending();
  const [busy, setBusy] = useState(false);
  const rescan = async () => {
    setBusy(true);
    try {
      await apiClient.get("/oracle/trending", { refresh: true });
      await trends.refresh();
    } catch (err) {
      toast.error("Scan failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title="Trending & public traps"
      icon="local_fire_department"
      className="lg:col-span-6"
      subtitle="priced against the de-vigged market"
      updatedAt={trends.updatedAt}
      actions={
        <Button size="sm" variant="ghost" icon="refresh" busy={busy} onClick={() => void rescan()}>
          Rescan
        </Button>
      }
    >
      <Async resource={trends} skeletonRows={3} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="local_fire_department" title="No trends right now" detail="No steam, no trap, no hybrid worth showing in the current market." />}>
        {(rows) => (
          <ul className="flex flex-col gap-3">
            {rows.map((t) => {
              const meta = TREND[t.category ?? ""] ?? { tone: "neutral" as Tone, icon: "trending_up", label: "Trending" };
              return (
                <li key={t.id} className={cx("flex flex-col gap-2 rounded-2xl p-4", t.category === "PUBLIC_TRAP" ? "bg-rose-50 dark:bg-rose-500/10" : "bg-stone-50 dark:bg-stone-800/40")}>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-semibold text-stone-900 dark:text-stone-100">{t.title}</p>
                    <Pill tone={meta.tone} icon={meta.icon}>
                      {meta.label}
                    </Pill>
                  </div>
                  {t.warning && <p className="text-sm text-rose-800 dark:text-rose-200">{t.warning}</p>}
                  <ul className="flex flex-col gap-0.5 text-xs text-stone-600 dark:text-stone-300">
                    {t.legs.map((leg, i) => (
                      <li key={`${leg.match_id}-${i}`}>
                        {leg.fixture ?? leg.match_id} — {leg.label ?? leg.selection} @ {leg.odds}
                        {leg.fair_probability !== undefined && leg.fair_probability !== null && <span className="text-stone-400"> · fair {pct(leg.fair_probability)}</span>}
                      </li>
                    ))}
                  </ul>
                  <p className="text-[11px] text-stone-500 dark:text-stone-400">
                    total {t.total_odds.toFixed(2)} · true probability {pct(t.true_probability)} · true EV {t.true_ev_pct === null ? "—" : `${t.true_ev_pct >= 0 ? "+" : ""}${t.true_ev_pct.toFixed(1)}%`}
                    {t.public_share_pct !== null && ` · public share ${t.public_share_pct}% (${t.public_share_source})`}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

// ---------------------------------------------------------------- my bets & the twin
const STATUS_TONE: Record<string, Tone> = { PENDING: "neutral", WON: "good", HALF_WON: "good", VOID: "neutral", HALF_LOST: "warning", LOST: "critical", CASHED_OUT: "info" };

export const MyBets = () => {
  const [which, setWhich] = useState<"active" | "settled">("active");
  const bets = useBets(which);
  const remove = async (id: string) => {
    try {
      await apiClient.delete(`/oracle/bets/${id}`);
      invalidate("oracle:bets", "oracle:pnl");
    } catch (err) {
      toast.error("Not removed", refusal(err));
    }
  };
  return (
    <Panel
      title="My placed bets"
      icon="receipt_long"
      className="lg:col-span-8"
      updatedAt={bets.updatedAt}
      actions={<Segmented size="sm" label="Which bets" value={which} onChange={setWhich} options={[{ value: "active", label: "Active" }, { value: "settled", label: "Settled" }]} />}
    >
      <Async resource={bets} skeletonRows={3} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="receipt_long" title={which === "active" ? "No active bets" : "Nothing settled yet"} detail="Press “I placed this bet” on a slip after you place it." />}>
        {(rows) => (
          <ul className="flex max-h-[38rem] flex-col gap-3 overflow-y-auto pr-1">
            {rows.map((bet) => (
              <li key={bet.id} className="rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-semibold text-stone-900 dark:text-stone-100">
                    {humanize(bet.structure)} · {rupees(bet.stake_inr)}
                    {bet.placed_odds && ` @ ${bet.placed_odds}`} · {bet.bookmaker_name ?? humanize(bet.bookmaker)}
                  </p>
                  <div className="flex items-center gap-2">
                    {bet.pnl_inr !== null && <span className={cx("font-mono text-sm", Number(bet.pnl_inr) >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-600")}>{rupees(bet.pnl_inr, true)}</span>}
                    <Pill tone={STATUS_TONE[bet.status] ?? "neutral"}>{humanize(bet.status)}</Pill>
                    {bet.status === "PENDING" && (
                      <Button size="sm" variant="ghost" icon="delete" aria-label="Remove this entry" onClick={() => void remove(bet.id)}>
                        Remove
                      </Button>
                    )}
                  </div>
                </div>
                <p className="text-[11px] text-stone-500 dark:text-stone-400">placed {bet.placed_at ? formatDateTime(bet.placed_at) : "—"}{bet.settled_at && ` · settled ${formatDateTime(bet.settled_at)}`}</p>
                <ul className="mt-2 flex flex-col gap-1 text-xs">
                  {bet.legs.map((leg) => (
                    <li key={leg.id} className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-stone-700 dark:text-stone-300">
                        {leg.home} vs {leg.away}: {leg.market} {leg.selection} @ {leg.odds}
                      </span>
                      <Pill tone={leg.result === "PENDING" ? (leg.match_status === "LIVE" ? "warning" : "neutral") : (STATUS_TONE[leg.result] ?? "neutral")} icon={leg.match_status === "LIVE" ? "sports_soccer" : undefined}>
                        {leg.result === "PENDING" ? humanize(leg.match_status) : `${humanize(leg.result)}${leg.score ? ` · ${leg.score}` : ""}`}
                      </Pill>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

export const TwinPanel = () => {
  const twin = useTwin();
  return (
    <Panel title="Your betting twin" icon="psychology_alt" className="lg:col-span-4" subtitle="where you win, where you leak" updatedAt={twin.updatedAt}>
      <Async resource={twin} skeletonRows={3}>
        {(t) =>
          t.settled_bets === 0 ? (
            <EmptyState icon="psychology_alt" title="Not enough history" detail="Your twin builds from settled bets: strengths and leaks appear after a few in each segment." />
          ) : (
            <div className="flex flex-col gap-4 text-sm">
              <p className="text-xs text-stone-500 dark:text-stone-400">
                {t.settled_bets} settled · average odds {t.average_odds ?? "—"} · average stake {rupees(t.average_stake_inr)} · segments need {t.min_bets}+ bets
              </p>
              {(["strengths", "leaks"] as const).map((kind) => (
                <div key={kind}>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">{kind === "strengths" ? "Strengths" : "Leaks"}</h3>
                  {t[kind].length === 0 ? (
                    <p className="text-xs text-stone-400">None yet.</p>
                  ) : (
                    <ul className="flex flex-col gap-1">
                      {t[kind].slice(0, 5).map((row) => (
                        <li key={`${row.axis}-${row.segment}`} className="flex items-center justify-between gap-2">
                          <span className="text-stone-700 dark:text-stone-300">
                            {humanize(row.axis)}: {row.segment}
                          </span>
                          <span className={cx("font-mono text-xs", row.roi >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-600")}>
                            ROI {signedPct(row.roi)} · {row.bets} bets
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )
        }
      </Async>
    </Panel>
  );
};
