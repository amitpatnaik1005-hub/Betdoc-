/**
 * Control Panel, The Hive: autonomous trading bots (Group 65).
 *
 * - Master kill switch: a sticky red bar. One press halts every bot (a global Redis flag); a flash
 *   crash sets the same flag on its own. Only an administrator resumes.
 * - Bots: each with its isolated sub-account, a capital slider, its pipeline chain and breakers.
 * - Pipeline builder: multi-selects over the seeded model registry (math models, risk models, bet
 *   types). Components a bot cannot run on a live signal are listed but locked ("backtest only").
 * - Collision map: which bot holds what in which market; collisions and opposing sides stand out.
 * - Decision log and TWAP plans.
 */
import { type ReactNode, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ApiError, apiClient } from "../../api/client";
import { useBankroll } from "../../lib/cfo";
import { formatAgo, formatINR, humanize } from "../../lib/format";
import {
  type Bot,
  type BotDraft,
  type Component,
  type ComponentKind,
  type Halt,
  MODE_LABEL,
  NEW_BOT,
  STAKING_MODEL,
  type Topology,
  draftOf,
  payloadOf,
  useHalt,
  useHiveBots,
  useHiveEvents,
  usePlans,
  useRegistry,
  useTopology,
} from "../../lib/hive";
import { invalidate, runMutation } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";
import { Async, Button, CARD_VARIANTS, ConfirmButton, EmptyState, Field, NumberInput, Panel, Pill, SPRING, SURFACE, Segmented, TextInput, Toggle } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const STATUS_TONE = { ACTIVE: "good", PAUSED: "neutral", SUSPENDED: "critical", ARCHIVED: "neutral" } as const;
const MODE_TONE = { PAPER_TRADE: "info", SHADOW_MODE: "neutral", LIVE_EXECUTION: "serious" } as const;
const SELECTION_COLOR: Record<string, string> = { HOME: "#10b981", DRAW: "#f59e0b", AWAY: "#6366f1" };
const EVENT_TONE: Record<string, "good" | "warning" | "critical" | "neutral" | "info"> = {
  FIRED: "good", SLICE_FIRED: "good", SHADOW_FILLED: "info", SLICED: "info", MERGED: "neutral", SKIPPED: "neutral",
  BLOCKED: "warning", SLICE_CANCELLED: "warning", SUSPENDED: "critical", HALTED: "critical", ALLOCATED: "neutral",
};

function refusalText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "No answer from the server";
}

// ---------------------------------------------------------------- master kill switch
const HaltBar = ({ halt }: { halt: Halt | undefined }) => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const set = async (halted: boolean) => {
    setBusy(true);
    await runMutation(() => apiClient.put<Halt>("/hive/trading/halt", { halted }), {
      invalidate: ["hive"],
      success: halted ? "Every bot halted" : "Autonomous trading resumed",
      errorTitle: halted ? "Halt failed" : "Resume refused",
    });
    setBusy(false);
  };
  const detail = halt?.detail ?? {};
  return (
    <div className="sticky top-3 z-30 lg:col-span-12">
      {halt?.halted ? (
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-3xl bg-rose-600 px-6 py-4 text-white shadow-soft-lg ring-1 ring-rose-900/20" role="alert">
          <div className="flex min-w-0 items-center gap-3">
            <span className="material-symbols-outlined text-[28px]">front_hand</span>
            <div className="min-w-0">
              <p className="text-sm font-bold uppercase tracking-[0.12em]">Autonomous trading halted</p>
              <p className="truncate text-[13px] text-rose-100">
                {humanize(halt.reason ?? "HALTED")}
                {halt.by && ` · by ${halt.by}`}
                {halt.at && ` · ${formatAgo(halt.at)}`}
                {typeof detail.swing_pct === "number" && ` · ${String(detail.market)} swung ${detail.swing_pct}% in ${String(detail.window_seconds)}s`}
              </p>
            </div>
          </div>
          {isAdmin && (
            <ConfirmButton variant="secondary" icon="play_arrow" busy={busy} confirmLabel="Resume every bot?" onConfirm={() => void set(false)}>
              Resume
            </ConfirmButton>
          )}
        </div>
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (armed) {
              setArmed(false);
              void set(true);
            } else {
              setArmed(true);
              window.setTimeout(() => setArmed(false), 4_000);
            }
          }}
          className={cx(
            "flex w-full items-center justify-center gap-3 rounded-3xl px-6 py-4 text-base font-bold uppercase tracking-[0.1em] text-white shadow-soft-lg transition-colors",
            "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-rose-300 disabled:opacity-60",
            armed ? "animate-breathe bg-rose-800" : "bg-rose-600 hover:bg-rose-700",
          )}
        >
          <span className="material-symbols-outlined text-[24px]">{busy ? "progress_activity" : "emergency_home"}</span>
          {armed ? "Press again: halt every bot" : "Halt all autonomous trading"}
        </button>
      )}
    </div>
  );
};

