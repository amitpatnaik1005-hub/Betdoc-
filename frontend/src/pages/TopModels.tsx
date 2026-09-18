import { motion, useReducedMotion } from 'framer-motion';
import { useId, useMemo, useState, useEffect, type ReactElement } from 'react';
import { AnimatedGlyph, BRAND, SURFACE } from '../ui/brand';

type AccentTone = 'positive' | 'negative' | 'neutral';

type ModelStatus = 'online' | 'training' | 'offline';
type TelemetrySource = 'live' | 'mock';

interface PredictiveModel {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly status: ModelStatus;
  readonly roi_percentage: number;
  readonly strike_rate: number;
  readonly sharpe_ratio: number;
  readonly max_drawdown: number;
  readonly open_positions: number;
  readonly updated_at: string;
}

interface FleetTelemetry {
  readonly onlineCount: number;
  readonly totalPositions: number;
  readonly meanSharpe: number;
  readonly aggregateRoi: number;
}

const computeTelemetry = (models: readonly PredictiveModel[]): FleetTelemetry => {
  if (models.length === 0) return { onlineCount: 0, totalPositions: 0, meanSharpe: 0, aggregateRoi: 0 };
  let onlineCount = 0; let totalPositions = 0; let sharpeSum = 0; let roiSum = 0;
  for (const m of models) {
    if (m.status === 'online') onlineCount++;
    totalPositions += m.open_positions || 0;
    sharpeSum += m.sharpe_ratio;
    roiSum += m.roi_percentage;
  }
  return {
    onlineCount,
    totalPositions,
    meanSharpe: sharpeSum / models.length,
    aggregateRoi: roiSum / models.length
  };
};

const formatRoi = (val: number) => (val > 0 ? '+' : '') + val.toFixed(2) + '%';
const formatPct = (val: number) => val.toFixed(1) + '%';
const formatSharpe = (val: number) => val.toFixed(2);
const toneOf = (roi: number): AccentTone => {
  if (roi > 0) return 'positive';
  if (roi < 0) return 'negative';
  return 'neutral';
};

