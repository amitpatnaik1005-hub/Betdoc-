/**
 * The manual parlay workbench (Group 77) on The Oracle. Developer: Amit Ashok Kumar Patnaik.
 *
 * - Chameleon skin: the workbench dresses as the account you bet from (Parimatch, 1xBet, Stake, BetDoc). The skin
 *   also decides where the parlay is priced: its book's own prices, and the margin shown is that book's measured
 *   overround (BetDoc: the best of the retail books). Administrators see the account's balance from the Vault.
 * - Match KPI cards across nine sports: each market's model probability, every book's price, the best one, its
 *   EV, sharp steam and the book's overround. Click a selection or drag it (or a whole card, for its best-EV
 *   selection) into the slip.
 * - The slip: the structures its legs allow (straight multiples, Trixie to Goliath), the stop-loss the shield will
 *   hold (15% to 40% of the stake), and "Inspect parlay": the 15-pillar fortress at the skin's book, rated 0-100.
 * - Place it at the book, then record it here: Ashoka's ledger takes it, the stop-loss shield watches it live.
 */
import { type CSSProperties, type DragEvent, useState } from "react";
import { ApiError } from "../../api/client";
import { ParimatchPriceEntry } from "./ParimatchPriceEntry";
import { formatDateTime, formatINR } from "../../lib/format";
import {
  autoFixSlip, BOOKMAKER_THEMES, emergencyCashout, inspectParlay, missingParimatch, STRUCTURES, submitParlay, TIER_STYLE, useAccounts, useBoard, useShields, useShieldStream,
  type FixtureCard, type Inspection, type PillarRow, type Shield, type Skin,
} from "../../lib/backtest_and_parlay";
import { invalidate } from "../../lib/resource";
import { payingBook, priceAt, type SlipLeg, slipSignature, toSlipLeg, useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Async, Button, EmptyState, Field, Meter, NumberInput, Panel, Pill, Select, TextInput } from "../../ui/kit";
import { boardTargets } from "../../lib/prediction";
import { SlipScoreMeter } from "../builder/SlipScoreMeter";
import { ScoreBackedLegs } from "../prediction/ScoreBackedLegs";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
const DRAG_TYPE = "application/x-betdoc-leg";

const Radar = ({ rows, color }: { rows: PillarRow[]; color: string }) => {
  const size = 180;
  const c = size / 2;
  const r = c - 18;
  const point = (i: number, v: number) => {
    const angle = (Math.PI * 2 * i) / rows.length - Math.PI / 2;
    return [c + Math.cos(angle) * r * v, c + Math.sin(angle) * r * v];
  };
  const shape = rows.map((row, i) => point(i, row.credit).join(",")).join(" ");
  return (
    <svg viewBox={`0 0 ${size} ${size}`} className="h-44 w-44 shrink-0" role="img" aria-label="pillar credits">
      {[0.25, 0.5, 0.75, 1].map((ring) => (
        <polygon key={ring} points={rows.map((_, i) => point(i, ring).join(",")).join(" ")} fill="none" stroke="currentColor" strokeOpacity={0.15} />
      ))}
      {rows.map((row, i) => {
        const [x, y] = point(i, 1.12);
        return <text key={row.number} x={x} y={y} fontSize="8" textAnchor="middle" dominantBaseline="middle" fill="currentColor" opacity={0.6}>{row.number}</text>;
      })}
      <polygon points={shape} fill={color} fillOpacity={0.25} stroke={color} strokeWidth={1.5} />
    </svg>
  );
};

