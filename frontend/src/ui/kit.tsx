/**
 * BetDoc section kit: theme-aware building blocks shared by every page.
 *
 * Every surface reads correctly in light and dark mode (the original glass panels hardcoded
 * white-on-dark). Section colour comes from `--accent`, which the shell sets from the active
 * commander, so each page keeps its identity without per-page colour constants.
 */
import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { animate, motion, useMotionValue, useReducedMotion, useTransform } from "framer-motion";
import type { ResourceResult } from "../lib/resource";
import { formatAgo } from "../lib/format";
import { KPI_GLOSSARY } from "./kpi_glossary";
import { useUIStore } from "../store/useUIStore";
import { CARD_VARIANTS, GRID_VARIANTS, ITEM_VARIANTS, LIST_VARIANTS, SPRING } from "./motion";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

// --------------------------------------------------------------------------- layout
export const Page = ({ children, dense = false }: { children: ReactNode; dense?: boolean }) => {
  // Group 107: the Infinity / Flexi view lifts the reading width so the workspace runs edge to edge
  const infinity = useUIStore((s) => s.isInfinityView);
  return (
    <motion.div
      variants={GRID_VARIANTS}
      initial="hidden"
      animate="show"
      // Group 111: a dense page (a trading desk) keeps its first screen for the work: tighter rhythm, less top margin
      className={cx("mx-auto grid w-full grid-cols-1 gap-x-6 lg:grid-cols-12", dense ? "gap-y-4 py-4 lg:py-5" : "gap-y-10 py-8 lg:py-10",
                    infinity ? "max-w-none" : "max-w-7xl")}
    >
      {children}
    </motion.div>
  );
};

/** Card surface: no border, a warm diffused lift in light mode, a matte step up in dark. */
export const SURFACE =
  "rounded-3xl bg-white shadow-soft dark:bg-stone-900 dark:shadow-none dark:ring-1 dark:ring-inset dark:ring-white/[0.04]";

interface PanelProps {
  title: string;
  icon: string;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Last successful refresh; renders a quiet "updated 12s ago". */
  updatedAt?: number | null;
  subtitle?: string;
}

export const Panel = ({ title, icon, children, actions, className, bodyClassName, updatedAt, subtitle }: PanelProps) => (
  <motion.section variants={CARD_VARIANTS} className={cx("flex min-w-0 flex-col gap-4", className)}>
    {/* Group 109: on a phone the actions drop under the title and wrap, rather than run past the screen's edge */}
    <div className="flex min-h-[32px] flex-wrap items-center justify-between gap-x-3 gap-y-2 px-1">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">{icon}</span>
        <h2 className="truncate text-[15px] font-semibold text-stone-900 first-letter:uppercase dark:text-stone-100">{title}</h2>
        {subtitle && <span className="hidden truncate text-xs text-stone-400 sm:inline dark:text-stone-500">{subtitle}</span>}
        {updatedAt ? <UpdatedAgo at={updatedAt} /> : null}
      </div>
      {actions && <div className="flex max-w-full flex-wrap items-center gap-2">{actions}</div>}
    </div>
    <div className={cx(SURFACE, "min-w-0 overflow-hidden", bodyClassName ?? "p-5 sm:p-6")}>{children}</div>
  </motion.section>
);

const UpdatedAgo = ({ at }: { at: number }) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(id);
  }, []);
  return <span className="hidden shrink-0 text-xs text-stone-400 sm:inline dark:text-stone-500">· {formatAgo(at, now)}</span>;
};

// --------------------------------------------------------------------------- buttons
type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "text-[var(--accent-ink)] bg-[var(--accent)] shadow-sm hover:shadow-md hover:brightness-[1.04] disabled:shadow-none",
  secondary:
    "text-stone-700 bg-stone-100 hover:bg-stone-200/70 dark:text-stone-200 dark:bg-stone-800 dark:hover:bg-stone-700/70",
  ghost: "text-stone-600 hover:bg-stone-900/[0.04] dark:text-stone-300 dark:hover:bg-white/[0.05]",
  danger: "text-rose-700 bg-rose-50 hover:bg-rose-100 dark:text-rose-300 dark:bg-rose-500/10 dark:hover:bg-rose-500/15",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  icon?: string;
  busy?: boolean;
}

