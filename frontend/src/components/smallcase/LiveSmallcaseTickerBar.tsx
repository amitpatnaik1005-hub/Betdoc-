/**
 * The live smallcases at a glance (Group 107, point b.8): the six baskets as ASHOKA's market fills them right now, each
 * with KUMBHA's stake across its legs, its expected return and its chance of ending in profit. An empty basket says why.
 * Load puts a basket's legs on the Oracle's shared slip (asking first when the slip already holds legs); Inspect opens
 * its holdings, risk and the components it runs on. Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { formatAgo, formatINR } from "../../lib/format";
import { type Basket, pct, TIER_LABEL, useLoadBasket, useSmallcases } from "../../lib/smallcase";
import { Async, Button, ConfirmButton, Pill } from "../../ui/kit";
import { BasketInspector } from "./BasketInspector";

/** The seconds since the market was read, ticking (the board itself refreshes on the backend's scan cadence). */
const useAge = (iso: string | undefined) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);
  return iso ? Math.max(0, Math.round((now - Date.parse(iso)) / 1000)) : null;
};

export const LoadBasketButton = ({ basket, afterLoad, size = "sm" }: { basket: Basket; afterLoad?: () => void; size?: "sm" | "md" }) => {
  const { slipSize, load } = useLoadBasket(afterLoad);
  if (basket.status !== "LIVE") return null;
  return slipSize > 0 ? (
    <ConfirmButton size={size} icon="playlist_add" confirmLabel={`Replace ${slipSize} leg${slipSize === 1 ? "" : "s"}?`} onConfirm={() => load(basket)}>Load</ConfirmButton>
  ) : (
    <Button size={size} variant="primary" icon="playlist_add" onClick={() => load(basket)}>Load</Button>
  );
};

export const LiveSmallcaseTickerBar = ({ toWorkbenchOnLoad = false }: { toWorkbenchOnLoad?: boolean }) => {
  const board = useSmallcases();
  const navigate = useNavigate();
  const age = useAge(board.data?.as_of);
  const [open, setOpen] = useState<string | null>(null);
  const afterLoad = toWorkbenchOnLoad ? () => navigate("/oracle?tab=builder&mode=manual") : undefined;
  const inspected = board.data?.baskets.find((b) => b.id === open) ?? null;
  return (
    <section aria-label="Live smallcases" className="flex min-w-0 flex-col gap-2 rounded-3xl bg-white/70 p-3 shadow-soft dark:bg-stone-900/50">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-[11px]">
        <span className="flex items-center gap-2 font-semibold text-stone-700 dark:text-stone-200">
          <span className="relative flex size-2" aria-hidden="true">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />
            <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
          </span>
          Live smallcases
          <span className="font-normal text-stone-500 dark:text-stone-400">baskets of positive-EV singles from ASHOKA's market, sized by KUMBHA</span>
        </span>
        {board.data && (
          <span className="font-mono text-stone-500 dark:text-stone-400">
            {board.data.eligible} of {board.data.valued} legs qualify · market read {age === null ? "—" : `${age}s`} ago · rescans every {board.data.refresh_seconds}s
            {board.data.bankroll_inr ? ` · bankroll ${formatINR(Number(board.data.bankroll_inr))}` : " · no bankroll: weights only"}
          </span>
        )}
      </div>
      <Async resource={board} skeletonRows={2}>
        {(d) => (
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
            {d.baskets.map((b) => {
              const tier = TIER_LABEL[b.tier];
              const live = b.status === "LIVE";
              return (
                <li key={b.id} className={`flex min-w-0 flex-col gap-2 rounded-2xl border p-3 transition-colors ${live ? "border-stone-200 bg-white dark:border-white/[0.08] dark:bg-stone-900" : "border-dashed border-stone-200 dark:border-white/[0.06]"}`}>
                  <div className="flex items-center justify-between gap-2">
                    <Pill tone={tier.tone}>{tier.label}</Pill>
                    <span className="font-mono text-[11px] text-stone-500 dark:text-stone-400">{b.holdings.length} leg{b.holdings.length === 1 ? "" : "s"}</span>
                  </div>
                  <p className="truncate text-sm font-bold text-stone-900 dark:text-stone-100" title={b.scope}>{b.name}</p>
                  {live && b.risk ? (
                    <dl className="grid grid-cols-3 gap-1 text-[11px]">
                      <div><dt className="text-stone-500 dark:text-stone-400">Stake</dt><dd className="font-mono font-semibold">{Number(b.stake_inr) > 0 ? formatINR(Number(b.stake_inr)) : "—"}</dd></div>
                      <div><dt className="text-stone-500 dark:text-stone-400">Exp. return</dt><dd className={`font-mono font-semibold ${b.risk.expected_return >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600"}`}>{pct(b.risk.expected_return, 1, true)}</dd></div>
                      <div><dt className="text-stone-500 dark:text-stone-400">In profit</dt><dd className="font-mono font-semibold">{pct(b.risk.p_profit, 0)}</dd></div>
                    </dl>
                  ) : (
                    <p className="line-clamp-2 text-[11px] text-stone-500 dark:text-stone-400">{b.empty_reason}</p>
                  )}
                  <div className="mt-auto flex flex-wrap items-center gap-1.5">
                    <LoadBasketButton basket={b} afterLoad={afterLoad} />
                    <Button size="sm" variant="ghost" icon="query_stats" onClick={() => setOpen(b.id)}>Inspect</Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Async>
      {board.data && <p className="px-1 text-[10px] text-stone-400 dark:text-stone-500">Computed {formatAgo(board.data.computed_at)}. Nothing is placed from here: a loaded basket is inspected against the 15 pillars in the workbench.</p>}
      {inspected && board.data && <BasketInspector basket={inspected} board={board.data} onClose={() => setOpen(null)} afterLoad={afterLoad} />}
    </section>
  );
};
