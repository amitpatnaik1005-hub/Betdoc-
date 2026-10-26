/**
 * The Chef's Table's parlay of the day on the match board's first screen (Group 111, points b.11 and b.14a): plate #1 as
 * the kitchen served it (`GET /oracle/chef-table`): its Slip Score and tier, total odds, joint EV, its chance of
 * landing, KUMBHA's stake and each leg at the plate's book. One line of figures and one of legs, so the first match cards
 * stay on the first screen; the full table, with every recipe card, is the Bet builder's third mode.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { TIER_STYLE } from "../../../lib/backtest_and_parlay";
import { type ChefPlate, toSlipLeg, useChefTable } from "../../../lib/builder";
import { formatINR } from "../../../lib/format";
import { BOOK_LABEL } from "../../../lib/oracle";
import { useParlaySlipStore } from "../../../store/useParlaySlipStore";
import { toast } from "../../../store/useToastStore";
import { Button, ConfirmButton } from "../../../ui/kit";

const pct = (p: number | null | undefined, digits = 1, signed = false) =>
  p === null || p === undefined ? "—" : `${signed && p >= 0 ? "+" : ""}${(100 * p).toFixed(digits)}%`;

const Shell = ({ children }: { children: ReactNode }) => (
  <section aria-label="Chef's Table parlay of the day"
           className="flex min-h-[92px] min-w-0 flex-col justify-center gap-2 rounded-3xl border border-[#C89B3C]/30 bg-gradient-to-r from-[#C89B3C]/[0.07] via-white/70 to-white/70 px-4 py-3 dark:from-[#C89B3C]/[0.10] dark:via-stone-900/50 dark:to-stone-900/50">
    {children}
  </section>
);

export const ChefTopPlate = () => {
  const table = useChefTable(true);
  const navigate = useNavigate();
  const slipSize = useParlaySlipStore((s) => s.legs.length);
  const clear = useParlaySlipStore((s) => s.clear);
  const addMany = useParlaySlipStore((s) => s.addMany);
  const plate: ChefPlate | undefined = table.data?.plates[0];

  const load = (p: ChefPlate) => {
    clear();
    addMany(p.legs.map(toSlipLeg));
    toast.success(`${p.title} is in the workbench`, "Press Inspect for its Slip Score at your book.");
    navigate("/oracle?tab=builder&mode=manual");
  };

  if (!table.data) {
    return (
      <Shell>
        <p className="flex items-center gap-2 text-xs text-stone-500 dark:text-stone-400">
          <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">restaurant</span>
          {table.error ? `The Chef's Table is unavailable: ${table.error}` : "The Chef's Table is re-rating Ashoka's best slips through the fortress…"}
        </p>
      </Shell>
    );
  }
  if (!plate) {
    return (
      <Shell>
        <p className="flex items-center gap-2 text-xs text-stone-600 dark:text-stone-300">
          <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">verified_user</span>
          No algorithmic parlay meets the 15-pillar risk criteria with positive expected value at retail prices right now. Only positive-edge slips are recommended.
        </p>
      </Shell>
    );
  }

  const tier = TIER_STYLE[plate.rating.tier];
  const book = plate.book ? BOOK_LABEL[plate.book] ?? plate.book : "the best retail book";
  const stake = Number(plate.stake_inr);
  return (
    <Shell>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="material-symbols-outlined text-[20px] text-[#C89B3C]" aria-hidden="true">restaurant</span>
          <h2 className="text-sm font-bold text-stone-900 dark:text-stone-100">Chef's Table · {plate.title}</h2>
          <span className="rounded-full px-2 py-0.5 font-mono text-[10px] font-black"
                style={{ color: "#1C1917", background: `color-mix(in srgb, ${tier.color} 30%, white)`, border: `1px solid ${tier.color}` }}>
            {Math.round(plate.rating.score)}/100 · {plate.rating.tier}
          </span>
          <span className="font-mono text-xs text-stone-600 dark:text-stone-300">
            {plate.kind.replace(/_/g, " ").toLowerCase()} @ <strong className="text-stone-900 dark:text-stone-100">{Number(plate.odds).toFixed(2)}</strong> at {book}
          </span>
          <span className="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-400">joint EV {pct(plate.joint_ev, 1, true)}</span>
          <span className="font-mono text-xs text-stone-500 dark:text-stone-400">lands {pct(plate.joint_probability)}</span>
          {stake > 0 && <span className="font-mono text-xs text-stone-500 dark:text-stone-400">KUMBHA {formatINR(stake)}</span>}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button size="sm" variant="ghost" icon="menu_book" onClick={() => navigate("/oracle?tab=builder&mode=chef")}>Recipe cards</Button>
          {slipSize > 0 ? (
            <ConfirmButton size="sm" variant="primary" icon="playlist_add" confirmLabel={`Replace ${slipSize} leg${slipSize === 1 ? "" : "s"}?`} onConfirm={() => load(plate)}>
              Load
            </ConfirmButton>
          ) : (
            <Button size="sm" variant="primary" icon="playlist_add" onClick={() => load(plate)}>Load</Button>
          )}
        </div>
      </div>
      <ul className="flex min-w-0 flex-wrap gap-1.5" aria-label="The parlay's legs">
        {plate.legs.map((leg) => {
          const price = plate.book ? leg.prices[plate.book] : Math.max(...Object.values(leg.prices));
          return (
            <li key={leg.leg_id} className="flex min-w-0 max-w-full items-center gap-1.5 rounded-full bg-white/80 px-2.5 py-1 text-[11px] ring-1 ring-stone-200 dark:bg-white/[0.05] dark:ring-white/10">
              <span className="truncate font-semibold text-stone-800 dark:text-stone-100">{leg.fixture}</span>
              <span className="truncate text-emerald-700 dark:text-emerald-400">{leg.label}</span>
              {Number.isFinite(price) && <span className="font-mono font-bold text-stone-900 dark:text-stone-100">{price.toFixed(2)}</span>}
            </li>
          );
        })}
      </ul>
    </Shell>
  );
};