export const Button = ({ variant = "secondary", size = "md", icon, busy, disabled, children, className, ...rest }: ButtonProps) => (
  <button
    type="button"
    disabled={disabled || busy}
    aria-busy={busy || undefined}
    className={cx(
      "inline-flex select-none items-center justify-center gap-2 rounded-full font-medium",
      "transition-[transform,background-color,box-shadow,filter] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)]",
      "hover:scale-[1.015] active:scale-[0.96] motion-reduce:transform-none",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--accent)_50%,transparent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[#F8F6F0] dark:focus-visible:ring-offset-stone-950",
      "disabled:pointer-events-none disabled:opacity-45",
      size === "sm" ? "px-3.5 py-1.5 text-xs" : "px-5 py-2.5 text-sm",
      VARIANT[variant],
      className,
    )}
    {...rest}
  >
    {busy ? (
      <span className="material-symbols-outlined animate-spin text-[16px]">progress_activity</span>
    ) : icon ? (
      <span className="material-symbols-outlined text-[16px]">{icon}</span>
    ) : null}
    {children}
  </button>
);

/** Two-step confirm for irreversible actions: first click arms it for 4s, second click fires. */
export const ConfirmButton = ({
  onConfirm,
  confirmLabel = "Confirm?",
  children,
  ...rest
}: ButtonProps & { onConfirm: () => void; confirmLabel?: string }) => {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), 4_000);
    return () => window.clearTimeout(id);
  }, [armed]);
  return (
    <Button
      {...rest}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }}
      className={cx(rest.className, armed && "animate-breathe")}
    >
      {armed ? confirmLabel : children}
    </Button>
  );
};

// --------------------------------------------------------------------------- badges & status
export type Tone = "neutral" | "accent" | "good" | "warning" | "serious" | "critical" | "info";

const TONE: Record<Tone, string> = {
  neutral: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300",
  accent: "bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent-text)]",
  good: "bg-emerald-50 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300/90",
  warning: "bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-200/90",
  serious: "bg-orange-50 text-orange-700 dark:bg-orange-400/10 dark:text-orange-200/90",
  critical: "bg-rose-50 text-rose-700 dark:bg-rose-400/10 dark:text-rose-300/90",
  info: "bg-sky-50 text-sky-700 dark:bg-sky-400/10 dark:text-sky-300/90",
};

export const Pill = ({ tone = "neutral", icon, children, className }: { tone?: Tone; icon?: string; children: ReactNode; className?: string }) => (
  <span
    className={cx(
      "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-medium",
      TONE[tone],
      className,
    )}
  >
    {icon && <span className="material-symbols-outlined text-[12px]">{icon}</span>}
    {children}
  </span>
);

/** Status words from any subsystem -> tone + icon, so state is never colour-alone. */
const STATUS_MAP: Record<string, [Tone, string]> = {
  ONLINE: ["good", "check_circle"], OK: ["good", "check_circle"], ACCEPTED: ["good", "check_circle"],
  WON: ["good", "trending_up"], COMPLETED: ["good", "task_alt"], DONE: ["good", "task_alt"], ACTIVE: ["good", "bolt"],
  CONCLUDED: ["good", "flag"], SUCCESS: ["good", "check_circle"], STABLE: ["good", "check_circle"], CONNECTED: ["good", "link"],
  HEALTHY: ["good", "check_circle"], NEEDS_KEY: ["warning", "key"],
  WORKING: ["info", "sync"], RUNNING: ["info", "sync"], IN_PROGRESS: ["info", "sync"], SCANNING: ["info", "radar"],
  QUEUED: ["neutral", "schedule"], PENDING: ["neutral", "schedule"], PENDING_NETWORK: ["warning", "hourglass_top"],
  BACKLOG: ["neutral", "inbox"], REVIEW: ["info", "rate_review"], STANDBY: ["neutral", "pause_circle"],
  IDLE: ["neutral", "pause_circle"], SLEEPING: ["warning", "bedtime"], OFFLINE: ["neutral", "cloud_off"],
  DEGRADED: ["warning", "warning"], HIGH: ["warning", "warning"], WARNING: ["warning", "warning"], BLOCKED: ["warning", "block"],
  MEDIUM: ["warning", "warning"], LOW: ["neutral", "info"], INFO: ["info", "info"], VOID: ["neutral", "remove"],
  UNKNOWN: ["serious", "help"], SATURATED: ["serious", "speed"], EXPIRED: ["neutral", "timer_off"],
  FATAL: ["critical", "error"], FAILED: ["critical", "error"], ERROR: ["critical", "error"], REJECTED: ["critical", "cancel"],
  LOST: ["critical", "trending_down"], CRITICAL: ["critical", "error"], DISABLED: ["neutral", "block"], DOWN: ["critical", "error"],
};

