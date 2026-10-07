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
  useRef,
  useState,
} from "react";
import { animate, motion, useMotionValue, useReducedMotion, useTransform } from "framer-motion";
import type { ResourceResult } from "../lib/resource";
import { formatAgo } from "../lib/format";

// --------------------------------------------------------------------------- motion
export const SPRING = { type: "spring", stiffness: 350, damping: 30 } as const;
export const CARD_VARIANTS = { hidden: { opacity: 0, y: 14 }, show: { opacity: 1, y: 0, transition: SPRING } };
const GRID_VARIANTS = { hidden: { opacity: 0 }, show: { opacity: 1, transition: { staggerChildren: 0.06 } } };

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

// --------------------------------------------------------------------------- layout
export const Page = ({ children }: { children: ReactNode }) => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="mx-auto grid w-full max-w-7xl grid-cols-1 gap-6 py-6 lg:grid-cols-12"
  >
    {children}
  </motion.div>
);

const SURFACE =
  "rounded-2xl bg-white ring-1 ring-inset ring-slate-900/[0.06] shadow-[0_1px_2px_rgba(15,23,42,0.04)] " +
  "dark:bg-[#161514] dark:ring-white/[0.08] dark:shadow-none";

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
  <motion.section variants={CARD_VARIANTS} className={cx("flex min-w-0 flex-col gap-3", className)}>
    <div className="flex min-h-[28px] items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <span className="material-symbols-outlined text-[18px] text-accent">{icon}</span>
        <h2 className="truncate text-[11px] font-bold uppercase tracking-[0.16em] text-slate-700 dark:text-slate-200">{title}</h2>
        {subtitle && <span className="truncate text-[11px] text-slate-400 dark:text-slate-500">· {subtitle}</span>}
        {updatedAt ? <UpdatedAgo at={updatedAt} /> : null}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
    <div className={cx(SURFACE, "min-w-0 overflow-hidden", bodyClassName ?? "p-4")}>{children}</div>
  </motion.section>
);

const UpdatedAgo = ({ at }: { at: number }) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(id);
  }, []);
  return <span className="hidden text-[10px] text-slate-400 sm:inline dark:text-slate-500">· {formatAgo(at, now)}</span>;
};

// --------------------------------------------------------------------------- buttons
type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary:
    "text-[var(--accent-ink)] bg-[var(--accent)] hover:brightness-110 shadow-[0_6px_18px_-8px_var(--accent-glow)] disabled:shadow-none",
  secondary:
    "text-slate-700 bg-white ring-1 ring-inset ring-slate-900/10 hover:bg-slate-50 dark:text-slate-200 dark:bg-white/[0.04] dark:ring-white/10 dark:hover:bg-white/[0.08]",
  ghost: "text-slate-600 hover:bg-slate-900/[0.04] dark:text-slate-300 dark:hover:bg-white/[0.06]",
  danger:
    "text-rose-700 bg-rose-50 ring-1 ring-inset ring-rose-600/20 hover:bg-rose-100 dark:text-rose-300 dark:bg-rose-500/10 dark:ring-rose-400/25 dark:hover:bg-rose-500/20",
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
      "inline-flex items-center justify-center gap-1.5 rounded-xl font-semibold transition-all duration-150",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#121110]",
      "disabled:cursor-not-allowed disabled:opacity-50 active:scale-[0.98]",
      size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-4 py-2.5 text-sm",
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
      className={cx(rest.className, armed && "animate-pulse")}
    >
      {armed ? confirmLabel : children}
    </Button>
  );
};

// --------------------------------------------------------------------------- badges & status
export type Tone = "neutral" | "accent" | "good" | "warning" | "serious" | "critical" | "info";

const TONE: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-600 ring-slate-900/10 dark:bg-white/[0.06] dark:text-slate-300 dark:ring-white/10",
  accent: "bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent-text)] ring-[color-mix(in_srgb,var(--accent)_30%,transparent)]",
  good: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-400/20",
  warning: "bg-amber-50 text-amber-700 ring-amber-600/25 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-400/20",
  serious: "bg-orange-50 text-orange-700 ring-orange-600/25 dark:bg-orange-500/10 dark:text-orange-300 dark:ring-orange-400/20",
  critical: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-400/20",
  info: "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-300 dark:ring-sky-400/20",
};