const useModelTelemetry = () => {
  const [models, setModels] = useState<readonly PredictiveModel[]>([]);
  const [loading, setLoading] = useState(true);
  
  useEffect(() => {
    fetch('/api/v1/models')
      .then(r => r.json())
      .then(d => { setModels(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  return { models, loading, source: 'live' as TelemetrySource };
};

const RAIL_TONE: Record<AccentTone, string> = {
  positive: BRAND.gradient,
  negative: 'bg-gradient-to-r from-rose-500 to-pink-500',
  neutral: 'bg-slate-400 dark:bg-slate-600',
};

const VALUE_TONE: Record<AccentTone, string> = {
  positive: BRAND.gradientText,
  negative:
    'bg-gradient-to-r from-rose-600 to-pink-600 bg-clip-text text-transparent dark:from-rose-400 dark:to-pink-400',
  neutral: SURFACE.primary,
};

const CARD_HOVER: Record<AccentTone, string> = {
  positive:
    'hover:shadow-[0_12px_32px_-8px_rgba(79,70,229,0.35)] hover:ring-indigo-500/30 dark:hover:ring-indigo-400/30',
  negative:
    'hover:shadow-[0_12px_32px_-8px_rgba(244,63,94,0.28)] hover:ring-rose-500/30 dark:hover:ring-rose-400/30',
  neutral:
    'hover:shadow-[0_12px_32px_-8px_rgba(15,23,42,0.12)] hover:ring-slate-900/15 dark:hover:ring-white/20',
};

// ... other consts
const STATUS_PRESENTATION: Record<
  ModelStatus,
  { readonly label: string; readonly dot: string; readonly text: string }
> = {
  online: {
    label: 'Online',
    dot: 'bg-emerald-500 animate-pulse',
    text: 'text-emerald-700 dark:text-emerald-300',
  },
  training: { label: 'Training', dot: 'bg-indigo-500', text: 'text-indigo-700 dark:text-indigo-300' },
  offline: { label: 'Offline', dot: 'bg-slate-300 dark:bg-slate-600', text: SURFACE.muted },
};

const PILL =
  'inline-flex items-center gap-1.5 rounded-full bg-white px-2.5 py-1 text-[11px] font-medium ring-1 ring-inset ring-slate-200 dark:bg-[#11161D] dark:ring-white/10';

const CARD = `rounded-2xl ${SURFACE.card} transition-[box-shadow,transform] duration-300 hover:-translate-y-0.5`;

interface MetricProps {
  readonly label: string;
  readonly value: string;
  readonly valueClass?: string;
  readonly align?: 'left' | 'right';
}

const Metric = ({ label, value, valueClass = SURFACE.primary, align = 'left' }: MetricProps) => (
  <div className={align === 'right' ? 'text-right' : 'text-left'}>
    <p className={SURFACE.eyebrow}>{label}</p>
    <p className={`mt-1 text-[15px] font-semibold leading-none tracking-tight tabular-nums ${valueClass}`}>
      {value}
    </p>
  </div>
);

const relativeTimeFrom = (nowMs: number, iso: string): string => {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 'Unknown';
  const s = Math.max(0, Math.round((nowMs - then) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
};

/** Three-node neural motif; signal travels along the edges, nodes fire in sequence. */
const NeuralMark = ({ size = 20 }: { readonly size?: number }): ReactElement => {
  const reduceMotion = useReducedMotion();
  const gradientId = useId();
  const nodes = [
    { cx: 5, cy: 17, color: BRAND.azure },
    { cx: 12, cy: 6, color: BRAND.dominant },
    { cx: 19, cy: 14, color: BRAND.violet },
    { cx: 12, cy: 18, color: BRAND.dominant },
  ] as const;
  const edges = ['M5 17L12 6', 'M12 6L19 14', 'M5 17L12 18', 'M12 18L19 14'] as const;

  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" aria-hidden="true">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor={BRAND.azure} />
          <stop offset="50%" stopColor={BRAND.dominant} />
          <stop offset="100%" stopColor={BRAND.violet} />
        </linearGradient>
      </defs>
      {edges.map((d, i) => (
        <motion.path
          key={d}
          d={d}
          stroke={`url(#${gradientId})`}
          strokeWidth="1.5"
          strokeLinecap="round"
          initial={reduceMotion ? false : { pathLength: 0, opacity: 0.3 }}
          animate={reduceMotion ? { pathLength: 1, opacity: 0.6 } : { pathLength: [0, 1, 1], opacity: [0.3, 0.9, 0.3] }}
          transition={{ duration: 2.4, repeat: Infinity, delay: i * 0.3, ease: 'easeInOut' }}
        />
      ))}
      {nodes.map((n, i) => (
        <motion.circle
          key={`${n.cx}-${n.cy}`}
          cx={n.cx}
          cy={n.cy}
          r="2.2"
          fill={n.color}
          animate={reduceMotion ? undefined : { scale: [1, 1.35, 1], opacity: [0.7, 1, 0.7] }}
          transition={{ duration: 2.4, repeat: Infinity, delay: i * 0.3, ease: 'easeInOut' }}
          style={{ originX: `${n.cx}px`, originY: `${n.cy}px` }}
        />
      ))}
    </svg>
  );
};

const FleetSectionTitle = ({ title, caption }: { readonly title: string; readonly caption: string }) => (
  <div className="flex items-center gap-2.5">
    <span className={`grid size-8 place-items-center rounded-lg ${SURFACE.card}`}>
      <NeuralMark size={18} />
    </span>
    <div>
      <h2 className={`text-[13px] font-semibold leading-none tracking-tight ${SURFACE.primary}`}>{title}</h2>
      <p className={`mt-1 text-[11px] ${SURFACE.secondary}`}>{caption}</p>
    </div>
  </div>
);

const ModelCard = ({ model, nowMs }: { readonly model: PredictiveModel; readonly nowMs: number }) => {
  const tone = toneOf(model.roi_percentage);
  const status = STATUS_PRESENTATION[model.status];
  const strikeWidth = Math.max(0, Math.min(100, model.strike_rate));

  return (
    <article className={`group relative flex h-full flex-col overflow-hidden p-5 ${CARD} ${CARD_HOVER[tone]}`}>
      <span
        aria-hidden="true"
        className={`pointer-events-none absolute inset-x-5 top-0 h-[2px] rounded-b-full ${RAIL_TONE[tone]} opacity-0 transition-opacity duration-300 group-hover:opacity-100`}
      />

      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className={`truncate text-[15px] font-semibold tracking-tight ${SURFACE.primary}`}>{model.name}</h3>
          <p className="mt-0.5 truncate font-mono text-[10px] tabular-nums tracking-wide text-slate-400 dark:text-slate-500">
            {model.id}
          </p>
        </div>
        <span className={`${PILL} shrink-0 ${status.text}`}>
          <span className={`size-1.5 shrink-0 rounded-full ${status.dot}`} aria-hidden="true" />
          {status.label}
        </span>
      </header>

      <p className={`mt-3 line-clamp-2 min-h-[2.5rem] text-[12.5px] leading-relaxed ${SURFACE.secondary}`}>
        {model.description}
      </p>

      <div className="mt-4 grid grid-cols-3 gap-3 rounded-xl bg-slate-50 p-3.5 dark:bg-white/[0.03]">
        <Metric label="ROI" value={formatRoi(model.roi_percentage)} valueClass={VALUE_TONE[tone]} />
        <Metric label="Strike" value={formatPct(model.strike_rate)} />
        <Metric
          label="Sharpe"
          value={formatSharpe(model.sharpe_ratio)}
          valueClass={model.sharpe_ratio >= 1 ? 'text-indigo-600 dark:text-indigo-400' : SURFACE.primary}
          align="right"
        />
      </div>

      <div className="mt-3.5">
        <div className="flex items-center justify-between">
          <p className={SURFACE.eyebrow}>Strike Distribution</p>
          <p className={`text-[11px] font-medium tabular-nums ${SURFACE.secondary}`}>{formatPct(model.strike_rate)}</p>
        </div>
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-white/[0.06]">
          <motion.div
            className={`h-full rounded-full ${RAIL_TONE[tone]}`}
            initial={{ width: 0 }}
            animate={{ width: `${strikeWidth}%` }}
            transition={{ duration: 0.8, ease: [0.22, 1, 0.36, 1] }}
            role="presentation"
          />
        </div>
      </div>

      <div className={`mt-4 grid grid-cols-2 gap-3 border-t pt-3.5 ${SURFACE.divider}`}>
        <Metric
          label="Max Drawdown"
          value={formatPct(model.max_drawdown)}
          valueClass={model.max_drawdown > 0 ? 'text-rose-600 dark:text-rose-400' : SURFACE.primary}
        />
        <Metric label="Open Positions" value={String(model.open_positions)} align="right" />
      </div>

      <footer className={`mt-4 flex items-center gap-1.5 border-t pt-3 ${SURFACE.divider}`}>
        <AnimatedGlyph icon="schedule" motionPreset="none" className="text-[14px] text-slate-300 dark:text-slate-600" />
        <p className={`text-[11px] tabular-nums ${SURFACE.muted}`}>Updated {relativeTimeFrom(nowMs, model.updated_at)}</p>
      </footer>
    </article>
  );
};

const SkeletonBlock = ({ className }: { readonly className: string }) => (
  <span className={`block animate-pulse rounded bg-slate-100 dark:bg-white/[0.06] ${className}`} aria-hidden="true" />
);

const SkeletonCard = () => (
  <div className={`flex h-full flex-col rounded-2xl p-5 ${SURFACE.card}`}>
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <SkeletonBlock className="h-4 w-2/3" />
        <SkeletonBlock className="mt-2 h-2.5 w-1/3" />
      </div>
      <SkeletonBlock className="h-6 w-16 rounded-full" />
    </div>
    <SkeletonBlock className="mt-4 h-3 w-full" />
    <SkeletonBlock className="mt-1.5 h-3 w-5/6" />
    <div className="mt-4 grid grid-cols-3 gap-3 rounded-xl bg-slate-50 p-3.5 dark:bg-white/[0.03]">
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i}>
          <SkeletonBlock className="h-2 w-10" />
          <SkeletonBlock className="mt-2 h-4 w-14" />
        </div>
      ))}
    </div>
    <SkeletonBlock className="mt-5 h-1 w-full rounded-full" />
    <div className={`mt-4 grid grid-cols-2 gap-3 border-t pt-3.5 ${SURFACE.divider}`}>
      <SkeletonBlock className="h-4 w-20" />
      <SkeletonBlock className="ml-auto h-4 w-12" />
    </div>
    <SkeletonBlock className="mt-4 h-3 w-24" />
  </div>
);

const SOURCE_PILL = {
  live: { dot: 'bg-emerald-500 animate-pulse', text: 'text-emerald-700 dark:text-emerald-300', label: 'Live telemetry' },
  mock: { dot: 'bg-slate-300 dark:bg-slate-600', text: SURFACE.muted, label: 'Mock telemetry' },
} as const;

export const TopModels = (): ReactElement => {
  const { models, loading, source } = useModelTelemetry();
  const nowMs = useMemo<number>(() => Date.now(), [models]);
  const ranked = useMemo<readonly PredictiveModel[]>(
    () => [...models].sort((a, b) => b.roi_percentage - a.roi_percentage),
    [models],
  );
  const telemetry = useMemo<FleetTelemetry>(() => computeTelemetry(models), [models]);
  const aggregateTone = toneOf(telemetry.aggregateRoi);

  const src = SOURCE_PILL[source === 'live' ? 'live' : 'mock'];

  return (
    <section className="mx-auto flex w-full max-w-[1600px] flex-col gap-6">
      <header className={`flex flex-wrap items-start justify-between gap-4 border-b pb-5 ${SURFACE.divider}`}>
        <div className="flex items-center gap-3">
          <motion.span
            whileHover={{ scale: 1.05, rotate: -3 }}
            className={`grid size-10 shrink-0 place-items-center rounded-xl ${SURFACE.card} ${BRAND.glow}`}
          >
            <NeuralMark size={24} />
          </motion.span>
          <div className="min-w-0">
            <h1 className={`text-xl font-semibold leading-none tracking-tight ${SURFACE.primary}`}>Model Fleet</h1>
            <p className={`mt-1.5 text-[11.5px] ${SURFACE.secondary}`}>Ranked by realised ROI · refreshed on telemetry tick</p>
          </div>
        </div>

        <div className="flex items-center gap-2" role="status" aria-live="polite">
          <span className={`${PILL} ${src.text}`}>
            <span className={`size-1.5 shrink-0 rounded-full ${src.dot}`} aria-hidden="true" />
            {src.label}
          </span>
          <span className={`${PILL} tabular-nums text-slate-700 dark:text-slate-200`}>
            {loading ? '—' : models.length} model{models.length === 1 ? '' : 's'}
          </span>
        </div>
      </header>

      <FleetSectionTitle title="Fleet Metrics" caption="Aggregate performance across deployed models" />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[
          {
            label: 'Fleet Mean ROI',
            value: loading ? '—' : formatRoi(telemetry.aggregateRoi),
            icon: 'trending_up',
            tone: VALUE_TONE[aggregateTone],
            hover: CARD_HOVER[aggregateTone],
          },
          {
            label: 'Mean Sharpe',
            value: loading ? '—' : formatSharpe(telemetry.meanSharpe),
            icon: 'speed',
            tone: telemetry.meanSharpe >= 1 ? 'text-indigo-600 dark:text-indigo-400' : SURFACE.primary,
            hover: CARD_HOVER.neutral,
          },
          {
            label: 'Open Positions',
            value: loading ? '—' : String(telemetry.totalPositions),
            icon: 'inventory_2',
            tone: SURFACE.primary,
            hover: CARD_HOVER.neutral,
          },
          {
            label: 'Deployed Models',
            value: loading ? '—' : String(models.length),
            icon: 'memory',
            tone: SURFACE.primary,
            hover: CARD_HOVER.neutral,
          },
        ].map((stat) => (
          <div key={stat.label} className={`p-4 ${CARD} ${stat.hover}`}>
            <div className="flex items-center justify-between gap-3">
              <p className={SURFACE.eyebrow}>{stat.label}</p>
              <AnimatedGlyph icon={stat.icon} motionPreset="pulse" className="text-[18px] text-indigo-500 dark:text-indigo-400" />
            </div>
            <motion.p
              key={stat.value}
              initial={{ opacity: 0.4, y: -3 }}
              animate={{ opacity: 1, y: 0 }}
              className={`mt-3 text-[22px] font-semibold leading-none tracking-tight tabular-nums ${stat.tone}`}
            >
              {stat.value}
            </motion.p>
          </div>
        ))}
      </div>

      <FleetSectionTitle title="Top Models" caption="Sorted by ROI, highest first" />

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
        {loading
          ? Array.from({ length: 6 }, (_, i) => <SkeletonCard key={`skeleton-${i}`} />)
          : ranked.map((model) => <ModelCard key={model.id} model={model} nowMs={nowMs} />)}
      </div>
    </section>
  );
};

export default TopModels;