// ---------------------------------------------------------------- one bot
const CapitalSlider = ({ bot, max }: { bot: Bot; max: number }) => {
  const [draft, setDraft] = useState<{ value: number; base: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const saved = bot.allocated_capital;
  const value = draft && draft.base === saved ? draft.value : saved;
  const ceiling = Math.max(max, saved, 1000);
  const step = ceiling > 200_000 ? 1000 : 100;
  const commit = async (next: number) => {
    if (next === saved || busy) return;
    setBusy(true);
    const result = await runMutation(() => apiClient.put<Bot>(`/hive/trading/bots/${bot.id}/capital`, { allocated_capital: next.toFixed(2) }), {
      invalidate: ["hive", "cfo"],
      success: `${bot.name}: ${formatINR(next)} allocated`,
      errorTitle: "Allocation refused",
    });
    setBusy(false);
    if (result === undefined) setDraft(null);
  };
  const at = (value / ceiling) * 100;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[11px] text-stone-400">
        <span>
          Capital · {bot.execution_mode === "LIVE_EXECUTION" ? "moved from your bankroll" : bot.execution_mode === "PAPER_TRADE" ? "virtual (paper)" : "sizing base (shadow)"}
        </span>
        <span className="font-mono text-sm font-semibold tabular-nums text-stone-800 dark:text-stone-100">
          {formatINR(value)}
          {busy && <span className="ml-1 text-[10px] font-normal text-stone-400">saving…</span>}
        </span>
      </div>
      <div className="relative mt-1 h-8">
        <div className="absolute inset-x-0 top-1/2 -mt-1 h-2 rounded-full bg-stone-100 dark:bg-stone-800" />
        <motion.div className="absolute left-0 top-1/2 -mt-1 h-2 rounded-full bg-[var(--accent)]" initial={false} animate={{ width: `${at}%` }} transition={SPRING} />
        <input
          type="range"
          min={0}
          max={ceiling}
          step={step}
          value={value}
          disabled={busy}
          aria-label={`Capital allocated to ${bot.name}`}
          aria-valuetext={formatINR(value)}
          onChange={(e) => setDraft({ value: Number(e.target.value), base: saved })}
          onPointerUp={(e) => void commit(Number(e.currentTarget.value))}
          onKeyUp={(e) => ["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(e.key) && void commit(Number(e.currentTarget.value))}
          className="peer absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0 disabled:cursor-wait"
        />
        <motion.div
          aria-hidden="true"
          className="absolute top-1/2 -ml-2.5 -mt-2.5 size-5 rounded-full bg-white shadow-md ring-1 ring-stone-900/5 peer-focus-visible:ring-2 peer-focus-visible:ring-[var(--accent)] dark:bg-stone-100"
          initial={false}
          animate={{ left: `${at}%` }}
          transition={SPRING}
        />
      </div>
    </div>
  );
};

const Chain = ({ bot, names }: { bot: Bot; names: Map<string, string> }) => {
  const label = (k: string) => names.get(k) ?? k;
  const math = bot.math_models.filter((k) => k !== STAKING_MODEL);
  const stage = (title: string, keys: string[], tone: string) => (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-[0.12em] text-stone-400">{title}</p>
      <div className="mt-1 flex flex-wrap gap-1">
        {keys.length ? keys.map((k) => <span key={k} className={cx("rounded-full px-2 py-0.5 text-[11px]", tone)}>{label(k)}</span>) : <span className="text-[11px] text-stone-400">none</span>}
      </div>
    </div>
  );
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto_1fr]">
      {stage("Math", math, "bg-sky-50 text-sky-700 dark:bg-sky-400/10 dark:text-sky-300")}
      <span className="hidden self-center text-stone-300 sm:block" aria-hidden="true">→ Kelly →</span>
      {stage("Risk", bot.risk_models, "bg-violet-50 text-violet-700 dark:bg-violet-400/10 dark:text-violet-300")}
    </div>
  );
};

