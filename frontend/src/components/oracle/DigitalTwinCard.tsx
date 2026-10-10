/**
 * ASHOKA's Digital Twin on The Oracle page (Group 72).
 *
 * - DigitalTwinCard: one 14-pillar audit. Every pillar with its verdict and reason (missing evidence shows as
 *   UNVERIFIED, never as a pass), the twin's stake, the legs with the bookmaker's own search codes, Copy, a
 *   price re-check just before placing, and "Add to my P&L ledger" with the booking code the bookmaker issued.
 * - TwinFortressPanel: recent audits, the in-play watches (win probability now vs at placing, fair value,
 *   the pullout call, the book's cashout offer as you read it) and the twin-placed bets' own scorecard.
 */
import { useState } from "react";
import { ApiError } from "../../api/client";
import { formatDateTime } from "../../lib/format";
import { BOOK_LABEL, PLACED_BOOKMAKERS, rupees } from "../../lib/oracle";
import { invalidate } from "../../lib/resource";
import {
  PILLAR_ICON,
  PILLAR_TONE,
  confirmAudit,
  placeFromAudit,
  setOffer,
  stopWatch,
  tickNow,
  useTwinAudits,
  useTwinLedger,
  useTwinMonitors,
  type ConfirmLeg,
  type ConfirmResult,
  type TwinAudit,
  type TwinMonitor,
} from "../../lib/twin";
import { toast } from "../../store/useToastStore";
import { Async, Button, EmptyState, Field, NumberInput, Panel, Pill, Segmented, Select, TextInput, Toggle } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
const signedPct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(digits)}%`);
const bookLabel = (book: string | null | undefined): string => (book ? BOOK_LABEL[book] ?? book : "—");

const Metric = ({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "good" | "bad" }) => (
  <div className="min-w-0 rounded-2xl bg-white p-3 dark:bg-stone-900/60">
    <p className="text-[11px] text-stone-500 dark:text-stone-400">{label}</p>
    <p className={cx("truncate font-mono text-base", tone === "good" ? "text-emerald-700 dark:text-emerald-300" : tone === "bad" ? "text-rose-600" : "text-stone-900 dark:text-stone-100")}>{value}</p>
    {hint && <p className="truncate text-[10px] text-stone-400">{hint}</p>}
  </div>
);

// ---------------------------------------------------------------- one audit
const PlaceForm = ({ audit }: { audit: TwinAudit }) => {
  const view = audit.slip.books?.find((b) => b.bookmaker === audit.bookmaker);
  const placed = PLACED_BOOKMAKERS.find((b) => b.book === audit.bookmaker) ?? PLACED_BOOKMAKERS[0];
  const [bookmaker, setBookmaker] = useState<string>(placed.value);
  const [stake, setStake] = useState(Number(audit.stake_inr) > 0 ? audit.stake_inr : "");
  const [odds, setOdds] = useState(view?.odds ?? audit.total_odds ?? "");
  const [code, setCode] = useState("");
  const [watch, setWatch] = useState(true);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const codeOk = code.trim() === "" || /^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$/.test(code.trim());
  const submit = async () => {
    setBusy(true);
    try {
      const res = await placeFromAudit(audit.id, { bookmaker, stake_inr: stake, placed_odds: odds || null, booking_code: code.trim() || null, watch });
      const watching = res.watch.started ? "watched in play" : res.watch.message ? `not watched: ${res.watch.message}` : "not watched";
      setDone(`${res.booking_code ? `${res.booking_code} · ` : ""}${watching}`);
      toast.success("Added to your P&L ledger", watching);
      invalidate("oracle:bets", "oracle:pnl", "oracle:twin", "twin:monitors", "twin:ledger");
    } catch (err) {
      toast.error("Not recorded", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  if (done)
    return (
      <p className="flex items-center gap-2 rounded-2xl bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-200">
        <span className="material-symbols-outlined text-[18px]" aria-hidden>
          task_alt
        </span>
        In your ledger · {done}
      </p>
    );
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Field label="Placed at">
        <Select value={bookmaker} onChange={(e) => setBookmaker(e.target.value)}>
          {PLACED_BOOKMAKERS.filter((b) => b.value !== "OTHER").map((b) => (
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Stake (₹)">
        <NumberInput value={stake} min={1} onChange={(e) => setStake(e.target.value)} />
      </Field>
      <Field label="Odds you got">
        <NumberInput value={odds} min={1.01} step="0.01" onChange={(e) => setOdds(e.target.value)} />
      </Field>
      <Field label="Booking code" hint={codeOk ? "as the bookmaker issued it" : "letters, digits, - or _"}>
        <TextInput value={code} maxLength={32} placeholder="optional" onChange={(e) => setCode(e.target.value)} />
      </Field>
      <div className="col-span-2 flex items-center sm:col-span-3">
        <Toggle checked={watch} onChange={setWatch} label="Watch it in play (pullout alerts to your phone)" />
      </div>
      <div className="col-span-2 flex justify-end sm:col-span-1">
        <Button variant="primary" icon="add_task" busy={busy} disabled={!(Number(stake) > 0) || !codeOk} onClick={() => void submit()}>
          Add to my P&L ledger
        </Button>
      </div>
    </div>
  );
};

const RecheckLine = ({ result }: { result: ConfirmResult }) => {
  const legs = (result.legs as ConfirmLeg[]).filter((l) => typeof l === "object");
  return (
    <p className={cx("rounded-2xl px-3 py-2 text-xs", result.status === "PASS" ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-200" : "bg-rose-50 text-rose-800 dark:bg-rose-500/10 dark:text-rose-200")}>
      <strong>{result.status === "PASS" ? "Still good to place" : "Do not place"}</strong> · {result.reason}
      {legs.length > 0 && ` · ${legs.map((l) => `${l.current_odds} (floor ${l.floor})`).join(", ")}`}
    </p>
  );
};

export const DigitalTwinCard = ({ audit, credit }: { audit: TwinAudit; credit?: string }) => {
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<ConfirmResult | null>(null);
  const [showAll, setShowAll] = useState(!audit.is_vetted);
  const view = audit.slip.books?.find((b) => b.bookmaker === audit.bookmaker);
  const failing = audit.pillars.filter((p) => p.status !== "PASS");
  const shownPillars = showAll ? audit.pillars : failing;
  const recheck = async () => {
    setBusy(true);
    try {
      setCheck(await confirmAudit(audit.id));
    } catch (err) {
      toast.error("Re-check failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const copy = () =>
    void navigator.clipboard
      ?.writeText(audit.slip.quick_copy)
      .then(() => toast.success("Slip copied", "Search each leg at the bookmaker, then add its booking code here"))
      .catch(() => toast.error("Copy failed", "The browser refused clipboard access"));
  return (
    <article className={cx("flex min-w-0 flex-col gap-4 rounded-3xl p-5 ring-1", audit.is_vetted ? "bg-emerald-50/40 ring-emerald-500/30 dark:bg-emerald-500/5" : "bg-stone-50 ring-stone-200 dark:bg-stone-800/40 dark:ring-stone-700/60")}>
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-stone-200/70 pb-3 dark:border-stone-700/60">
        <div className="flex min-w-0 items-center gap-2">
          <span className="material-symbols-outlined text-[22px] text-emerald-600 dark:text-emerald-400" aria-hidden>
            shield_person
          </span>
          <div className="min-w-0">
            <h3 className="truncate font-semibold text-stone-900 dark:text-stone-100">Ashoka Digital Twin · {audit.slip.title}</h3>
            <p className="text-[11px] text-stone-500 dark:text-stone-400">
              audited {formatDateTime(audit.created_at)} · {audit.leg_ids.length} leg{audit.leg_ids.length === 1 ? "" : "s"}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone={audit.is_vetted ? "good" : "warning"} icon={audit.is_vetted ? "verified" : "gpp_maybe"}>
            {audit.pillars_passed}/14 pillars · {audit.conviction_score.toFixed(0)}%
          </Pill>
          <Pill tone={audit.is_vetted ? "good" : "critical"}>{audit.is_vetted ? "Ultra-vetted" : "Not vetted"}</Pill>
        </div>
      </header>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        <Metric label="Bookmaker" value={bookLabel(audit.bookmaker)} />
        <Metric label="Total odds" value={audit.total_odds ?? "—"} />
        <Metric label="Quarter-Kelly stake" value={Number(audit.stake_inr) > 0 ? rupees(audit.stake_inr) : "—"} hint={`${pct(audit.kelly_fraction, 2)} of ${audit.bankroll_inr ? rupees(audit.bankroll_inr) : "bankroll"}`} />
        <Metric label="Edge over sharp" value={signedPct(audit.sharp_edge)} tone={audit.sharp_edge !== null && audit.sharp_edge > 0 ? "good" : undefined} hint="Shin de-vigged" />
        <Metric label="Joint EV · probability" value={signedPct(audit.joint_ev)} tone={(audit.joint_ev ?? 0) > 0 ? "good" : "bad"} hint={`${pct(audit.joint_probability)} likely`} />
      </div>

      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-semibold text-stone-500 dark:text-stone-400">{showAll ? "The 14 pillars" : failing.length ? `${failing.length} pillar${failing.length === 1 ? "" : "s"} not passed` : "Every pillar passed"}</p>
          <Button size="sm" variant="ghost" onClick={() => setShowAll((v) => !v)}>
            {showAll ? "Only what failed" : "Show all 14"}
          </Button>
        </div>
        <ul className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {shownPillars.map((p) => (
            <li key={p.number} className="flex min-w-0 gap-2 rounded-2xl bg-white p-2.5 dark:bg-stone-900/60">
              <Pill tone={PILLAR_TONE[p.status]} icon={PILLAR_ICON[p.status]}>
                {p.number}
              </Pill>
              <div className="min-w-0">
                <p className="text-xs font-semibold text-stone-800 dark:text-stone-200">
                  {p.title} <span className="font-normal text-stone-400">· {p.status.toLowerCase()}</span>
                </p>
                <p className="line-clamp-2 text-[11px] text-stone-500 dark:text-stone-400" title={p.reason}>
                  {p.reason}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <ol className="flex flex-col gap-2">
        {audit.slip.legs.map((leg, i) => {
          const line = view?.legs[i];
          return (
            <li key={leg.leg_id} className="flex flex-wrap items-baseline justify-between gap-x-3 rounded-2xl bg-white px-3 py-2 text-sm dark:bg-stone-900/60">
              <span className="min-w-0 text-stone-700 dark:text-stone-300">
                <strong className="text-stone-900 dark:text-stone-100">{leg.fixture}</strong> · {leg.label}
                {line?.search_code && <span className="ml-2 rounded bg-stone-100 px-1.5 py-0.5 font-mono text-[10px] text-stone-500 dark:bg-stone-800 dark:text-stone-400">{line.search_code}</span>}
              </span>
              <span className="font-mono text-emerald-700 dark:text-emerald-300">@ {line?.odds ?? leg.prices[audit.bookmaker ?? ""] ?? "—"}</span>
            </li>
          );
        })}
      </ol>

      {check && <RecheckLine result={check} />}
      {audit.is_vetted ? (
        <PlaceForm audit={audit} />
      ) : (
        <p className="text-[11px] text-stone-500 dark:text-stone-400">Not vetted: the twin stakes nothing until every enforced pillar passes. Unverified pillars clear when their evidence arrives.</p>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-stone-200/70 pt-3 dark:border-stone-700/60">
        <p className="text-[11px] text-stone-400 dark:text-stone-500">
          Developer: {credit ?? audit.developer_credit ?? "—"} · 14 pillars measure how much was checked, not a certainty
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" icon="content_copy" onClick={copy}>
            Copy slip
          </Button>
          <Button size="sm" variant="ghost" icon="price_check" busy={busy} onClick={() => void recheck()}>
            Re-check before placing
          </Button>
        </div>
      </footer>
    </article>
  );
};

// ---------------------------------------------------------------- the watches
const ADVICE_TONE = { HOLD: "info", CASH_OUT: "good", HEDGE_LEG: "accent" } as const;

const WatchRow = ({ m }: { m: TwinMonitor }) => {
  const [offer, setOfferText] = useState(m.cashout_offer_inr ?? "");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(ok);
      invalidate("twin:monitors");
    } catch (err) {
      toast.error("Not saved", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const drop = m.initial_win_prob - m.current_win_prob;
  return (
    <li className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-3 dark:bg-stone-800/40">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-stone-800 dark:text-stone-200">
          <strong>{m.bet ? rupees(m.bet.stake_inr) : "—"}</strong> at {m.bet ? bookLabel(m.bet.bookmaker.toLowerCase()) : "—"}
          {m.bet?.booking_code && <span className="ml-2 rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-amber-700 dark:bg-stone-900 dark:text-amber-300">{m.bet.booking_code}</span>}
        </p>
        <div className="flex flex-wrap gap-1.5">
          {m.last_advice && <Pill tone={ADVICE_TONE[m.last_advice]}>{m.last_advice.replace("_", " ")}</Pill>}
          <Pill tone={m.pullout_triggered ? "critical" : m.is_active ? "good" : "neutral"} icon={m.pullout_triggered ? "notifications_active" : m.is_active ? "visibility" : "visibility_off"}>
            {m.pullout_triggered ? (m.pullout_reason ?? "PULLOUT").replaceAll("_", " ").toLowerCase() : m.is_active ? "watching" : "stopped"}
          </Pill>
        </div>
      </div>
      <p className="text-xs text-stone-500 dark:text-stone-400">
        win {pct(m.initial_win_prob)} → <span className={drop > 0.1 ? "text-rose-600" : undefined}>{pct(m.current_win_prob)}</span> · fair value {rupees(m.fair_value_inr)} (peak {rupees(m.peak_fair_value_inr)}) · target +
        {pct(m.target_profit_pct, 0)} · {m.ticks} ticks{m.last_tick_at ? `, last ${formatDateTime(m.last_tick_at)}` : ""}
        {m.detail.status === "NO_LIVE_PRICE" && " · no live price right now"}
      </p>
      {m.detail.action && <p className="rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-500/10 dark:text-amber-100">{m.detail.action}</p>}
      {m.is_active && (
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Book's cashout offer (₹)" className="w-44">
            <NumberInput value={offer} min={0} onChange={(e) => setOfferText(e.target.value)} />
          </Field>
          <Button size="sm" busy={busy} onClick={() => void run(() => setOffer(m.id, offer.trim() === "" ? null : offer), "Offer saved")}>
            Save offer
          </Button>
          <Button size="sm" variant="ghost" icon="stop_circle" busy={busy} onClick={() => void run(() => stopWatch(m.id), "Stopped watching")}>
            Stop
          </Button>
        </div>
      )}
    </li>
  );
};

export const TwinFortressPanel = () => {
  const [which, setWhich] = useState<"VETTED" | "ALL">("VETTED");
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const audits = useTwinAudits(which === "VETTED");
  const watches = useTwinMonitors();
  const ledger = useTwinLedger();
  const tick = async () => {
    setBusy(true);
    try {
      const r = await tickNow();
      toast.success(r.ran ? `Re-priced ${r.priced} of ${r.watched}` : "A tick is already running", r.alerts.map((a) => a.action).join(" · ") || undefined);
      invalidate("twin:monitors");
    } catch (err) {
      toast.error("Re-price failed", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title="Digital twin · 14-pillar fortress"
      icon="shield_person"
      className="lg:col-span-12"
      subtitle="Run any slip through the 14 pillars from its card above; vetted slips, your placed twin bets and their in-play watch live here"
      updatedAt={audits.updatedAt}
      actions={
        <Button size="sm" variant="ghost" icon="sync" busy={busy} onClick={() => void tick()}>
          Re-price watches
        </Button>
      }
    >
      <div className="flex flex-col gap-5">
        <Async resource={ledger} skeletonRows={1}>
          {(l) => (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Metric label="Twin bets · pending" value={`${l.periods.all_time.bets} · ${l.pending.bets}`} hint={`${rupees(l.pending.staked_inr)} in play`} />
              <Metric label="P&L all time" value={rupees(l.periods.all_time.pnl_inr, true)} tone={Number(l.periods.all_time.pnl_inr) >= 0 ? "good" : "bad"} />
              <Metric label="ROI" value={signedPct(l.periods.all_time.roi)} />
              <Metric label="This month" value={rupees(l.periods.month.pnl_inr, true)} hint={`streak ${l.streak.label}`} />
            </div>
          )}
        </Async>

        <section className="flex flex-col gap-3">
          <Segmented size="sm" label="Audits" value={which} onChange={setWhich} options={[{ value: "VETTED", label: "Vetted" }, { value: "ALL", label: "All audits" }]} />
          <Async
            resource={audits}
            isEmpty={(d) => d.audits.length === 0}
            empty={<EmptyState icon="shield" title={which === "VETTED" ? "No slip has cleared all 14 pillars" : "No audits yet"} detail="Use “Run 14 pillars” on a slip above. Missing evidence (weather, lineups, injuries, referee …) keeps a pillar unverified, and an unverified pillar is never a pass." />}
          >
            {(d) => (
              <ul className="flex flex-col gap-2">
                {d.audits.map((a) => (
                  <li key={a.id} className="flex flex-col gap-2">
                    <button
                      type="button"
                      onClick={() => setOpen(open === a.id ? null : a.id)}
                      aria-expanded={open === a.id}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-stone-50 px-3 py-2 text-left text-sm hover:bg-stone-100 dark:bg-stone-800/40 dark:hover:bg-stone-800/70"
                    >
                      <span className="min-w-0 truncate text-stone-800 dark:text-stone-200">
                        {a.slip.title} · {bookLabel(a.bookmaker)} @ {a.total_odds ?? "—"} · {Number(a.stake_inr) > 0 ? rupees(a.stake_inr) : "no stake"}
                      </span>
                      <span className="flex items-center gap-2 text-[11px] text-stone-500">
                        <Pill tone={a.is_vetted ? "good" : "warning"}>{a.pillars_passed}/14</Pill>
                        {formatDateTime(a.created_at)}
                      </span>
                    </button>
                    {open === a.id && <DigitalTwinCard audit={a} credit={d.developer_credit} />}
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </section>

        <section className="flex flex-col gap-3">
          <p className="text-xs font-semibold text-stone-500 dark:text-stone-400">In-play watch</p>
          <Async resource={watches} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="visibility" title="Nothing watched" detail="A twin bet you add to your ledger is watched in play: a collapse, a good cashout or a hedge lock pages your phone." />}>
            {(d) => (
              <ul className="flex flex-col gap-2">
                {d.map((m) => (
                  <WatchRow key={m.id} m={m} />
                ))}
              </ul>
            )}
          </Async>
        </section>
      </div>
    </Panel>
  );
};
