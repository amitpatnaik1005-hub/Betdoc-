/**
 * The match board's betslip rail (Group 103, point b.11 screenshot 5). Developed for Amit Ashok Kumar Patnaik.
 *
 * - One slip with the workbench (`useParlaySlipStore`): one leg is a single bet, two or more a parlay.
 * - "Add and win more!": the best positive-EV selections on fixtures not already in the slip.
 * - Stake chips in rupees; the Kelly chip appears once the slip is inspected and is KUMBHA's stake from the 15-pillar
 *   fortress, never a fixed figure. The return shows in USD too only while TODAR MAL holds a fresh rate.
 * - Nothing is placed from here: place it at the book, then record it in the workbench, which the last button opens.
 */
import { useState } from "react";
import { ApiError } from "../../../api/client";
import { BOOKMAKER_THEMES, inspectParlay, type Inspection, STRUCTURES, TIER_STYLE } from "../../../lib/backtest_and_parlay";
import { useFxBoard } from "../../../lib/currency";
import { formatINR } from "../../../lib/format";
import { boosters as pickBoosters, type BoardFixture, priceFor } from "../../../lib/match_board";
import { priceAt, slipSignature, toSlipLeg, useParlaySlipStore } from "../../../store/useParlaySlipStore";
import { toast } from "../../../store/useToastStore";

const CHIPS = [100, 200, 1000];
const pct = (p: number, digits = 1) => `${(p * 100).toFixed(digits)}%`;

