/**
 * The signed verdicts, newest first (Group 105). The server re-checks each seal against its stored report on every
 * read: a verdict whose report was changed after signing shows as broken. Developed for Amit Ashok Kumar Patnaik.
 */
import { TIER_STYLE } from "../../lib/backtest_and_parlay";
import { useVerdicts, VERDICT_STYLE } from "../../lib/consultant";
import { formatDateTime } from "../../lib/format";
import { Async, EmptyState, Panel, Pill } from "../../ui/kit";

export const VerdictHistory = () => {
  const verdicts = useVerdicts();
  return (
    <Panel title="Signed verdicts" icon="history_edu" subtitle="every consultation, sealed" className="lg:col-span-12" updatedAt={verdicts.updatedAt}>
      <Async resource={verdicts} isEmpty={(d) => d.verdicts.length === 0}
             empty={<EmptyState icon="history_edu" title="No verdict yet" detail="Each consultation is signed and recorded here." />}>
        {(d) => (
          <ul className="flex flex-col divide-y divide-stone-100 dark:divide-white/[0.06]">
            {d.verdicts.map((v) => {
              const style = VERDICT_STYLE[v.verdict];
              return (
                <li key={v.report_id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-xs">
                  <span className="flex min-w-0 items-center gap-2">
                    <Pill tone={style.tone} icon={style.icon}>{v.title ?? v.verdict}</Pill>
                    <span className="min-w-0 truncate text-stone-600 dark:text-stone-300">{v.legs.join(" + ")}</span>
                  </span>
                  <span className="flex items-center gap-3 font-mono text-[11px] text-stone-500 dark:text-stone-400">
                    <span style={{ color: TIER_STYLE[v.tier].color }}>{Math.round(v.slip_score)} {v.tier}</span>
                    <span>{v.bookmaker ?? "—"} @ {v.odds ?? "—"}</span>
                    <span>{formatDateTime(v.signed_at)}</span>
                    {v.seal_holds ? <Pill tone="good" icon="verified_user">seal holds</Pill> : <Pill tone="critical" icon="gpp_bad">seal broken</Pill>}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Async>
    </Panel>
  );
};