const BotCard = ({ bot, names, capitalCeiling, onEdit }: { bot: Bot; names: Map<string, string>; capitalCeiling: number; onEdit: () => void }) => {
  const [busy, setBusy] = useState(false);
  const act = async (status: "ACTIVE" | "PAUSED") => {
    setBusy(true);
    await runMutation(() => apiClient.post<Bot>(`/hive/trading/bots/${bot.id}/status`, { status }), {
      invalidate: ["hive"],
      success: status === "ACTIVE" ? `${bot.name} is trading` : `${bot.name} paused`,
      errorTitle: `${bot.name} not ${status === "ACTIVE" ? "activated" : "paused"}`,
    });
    setBusy(false);
  };
  const archive = async () => {
    setBusy(true);
    await runMutation(() => apiClient.delete(`/hive/trading/bots/${bot.id}`), { invalidate: ["hive"], success: `${bot.name} archived`, errorTitle: "Not archived" });
    setBusy(false);
  };
  const a = bot.account;
  return (
    <motion.article layout variants={CARD_VARIANTS} className={cx(SURFACE, "flex min-w-0 flex-col gap-4 p-5", bot.status === "SUSPENDED" && "ring-1 ring-rose-400/40")}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-semibold text-stone-900 dark:text-stone-50">{bot.name}</h3>
          <p className="mt-0.5 truncate text-xs text-stone-400">{bot.target_bet_types.map((k) => names.get(k) ?? k).join(" · ")}</p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <Pill tone={MODE_TONE[bot.execution_mode]}>{MODE_LABEL[bot.execution_mode]}</Pill>
          <Pill tone={STATUS_TONE[bot.status]}>{humanize(bot.status)}</Pill>
        </div>
      </header>
      {bot.status === "SUSPENDED" && (
        <p className="rounded-2xl bg-rose-50 px-3 py-2 text-[12px] text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
          Suspended by the {humanize(bot.suspended_reason ?? "breaker").toLowerCase()}
          {bot.suspended_at && ` ${formatAgo(bot.suspended_at)}`}. Review it, then activate it again.
        </p>
      )}
      <Chain bot={bot} names={names} />
      <dl className="grid grid-cols-3 gap-x-4 gap-y-2 text-[11px]">
        {[
          ["Equity", formatINR(a.equity)],
          ["Free", formatINR(a.available)],
          ["Exposure", formatINR(a.exposure)],
          ["Realised", `${a.realized_pnl >= 0 ? "+" : "−"}${formatINR(Math.abs(a.realized_pnl))}`],
          ["Open", String(a.open_positions)],
          ["Orders / min", `${bot.orders_last_minute} / ${bot.max_bets_per_minute}`],
        ].map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-stone-400">{k}</dt>
            <dd className="truncate font-mono text-[13px] font-semibold tabular-nums text-stone-800 dark:text-stone-100">{v}</dd>
          </div>
        ))}
      </dl>
      <CapitalSlider bot={bot} max={capitalCeiling} />
      <p className="text-[11px] text-stone-400">
        Kelly ×{bot.kelly_multiplier} · max {bot.max_stake_pct}% · edge ≥ {bot.min_edge_pct}% · ≥ {bot.min_quoting_books} books · drawdown breaker {bot.drawdown_limit_pct}%
        {bot.enable_order_slicing && ` · TWAP slices of ${formatINR(bot.slice_size_inr)}`}
      </p>
      {bot.pipeline_problems.length > 0 && <p className="text-[11px] text-amber-700 dark:text-amber-300">{bot.pipeline_problems.join(" · ")}</p>}
      <footer className="flex flex-wrap gap-2">
        {bot.status === "ACTIVE" ? (
          <Button size="sm" icon="pause" busy={busy} onClick={() => void act("PAUSED")}>Pause</Button>
        ) : (
          <Button size="sm" variant="primary" icon="play_arrow" busy={busy} onClick={() => void act("ACTIVE")}>
            {bot.status === "SUSPENDED" ? "Resume" : "Activate"}
          </Button>
        )}
        <Button size="sm" icon="tune" onClick={onEdit}>Pipeline</Button>
        {bot.status !== "ACTIVE" && (
          <ConfirmButton size="sm" variant="ghost" icon="archive" confirmLabel="Archive it?" onConfirm={() => void archive()}>Archive</ConfirmButton>
        )}
      </footer>
    </motion.article>
  );
};