export const SlipRail = ({ fixtures, onRecord }: { fixtures: BoardFixture[]; onRecord: () => void }) => {
  const legs = useParlaySlipStore((s) => s.legs);
  const skin = useParlaySlipStore((s) => s.skin);
  const add = useParlaySlipStore((s) => s.add);
  const remove = useParlaySlipStore((s) => s.remove);
  const clear = useParlaySlipStore((s) => s.clear);
  const theme = BOOKMAKER_THEMES[skin];
  const fx = useFxBoard();
  const [stake, setStake] = useState("");
  const [busy, setBusy] = useState(false);
  const [inspected, setInspected] = useState<{ sig: string; result: Inspection } | null>(null);

  const kind = STRUCTURES.find((s) => legs.length >= s.legs[0] && legs.length <= s.legs[1])?.kind ?? null;
  const sig = slipSignature(legs, skin, kind);
  const inspection = inspected?.sig === sig ? inspected.result : null;
  const prices = legs.map((l) => priceAt(l, theme.book));
  const odds = legs.length && prices.every((p) => p !== null) ? prices.reduce<number>((a, p) => a * (p as number), 1) : null;
  const amount = Number(stake);
  const validStake = /^\d+(\.\d{1,2})?$/.test(stake.trim()) && amount > 0;
  const returns = validStake && odds ? amount * odds : null;
  const usdRate = fx.data?.rates?.USD?.inr_per_unit;
  const held = new Set(legs.map((l) => l.leg_id.split("|")[0]));
  const extra = pickBoosters(fixtures, held);
  const state = legs.length === 0 ? "Empty" : legs.length === 1 ? "Single bet" : `Parlay · ${legs.length} legs`;
  const kellyStake = inspection && Number(inspection.audit.stake_inr) > 0 ? inspection.audit.stake_inr : null;

  const inspect = async () => {
    setBusy(true);
    try {
      setInspected({ sig, result: await inspectParlay({ leg_ids: legs.map((l) => l.leg_id), kind, skin }) });
    } catch (err) {
      toast.error("Inspection refused", err instanceof ApiError ? err.message : "No answer from the server");
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside aria-label="Betslip" className="flex flex-col gap-3 rounded-2xl p-3 lg:sticky lg:top-20" style={{ background: "var(--wb-card)" }}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-bold" style={{ color: "var(--wb-primary)" }}>Betslip</h3>
        <span className="rounded-full px-2 py-0.5 text-[10px] font-bold" style={legs.length ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { color: "var(--wb-muted)" }}>{state}</span>
        {legs.length > 0 && <button type="button" onClick={clear} className="text-[11px] underline">clear</button>}
      </div>

      {legs.length === 0 ? (
        <p className="text-xs" style={{ color: "var(--wb-muted)" }}>Click any price on the board, or add a top parlay, to start a slip. The workbench below sees the same slip.</p>
      ) : (
        <ul className="flex max-h-64 flex-col gap-1.5 overflow-y-auto">
          {legs.map((l, i) => (
            <li key={l.leg_id} className="flex items-center justify-between gap-2 rounded-xl px-2 py-1.5 text-xs" style={{ background: "color-mix(in srgb, var(--wb-primary) 8%, transparent)" }}>
              <span className="min-w-0">
                <span className="block truncate font-semibold">{l.label}</span>
                <span className="block truncate text-[10px]" style={{ color: "var(--wb-muted)" }}>{l.fixture} · {l.market}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="font-mono font-bold">{prices[i]?.toFixed(2) ?? "—"}</span>
                <button type="button" aria-label={`Remove ${l.label}`} onClick={() => remove(l.leg_id)}>✕</button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {legs.length > 0 && extra.length > 0 && (
        <section aria-label="Add and win more">
          <p className="mb-1 text-[11px] font-bold" style={{ color: "var(--wb-primary)" }}>Add and win more!</p>
          <ul className="flex gap-1.5 overflow-x-auto pb-1">
            {extra.map(({ card, market, selection }) => (
              <li key={selection.leg_id} className="flex w-44 shrink-0 flex-col gap-1 rounded-xl p-2 text-[11px]" style={{ border: "1px solid color-mix(in srgb, var(--wb-primary) 35%, transparent)" }}>
                <span className="truncate font-semibold">{selection.label}</span>
                <span className="truncate" style={{ color: "var(--wb-muted)" }}>{card.home} v {card.away} · {market.market}</span>
                <span className="flex items-center justify-between">
                  <span style={{ color: "#22C55E" }}>EV +{pct(selection.ev ?? 0)} at {selection.best?.odds.toFixed(2)}</span>
                  <button type="button" disabled={priceFor(selection, theme.book) === undefined} onClick={() => add(toSlipLeg(card, market.market, selection))}
                          className="rounded-md px-1.5 font-black disabled:opacity-40" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }} aria-label={`Add ${selection.label}`}>+</button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {legs.length > 0 && (
        <div className="flex flex-col gap-2 border-t pt-3" style={{ borderColor: "color-mix(in srgb, var(--wb-muted) 30%, transparent)" }}>
          <div className="flex flex-wrap items-center gap-1">
            <span className="mr-1 text-[11px]" style={{ color: "var(--wb-muted)" }}>Stake ₹</span>
            {CHIPS.map((c) => (
              <button key={c} type="button" onClick={() => setStake(String(c))} className="rounded-md px-2 py-0.5 text-[11px] font-bold" style={{ border: "1px solid var(--wb-primary)" }}>{c}</button>
            ))}
            {kellyStake && (
              <button type="button" onClick={() => setStake(kellyStake)} title="KUMBHA's stake from the 15-pillar inspection" className="rounded-md px-2 py-0.5 text-[11px] font-bold" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
                Kelly {formatINR(Number(kellyStake))}
              </button>
            )}
          </div>
          <input type="text" inputMode="decimal" value={stake} onChange={(e) => setStake(e.target.value.replace(/[^\d.]/g, ""))} aria-label="Stake in rupees" placeholder="Amount in ₹"
                 className="w-full rounded-xl border bg-transparent px-3 py-2 text-sm font-bold" style={{ borderColor: "color-mix(in srgb, var(--wb-muted) 40%, transparent)" }} />
          <div className="flex justify-between text-xs"><span style={{ color: "var(--wb-muted)" }}>Odds at {theme.label}</span><strong className="font-mono">{odds ? odds.toFixed(2) : "not all quoted"}</strong></div>
          <div className="flex justify-between text-xs">
            <span style={{ color: "var(--wb-muted)" }}>Potential return</span>
            <span className="text-right">
              <strong className="block font-mono">{returns !== null ? formatINR(returns) : "—"}</strong>
              {returns !== null && usdRate ? <span className="block text-[10px]" style={{ color: "var(--wb-muted)" }}>≈ ${(returns / usdRate).toFixed(2)} at TODAR MAL's rate</span> : null}
            </span>
          </div>
          {inspection && (
            <p className="rounded-xl p-2 text-[11px]" style={{ border: `1px solid ${TIER_STYLE[inspection.rating.tier].color}` }}>
              Slip score <strong>{inspection.rating.score}</strong> · {inspection.rating.tier} · {inspection.audit.pillars_passed} pillars passed{inspection.audit.is_vetted ? " · vetted" : ""}
              {inspection.audit.joint_ev !== null ? ` · joint EV ${inspection.audit.joint_ev >= 0 ? "+" : ""}${pct(inspection.audit.joint_ev)}` : ""}
            </p>
          )}
          <button type="button" disabled={busy || !kind} onClick={() => void inspect()} className="w-full rounded-xl py-2.5 text-xs font-black disabled:opacity-50"
                  style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
            {busy ? "Inspecting…" : inspection ? "Re-inspect against the 15 pillars" : "Inspect against the 15 pillars"}
          </button>
          <button type="button" onClick={onRecord} className="w-full rounded-xl py-2 text-xs font-bold" style={{ border: "1px solid var(--wb-primary)" }}>
            Placed it at the book? Record it in the workbench
          </button>
        </div>
      )}
    </aside>
  );
};
