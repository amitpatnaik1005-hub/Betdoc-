/**
 * Charts for section panels, built to the data-viz method:
 * one y-axis, 2px lines, recessive grid, ink-coloured text, a legend for >= 2 series plus direct end
 * labels, crosshair + tooltip on hover/focus, and an accessible table fallback. Colours are the
 * validated `--viz-*` roles from index.css (light and dark steps), never ad-hoc hex.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

export interface Series {
  name: string;
  /** CSS colour role, e.g. "var(--viz-series-1)". */
  color: string;
  values: readonly number[];
  dashed?: boolean;
}

interface LineChartProps {
  labels: readonly string[];
  series: readonly Series[];
  formatValue: (n: number) => string;
  height?: number;
  /** Caption read by screen readers and shown above the table fallback. */
  caption: string;
  area?: boolean;
}

const PAD = { top: 12, right: 64, bottom: 24, left: 56 };

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) {
    const pad = Math.abs(min) * 0.1 || 1;
    min -= pad;
    max += pad;
  }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const start = Math.floor(min / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= max + step * 0.5; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(240, entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function LineChart({ labels, series, formatValue, height = 220, caption, area = true }: LineChartProps) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const gradientId = useId().replace(/:/g, "");
  const n = labels.length;

  const { ticks, x, y } = useMemo(() => {
    const all = series.flatMap((s) => s.values.filter(Number.isFinite));
    const ticks = niceTicks(Math.min(...all), Math.max(...all));
    const lo = ticks[0];
    const hi = ticks[ticks.length - 1];
    const innerW = width - PAD.left - PAD.right;
    const innerH = height - PAD.top - PAD.bottom;
    return {
      ticks,
      x: (i: number) => PAD.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW),
      y: (v: number) => PAD.top + innerH - ((v - lo) / (hi - lo || 1)) * innerH,
    };
  }, [series, width, height, n]);

  if (n === 0) return null;

  const path = (values: readonly number[]) => values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const baseline = y(ticks[0]);
  const xLabelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((width - PAD.left - PAD.right) / 90))));

  const onMove = (clientX: number) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    const rel = clientX - rect.left - PAD.left;
    const innerW = width - PAD.left - PAD.right;
    setHover(Math.max(0, Math.min(n - 1, Math.round((rel / innerW) * (n - 1)))));
  };

  return (
    <figure className="flex flex-col gap-2">
      {series.length >= 2 && (
        <figcaption className="flex flex-wrap items-center gap-4 text-[11px] text-stone-600 dark:text-stone-300" aria-hidden="true">
          {series.map((s) => (
            <span key={s.name} className="inline-flex items-center gap-1.5">
              <svg width="18" height="6" aria-hidden="true">
                <line x1="0" y1="3" x2="18" y2="3" stroke={s.color} strokeWidth="2" strokeDasharray={s.dashed ? "4 3" : undefined} strokeLinecap="round" />
              </svg>
              {s.name}
            </span>
          ))}
        </figcaption>
      )}
      <div
        ref={ref}
        className="relative w-full touch-none select-none outline-none"
        tabIndex={0}
        role="img"
        aria-label={caption}
        onMouseMove={(e) => onMove(e.clientX)}
        onMouseLeave={() => setHover(null)}
        onFocus={() => setHover(n - 1)}
        onBlur={() => setHover(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? n - 1) - 1));
          if (e.key === "ArrowRight") setHover((h) => Math.min(n - 1, (h ?? 0) + 1));
        }}
      >
        <svg width={width} height={height} className="block overflow-visible">
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={series[0]?.color} stopOpacity="0.16" />
              <stop offset="100%" stopColor={series[0]?.color} stopOpacity="0" />
            </linearGradient>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} stroke="var(--viz-grid)" strokeWidth="1" />
              <text x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="fill-stone-400 text-[10px] tabular-nums dark:fill-stone-500">
                {formatValue(t)}
              </text>
            </g>
          ))}
          {labels.map((label, i) =>
            i % xLabelEvery === 0 || i === n - 1 ? (
              <text key={label + i} x={x(i)} y={height - 6} textAnchor="middle" className="fill-stone-400 text-[10px] dark:fill-stone-500">
                {label}
              </text>
            ) : null,
          )}
          {area && series[0] && n > 1 && (
            <path d={`${path(series[0].values)}L${x(n - 1)},${baseline}L${x(0)},${baseline}Z`} fill={`url(#${gradientId})`} />
          )}
          {series.map((s) => (
            <path
              key={s.name}
              d={path(s.values)}
              fill="none"
              stroke={s.color}
              strokeWidth="2"
              strokeLinejoin="round"
              strokeLinecap="round"
              strokeDasharray={s.dashed ? "5 4" : undefined}
            />
          ))}
          {/* Direct end labels so identity is never colour-alone */}
          {series.map((s) => {
            const last = s.values[n - 1];
            return Number.isFinite(last) ? (
              <text key={s.name} x={x(n - 1) + 8} y={y(last)} dy="0.32em" className="fill-stone-600 text-[10px] font-semibold dark:fill-stone-300">
                {series.length >= 2 ? s.name : formatValue(last)}
              </text>
            ) : null;
          })}
          {hover !== null && (
            <g>
              <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={baseline} stroke="var(--viz-axis)" strokeWidth="1" strokeDasharray="3 3" />
              {series.map((s) => (
                <circle key={s.name} cx={x(hover)} cy={y(s.values[hover])} r="4.5" fill={s.color} stroke="var(--chart-ring, white)" strokeWidth="2" className="[--chart-ring:white] dark:[--chart-ring:#1c1917]" />
              ))}
            </g>
          )}
        </svg>
        {hover !== null && (
          <div
            className="pointer-events-none absolute top-1 z-10 min-w-[150px] rounded-xl bg-white px-3 py-2 text-xs shadow-lg ring-1 ring-stone-900/10 dark:bg-[#292524] dark:ring-white/10"
            style={x(hover) > width / 2 ? { right: width - x(hover) + 12 } : { left: x(hover) + 12 }}
          >
            <p className="mb-1 font-semibold text-stone-800 dark:text-stone-100">{labels[hover]}</p>
            {series.map((s) => (
              <p key={s.name} className="flex items-center justify-between gap-4 text-stone-600 dark:text-stone-300">
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-full" style={{ background: s.color }} />
                  {s.name}
                </span>
                <span className="font-semibold tabular-nums text-stone-900 dark:text-stone-50">{formatValue(s.values[hover])}</span>
              </p>
            ))}
          </div>
        )}
      </div>
      <TableFallback caption={caption} labels={labels} series={series} formatValue={formatValue} />
    </figure>
  );
}