const MatchCard = ({ card, book, onPick, onScores }: { card: FixtureCard; book: string | null; onPick: (leg: SlipLeg) => void; onScores: (() => void) | null }) => {
  const [open, setOpen] = useState(false);
  const shown = open ? card.markets : card.markets.slice(0, 1);
  const best = card.markets.flatMap((m) => m.selections.map((s) => ({ m, s }))).filter((x) => x.s.ev !== null).sort((a, b) => (b.s.ev ?? 0) - (a.s.ev ?? 0))[0];
  const drag = (e: DragEvent, leg: SlipLeg) => {
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(leg));
    e.dataTransfer.effectAllowed = "copy";
  };
  return (
    <li
      draggable={best !== undefined}
      onDragStart={(e) => best && drag(e, toSlipLeg(card, best.m.market, best.s))}
      className="flex min-w-0 flex-col gap-2 rounded-2xl p-3"
      style={{ background: "var(--wb-card)" }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{card.home} v {card.away}</p>
          <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
            {card.league ?? card.sport ?? card.sport_key} · {card.in_play ? `in play · ${card.minutes_since_kickoff}′ since kickoff` : card.kickoff ? formatDateTime(card.kickoff) : "—"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {onScores && (
            <button type="button" onClick={onScores} title="Score prediction: the scorelines this board implies, and the legs each one backs"
                    className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                    style={{ border: "1px solid var(--wb-primary)", color: "var(--wb-primary)" }}>
              <span className="material-symbols-outlined text-[13px]" aria-hidden="true">scoreboard</span>Scores
            </button>
          )}
          {card.in_play && <span className="rounded-full px-2 py-0.5 text-[10px] font-bold" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>LIVE</span>}
        </div>
      </div>
      {shown.map((m) => {
        const margin = book ? m.margins.by_book[book] : m.margins.best_price;
        return (
          <div key={m.market} className="flex flex-col gap-1">
            <div className="flex items-baseline justify-between text-[11px]" style={{ color: "var(--wb-muted)" }}>
              <span>{m.market}</span>
              <span>margin {margin === undefined || margin === null ? "—" : pct(margin, 1)}</span>
            </div>
            <div className="grid grid-cols-3 gap-1">
              {m.selections.map((s) => {
                const price = book ? s.prices[book] : s.best?.odds;
                const leg = toSlipLeg(card, m.market, s);
                return (
                  <button
                    key={s.leg_id}
                    type="button"
                    draggable
                    onDragStart={(e) => {
                      e.stopPropagation();
                      drag(e, leg);
                    }}
                    onClick={() => onPick(leg)}
                    disabled={price === undefined}
                    title={`${s.label}: models ${Object.entries(s.models).map(([k, v]) => `${k} ${pct(v, 0)}`).join(", ")}`}
                    className="flex flex-col items-center rounded-xl px-1.5 py-1 text-[11px] transition disabled:opacity-40"
                    style={{ border: "1px solid color-mix(in srgb, var(--wb-primary) 35%, transparent)" }}
                  >
                    <span className="truncate font-medium">{s.selection}</span>
                    <span className="font-mono text-sm font-bold" style={{ color: "var(--wb-primary)" }}>{price ?? "—"}</span>
                    <span style={{ color: "var(--wb-muted)" }}>{pct(s.fair_probability, 0)}{s.ev !== null ? ` · EV ${s.ev >= 0 ? "+" : ""}${(s.ev * 100).toFixed(1)}%` : ""}</span>
                    {s.steam && <span className="text-[9px] font-bold text-amber-400">STEAM</span>}
                  </button>
                );
              })}
            </div>
            {book === "parimatch" && missingParimatch(m) && <ParimatchPriceEntry card={card} market={m} />}
          </div>
        );
      })}
      <div className="flex items-center justify-between text-[11px]" style={{ color: "var(--wb-muted)" }}>
        <span>{card.max_stake_inr && book && card.max_stake_inr[book] ? `max stake ${formatINR(Number(card.max_stake_inr[book]))}` : "liquidity: not reported"}</span>
        {card.markets.length > 1 && (
          <button type="button" className="underline" onClick={() => setOpen((v) => !v)}>
            {open ? "fewer markets" : `${card.markets.length - 1} more markets`}
          </button>
        )}
      </div>
    </li>
  );
};

const ShieldRow = ({ shield, live }: { shield: Shield; live: Shield["frame"] | undefined }) => {
  const f = live ?? shield.frame;
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const floor = Number(f.floor_inr ?? 0);
  const value = Number(f.cashout_offer_inr ?? f.fair_value_inr ?? f.stake_inr);
  const stake = Number(f.stake_inr);
  const ticket = f.ticket ?? shield.detail.cashout_ticket ?? null;
  const settled = shield.bet && shield.bet.status !== "PENDING";
  const act = async (record: boolean) => {
    setBusy(true);
    try {
      const r = await emergencyCashout(shield.id, record ? amount : undefined);
      toast.success(r.recorded ? `Cashout of ${formatINR(Number(r.salvaged_inr))} recorded` : "Cashout ticket issued", r.recorded ? "The bet is settled at what you received" : "Take it at the book, then record the amount");
      invalidate("parlay:shields");
    } catch (err) {
      toast.error("Refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-col gap-2 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="font-semibold">{f.bookmaker} · stake {formatINR(stake)}{f.booking_code ? ` · ${f.booking_code}` : ""}</span>
        <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
          {shield.is_active ? "watching" : shield.pullout_reason ? shield.pullout_reason.replaceAll("_", " ").toLowerCase() : "closed"} {f.at ? `· ${formatDateTime(f.at)}` : ""}
        </span>
      </div>
      <div>
        <div className="mb-1 flex justify-between text-[11px]" style={{ color: "var(--wb-muted)" }}>
          <span>win {pct(f.initial_win_prob)} → {pct(f.current_win_prob)}</span>
          <span>{f.cashout_offer_inr ? "offer" : "fair value"} {formatINR(value)} · floor {formatINR(floor)} ({pct(f.stop_loss_pct, 0)} stop)</span>
        </div>
        <Meter value={stake > 0 ? Math.min(value / (stake * 2), 1) : 0} tone={value <= floor ? "critical" : value < stake ? "warning" : "good"} label="cashout value against the stake" />
      </div>
      {ticket && (
        <div className="rounded-xl p-2 text-[11px] leading-relaxed" style={{ border: "1px solid var(--wb-primary)" }}>
          <p className="font-semibold">{ticket.reason}</p>
          <ol className="ml-4 list-decimal">{ticket.instructions.map((s) => <li key={s}>{s}</li>)}</ol>
          {ticket.caveats.map((c) => <p key={c} style={{ color: "var(--wb-muted)" }}>{c}</p>)}
        </div>
      )}
      {shield.detail.salvaged_inr && <p className="text-[11px]">Cashed out: {formatINR(Number(shield.detail.salvaged_inr))} of {formatINR(stake)}.</p>}
      {!settled && (
        <div className="flex flex-wrap items-end gap-2">
          {!ticket && <Button size="sm" variant="secondary" icon="emergency" busy={busy} onClick={() => void act(false)}>Emergency cashout</Button>}
          <Field label="Cashout received (₹)" className="w-40">
            <NumberInput min="0" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </Field>
          <Button size="sm" icon="task_alt" busy={busy} disabled={amount.trim() === "" || Number(amount) < 0} onClick={() => void act(true)}>Record cashout</Button>
        </div>
      )}
    </li>
  );
};

/** ``embedded``: inside the Bet Builder (Group 104), which frames it; standalone it is its own panel. */
export const ManualParlayWorkbench = ({ embedded = false }: { embedded?: boolean }) => {
  // Group 103: the slip and the skin are The Oracle's (shared with the match board)
  const skin = useParlaySlipStore((s) => s.skin);
  const setSkin = useParlaySlipStore((s) => s.setSkin);
  const slip = useParlaySlipStore((s) => s.legs);
  const addLeg = useParlaySlipStore((s) => s.add);
  const removeLeg = useParlaySlipStore((s) => s.remove);
  const replaceLeg = useParlaySlipStore((s) => s.replace);
  const clearSlip = useParlaySlipStore((s) => s.clear);
  const [sport, setSport] = useState<string | null>(null);
  const [kind, setKind] = useState<string | null>(null);
  const [stopLoss, setStopLoss] = useState(0.25);
  // An inspection holds only for the exact legs, structure and skin it rated
  const [inspected, setInspected] = useState<{ sig: string; result: Inspection } | null>(null);
  const [busy, setBusy] = useState<"inspect" | "submit" | "fix" | null>(null);
  const [placed, setPlaced] = useState({ stake: "", odds: "", code: "" });
  const [over, setOver] = useState(false);
  const [scoresId, setScoresId] = useState<string | null>(null);
  const theme = BOOKMAKER_THEMES[skin];
  const board = useBoard(sport);
  // the live card (the board refreshes): its latest prices back the score view
  const scoresFor = board.data?.fixtures.find((f) => f.fixture_id === scoresId) ?? null;
  const accounts = useAccounts();
  const shields = useShields();
  const stream = useShieldStream();

  const structures = STRUCTURES.filter((s) => slip.length >= s.legs[0] && slip.length <= s.legs[1]);
  const chosen = kind && structures.some((s) => s.kind === kind) ? kind : structures[0]?.kind ?? null;
  const prices = slip.map((l) => priceAt(l, theme.book));
  const combined = prices.every((p) => p !== null) && slip.length ? prices.reduce<number>((a, p) => a * (p as number), 1) : null;
  const balance = theme.book ? accounts.data?.accounts?.[theme.book] : undefined;
  const signature = slipSignature(slip, skin, chosen);
  const inspection = inspected?.sig === signature ? inspected.result : null;

  const add = (leg: SlipLeg) => addLeg(leg);
  const drop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const raw = e.dataTransfer.getData(DRAG_TYPE);
    if (raw) add(JSON.parse(raw) as SlipLeg);
  };
  const inspect = async () => {
    setBusy("inspect");
    try {
      const r = await inspectParlay({ leg_ids: slip.map((l) => l.leg_id), kind: chosen, skin });
      setInspected({ sig: signature, result: r });
      setStopLoss(r.stop_loss.recommended_pct);
      setPlaced((p) => ({ ...p, odds: r.audit.total_odds ?? p.odds, stake: Number(r.audit.stake_inr) > 0 ? r.audit.stake_inr : p.stake }));
    } catch (err) {
      toast.error("Inspection refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  // Group 104: the 1-click auto-fix (the offending leg swapped for the quoted alternative the fortress rates highest)
  const autoFix = async () => {
    setBusy("fix");
    try {
      const r = await autoFixSlip({ leg_ids: slip.map((l) => l.leg_id), kind: chosen, skin });
      if (r.improved && r.replaced && r.replacement && r.inspection) {
        const fresh: SlipLeg = { leg_id: r.replacement.leg_id, fixture: r.replacement.fixture, label: r.replacement.label, market: r.replacement.market,
                                 prices: r.replacement.prices, fair_probability: r.replacement.fair_probability };
        const next = slip.map((l) => (l.leg_id === r.replaced?.leg_id ? fresh : l));
        replaceLeg(r.replaced.leg_id, fresh);
        setInspected({ sig: slipSignature(next, skin, chosen), result: r.inspection });
        setStopLoss(r.inspection.stop_loss.recommended_pct);
        setPlaced((p) => ({ ...p, odds: r.inspection?.audit.total_odds ?? p.odds, stake: Number(r.inspection?.audit.stake_inr) > 0 ? (r.inspection?.audit.stake_inr ?? p.stake) : p.stake }));
        toast.success(`Slip score ${Math.round(r.before.score)} → ${Math.round(r.after?.score ?? r.before.score)}`,
                      `Swapped ${r.replaced.label} (${r.replaced.reason}) for ${r.replacement.label}, the best of ${r.tried.length} tried.`);
      } else {
        toast.info("Nothing swapped", r.message ?? "No alternative rates higher.");
      }
    } catch (err) {
      toast.error("Auto-fix refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  const submit = async () => {
    if (!inspection) return;
    setBusy("submit");
    try {
      const r = await submitParlay({ audit_id: inspection.audit.id, skin, stake_inr: placed.stake, placed_odds: placed.odds || undefined, booking_code: placed.code || undefined, stop_loss_pct: stopLoss });
      toast.success("Recorded in your ledger", r.shield.armed ? `Stop-loss shield armed at ${pct(stopLoss, 0)}` : (r.shield.message ?? "The shield is not watching this slip"));
      clearSlip();
      setInspected(null);
      invalidate("parlay:shields");
    } catch (err) {
      toast.error("Not recorded", refusal(err));
    } finally {
      setBusy(null);
    }
  };

  const style = { "--wb-primary": theme.primary, "--wb-on-primary": theme.onPrimary, "--wb-card": theme.card, "--wb-muted": theme.muted, background: theme.surface, color: theme.text, fontFamily: theme.font } as CSSProperties;
  const tier = inspection ? TIER_STYLE[inspection.rating.tier] : null;
  const body = (
      <div id="parlay-workbench" className="flex scroll-mt-24 flex-col gap-4 rounded-3xl p-4 transition-colors duration-300" style={style}>
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Betting account">
            {(Object.keys(BOOKMAKER_THEMES) as Skin[]).map((key) => {
              const t = BOOKMAKER_THEMES[key];
              return (
                <button key={key} type="button" role="radio" aria-checked={skin === key} onClick={() => setSkin(key)}
                        className="rounded-full px-3 py-1 text-xs font-bold transition"
                        style={skin === key ? { background: t.primary, color: t.onPrimary } : { border: `1px solid ${t.primary}`, color: t.primary }}>
                  {t.label}
                </button>
              );
            })}
          </div>
          <p className="text-[11px]" style={{ color: theme.muted }}>
            {theme.book
              ? balance
                ? `${theme.label} balance (as recorded in the Vault): ${Object.entries(balance.balances).map(([c, v]) => `${c} ${Number(v).toLocaleString("en-IN")}`).join(" · ") || "not recorded"}`
                : accounts.data && !accounts.data.visible ? "balances: administrators only" : `no ${theme.label} account in the Vault`
              : "priced at the best of the retail books"}{" "}
            · {theme.couponStyle} · Developer: Amit Ashok Kumar Patnaik
          </p>
        </header>

        <Async resource={board} skeletonRows={3}>
          {(b) => (
            <div className="flex flex-col gap-3">
              <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label="Sports">
                {[null, ...Object.keys(b.sports)].map((label) => (
                  <button key={label ?? "all"} type="button" role="tab" aria-selected={sport === label} onClick={() => setSport(label)}
                          className="shrink-0 rounded-full px-3 py-1 text-xs transition"
                          style={sport === label ? { background: theme.primary, color: theme.onPrimary } : { background: theme.card, color: theme.text }}>
                    {label ?? "All"}{label ? ` · ${b.sports[label] ?? 0}` : ""}
                  </button>
                ))}
              </div>
              {b.fixtures.length === 0 ? (
                <EmptyState icon="sports" title={sport ? `No ${sport} fixture priced right now` : "No fixture priced right now"} detail="Cards appear as the feeds price fixtures (pre-match, and in play for three hours after kickoff)." />
              ) : (
                <ul className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {b.fixtures.slice(0, 30).map((card) => (
                    <MatchCard key={card.fixture_id} card={card} book={theme.book} onPick={add} onScores={boardTargets(card) ? () => setScoresId(card.fixture_id) : null} />
                  ))}
                </ul>
              )}
              {!b.steam_readable && <p className="text-[11px]" style={{ color: theme.muted }}>Sharp steam is unreadable right now: no card shows it.</p>}
            </div>
          )}
        </Async>

        {scoresFor && (
          <section aria-label="Score prediction" className="rounded-2xl p-3" style={{ border: "1px solid color-mix(in srgb, var(--wb-primary) 35%, transparent)" }}>
            <ScoreBackedLegs card={scoresFor} book={theme.book} onClose={() => setScoresId(null)}
                             onAdd={(market, s) => { add(toSlipLeg(scoresFor, market, s)); toast.success("Added to the slip", s.label); }} />
          </section>
        )}

        <section
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={drop}
          aria-label="Betslip"
          className="flex flex-col gap-3 rounded-2xl p-3 transition"
          style={{ background: theme.card, outline: over ? `2px dashed ${theme.primary}` : "none" }}
        >
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold" style={{ color: theme.primary }}>Betslip · {slip.length} leg{slip.length === 1 ? "" : "s"}</h3>
            {slip.length > 0 && <button type="button" className="text-[11px] underline" onClick={clearSlip}>clear</button>}
          </div>
          {slip.length === 0 ? (
            <p className="text-xs" style={{ color: theme.muted }}>Drop a card or a selection here, or click a price.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {slip.map((l, i) => (
                <li key={l.leg_id} className="flex items-center justify-between gap-2 text-xs">
                  <span className="min-w-0 truncate">{l.fixture} · {l.label}</span>
                  <span className="flex items-center gap-2">
                    {!theme.book && payingBook(l, null) && <span className="text-[10px]" style={{ color: theme.muted }}>@ {payingBook(l, null)}</span>}
                    <span className="font-mono font-bold" style={{ color: theme.primary }}>{prices[i] ?? "—"}</span>
                    <button type="button" aria-label={`remove ${l.label}`} onClick={() => removeLeg(l.leg_id)}>✕</button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Structure">
              <Select value={chosen ?? ""} disabled={!structures.length} onChange={(e) => setKind(e.target.value)}>
                {structures.map((s) => <option key={s.kind} value={s.kind}>{s.label} ({s.lines})</option>)}
              </Select>
            </Field>
            <Field label={`Stop-loss: cash out at −${Math.round(stopLoss * 100)}%`}>
              <input type="range" min={15} max={40} step={1} value={Math.round(stopLoss * 100)} onChange={(e) => setStopLoss(Number(e.target.value) / 100)} className="w-full" style={{ accentColor: theme.primary }} aria-label="Stop-loss percent" />
            </Field>
            <div className="flex flex-col justify-end text-xs">
              <span>Combined {theme.book ? `at ${theme.label}` : "at the best prices"}: <strong className="font-mono">{combined ? combined.toFixed(2) : "—"}</strong></span>
              <span style={{ color: theme.muted }}>model win chance {slip.length ? pct(slip.reduce((a, l) => a * l.fair_probability, 1)) : "—"} (independent legs)</span>
            </div>
          </div>
          <Button variant="primary" icon="psychology" busy={busy === "inspect"} disabled={!slip.length || !chosen} onClick={() => void inspect()}>Inspect parlay</Button>
        </section>

        {inspection && tier && (
          <section className="flex flex-col gap-3 rounded-2xl p-3" style={{ background: theme.card }} aria-label="Cognitive rating">
            <div className="flex flex-wrap items-start gap-4">
              <div className="w-full sm:w-80"><SlipScoreMeter rating={inspection.rating} onAutoFix={() => void autoFix()} fixing={busy === "fix"} /></div>
              <Radar rows={inspection.rating.breakdown} color={tier.color} />
              <div className="flex min-w-[14rem] flex-1 flex-col gap-1 text-xs">
                <p>
                  {inspection.audit.pillars_passed}/15 pillars passed at {inspection.book ?? "—"} · odds {inspection.audit.total_odds ?? "—"} · joint EV{" "}
                  {inspection.audit.joint_ev === null ? "—" : `${inspection.audit.joint_ev >= 0 ? "+" : ""}${(inspection.audit.joint_ev * 100).toFixed(1)}%`} · win {pct(inspection.audit.joint_probability)}
                </p>
                <p style={{ color: theme.muted }}>
                  Parlay overround {inspection.margins.parlay_overround === null ? "unmeasured" : pct(inspection.margins.parlay_overround)} at {inspection.margins.account_book}
                  {inspection.rating.capped ? " · capped: a pillar failed" : ""}
                </p>
                <p>Stop-loss suggested: {pct(inspection.stop_loss.recommended_pct, 0)} ({inspection.stop_loss.basis}).</p>
              </div>
            </div>
            {inspection.rating.warnings.map((w) => <p key={w} className="text-xs font-semibold text-amber-400">{w}</p>)}
            {Object.values(inspection.score_sync).some((v) => v !== null) && (
              <ul className="flex flex-wrap gap-1.5 text-[11px]" aria-label="Score sync">
                {slip.map((l) => {
                  const sync = inspection.score_sync[l.leg_id];
                  return sync ? (
                    <li key={l.leg_id} className="rounded-lg px-2 py-0.5" title={`${(sync.leg_probability * 100).toFixed(1)}% under the fitted scorelines`}
                        style={{ border: `1px solid ${sync.synced ? "#22C55E" : theme.muted}` }}>
                      {sync.synced ? "🎯" : "·"} {l.label}: {sync.result_at_likeliest.toLowerCase().replace("_", " ")} at {sync.likeliest_score}
                    </li>
                  ) : null;
                })}
              </ul>
            )}
            {inspection.rating.advice.length > 0 && (
              <ul className="ml-4 list-disc text-xs leading-relaxed">{inspection.rating.advice.slice(0, 8).map((a) => <li key={a}>{a}</li>)}</ul>
            )}
            <div className="flex flex-wrap gap-1">
              {inspection.rating.breakdown.map((p) => (
                <Pill key={p.number} tone={p.status === "PASS" ? "good" : p.status === "FAIL" ? "critical" : p.status === "ADVISORY" ? "warning" : "neutral"}>
                  {p.number} {p.title}
                </Pill>
              ))}
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
              <Field label="Stake placed (₹)"><NumberInput min="1" step="1" value={placed.stake} onChange={(e) => setPlaced((p) => ({ ...p, stake: e.target.value }))} /></Field>
              <Field label="Odds you got"><NumberInput min="1.01" step="0.01" value={placed.odds} onChange={(e) => setPlaced((p) => ({ ...p, odds: e.target.value }))} /></Field>
              <Field label="Booking code (the book's)"><TextInput value={placed.code} maxLength={32} onChange={(e) => setPlaced((p) => ({ ...p, code: e.target.value }))} /></Field>
              <div className="flex items-end">
                <Button variant="primary" icon="shield" busy={busy === "submit"} disabled={!(Number(placed.stake) > 0)} onClick={() => void submit()}>I placed it: arm the shield</Button>
              </div>
            </div>
          </section>
        )}

        <section className="flex flex-col gap-2" aria-label="Stop-loss shields">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold" style={{ color: theme.primary }}>Stop-loss shields</h3>
            <span className="text-[11px]" style={{ color: theme.muted }}>{stream.connected ? "live" : "polling"}</span>
          </div>
          <Async resource={shields} skeletonRows={1} isEmpty={(d) => d.shields.length === 0} empty={<p className="text-xs" style={{ color: theme.muted }}>No bet is watched yet.</p>}>
            {(d) => <ul className="flex flex-col gap-2">{d.shields.slice(0, 10).map((s) => <ShieldRow key={s.id} shield={s} live={stream.frames[s.id]} />)}</ul>}
          </Async>
          <p className="text-[11px]" style={{ color: theme.muted }}>
            The shield fires the moment a rule is met and sends the cashout steps to your phone. It cannot take the cashout for you or promise an amount: books suspend cashout when a game swings.
          </p>
        </section>
      </div>
  );
  return embedded ? body : (
    <Panel title="Manual parlay workbench" icon="construction" className="lg:col-span-12" subtitle="Build a parlay by hand, rate it against the 15 pillars, and let the stop-loss shield watch it">
      {body}
    </Panel>
  );
};
