/**
 * One live smallcase in full (Group 107, point b.8): its holdings at their best retail price with BetDoc's fair chance,
 * EV, KUMBHA's stake and the in-play stop-loss floor; the basket's measured risk (expected return, chance of profit,
 * chance of losing every leg, 95% VaR and CVaR, HHI, KUMBHA's weights beside the equal-risk ones, each leg's share of
 * the VaR); the same legs as one parlay; and the components it runs on. Rebalance rebuilds it from a fresh read of the
 * market. Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { formatINR } from "../../lib/format";
import { invalidate } from "../../lib/resource";
import { type Basket, type Board, pct, rebalanceBasket, TIER_LABEL, useComponents } from "../../lib/smallcase";
import { toast } from "../../store/useToastStore";
import { Button, Pill } from "../../ui/kit";
import { LoadBasketButton } from "./LiveSmallcaseTickerBar";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const inr = (v: string | number | null | undefined) => (v === null || v === undefined || Number(v) <= 0 ? "—" : formatINR(Number(v)));

const Stat = ({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) => (
  <div className="min-w-0 rounded-xl bg-stone-50 p-2.5 dark:bg-white/[0.04]">
    <div className="text-[10px] font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">{label}</div>
    <div className={`truncate font-mono text-base font-bold ${tone ?? "text-stone-900 dark:text-stone-100"}`}>{value}</div>
    {hint && <div className="text-[10px] text-stone-500 dark:text-stone-400">{hint}</div>}
  </div>
);

export const BasketInspector = ({ basket, board, onClose, afterLoad }: { basket: Basket; board: Board; onClose: () => void; afterLoad?: () => void }) => {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const components = useComponents(true);
  useEffect(() => {
    box.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const rebalance = async () => {
    setBusy(true);
    try {
      const r = await rebalanceBasket(basket.id);
      invalidate("smallcase");
      toast.success(`${basket.name} rebuilt from a fresh read`, `${r.basket.holdings.length} leg${r.basket.holdings.length === 1 ? "" : "s"} now`);
    } catch (err) {
      toast.error("Not rebuilt", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const r = basket.risk;
  const stake = Number(basket.stake_inr);
  const tier = TIER_LABEL[basket.tier];
  const used = (components.data?.components ?? []).filter((c) => c.used?.scope === "BASKET");
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/40 p-3 sm:items-center" role="presentation" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={titleId}
           className="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-3xl bg-white shadow-soft-lg outline-none dark:bg-stone-900 dark:ring-1 dark:ring-white/10">
        <header className="flex items-start justify-between gap-3 border-b border-stone-200 p-5 dark:border-white/[0.08]">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><Pill tone={tier.tone}>{tier.label}</Pill><span className="font-mono text-[11px] text-stone-500">{basket.id}</span></div>
            <h3 id={titleId} className="mt-1 text-lg font-black text-stone-900 dark:text-stone-100">{basket.name}</h3>
            <p className="mt-0.5 text-xs leading-relaxed text-stone-600 dark:text-stone-300">{basket.scope}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-lg p-1.5 text-stone-400 hover:bg-stone-100 dark:hover:bg-white/[0.06]">
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">close</span>
          </button>
        </header>
        <div className="flex flex-col gap-4 overflow-y-auto p-5">
          {basket.status === "EMPTY" ? (
            <p className="rounded-xl bg-stone-50 p-4 text-sm text-stone-600 dark:bg-white/[0.04] dark:text-stone-300">{basket.empty_reason}</p>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="KUMBHA's stake" value={inr(basket.stake_inr)} hint={board.bankroll_inr ? `${pct(basket.fraction, 2)} of bankroll${basket.scaled_to_cap ? `, capped at ${pct(basket.cap_fraction, 0)}` : ""}` : "no bankroll: weights only"} />
                <Stat label="Expected return" value={pct(r?.expected_return, 1, true)} hint={basket.expected_profit_inr ? `${inr(basket.expected_profit_inr)} on the stake` : "per rupee staked"} tone="text-emerald-600 dark:text-emerald-400" />
                <Stat label="Ends in profit" value={pct(r?.p_profit, 0)} hint={`loses every leg ${pct(r?.p_lose_all, 1)}`} />
                <Stat label="95% VaR · CVaR" value={`${pct(r?.var_95, 0)} · ${pct(r?.cvar_95, 0)}`}
                      hint={r && r.var_95 >= 0.999 ? "the worst 5% lose the whole stake" : stake > 0 && r ? `${inr(stake * r.var_95)} · ${inr(stake * r.cvar_95)}` : "of the basket's stake"}
                      tone="text-rose-600 dark:text-rose-400" />
              </div>
              {basket.halted && <p className="rounded-xl bg-rose-50 p-3 text-xs text-rose-700 dark:bg-rose-950/30 dark:text-rose-300">A drawdown halt holds: KUMBHA stakes nothing until it is signed off.</p>}
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-xs">
                  <thead className="text-[10px] uppercase tracking-wide text-stone-500 dark:text-stone-400">
                    <tr><th className="py-1.5 pr-2">Leg</th><th className="pr-2">Price</th><th className="pr-2">Fair · EV</th><th className="pr-2">KUMBHA</th><th className="pr-2">Equal-risk</th><th className="pr-2">Share of VaR</th><th>Stop floor</th></tr>
                  </thead>
                  <tbody className="divide-y divide-stone-100 dark:divide-white/[0.06]">
                    {basket.holdings.map((h, i) => (
                      <tr key={h.leg_id} className="align-top">
                        <td className="py-2 pr-2">
                          <div className="font-semibold text-stone-900 dark:text-stone-100">{h.fixture}</div>
                          <div className="text-stone-500 dark:text-stone-400">{h.label} · {[h.sport, h.league].filter(Boolean).join(" · ")}{h.in_play ? " · in play" : ""}</div>
                          {h.also_in && h.also_in.length > 0 && <div className="text-[10px] text-amber-700 dark:text-amber-300">also in {h.also_in.map((id) => board.baskets.find((b) => b.id === id)?.name ?? id).join(", ")}</div>}
                        </td>
                        <td className="pr-2 font-mono">{h.price.toFixed(2)}<div className="text-[10px] text-stone-500">{h.book_label}</div></td>
                        <td className="pr-2 font-mono">{pct(h.fair_probability)}<div className="text-emerald-600 dark:text-emerald-400">{pct(h.ev, 1, true)}</div></td>
                        <td className="pr-2 font-mono">{inr(h.stake_inr)}<div className="text-[10px] text-stone-500">{pct(r?.weights?.[i], 0)}</div></td>
                        <td className="pr-2 font-mono">{pct(r?.risk_parity_weights?.[i], 0)}</td>
                        <td className="pr-2 font-mono">{pct(r?.var_share?.[i], 0)}</td>
                        <td className="font-mono">{inr(h.stop_loss.floor_inr)}<div className="text-[10px] text-stone-500">at {pct(h.stop_loss.pct, 0)} down</div></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                {r?.basis}; {r?.paths.toLocaleString("en-IN")} paths. KUMBHA's weights are its Kelly sizing ({board.policy.kelly_multiplier}× Kelly, drawdown regime {basket.regime?.toLowerCase().replace(/_/g, " ")});
                equal-risk weights are shown for comparison only. HHI {r?.hhi.toFixed(2)} (1 = everything on one leg).
              </p>
              {basket.parlay && (
                <p className="rounded-xl bg-stone-50 p-3 text-xs text-stone-700 dark:bg-white/[0.04] dark:text-stone-300">
                  <strong>As one parlay instead:</strong> odds {basket.parlay.odds.toFixed(2)}, wins {pct(basket.parlay.win_probability, 1)} of the time, EV {pct(basket.parlay.ev, 1, true)}.
                  The smallcase stakes each leg on its own, so one losing leg does not sink the rest.
                </p>
              )}
            </>
          )}
          <section aria-label="Components this basket runs on" className="rounded-xl border border-stone-200 p-3 text-xs dark:border-white/[0.08]">
            <p className="font-semibold text-stone-800 dark:text-stone-100">What it runs on</p>
            <ul className="mt-1.5 flex flex-col gap-1 text-stone-600 dark:text-stone-300">
              {used.map((c) => <li key={c.key}><span className="font-mono text-[11px]">{c.name}</span>: {c.used?.role}</li>)}
              <li>KUMBHA's sizing policy (the CFO's growth maths), and each leg's in-play stop-loss floor.</li>
              <li>Once loaded, every leg goes through the fortress's 15 pillars in the workbench, and the stop-loss engine guards the account it is recorded against.</li>
            </ul>
            {components.data && (
              <p className="mt-1.5 text-[11px] text-stone-500 dark:text-stone-400">
                Of BetDoc's {components.data.totals.catalogued} catalogued components ({components.data.totals.implemented} implemented), the live smallcases use {components.data.totals.used}: the full list is in The Vault.
              </p>
            )}
          </section>
        </div>
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-stone-200 p-4 dark:border-white/[0.08]">
          <Button size="sm" variant="ghost" icon="sync" busy={busy} onClick={() => void rebalance()}>Rebalance from a fresh read</Button>
          <LoadBasketButton basket={basket} afterLoad={() => { onClose(); afterLoad?.(); }} size="md" />
        </footer>
      </div>
    </div>
  );
};