/** "ONLINE" -> "Online": status words read calmly, never shouted. */
const sentence = (text: string): string => (text === text.toUpperCase() ? text.charAt(0) + text.slice(1).toLowerCase() : text);

export const StatusBadge = ({ status, label }: { status: string; label?: string }) => {
  const [tone, icon] = STATUS_MAP[status.toUpperCase()] ?? ["neutral", "radio_button_unchecked"];
  return (
    <Pill tone={tone} icon={icon}>
      {sentence(label ?? status.replace(/_/g, " "))}
    </Pill>
  );
};

export const LiveDot = ({ active, tone = "good" }: { active: boolean; tone?: "good" | "warning" | "critical" }) => {
  const color = tone === "good" ? "bg-emerald-400" : tone === "warning" ? "bg-amber-400" : "bg-rose-400";
  return (
    <span className="relative flex size-2 shrink-0" aria-hidden="true">
      <span className={cx("relative inline-flex size-2 rounded-full", active ? cx(color, "animate-breathe") : "bg-stone-300 dark:bg-stone-600")} />
    </span>
  );
};

// --------------------------------------------------------------------------- numbers
/** Tweens to each new value (Phase 7: numbers glide instead of jumping). */
export const AnimatedNumber = ({ value, format }: { value: number; format: (n: number) => string }) => {
  const reduce = useReducedMotion();
  const mv = useMotionValue(value);
  const text = useTransform(mv, (v) => format(v));
  const first = useRef(true);
  useEffect(() => {
    if (first.current || reduce) {
      first.current = false;
      mv.set(value);
      return;
    }
    const controls = animate(mv, value, { duration: 0.6, ease: [0.22, 1, 0.36, 1] });
    return () => controls.stop();
  }, [value, mv, reduce]);
  return <motion.span className="font-mono tabular-nums">{text}</motion.span>;
};

interface StatProps {
  label: string;
  value: ReactNode;
  icon?: string;
  hint?: ReactNode;
  tone?: "neutral" | "positive" | "negative" | "caution";
}

const STAT_TONE = {
  neutral: "text-stone-800 dark:text-stone-300",
  positive: "text-emerald-700 dark:text-emerald-300/90",
  negative: "text-rose-600 dark:text-rose-300/90",
  caution: "text-amber-700 dark:text-amber-200/90",
};

export const Stat = ({ label, value, icon, hint, tone = "neutral" }: StatProps) => (
  <motion.div variants={ITEM_VARIANTS} className="flex min-w-0 flex-col gap-1.5">
    <div className="flex items-center gap-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">
      {icon && <span className="material-symbols-outlined text-[15px] text-stone-400 dark:text-stone-500">{icon}</span>}
      <span className="truncate first-letter:uppercase">{label}</span>
    </div>
    <div className={cx("truncate font-mono text-[22px] font-medium tracking-tight", STAT_TONE[tone])}>{value}</div>
    {hint && <div className="truncate text-xs text-stone-400 dark:text-stone-500">{hint}</div>}
  </motion.div>
);

