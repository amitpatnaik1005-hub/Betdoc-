/** Fleet Command building blocks: the spring switch, the write-only key field, quota and status cells. */
import { type FormEvent, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { apiClient } from "../../api/client";
import { formatInt } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import { LiveDot, Pill, SPRING, type Tone } from "../../ui/kit";
import { type FleetRole, type FleetSource, cx, groupLabel, seconds } from "./types";

// --------------------------------------------------------------------------- switch
/** Spring knob; a slow breathing halo while the source is live. */
export const FleetSwitch = ({ on, onChange, label, disabled, busy }: { on: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean; busy?: boolean }) => {
  const reduce = useReducedMotion();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      onClick={() => onChange(!on)}
      className={cx(
        "relative inline-flex h-7 w-[3.25rem] shrink-0 items-center rounded-full p-1 transition-colors duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[#F8F6F0] dark:focus-visible:ring-offset-stone-950",
        "disabled:cursor-not-allowed disabled:opacity-50",
        on ? "justify-end bg-[var(--accent)]" : "justify-start bg-stone-200 dark:bg-stone-700",
      )}
    >
      {on && !reduce && (
        <motion.span
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 rounded-full"
          initial={{ boxShadow: "0 0 0 0 var(--accent-glow)" }}
          animate={{ boxShadow: ["0 0 0 0 var(--accent-glow)", "0 0 0 7px rgba(0,0,0,0)"] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: "easeOut" }}
        />
      )}
      <motion.span layout transition={SPRING} className="relative grid size-5 place-items-center rounded-full bg-white shadow-sm dark:bg-stone-100">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={busy ? "busy" : on ? "on" : "off"}
            initial={{ scale: 0.4, opacity: 0, rotate: -40 }}
            animate={{ scale: 1, opacity: 1, rotate: 0 }}
            exit={{ scale: 0.4, opacity: 0 }}
            transition={{ duration: 0.22 }}
            className={cx("material-symbols-outlined text-[12px]", busy && "animate-spin", on ? "text-[var(--accent-text)]" : "text-stone-400")}
          >
            {busy ? "progress_activity" : on ? "bolt" : "power_settings_new"}
          </motion.span>
        </AnimatePresence>
      </motion.span>
    </button>
  );
};

