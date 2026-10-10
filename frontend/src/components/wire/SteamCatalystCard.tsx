/**
 * News the market followed: the consensus move on the named fixture inside the catalyst window.
 * The latency is an upper bound (it is measured at the scan that saw the move). Developed for Amit Ashok Kumar Patnaik.
 */
import { formatAgo } from "../../lib/format";
import { type SteamCatalystAlert } from "../../lib/the_wire";
import { Pill } from "../../ui/kit";

export const SteamCatalystCard = ({ alert, fixture }: { alert: SteamCatalystAlert; fixture?: string }) => {
  const up = alert.shift_pct > 0;
  return (
    <div className="rounded-2xl border-l-4 border-sky-500 bg-stone-50 p-3.5 dark:bg-white/[0.03]">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span className="font-bold uppercase tracking-wider text-sky-600 dark:text-sky-300">Steam catalyst</span>
        <span className="text-stone-400">≤ {Math.round(alert.latency_seconds)}s after the story · {formatAgo(alert.published_at)}</span>
      </div>
      <p className="mt-1 text-sm font-semibold text-stone-800 dark:text-stone-100">{alert.headline}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-stone-500 dark:text-stone-400">
        {fixture && <span className="truncate">{fixture}</span>}
        <span className="font-mono">
          {alert.selection} {(alert.probability_before * 100).toFixed(1)}% → {(alert.probability_after * 100).toFixed(1)}%
        </span>
        <Pill tone={up ? "good" : "critical"}>{up ? "+" : ""}{alert.shift_pct.toFixed(1)} pts</Pill>
        <Pill tone={alert.tactical_impact === "CRITICAL" ? "critical" : "warning"}>{alert.tactical_impact}</Pill>
        <Pill>credibility {(alert.source_credibility * 100).toFixed(0)}%</Pill>
      </div>
    </div>
  );
};