export const StatGrid = ({ children, cols = 4 }: { children: ReactNode; cols?: 2 | 3 | 4 | 5 }) => (
  <motion.div
    variants={LIST_VARIANTS}
    initial="hidden"
    animate="show"
    className={cx(
      "grid grid-cols-2 gap-x-8 gap-y-7",
      cols === 3 && "sm:grid-cols-3",
      cols === 4 && "sm:grid-cols-4",
      cols === 5 && "sm:grid-cols-3 xl:grid-cols-5",
    )}
  >
    {children}
  </motion.div>
);

export const Meter = ({ value, tone = "accent", label }: { value: number; tone?: "accent" | "good" | "warning" | "critical"; label?: string }) => {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100;
  const color = { accent: "bg-[var(--accent)]", good: "bg-emerald-500", warning: "bg-amber-500", critical: "bg-rose-500" }[tone];
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800" role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <motion.div className={cx("h-full rounded-full", color)} initial={{ width: 0 }} animate={{ width: `${pct}%` }} transition={SPRING} />
    </div>
  );
};

// --------------------------------------------------------------------------- async states
export const Skeleton = ({ rows = 3 }: { rows?: number }) => (
  <div className="flex flex-col gap-2.5" aria-hidden="true">
    {Array.from({ length: rows }, (_, i) => (
      <div key={i} className="h-10 animate-breathe rounded-2xl bg-stone-100 dark:bg-stone-800/70" style={{ opacity: 1 - i * 0.15 }} />
    ))}
  </div>
);

export const EmptyState = ({ icon = "inbox", title, detail, action }: { icon?: string; title: string; detail?: ReactNode; action?: ReactNode }) => (
  <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
    <span className="mb-1 grid size-12 place-items-center rounded-2xl bg-stone-100 dark:bg-stone-800">
      <span className="material-symbols-outlined text-[22px] text-stone-400 dark:text-stone-500">{icon}</span>
    </span>
    <p className="font-display text-sm font-semibold text-stone-700 dark:text-stone-200">{title}</p>
    {detail && <p className="max-w-sm text-xs leading-relaxed text-stone-500 dark:text-stone-400">{detail}</p>}
    {action && <div className="mt-2">{action}</div>}
  </div>
);

export const ErrorState = ({ message, onRetry }: { message: string; onRetry?: () => void }) => (
  <div role="alert" className="flex flex-col items-center justify-center gap-2 px-4 py-8 text-center">
    <span className="material-symbols-outlined text-[28px] text-rose-500">cloud_off</span>
    <p className="text-sm font-semibold text-stone-700 dark:text-stone-200">Couldn't load this panel</p>
    <p className="max-w-sm break-words font-mono text-[11px] text-stone-500 dark:text-stone-400">{message}</p>
    {onRetry && (
      <Button size="sm" icon="refresh" onClick={onRetry} className="mt-1">
        Retry
      </Button>
    )}
  </div>
);

interface AsyncProps<T> {
  resource: ResourceResult<T>;
  children: (data: T) => ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  skeletonRows?: number;
}