export const Pill = ({ tone = "neutral", icon, children, className }: { tone?: Tone; icon?: string; children: ReactNode; className?: string }) => (
  <span
    className={cx(
      "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ring-inset",
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

export const statusTone = (status: string): Tone => STATUS_MAP[status.toUpperCase()]?.[0] ?? "neutral";

export const StatusBadge = ({ status, label }: { status: string; label?: string }) => {
  const [tone, icon] = STATUS_MAP[status.toUpperCase()] ?? ["neutral", "radio_button_unchecked"];
  return (
    <Pill tone={tone} icon={icon}>
      {label ?? status.replace(/_/g, " ")}
    </Pill>
  );
};

export const LiveDot = ({ active, tone = "good" }: { active: boolean; tone?: "good" | "warning" | "critical" }) => {
  const color = tone === "good" ? "bg-emerald-500" : tone === "warning" ? "bg-amber-500" : "bg-rose-500";
  return (
    <span className="relative flex size-2 shrink-0" aria-hidden="true">
      {active && <span className={cx("absolute inline-flex size-full animate-ping rounded-full opacity-60", color)} />}
      <span className={cx("relative inline-flex size-2 rounded-full", active ? color : "bg-slate-300 dark:bg-slate-600")} />
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
  return <motion.span className="tabular-nums">{text}</motion.span>;
};

interface StatProps {
  label: string;
  value: ReactNode;
  icon?: string;
  hint?: ReactNode;
  tone?: "neutral" | "positive" | "negative" | "caution";
}

const STAT_TONE = {
  neutral: "text-slate-900 dark:text-slate-50",
  positive: "text-emerald-600 dark:text-emerald-400",
  negative: "text-rose-600 dark:text-rose-400",
  caution: "text-amber-600 dark:text-amber-400",
};

export const Stat = ({ label, value, icon, hint, tone = "neutral" }: StatProps) => (
  <div className="flex min-w-0 flex-col gap-1">
    <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400 dark:text-slate-500">
      {icon && <span className="material-symbols-outlined text-[14px]">{icon}</span>}
      <span className="truncate">{label}</span>
    </div>
    <div className={cx("truncate text-xl font-semibold tracking-tight tabular-nums", STAT_TONE[tone])}>{value}</div>
    {hint && <div className="truncate text-[11px] text-slate-500 dark:text-slate-400">{hint}</div>}
  </div>
);

export const StatGrid = ({ children, cols = 4 }: { children: ReactNode; cols?: 2 | 3 | 4 | 5 }) => (
  <div
    className={cx(
      "grid grid-cols-2 gap-x-6 gap-y-5",
      cols === 3 && "sm:grid-cols-3",
      cols === 4 && "sm:grid-cols-4",
      cols === 5 && "sm:grid-cols-3 xl:grid-cols-5",
    )}
  >
    {children}
  </div>
);

export const Meter = ({ value, tone = "accent", label }: { value: number; tone?: "accent" | "good" | "warning" | "critical"; label?: string }) => {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100;
  const color = { accent: "bg-[var(--accent)]", good: "bg-emerald-500", warning: "bg-amber-500", critical: "bg-rose-500" }[tone];
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-900/[0.06] dark:bg-white/[0.07]" role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <motion.div className={cx("h-full rounded-full", color)} initial={{ width: 0 }} animate={{ width: `${pct}%` }} transition={SPRING} />
    </div>
  );
};

// --------------------------------------------------------------------------- async states
export const Skeleton = ({ rows = 3 }: { rows?: number }) => (
  <div className="flex flex-col gap-2.5" aria-hidden="true">
    {Array.from({ length: rows }, (_, i) => (
      <div key={i} className="h-9 animate-pulse rounded-lg bg-slate-900/[0.05] dark:bg-white/[0.05]" style={{ opacity: 1 - i * 0.15 }} />
    ))}
  </div>
);

export const EmptyState = ({ icon = "inbox", title, detail, action }: { icon?: string; title: string; detail?: ReactNode; action?: ReactNode }) => (
  <div className="flex flex-col items-center justify-center gap-2 px-4 py-8 text-center">
    <span className="material-symbols-outlined text-[28px] text-slate-300 dark:text-slate-600">{icon}</span>
    <p className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</p>
    {detail && <p className="max-w-sm text-xs leading-relaxed text-slate-500 dark:text-slate-400">{detail}</p>}
    {action && <div className="mt-2">{action}</div>}
  </div>
);

export const ErrorState = ({ message, onRetry }: { message: string; onRetry?: () => void }) => (
  <div role="alert" className="flex flex-col items-center justify-center gap-2 px-4 py-8 text-center">
    <span className="material-symbols-outlined text-[28px] text-rose-500">cloud_off</span>
    <p className="text-sm font-semibold text-slate-700 dark:text-slate-200">Couldn't load this panel</p>
    <p className="max-w-sm break-words font-mono text-[11px] text-slate-500 dark:text-slate-400">{message}</p>
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
  "w-full rounded-xl bg-white px-3 py-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-900/10 placeholder:text-slate-400 " +
  "focus:outline-none focus:ring-2 focus:ring-[var(--accent)] disabled:opacity-50 " +
  "dark:bg-white/[0.04] dark:text-slate-100 dark:ring-white/10 dark:placeholder:text-slate-500";

export const Field = ({ label, hint, children, className }: { label: string; hint?: ReactNode; children: ReactNode; className?: string }) => (
  <label className={cx("flex min-w-0 flex-col gap-1.5", className)}>
    <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">{label}</span>
    {children}
    {hint && <span className="text-[11px] text-slate-400 dark:text-slate-500">{hint}</span>}
  </label>
);

export const TextInput = (props: InputHTMLAttributes<HTMLInputElement>) => (
  <input spellCheck={false} autoComplete="off" {...props} className={cx(inputClass, props.className)} />
);

export const NumberInput = (props: InputHTMLAttributes<HTMLInputElement>) => (
  <input type="number" inputMode="decimal" step="any" {...props} className={cx(inputClass, "tabular-nums", props.className)} />
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
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#121110]",
      "disabled:cursor-not-allowed disabled:opacity-50",
      checked ? "bg-[var(--accent)]" : "bg-slate-300 dark:bg-white/15",
    )}
  >
    <motion.span layout transition={SPRING} className={cx("size-5 rounded-full bg-white shadow", checked ? "ml-[22px]" : "ml-0.5")} />
  </button>
);

/** Parse a numeric input; NaN for blanks so callers can disable submit. */
export const num = (raw: string): number => (raw.trim() === "" ? Number.NaN : Number(raw));

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
    <div className="-mx-4 -my-4 overflow-x-auto">
      <table className="w-full min-w-[520px] text-left text-sm">
        <thead>
          <tr className="border-b border-slate-900/[0.06] dark:border-white/[0.06]">
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={cx(
                  "whitespace-nowrap px-4 py-2.5 text-[10px] font-bold uppercase tracking-[0.14em] text-slate-400 dark:text-slate-500",
                  c.align === "right" && "text-right",
                  c.align === "center" && "text-center",
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-900/[0.05] dark:divide-white/[0.05]">
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              title={rowTitle}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={onRowClick ? (e) => (e.key === "Enter" ? onRowClick(row) : undefined) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              className={cx(
                "transition-colors",
                onRowClick && "cursor-pointer hover:bg-slate-900/[0.025] focus-visible:bg-slate-900/[0.04] focus-visible:outline-none dark:hover:bg-white/[0.03] dark:focus-visible:bg-white/[0.05]",
              )}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={cx(
                    "px-4 text-slate-700 dark:text-slate-200",
                    dense ? "py-2" : "py-3",
                    c.align === "right" && "text-right tabular-nums",
                    c.align === "center" && "text-center",
                    c.className,
                  )}
                >
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Label / value list for detail panels. */
export const KeyValues = ({ items }: { items: { label: string; value: ReactNode }[] }) => (
  <dl className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-x-6 gap-y-2">
    {items.map(({ label, value }) => (
      <div key={label} className="flex min-w-0 items-baseline justify-between gap-3 border-b border-dashed border-slate-900/[0.07] py-1.5 dark:border-white/[0.07]">
        <dt className="shrink-0 text-xs text-slate-500 dark:text-slate-400">{label}</dt>
        <dd className="min-w-0 break-words text-right text-sm font-medium tabular-nums text-slate-800 dark:text-slate-100">{value}</dd>
      </div>
    ))}
  </dl>
);