// --------------------------------------------------------------------------- write-only key
/** The key leaves this component once, encrypted server-side; only a masked hint ever comes back. */
export const SecureKeyInline = ({ source, canEdit, vaultReady, compact }: { source: FleetSource; canEdit: boolean; vaultReady: boolean; compact?: boolean }) => {
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const id = source.source_id;

  if (!source.requires_api_key) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-stone-400 dark:text-stone-500">
        <span className="material-symbols-outlined text-[14px] text-emerald-600 dark:text-emerald-400">public</span>
        No key needed
      </span>
    );
  }

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const key = value.trim();
    if (key.length < 8) return;
    setBusy(true);
    const ok = await runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/${id}/api-key`, { api_key: key }), {
      invalidate: ["fleet"],
      success: `${source.display_name} key encrypted and stored`,
      errorTitle: "Key rejected",
    });
    setBusy(false);
    if (ok) {
      setValue("");
      setReveal(false);
    }
  };

  const stored = source.key_origin === "vault" ? source.api_key_hint ?? "in vault" : source.key_origin === "environment" ? `env ${source.secret_env ?? ""}`.trim() : "missing";
  const locked = !canEdit || !vaultReady || busy;

  return (
    <form onSubmit={save} autoComplete="off" className={cx("flex flex-col gap-1", compact ? "w-32" : "w-full max-w-sm")}>
      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <input
            aria-label={`${source.display_name} API key`}
            name={`fleet-${id}-key`}
            type={reveal ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={source.has_api_key ? "Replace key" : "Paste key"}
            autoComplete="new-password"
            spellCheck={false}
            data-lpignore="true"
            data-1p-ignore="true"
            disabled={locked}
            className="w-full rounded-full bg-stone-100/80 py-1.5 pl-3 pr-8 font-mono text-xs text-stone-800 ring-1 ring-inset ring-transparent transition-[background-color,box-shadow] placeholder:font-sans placeholder:text-stone-400 focus:bg-white focus:outline-none focus:ring-[color-mix(in_srgb,var(--accent)_45%,transparent)] disabled:opacity-50 dark:bg-stone-800/70 dark:text-stone-100 dark:focus:bg-stone-800"
          />
          <button
            type="button"
            onClick={() => setReveal((r) => !r)}
            disabled={!value}
            aria-label={reveal ? "Hide key" : "Show key"}
            className="absolute right-1.5 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded-full text-stone-400 hover:text-stone-600 disabled:opacity-30"
          >
            <span className="material-symbols-outlined text-[14px]">{reveal ? "visibility_off" : "visibility"}</span>
          </button>
        </div>
        <motion.button
          whileTap={{ scale: 0.9 }}
          type="submit"
          aria-label="Encrypt and store key"
          disabled={locked || value.trim().length < 8}
          className="grid size-7 shrink-0 place-items-center rounded-full bg-[var(--accent)] text-[var(--accent-ink)] shadow-sm transition-opacity disabled:opacity-35"
        >
          <span className={cx("material-symbols-outlined text-[15px]", busy && "animate-spin")}>{busy ? "progress_activity" : "lock"}</span>
        </motion.button>
      </div>
      <span className={cx("flex items-center gap-1 truncate pl-3 text-[10px]", source.has_api_key ? "text-stone-400 dark:text-stone-500" : "text-amber-700 dark:text-amber-300/90")}>
        <span className="material-symbols-outlined text-[11px]">{source.has_api_key ? "key" : "key_off"}</span>
        {stored}
      </span>
    </form>
  );
};

// --------------------------------------------------------------------------- cells
export const BreakerCell = ({ source }: { source: FleetSource }) => {
  if (!source.is_enabled || source.status === "FATAL") return <span className="text-xs text-stone-400">—</span>;
  if (source.breaker_state === "open")
    return (
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-rose-50 px-2.5 py-0.5 text-[11px] font-medium text-rose-700 dark:bg-rose-400/10 dark:text-rose-300">
        <span className="material-symbols-outlined text-[12px]">power_off</span>
        Tripped{source.breaker_remaining_seconds ? ` · ${seconds(source.breaker_remaining_seconds)}` : ""}
      </span>
    );
  if (source.breaker_state === "half_open")
    return (
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-400/10 dark:text-amber-200">
        <span className="material-symbols-outlined text-[12px]">science</span>
        Trial run
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium text-emerald-700 dark:text-emerald-300/90">
      <LiveDot active /> Live
    </span>
  );
};

export const QuotaBar = ({ source, reserve }: { source: FleetSource; reserve: number }) => {
  if (source.cost === "free") return <span className="text-xs text-stone-400">∞ free</span>;
  const fraction = source.quota_fraction;
  if (fraction === null)
    return <span className="text-xs text-stone-400">{source.quota_remaining !== null ? `${formatInt(source.quota_remaining)} left` : "unreported"}</span>;
  const tone = fraction < reserve ? "bg-rose-500" : fraction < 0.25 ? "bg-amber-500" : "bg-emerald-500";
  const total = source.quota_limit ?? (source.quota_used !== null && source.quota_remaining !== null ? source.quota_used + source.quota_remaining : null);
  return (
    <div className="flex w-24 flex-col gap-1" title={`${(fraction * 100).toFixed(1)}% left · failover below ${(reserve * 100).toFixed(0)}%`}>
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800">
        <motion.div className={cx("h-full rounded-full", tone)} initial={{ width: 0 }} animate={{ width: `${Math.max(fraction * 100, 1.5)}%` }} transition={SPRING} />
        <span className="absolute inset-y-0 w-px bg-stone-400/70" style={{ left: `${reserve * 100}%` }} aria-hidden="true" />
      </div>
      <span className="font-mono text-[10px] tabular-nums text-stone-500 dark:text-stone-400">
        {formatInt(source.quota_remaining)}
        {total !== null ? ` / ${formatInt(total)}` : ""}
      </span>
    </div>
  );
};

const ROLE: Record<FleetRole, [string, Tone, string]> = {
  primary: ["Primary", "accent", "star"],
  failover: ["Failover", "warning", "swap_horiz"],
  always_on: ["Always on", "info", "all_inclusive"],
  standby: ["Standby", "neutral", "pause_circle"],
  unavailable: ["Out", "neutral", "block"],
};

const REASON: Record<string, string> = {
  quota_reserve: "quota reserve",
  circuit_open: "circuit open",
  disabled: "switched off",
  paused: "dead-lettered",
  needs_key: "needs key",
};

export const FailoverCell = ({ source, names }: { source: FleetSource; names: Record<string, string> }) => {
  const [label, tone, icon] = ROLE[source.role];
  const detail =
    source.role === "failover"
      ? source.covering.map((n) => `${groupLabel(n.group)} for ${names[n.replacing] ?? n.replacing} (${REASON[n.reason] ?? n.reason})`).join("; ")
      : source.role === "unavailable"
        ? REASON[source.availability] ?? source.availability
        : source.scope.length
          ? source.scope.map(groupLabel).join(", ")
          : "no market needs it now";
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <Pill tone={tone} icon={icon} className="self-start">
        {label}
      </Pill>
      <span className="max-w-[8.5rem] truncate pl-1 text-[10px] text-stone-400 dark:text-stone-500" title={detail}>
        {detail}
      </span>
    </div>
  );
};
