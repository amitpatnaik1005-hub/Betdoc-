/**
 * The exact-scoreline heatmap of BETDOC SCORE PREDICTION (Group 102). Developed for Amit Ashok Kumar Patnaik.
 *
 * Rows are the home side's goals, columns the away side's, 0..N. Each cell is the model's exact probability of that
 * score (never re-normalised: the mass outside the grid is stated under it). The likeliest scores are ringed and
 * listed; a cell is a button, so a scoreline can be chosen to back a parlay leg.
 */
import { formatRatioPct } from "../../lib/format";

interface FootballScoreHeatmapProps {
  heatmap: number[][];
  top: { score: string; probability: number }[];
  /** The heatmap's share of all outcomes. */
  mass: number;
  homeLabel: string;
  awayLabel: string;
  selected?: string | null;
  onSelect?: (score: string, home: number, away: number) => void;
}

export const FootballScoreHeatmap = ({ heatmap, top, mass, homeLabel, awayLabel, selected = null, onSelect }: FootballScoreHeatmapProps) => {
  const peak = Math.max(...heatmap.flat(), 1e-9);
  const goals = heatmap.map((_, i) => i);
  const rank = new Map(top.map((t, i) => [t.score, i + 1]));
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ol className="flex flex-wrap gap-2" aria-label="Most likely scorelines">
        {top.map((t, i) => (
          <li key={t.score} className="inline-flex items-baseline gap-1.5 rounded-xl bg-stone-900/[0.04] px-2.5 py-1 text-xs dark:bg-white/[0.06]">
            <span className="font-mono text-[10px] text-stone-500 dark:text-stone-400">#{i + 1}</span>
            <span className="font-semibold tabular-nums text-stone-900 dark:text-stone-50">{t.score}</span>
            <span className="tabular-nums text-stone-500 dark:text-stone-400">{formatRatioPct(t.probability)}</span>
          </li>
        ))}
      </ol>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[320px] border-separate border-spacing-1 text-center text-[11px]">
          <caption className="sr-only">Probability of each exact score, {homeLabel} goals by row, {awayLabel} goals by column</caption>
          <thead>
            <tr>
              <th scope="col" className="w-16 text-left text-[10px] font-medium text-stone-400">
                <span className="block truncate">{homeLabel} ↓</span>
                <span className="block truncate">{awayLabel} →</span>
              </th>
              {goals.map((a) => <th key={a} scope="col" className="font-semibold text-stone-600 dark:text-stone-300">{a}</th>)}
            </tr>
          </thead>
          <tbody>
            {heatmap.map((row, h) => (
              <tr key={h}>
                <th scope="row" className="text-left font-semibold text-stone-600 dark:text-stone-300">{h}</th>
                {row.map((p, a) => {
                  const score = `${h}-${a}`;
                  const strength = Math.min(1, p / peak);
                  const isTop = rank.has(score);
                  const isSelected = selected === score;
                  return (
                    <td key={a} className="p-0">
                      <button
                        type="button"
                        onClick={() => onSelect?.(score, h, a)}
                        disabled={!onSelect}
                        aria-pressed={onSelect ? isSelected : undefined}
                        aria-label={`${homeLabel} ${h}, ${awayLabel} ${a}: ${formatRatioPct(p)}${isTop ? `, #${rank.get(score)} most likely` : ""}`}
                        className={`w-full rounded-lg px-1 py-2 font-mono tabular-nums transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 enabled:hover:ring-2 enabled:hover:ring-[var(--viz-series-1)] ${
                          isSelected ? "ring-2 ring-[var(--accent)]" : isTop ? "ring-1 ring-[var(--viz-series-1)]" : ""
                        } ${strength > 0.55 ? "font-semibold text-white" : "text-stone-700 dark:text-stone-200"}`}
                        style={{ background: `color-mix(in srgb, var(--viz-series-1) ${Math.round(6 + strength * 84)}%, transparent)` }}
                      >
                        {(p * 100).toFixed(1)}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] text-stone-500 dark:text-stone-400">
        Percent chance of each exact score. {formatRatioPct(1 - mass)} of outcomes have a side scoring more than {goals.length - 1}.
        {onSelect ? " Choose a score to see the legs it backs." : ""}
      </p>
    </div>
  );
};