const TableFallback = ({ caption, labels, series, formatValue }: { caption: string; labels: readonly string[]; series: readonly Series[]; formatValue: (n: number) => string }) => (
  <table className="sr-only">
    <caption>{caption}</caption>
    <thead>
      <tr>
        <th scope="col">Period</th>
        {series.map((s) => (
          <th key={s.name} scope="col">{s.name}</th>
        ))}
      </tr>
    </thead>
    <tbody>
      {labels.map((label, i) => (
        <tr key={label + i}>
          <th scope="row">{label}</th>
          {series.map((s) => (
            <td key={s.name}>{formatValue(s.values[i])}</td>
          ))}
        </tr>
      ))}
    </tbody>
  </table>
);

// --------------------------------------------------------------------------- diverging bars
export interface BarItem {
  label: string;
  value: number;
  detail?: ReactNode;
}

/**
 * Signed magnitudes around a zero line: positive in --viz-positive, negative in --viz-negative
 * (the validated diverging pair). Values are printed in ink, with a sign, so polarity is never colour-alone.
 */
export function DivergingBars({ items, formatValue, caption }: { items: readonly BarItem[]; formatValue: (n: number) => string; caption: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const maxAbs = Math.max(1e-9, ...items.map((i) => Math.abs(i.value)));
  const hasNegative = items.some((i) => i.value < 0);
  return (
    <figure aria-label={caption} className="flex flex-col gap-1.5">
      {items.map((item, idx) => {
        const share = Math.abs(item.value) / maxAbs;
        const positive = item.value >= 0;
        return (
          <div
            key={item.label}
            className="group relative grid grid-cols-[minmax(80px,140px)_1fr_auto] items-center gap-3 rounded-lg px-1 py-1 hover:bg-stone-900/[0.03] dark:hover:bg-white/[0.03]"
            onMouseEnter={() => setHover(idx)}
            onMouseLeave={() => setHover(null)}
          >
            <span className="truncate text-xs text-stone-600 dark:text-stone-300" title={item.label}>{item.label}</span>
            <div className={hasNegative ? "grid grid-cols-2" : "grid grid-cols-1"}>
              {hasNegative && (
                <div className="flex justify-end border-r border-[var(--viz-axis)]/40">
                  {!positive && <div className="h-3 rounded-l-[4px]" style={{ width: `${share * 100}%`, background: "var(--viz-negative)" }} />}
                </div>
              )}
              <div className="flex justify-start">
                {positive && <div className="h-3 rounded-r-[4px]" style={{ width: `${share * 100}%`, background: "var(--viz-positive)" }} />}
              </div>
            </div>
            <span className="min-w-[88px] text-right text-xs font-semibold tabular-nums text-stone-800 dark:text-stone-100">
              {positive && item.value > 0 ? "+" : ""}
              {formatValue(item.value)}
            </span>
            {hover === idx && item.detail && (
              <div className="pointer-events-none absolute left-1/3 top-full z-10 mt-1 rounded-xl bg-white px-3 py-2 text-xs text-stone-600 shadow-lg ring-1 ring-stone-900/10 dark:bg-[#292524] dark:text-stone-300 dark:ring-white/10">
                {item.detail}
              </div>
            )}
          </div>
        );
      })}
    </figure>
  );
}
