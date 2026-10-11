/**
 * Dynamic Multi-Sport Metrics Renderer (Group 79)
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 *
 * Renders an adapter's `sport_specific_metrics` as it returned them: numbers formatted, booleans as states, short
 * lists inline. Nested objects (the consensus block) are left to their own panel.
 */
import { humanize } from '../../lib/format';

interface Props {
  sportKey: string;
  metrics: Record<string, unknown>;
  /** Keys rendered elsewhere on the page. */
  exclude?: readonly string[];
}

const render = (value: unknown): string => {
  if (typeof value === 'boolean') return value ? 'Active' : 'Inactive';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(Math.abs(value) < 1 ? 4 : 2);
  if (Array.isArray(value)) return value.map((v) => (Array.isArray(v) ? v.map(render).join(' ') : render(v))).join(', ');
  if (value === null || value === undefined) return '—';
  return String(value);
};

export const SportSpecificStatCard = ({ sportKey, metrics, exclude = [] }: Props) => {
  const rows = Object.entries(metrics).filter(([k, v]) => !exclude.includes(k) && (typeof v !== 'object' || v === null || Array.isArray(v)));
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between text-[11px] uppercase tracking-[0.12em] text-stone-400">
        <span className="flex items-center gap-1.5 font-semibold">
          <span className="size-2 animate-pulse rounded-full bg-emerald-400" aria-hidden />
          {humanize(sportKey)} quantitative factors
        </span>
        <span>{rows.length} metrics</span>
      </div>
      <dl className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
        {rows.map(([key, val]) => (
          <div key={key} className="rounded-2xl bg-stone-50 p-3 dark:bg-white/[0.03]">
            <dt className="text-[10px] font-semibold uppercase tracking-wide text-stone-400">{key.replace(/_/g, ' ')}</dt>
            <dd className="mt-0.5 break-words font-mono text-sm font-semibold text-stone-800 dark:text-stone-100">{render(val)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
};