/** Loading skeleton -> error with retry -> empty state -> content. Keeps stale data visible while refetching. */
export function Async<T>({ resource, children, isEmpty, empty, skeletonRows = 3 }: AsyncProps<T>) {
  if (resource.data === undefined) {
    if (resource.error) return <ErrorState message={resource.error} onRetry={() => void resource.refresh()} />;
    return <Skeleton rows={skeletonRows} />;
  }
  if (isEmpty?.(resource.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  return <>{children(resource.data)}</>;
}

// --------------------------------------------------------------------------- forms
export const inputClass =
  "w-full rounded-2xl bg-stone-100/80 px-4 py-2.5 text-sm text-stone-900 ring-1 ring-inset ring-transparent placeholder:text-stone-400 " +
  "transition-[background-color,box-shadow] duration-200 hover:bg-stone-100 " +
  "focus:bg-white focus:shadow-soft focus:outline-none focus:ring-[color-mix(in_srgb,var(--accent)_45%,transparent)] disabled:opacity-50 " +
  "dark:bg-stone-800/70 dark:text-stone-100 dark:placeholder:text-stone-500 dark:hover:bg-stone-800 dark:focus:bg-stone-800";

export const Field = ({ label, hint, children, className }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) => (
  <label className={cx("flex min-w-0 flex-col gap-1.5", className)}>
    <span className="text-xs font-medium text-stone-500 dark:text-stone-400">{label}</span>
    {children}
    {hint && <span className="text-[11px] text-stone-400 dark:text-stone-500">{hint}</span>}
  </label>
);

export const TextInput = (props: InputHTMLAttributes<HTMLInputElement>) => (
  <input spellCheck={false} autoComplete="off" {...props} className={cx(inputClass, props.className)} />
);

export const NumberInput = (props: InputHTMLAttributes<HTMLInputElement>) => (
  <input type="number" inputMode="decimal" step="any" {...props} className={cx(inputClass, "font-mono", props.className)} />
);

export const Select = ({ children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) => (
  <select {...props} className={cx(inputClass, "pr-8", props.className)}>
    {children}
  </select>
);

export const Toggle = ({ checked, onChange, label, disabled }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) => (
  <button
    type="button"
    role="switch"
    aria-checked={checked}
    aria-label={label}
    disabled={disabled}
    onClick={() => onChange(!checked)}
    className={cx(
      "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors duration-200",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 dark:focus-visible:ring-offset-stone-950",
      "disabled:cursor-not-allowed disabled:opacity-50",
      checked ? "bg-[var(--accent)]" : "bg-stone-200 dark:bg-stone-700",
    )}
  >
    <motion.span layout transition={SPRING} className={cx("size-5 rounded-full bg-white shadow-sm", checked ? "ml-[22px]" : "ml-0.5")} />
  </button>
);


// --------------------------------------------------------------------------- segmented control
interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  icon?: string;
}

/** Pill tabs: the active pill glides between options on a soft spring. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  size = "md",
}: {
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (next: T) => void;
  label: string;
  size?: "sm" | "md";
}) {
  const id = useId();
  return (
    <div role="tablist" aria-label={label} className="inline-flex max-w-full flex-wrap gap-0.5 rounded-full bg-stone-100 p-1 dark:bg-stone-800">
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={cx(
              "relative inline-flex items-center gap-1.5 rounded-full font-medium transition-colors duration-300 active:scale-[0.97]",
              size === "sm" ? "px-3 py-1 text-xs" : "px-3.5 py-1.5 text-[13px]",
              active ? "text-stone-900 dark:text-stone-50" : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200",
            )}
          >
            {active && <motion.span layoutId={`seg-${id}`} className="absolute inset-0 rounded-full bg-white shadow-sm dark:bg-stone-700" transition={SPRING} />}
            {o.icon && <span className="material-symbols-outlined relative text-[15px]">{o.icon}</span>}
            <span className="relative">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- tables
export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  className?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  rowTitle?: string;
  dense?: boolean;
}

export function DataTable<T>({ columns, rows, rowKey, onRowClick, rowTitle, dense }: DataTableProps<T>) {
  return (
    <div className="-m-5 overflow-x-auto sm:-m-6">
      <table className="mb-3 w-full min-w-[520px] text-left text-sm">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={cx(
                  "whitespace-nowrap px-3 pb-3 pt-5 text-xs font-medium text-stone-400 first:pl-5 last:pr-5 sm:pt-6 sm:first:pl-6 sm:last:pr-6 dark:text-stone-500",
                  c.align === "right" && "text-right",
                  c.align === "center" && "text-center",
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <motion.tbody variants={LIST_VARIANTS} initial="hidden" animate="show">
          {rows.map((row) => (
            <motion.tr
              variants={ITEM_VARIANTS}
              key={rowKey(row)}
              title={rowTitle}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (e) => (e.key === "Enter" ? onRowClick(row) : undefined) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              className={cx(
                "transition-colors duration-200 hover:bg-stone-50 dark:hover:bg-white/[0.02]",
                onRowClick && "cursor-pointer focus-visible:bg-stone-100 focus-visible:outline-none dark:focus-visible:bg-white/[0.04]",
              )}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={cx(
                    "px-3 text-stone-700 first:pl-5 last:pr-5 sm:first:pl-6 sm:last:pr-6 dark:text-stone-300",
                    dense ? "py-2.5" : "py-3.5",
                    c.align === "right" && "text-right font-mono tabular-nums",
                    c.align === "center" && "text-center",
                    c.className,
                  )}
                >
                  {c.render(row)}
                </td>
              ))}
            </motion.tr>
          ))}
        </motion.tbody>
      </table>
    </div>
  );
}

/** Label / value list for detail panels. */
export const KeyValues = ({ items }: { items: { label: string; value: ReactNode }[] }) => (
  <dl className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-x-10 gap-y-4">
    {items.map(({ label, value }) => (
      <div key={label} className="flex min-w-0 items-baseline justify-between gap-4">
        <dt className="shrink-0 text-sm text-stone-500 dark:text-stone-400">{label}</dt>
        <dd className="min-w-0 break-words text-right text-sm font-medium tabular-nums text-stone-800 dark:text-stone-300">{value}</dd>
      </div>
    ))}
  </dl>
);

// --------------------------------------------------------------------------- micro-guides (Group 99)
/** A KPI's plain-English definition on hover, focus or tap (Escape closes). Unknown codes render nothing. */
export const KpiGlossaryPill = ({ kpi }: { kpi: string }) => {
  const [open, setOpen] = useState(false);
  const id = useId();
  const item = KPI_GLOSSARY[kpi.toUpperCase()];
  if (!item) return null;

  return (
    <span className="relative inline-block" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        title={item.shortDesc}
        onClick={() => setOpen((o) => !o)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}
        className="inline-flex items-center gap-1 rounded-full border border-stone-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-stone-700 transition-colors hover:border-[#C89B3C] hover:text-[#9A7424] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C89B3C]/50 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300 dark:hover:border-[#E3BE63] dark:hover:text-[#E3BE63]"
      >
        <span className="font-mono">{item.code.replace(/_/g, " ")}</span>
        <span className="material-symbols-outlined text-[11px] opacity-60" aria-hidden="true">info</span>
      </button>
      {open && (
        <span
          id={id}
          role="tooltip"
          className="absolute left-0 top-full z-50 mt-1.5 block w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-stone-200 bg-white p-3 text-[11px] leading-relaxed text-stone-700 shadow-xl dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
        >
          <span className="block font-semibold text-[#9A7424] dark:text-[#E3BE63]">{item.name}</span>
          <span className="mt-1 block text-stone-600 dark:text-stone-300">{item.plainEnglish}</span>
          <span className="mt-1.5 block rounded-md bg-stone-50 p-1.5 font-mono text-[10px] text-stone-500 dark:bg-stone-800/80 dark:text-stone-400">
            {item.example}
          </span>
        </span>
      )}
    </span>
  );
};

export interface ComponentGuideModalProps {
  moduleName: string;
  purpose: string;
  steps: [string, string, string];
  kpiCodes?: string[];
  className?: string;
}
export type ComponentGuideBannerProps = ComponentGuideModalProps;

/** Every section's guide (Group 111): a one-line ribbon (what the section is for, its key figures) that opens the full
 * guide (the purpose, three steps, the figures explained) in a sheet. The sheet is portalled to the page so no animated
 * ancestor can clip it; Escape, the backdrop or the button close it, and focus returns to the Guide button. */
export const ComponentGuideBanner = ({ moduleName, purpose, steps, kpiCodes = [], className }: ComponentGuideModalProps) => {
  const [isOpen, setIsOpen] = useState(false);
  const titleId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const close = () => {
    setIsOpen(false);
    trigger.current?.focus();
  };

  useEffect(() => {
    if (!isOpen) return;
    sheet.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setIsOpen(false);
        trigger.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen]);

  return (
    <section
      aria-label={`${moduleName} guide`}
      className={cx(
        "flex min-h-9 min-w-0 items-center justify-between gap-3 rounded-2xl border border-stone-200/70 bg-white/70 px-3 py-1.5 dark:border-white/[0.07] dark:bg-stone-900/50",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="material-symbols-outlined shrink-0 text-[17px] text-[#C89B3C]" aria-hidden="true">school</span>
        <h2 className="shrink-0 text-xs font-semibold text-stone-800 dark:text-stone-100">{moduleName}</h2>
        <p className="hidden min-w-0 truncate text-[11px] text-stone-500 md:block dark:text-stone-400" title={purpose}>{purpose}</p>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <span className="hidden items-center gap-1 lg:flex">
          {kpiCodes.slice(0, 3).map((code) => <KpiGlossaryPill key={code} kpi={code} />)}
        </span>
        <button
          ref={trigger}
          type="button"
          onClick={() => setIsOpen(true)}
          aria-haspopup="dialog"
          aria-label={`Open the guide to ${moduleName}`}
          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold text-[#9A7424] transition-colors hover:bg-[#C89B3C]/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C89B3C]/50 dark:text-[#E3BE63]"
        >
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">help</span>
          Guide
        </button>
      </div>

      {isOpen && typeof document !== "undefined" && createPortal(
        <div className="fixed inset-0 z-[70] flex items-end justify-center bg-stone-950/50 p-3 backdrop-blur-sm sm:items-center sm:p-4" role="presentation"
             onClick={(e) => e.target === e.currentTarget && close()}>
          <div ref={sheet} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
               className="w-full max-w-lg rounded-3xl bg-white p-5 shadow-soft-lg outline-none sm:p-6 dark:bg-stone-900 dark:ring-1 dark:ring-white/10">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="grid size-8 shrink-0 place-items-center rounded-xl bg-[#C89B3C]/15 text-[#9A7424] dark:text-[#E3BE63]" aria-hidden="true">
                  <span className="material-symbols-outlined text-[18px]">school</span>
                </span>
                <h2 id={titleId} className="min-w-0 font-display text-base font-bold text-stone-900 dark:text-stone-100">{moduleName}</h2>
              </div>
              <button type="button" onClick={close} aria-label="Close the guide"
                      className="grid size-8 shrink-0 place-items-center rounded-xl text-stone-500 transition-colors hover:bg-stone-100 dark:hover:bg-white/[0.06]">
                <span className="material-symbols-outlined text-[20px]" aria-hidden="true">close</span>
              </button>
            </div>
            <p className="mt-4 text-sm leading-relaxed text-stone-700 dark:text-stone-200">{purpose}</p>
            <ol className="mt-4 flex flex-col gap-2">
              {steps.map((step, idx) => (
                <li key={idx} className="flex items-start gap-2.5 rounded-2xl bg-stone-50 p-3 text-xs leading-snug text-stone-700 dark:bg-white/[0.04] dark:text-stone-300">
                  <span className="grid size-5 shrink-0 place-items-center rounded-full bg-[#C89B3C]/15 font-mono text-[10px] font-bold text-[#9A7424] dark:text-[#E3BE63]" aria-hidden="true">
                    {idx + 1}
                  </span>
                  <span>{step}</span>
                </li>
              ))}
            </ol>
            {kpiCodes.length > 0 && (
              <div className="mt-4 flex flex-wrap items-center gap-1.5">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-stone-400">Key figures</span>
                {kpiCodes.map((code) => <KpiGlossaryPill key={code} kpi={code} />)}
              </div>
            )}
            <div className="mt-5 flex justify-end">
              <button type="button" onClick={close}
                      className="rounded-full bg-[#C89B3C] px-4 py-2 text-xs font-bold text-stone-950 transition-colors hover:bg-[#D9AC4D] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C89B3C]/50">
                Got it
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </section>
  );
};