// ---------------------------------------------------------------- pipeline builder
const Picker = ({
  title, kind, components, selected, onChange, locked = [],
}: { title: string; kind: ComponentKind; components: Component[]; selected: string[]; onChange: (keys: string[]) => void; locked?: string[] }) => {
  const [query, setQuery] = useState("");
  const pool = components.filter((c) => c.kind === kind);
  const shown = pool.filter((c) => !query || `${c.name} ${c.category} ${c.description}`.toLowerCase().includes(query.toLowerCase()));
  const groups = new Map<string, Component[]>();
  for (const c of shown) groups.set(c.category ?? "other", [...(groups.get(c.category ?? "other") ?? []), c]);
  const live = pool.filter((c) => c.live_capable).length;
  const toggle = (key: string) => onChange(selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key]);
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold text-stone-800 dark:text-stone-100">{title}</h4>
        <span className="text-[11px] text-stone-400">{selected.length} chosen · {live}/{pool.length} run live</span>
      </div>
      <TextInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${pool.length} ${title.toLowerCase()}`} aria-label={`Search ${title}`} />
      <div className="max-h-64 overflow-y-auto rounded-2xl bg-stone-50/80 p-2 dark:bg-white/[0.03]" role="group" aria-label={title}>
        {[...groups.entries()].map(([category, items]) => (
          <div key={category} className="mb-2">
            <p className="px-2 pt-1 text-[10px] uppercase tracking-[0.12em] text-stone-400">{humanize(category)}</p>
            {items.map((c) => {
              const on = selected.includes(c.key);
              const fixed = locked.includes(c.key);
              return (
                <label
                  key={c.key}
                  title={c.live_capable ? c.description : `${c.description} Backtest only: The Core can run it, a live bot cannot.`}
                  className={cx(
                    "flex cursor-pointer items-start gap-2 rounded-xl px-2 py-1.5 text-[13px]",
                    c.live_capable ? "hover:bg-white dark:hover:bg-white/5" : "cursor-not-allowed opacity-50",
                    on && "bg-white shadow-soft dark:bg-white/5",
                  )}
                >
                  <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={on} disabled={!c.live_capable || fixed} onChange={() => toggle(c.key)} />
                  <span className="min-w-0">
                    <span className="text-stone-800 dark:text-stone-100">{c.name}</span>
                    {!c.live_capable && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-stone-400">backtest only</span>}
                    {fixed && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-stone-400">required</span>}
                  </span>
                </label>
              );
            })}
          </div>
        ))}
        {shown.length === 0 && <p className="p-3 text-xs text-stone-400">Nothing matches.</p>}
      </div>
    </section>
  );
};

const NumberField = ({ label, value, onChange, hint, step = "any" }: { label: string; value: string; onChange: (v: string) => void; hint?: ReactNode; step?: string }) => (
  <Field label={label} hint={hint}>
    <NumberInput value={value} step={step} onChange={(e) => onChange(e.target.value)} />
  </Field>
);

const PipelineBuilder = ({ bot, components, onClose }: { bot: Bot | null; components: Component[]; onClose: () => void }) => {
  const reduce = useReducedMotion();
  const [d, setD] = useState<BotDraft>(() => (bot ? draftOf(bot) : NEW_BOT));
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof BotDraft>(key: K, value: BotDraft[K]) => setD((prev) => ({ ...prev, [key]: value }));
  const save = async () => {
    setBusy(true);
    setProblems([]);
    try {
      const body = payloadOf(d);
      if (bot) await apiClient.patch<Bot>(`/hive/trading/bots/${bot.id}`, body);
      else await apiClient.post<Bot>("/hive/trading/bots", body);
      invalidate("hive");
      toast.success(bot ? `${d.name} updated` : `${d.name} created (paused)`);
      onClose();
    } catch (err) {
      const listed = err instanceof ApiError && Array.isArray(err.detail?.problems) ? (err.detail.problems as string[]) : null;
      setProblems(listed ?? [refusalText(err)]);
    } finally {
      setBusy(false);
    }
  };
  return createPortal(
    <motion.div className="fixed inset-0 z-[90] grid place-items-center bg-stone-900/25 p-4 backdrop-blur-sm dark:bg-black/45" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={bot ? `Edit ${bot.name}` : "New bot"}
        initial={reduce ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.98 }}
        animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
        exit={reduce ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.98 }}
        transition={SPRING}
        onKeyDown={(e) => e.key === "Escape" && !busy && onClose()}
        className="flex max-h-[calc(100vh-2rem)] w-full max-w-5xl flex-col overflow-hidden rounded-3xl bg-[#FBFAF6]/95 shadow-soft-lg backdrop-blur-md dark:bg-stone-900/95 dark:ring-1 dark:ring-white/[0.06]"
      >
        <header className="flex items-start justify-between gap-4 px-7 pb-3 pt-7">
          <div>
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-stone-400">Pipeline builder · {components.length} registry components</p>
            <h2 className="mt-1 text-xl font-semibold tracking-tight text-stone-900 dark:text-stone-50">{bot ? bot.name : "New trading bot"}</h2>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className="grid size-9 place-items-center rounded-full text-stone-400 hover:bg-stone-200/60 hover:text-stone-700 dark:hover:bg-white/10">
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-7 pb-6">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-[2fr_3fr]">
            <Field label="Name">
              <TextInput autoFocus value={d.name} maxLength={80} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Shin consensus · EPL 1X2" />
            </Field>
            <Field label="Execution" hint={d.execution_mode === "LIVE_EXECUTION" ? "Real orders through the Omni-Sniper; capital moves out of your bankroll." : d.execution_mode === "SHADOW_MODE" ? "No orders: hypothetical positions and P&L." : "Simulated fills against virtual capital."}>
              <Segmented<BotDraft["execution_mode"]>
                options={[{ value: "PAPER_TRADE", label: "Paper", icon: "description" }, { value: "SHADOW_MODE", label: "Shadow", icon: "visibility" }, { value: "LIVE_EXECUTION", label: "Live", icon: "bolt" }]}
                value={d.execution_mode}
                onChange={(v) => set("execution_mode", v)}
                label="Execution mode"
              />
            </Field>
          </div>
          <p className="mt-5 text-[12px] text-stone-500 dark:text-stone-400">
            Chain: <span className="font-medium text-sky-700 dark:text-sky-300">{d.math_models.filter((k) => k !== STAKING_MODEL).length} math</span> → Kelly staking →{" "}
            <span className="font-medium text-violet-700 dark:text-violet-300">{d.risk_models.length} risk</span> → {d.target_bet_types.length} bet types. Conviction is the mean of the math models' estimates; each risk model can scale the stake down or veto it.
          </p>
          <div className="mt-4 grid grid-cols-1 gap-5 lg:grid-cols-3">
            <Picker title="Math models" kind="MATH_MODEL" components={components} selected={d.math_models} onChange={(v) => set("math_models", v.includes(STAKING_MODEL) ? v : [...v, STAKING_MODEL])} locked={[STAKING_MODEL]} />
            <Picker title="Risk models" kind="RISK_MODEL" components={components} selected={d.risk_models} onChange={(v) => set("risk_models", v)} />
            <Picker title="Bet types" kind="BET_TYPE" components={components} selected={d.target_bet_types} onChange={(v) => set("target_bet_types", v)} />
          </div>
          <div className="mt-6 grid grid-cols-2 gap-4 md:grid-cols-4">
            <NumberField label="Kelly multiplier" value={d.kelly_multiplier} onChange={(v) => set("kelly_multiplier", v)} step="0.05" />
            <NumberField label="Max stake %" value={d.max_stake_pct} onChange={(v) => set("max_stake_pct", v)} hint="of the bot's equity" />
            <NumberField label="Min edge %" value={d.min_edge_pct} onChange={(v) => set("min_edge_pct", v)} />
            <NumberField label="Min quoting books" value={d.min_quoting_books} onChange={(v) => set("min_quoting_books", v)} step="1" hint="ghost-line guard" />
            <NumberField label="Min traded volume ₹" value={d.min_market_liquidity} onChange={(v) => set("min_market_liquidity", v)} hint="0 = off; needs a source that reports volume" />
            <NumberField label="Velocity breaker" value={d.max_bets_per_minute} onChange={(v) => set("max_bets_per_minute", v)} step="1" hint="orders per minute" />
            <NumberField label="Drawdown breaker %" value={d.drawdown_limit_pct} onChange={(v) => set("drawdown_limit_pct", v)} hint="24h, of its sub-bankroll" />
            <NumberField label="Cooldown s" value={d.cooldown_seconds} onChange={(v) => set("cooldown_seconds", v)} step="60" hint="per selection" />
          </div>
          <div className="mt-4 flex flex-wrap items-end gap-4">
            <Toggle checked={d.enable_order_slicing} onChange={(v) => set("enable_order_slicing", v)} label="Order slicing (TWAP)" />
            {d.enable_order_slicing && (
              <div className="w-48">
                <NumberField label="Slice size ₹" value={d.slice_size_inr} onChange={(v) => set("slice_size_inr", v)} hint="60-120s apart, re-checked" />
              </div>
            )}
          </div>
          {problems.length > 0 && (
            <ul className="mt-5 list-disc rounded-2xl bg-rose-50 py-3 pl-8 pr-4 text-[13px] text-rose-700 dark:bg-rose-500/10 dark:text-rose-300" role="alert">
              {problems.map((p) => <li key={p}>{p}</li>)}
            </ul>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-stone-900/[0.05] px-7 pb-7 pt-5 dark:border-white/[0.06]">
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="save" busy={busy} disabled={!d.name.trim()} onClick={() => void save()}>{bot ? "Save pipeline" : "Create bot"}</Button>
        </footer>
      </motion.div>
    </motion.div>,
    document.body,
  );
};

// ---------------------------------------------------------------- collision map
const CollisionMap = ({ topology }: { topology: Topology }) => {
  const holders = useMemo(() => {
    const ids = new Set(topology.links.map((l) => l.holder));
    const bots = topology.bots.filter((b) => ids.has(b.id)).map((b) => ({ id: b.id, name: b.name }));
    return ids.has("main") ? [...bots, { id: "main", name: "Main account" }] : bots;
  }, [topology]);
  if (topology.markets.length === 0) {
    return <EmptyState icon="hub" title="No positions held" detail="When bots hold positions, each line here joins a bot to a market. Two holders on one market light up; opposite sides would light up red." />;
  }
  const rowH = 44;
  const height = Math.max(holders.length, topology.markets.length) * rowH + 24;
  const leftY = (i: number) => 24 + i * rowH + (height - 24 - holders.length * rowH) / 2;
  const rightY = (i: number) => 24 + i * rowH + (height - 24 - topology.markets.length * rowH) / 2;
  return (
    <svg viewBox={`0 0 640 ${height}`} className="h-auto w-full" role="img" aria-label="Collision map of bots and markets">
      {topology.links.map((link, i) => {
        const a = holders.findIndex((h) => h.id === link.holder);
        const b = topology.markets.findIndex((m) => m.market_key === link.market_key);
        if (a < 0 || b < 0) return null;
        const y1 = leftY(a) + 10;
        const y2 = rightY(b) + 10;
        return (
          <path
            key={`${link.holder}-${link.market_key}-${link.selection}-${i}`}
            d={`M 170 ${y1} C 320 ${y1}, 320 ${y2}, 450 ${y2}`}
            fill="none"
            stroke={SELECTION_COLOR[link.selection] ?? "#a8a29e"}
            strokeOpacity={0.75}
            strokeDasharray={link.source === "shadow" ? "4 4" : undefined}
            strokeWidth={Math.min(8, 1.5 + Math.log10(Math.max(link.stake_inr, 10)))}
          >
            <title>{`${link.selection} ${formatINR(link.stake_inr)}${link.strategy ? ` (${link.strategy})` : ""}${link.source === "shadow" ? " · shadow" : ""}`}</title>
          </path>
        );
      })}
      {holders.map((h, i) => (
        <g key={h.id} transform={`translate(10 ${leftY(i)})`}>
          <rect width="160" height="22" rx="11" className="fill-stone-100 dark:fill-stone-800" />
          <text x="12" y="15" className="fill-stone-700 text-[11px] dark:fill-stone-200">{h.name.length > 22 ? `${h.name.slice(0, 21)}…` : h.name}</text>
        </g>
      ))}
      {topology.markets.map((m, i) => (
        <g key={m.market_key} transform={`translate(450 ${rightY(i)})`}>
          <rect width="180" height="22" rx="11" className={m.opposing ? "fill-rose-100 dark:fill-rose-500/20" : m.collision ? "fill-amber-100 dark:fill-amber-400/20" : "fill-stone-100 dark:fill-stone-800"}
            stroke={m.shadow_overlap ? "#38bdf8" : "none"}
            strokeDasharray={m.shadow_overlap ? "3 3" : undefined} />
          <text x="12" y="15" className="fill-stone-700 text-[11px] dark:fill-stone-200">
            {(m.home && m.away ? `${m.home} v ${m.away}` : m.fixture_id).slice(0, 26)}
          </text>
        </g>
      ))}
    </svg>
  );
};

// ---------------------------------------------------------------- the tab
export const HiveBots = () => {
  const halt = useHalt();
  const bots = useHiveBots();
  const registry = useRegistry();
  const bank = useBankroll();
  const topology = useTopology();
  const events = useHiveEvents();
  const plans = usePlans();
  const [editing, setEditing] = useState<Bot | "new" | null>(null);
  const components = useMemo(() => registry.data?.components ?? [], [registry.data]);
  const names = useMemo(() => new Map(components.map((c) => [c.key, c.name])), [components]);
  const botNames = useMemo(() => new Map((bots.data ?? []).map((b) => [b.id, b.name])), [bots.data]);
  const ceiling = Math.max(bank.data?.equity ?? 0, 10_000);

  return (
    <>
      <HaltBar halt={halt.data} />
      <Panel
        title="The Hive · trading bots"
        icon="hub"
        className="lg:col-span-12"
        subtitle={registry.data ? `${registry.data.components.length} registry components · ${Object.values(registry.data.live_counts).reduce((a, b) => a + b, 0)} run live` : undefined}
        actions={<Button variant="primary" size="sm" icon="add" disabled={!components.length} onClick={() => setEditing("new")}>New bot</Button>}
      >
        <Async resource={bots} skeletonRows={2} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="smart_toy" title="No bots yet" detail="Create one from the registry's models. It starts paused, in paper mode, with no capital." />}>
          {(list) => (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,22rem),1fr))] gap-4">
              {list.map((bot) => <BotCard key={bot.id} bot={bot} names={names} capitalCeiling={ceiling} onEdit={() => setEditing(bot)} />)}
            </div>
          )}
        </Async>
      </Panel>
      <Panel title="Collision map" icon="hub" className="lg:col-span-7" subtitle="bots → markets · line colour = outcome · dashed = shadow · amber = shared market · red = opposite sides">
        <Async resource={topology} skeletonRows={3}>{(t) => <CollisionMap topology={t} />}</Async>
      </Panel>
      <Panel title="Decisions" icon="receipt_long" className="lg:col-span-5" subtitle="every fire, merge, block and breaker">
        <Async resource={events} skeletonRows={4} isEmpty={(d) => d.length === 0} empty={<EmptyState icon="receipt_long" title="Nothing decided yet" />}>
          {(list) => (
            <ul className="flex max-h-[28rem] flex-col gap-1.5 overflow-y-auto">
              {list.map((e) => (
                <li key={e.id} className="flex items-start justify-between gap-3 rounded-2xl px-3 py-2 hover:bg-stone-50 dark:hover:bg-white/[0.03]">
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-1.5 text-[13px]">
                      <Pill tone={EVENT_TONE[e.event] ?? "neutral"}>{humanize(e.event)}</Pill>
                      <span className="font-medium text-stone-800 dark:text-stone-100">{e.bot_id ? botNames.get(e.bot_id) ?? "bot" : "Hive"}</span>
                      <span className="text-stone-500">{humanize(e.reason)}</span>
                    </p>
                    <p className="mt-0.5 truncate text-[11px] text-stone-400">
                      {e.selection && `${e.selection} `}
                      {e.odds !== null && `@ ${e.odds} `}
                      {e.stake_inr !== null && `· ${formatINR(e.stake_inr)} `}
                      {e.fixture_id && `· ${e.fixture_id.slice(0, 8)}`}
                    </p>
                  </div>
                  <span className="shrink-0 text-[11px] text-stone-400">{formatAgo(e.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Async>
        {plans.data && plans.data.length > 0 && (
          <div className="mt-4 border-t border-stone-900/[0.05] pt-3 dark:border-white/[0.06]">
            <p className="text-[11px] uppercase tracking-[0.12em] text-stone-400">TWAP plans</p>
            <ul className="mt-2 flex flex-col gap-2">
              {plans.data.slice(0, 5).map((p) => (
                <li key={p.id} className="text-[12px] text-stone-600 dark:text-stone-300">
                  <span className="font-medium">{botNames.get(p.bot_id) ?? "bot"}</span> · {p.selection} {formatINR(p.total_stake_inr)} · {humanize(p.status)}
                  <span className="ml-2 inline-flex gap-1 align-middle">
                    {p.slices.map((s) => (
                      <span key={s.index} title={`${formatINR(Number(s.stake_inr))} · +${s.countdown_s}s · ${s.status}${s.reason ? ` (${s.reason})` : ""}`} className={cx("inline-block size-2.5 rounded-full", s.status === "FIRED" ? "bg-emerald-500" : s.status === "SCHEDULED" ? "bg-stone-300" : "bg-rose-400")} />
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>
      <AnimatePresence>{editing && <PipelineBuilder key={editing === "new" ? "new" : editing.id} bot={editing === "new" ? null : editing} components={components} onClose={() => setEditing(null)} />}</AnimatePresence>
    </>
  );
};
