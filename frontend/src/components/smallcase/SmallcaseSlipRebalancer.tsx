/**
 * The slip as a smallcase (Group 107, point b.8), beside the manual workbench: the Oracle's slip re-priced at the book
 * you bet from, one holding per fixture, legs priced under fair left out (KUMBHA stakes nothing on them), the rest
 * sized leg by leg by KUMBHA and measured as a basket, beside the slip as you built it as one parlay. "Keep only the
 * holdings" drops the left-out legs from the slip. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { ApiError } from "../../api/client";
import { formatINR } from "../../lib/format";
import { pct, type SlipSmallcase, slipAsSmallcase } from "../../lib/smallcase";
import { slipSignature, useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Button, Panel, Pill } from "../../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const REASON: Record<SlipSmallcase["excluded"][number]["reason"], string> = { NOT_QUOTED: "not quoted", UNDER_FAIR: "under fair", SAME_FIXTURE: "same fixture" };

export const SmallcaseSlipRebalancer = () => {
  const legs = useParlaySlipStore((s) => s.legs);
  const skin = useParlaySlipStore((s) => s.skin);
  const remove = useParlaySlipStore((s) => s.remove);
  const [result, setResult] = useState<{ sig: string; data: SlipSmallcase } | null>(null);
  const [busy, setBusy] = useState(false);
  const signature = slipSignature(legs, skin, null);
  const data = result?.sig === signature ? result.data : null;

  const run = async () => {
    setBusy(true);
    try {
      setResult({ sig: signature, data: await slipAsSmallcase({ leg_ids: legs.map((l) => l.leg_id), skin }) });
    } catch (err) {
      toast.error("Not rebalanced", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const keep = () => {
    if (!data) return;
    data.excluded.forEach((e) => remove(e.leg_id));
    toast.info("Left-out legs dropped", `${data.excluded.length} removed; the slip now holds the smallcase's ${data.holdings.length}.`);
  };
  const label = (legId: string) => legs.find((l) => l.leg_id === legId)?.label ?? legId;

  return (
    <Panel title="Rebalance my slip as a smallcase" icon="balance" subtitle="each leg its own single, sized by KUMBHA">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-stone-600 dark:text-stone-300">
          <span>{legs.length ? `${legs.length} leg${legs.length === 1 ? "" : "s"} on the slip, priced at your book.` : "Put legs on the slip first (the match board, or a smallcase above)."}</span>
          <Button size="sm" variant="primary" icon="auto_fix_high" disabled={legs.length === 0} busy={busy} onClick={() => void run()}>Rebalance</Button>
        </div>
        {data && (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                ["KUMBHA's stake", Number(data.stake_inr) > 0 ? formatINR(Number(data.stake_inr)) : "—"],
                ["Expected return", pct(data.risk?.expected_return, 1, true)],
                ["Ends in profit", pct(data.risk?.p_profit, 0)],
                ["As your parlay", data.as_built ? `${data.as_built.odds.toFixed(2)} · EV ${pct(data.as_built.ev, 1, true)}` : "—"],
              ].map(([k, v]) => (
                <div key={k} className="rounded-xl bg-stone-50 p-2.5 dark:bg-white/[0.04]">
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">{k}</div>
                  <div className="font-mono text-sm font-bold">{v}</div>
                </div>
              ))}
            </div>
            {data.holdings.length > 0 && (
              <ul className="flex flex-col gap-1.5 text-xs">
                {data.holdings.map((h, i) => (
                  <li key={h.leg_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-stone-50 px-2.5 py-1.5 dark:bg-white/[0.03]">
                    <span className="min-w-0 truncate"><strong>{h.fixture}</strong> · {h.label} @ {h.price.toFixed(2)} ({h.book_label})</span>
                    <span className="font-mono text-stone-600 dark:text-stone-300">EV {pct(h.ev, 1, true)} · {Number(h.stake_inr) > 0 ? formatINR(Number(h.stake_inr)) : pct(data.risk?.weights?.[i], 0)}</span>
                  </li>
                ))}
              </ul>
            )}
            {data.excluded.length > 0 && (
              <div className="flex flex-col gap-1.5 rounded-xl border border-amber-200 bg-amber-50/60 p-2.5 text-xs dark:border-amber-900/50 dark:bg-amber-950/20">
                {data.excluded.map((e) => (
                  <p key={e.leg_id}><Pill tone="warning">{REASON[e.reason]}</Pill> <strong>{e.label ?? label(e.leg_id)}</strong>: {e.message}</p>
                ))}
                <Button size="sm" variant="ghost" icon="playlist_remove" className="self-start" onClick={keep}>Keep only the holdings</Button>
              </div>
            )}
            <p className="text-[11px] text-stone-500 dark:text-stone-400">
              Priced at {data.book_label}. {data.parlay ? `The holdings as one parlay: ${data.parlay.odds.toFixed(2)}, wins ${pct(data.parlay.win_probability, 1)}, EV ${pct(data.parlay.ev, 1, true)}. ` : ""}
              As a smallcase each leg settles on its own; the workbench still inspects every leg against the 15 pillars before you place it.
            </p>
          </>
        )}
      </div>
    </Panel>
  );
};
