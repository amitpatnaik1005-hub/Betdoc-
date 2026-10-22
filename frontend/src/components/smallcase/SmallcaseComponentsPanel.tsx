/**
 * BetDoc's quantitative components and how the live smallcases use them (Group 107, point b.8): the code's own
 * catalogue (the model registry's math models, risk models and bet types, the fortress's 15 pillars, the stop-loss
 * engine's five mechanisms), each implementation imported to prove it exists, and a use shown only where the
 * smallcases make one. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { type ComponentRow, useComponents } from "../../lib/smallcase";
import { Async, Panel, Pill } from "../../ui/kit";

const KIND_LABEL: Record<ComponentRow["kind"], string> = {
  BET_TYPE: "Bet types", MATH_MODEL: "Math models", RISK_MODEL: "Risk models", STOP_LOSS: "Stop-loss mechanisms", FORTRESS_PILLAR: "Fortress pillars",
};
const SCOPE: Record<NonNullable<ComponentRow["used"]>["scope"], { label: string; tone: "good" | "info" | "accent" | "neutral" }> = {
  BASKET: { label: "every basket", tone: "good" }, HOLDING: { label: "held now", tone: "info" }, INSPECTION: { label: "on inspection", tone: "accent" },
  ACCOUNT: { label: "account guard", tone: "neutral" },
};

export const SmallcaseComponentsPanel = () => {
  const data = useComponents(true);
  const [kind, setKind] = useState<ComponentRow["kind"] | null>(null);
  return (
    <Panel title="Components behind the smallcases" icon="hub" subtitle="the code's own catalogue" className="lg:col-span-12" updatedAt={data.updatedAt}>
      <Async resource={data} skeletonRows={3}>
        {(d) => (
          <div className="flex flex-col gap-3">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-xs">
                <thead className="text-[10px] uppercase tracking-wide text-stone-500 dark:text-stone-400">
                  <tr><th className="py-1.5">Kind</th><th>Catalogued</th><th>Implemented</th><th>Every basket</th><th>Held now</th><th>On inspection</th><th>Account guard</th><th /></tr>
                </thead>
                <tbody className="divide-y divide-stone-100 font-mono dark:divide-white/[0.06]">
                  {(Object.keys(KIND_LABEL) as ComponentRow["kind"][]).map((k) => {
                    const row = d.by_kind[k];
                    return (
                      <tr key={k}>
                        <td className="py-1.5 font-sans font-semibold">{KIND_LABEL[k]}</td>
                        <td>{row.catalogued}<span className="text-stone-400"> / {row.blueprint} blueprint</span></td>
                        <td>{row.implemented}</td><td>{row.BASKET || "—"}</td><td>{row.HOLDING || "—"}</td><td>{row.INSPECTION || "—"}</td><td>{row.ACCOUNT || "—"}</td>
                        <td><button type="button" className="font-sans text-[11px] text-[var(--accent-text)] underline" onClick={() => setKind(kind === k ? null : k)}>{kind === k ? "hide" : "list"}</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] text-stone-500 dark:text-stone-400">
              {d.totals.catalogued} catalogued, {d.totals.implemented} implemented, {d.totals.used} used by the {d.baskets_live} live basket{d.baskets_live === 1 ? "" : "s"}. {d.note}
            </p>
            {kind && (
              <ul className="grid grid-cols-1 gap-1.5 text-xs md:grid-cols-2">
                {d.components.filter((c) => c.kind === kind).map((c) => (
                  <li key={c.key} className="flex flex-col gap-0.5 rounded-lg bg-stone-50 p-2 dark:bg-white/[0.03]">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <strong>{c.name}</strong>
                      {c.used ? <Pill tone={SCOPE[c.used.scope].tone}>{SCOPE[c.used.scope].label}</Pill> : <Pill tone="neutral">catalogued</Pill>}
                      {!c.implemented && <Pill tone="critical">not importable</Pill>}
                    </span>
                    <span className="text-stone-500 dark:text-stone-400">{c.used?.role ?? c.description}</span>
                    <span className="truncate font-mono text-[10px] text-stone-400" title={c.implementation}>{c.implementation}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Async>
    </Panel>
  );
};
