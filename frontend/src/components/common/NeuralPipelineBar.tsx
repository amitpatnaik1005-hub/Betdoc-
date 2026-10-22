/**
 * The live inter-module pipeline bar (Group 108, point b.9), across the top of every page: the five modules in chain
 * order (VIDUR / THE WIRE → SCORE PREDICTOR → ORACLE & BET BUILDER → BETDOC CONSULTANT → THE VAULT & SMALLCASES), the
 * chain in flight with the step it is on pulsing, and a drawer with each step's own report and the recent trace.
 * A chain starts when the Wire's scan reads a breaking story or a catalyst, or from a card on VIDUR's board.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useId } from "react";
import { Link } from "react-router-dom";
import { ago, MODULES, type ChainStep, type StepStatus, useRecentChains } from "../../lib/neural_bus";
import { MOCK_AUTH } from "../../store/useAuthStore";
import { useNeuralBusStore } from "../../store/useNeuralBusStore";

const CHIP: Record<StepStatus | "PENDING", string> = {
  PENDING: "bg-white/[0.04] text-stone-500",
  RUNNING: "bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-400/60 animate-pulse",
  COMPLETED: "bg-emerald-500/15 text-emerald-300",
  SKIPPED: "bg-white/[0.06] text-stone-400",
  FAILED: "bg-rose-500/15 text-rose-300",
};
const STATUS_ICON: Record<StepStatus, string> = { RUNNING: "progress_activity", COMPLETED: "check_circle", SKIPPED: "remove_circle", FAILED: "error" };
const STATUS_TONE: Record<StepStatus, string> = {
  RUNNING: "text-emerald-600 dark:text-emerald-400", COMPLETED: "text-emerald-600 dark:text-emerald-400",
  SKIPPED: "text-stone-400 dark:text-stone-500", FAILED: "text-rose-600 dark:text-rose-400",
};

const StepCard = ({ number, step }: { number: number; step: ChainStep | undefined }) => {
  const meta = MODULES[number - 1];
  return (
    <li className="min-w-0 rounded-xl bg-stone-50 p-3 dark:bg-white/[0.04]">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-stone-500 dark:text-stone-400">
          <span className="material-symbols-outlined text-[14px]" aria-hidden="true">{meta.icon}</span>
          <span className="truncate">{number}. {meta.name}</span>
        </span>
        {step && (
          <span className={`flex shrink-0 items-center gap-0.5 text-[10px] font-semibold ${STATUS_TONE[step.status]}`}>
            <span className={`material-symbols-outlined text-[14px] ${step.status === "RUNNING" ? "animate-spin" : ""}`} aria-hidden="true">{STATUS_ICON[step.status]}</span>
            {step.status.toLowerCase()}{step.ms !== null && step.status !== "RUNNING" ? ` · ${step.ms} ms` : ""}
          </span>
        )}
      </div>
      <p className="mt-1.5 text-xs leading-snug text-stone-700 dark:text-stone-200">{step ? step.summary || "Running…" : "Waiting for its handoff."}</p>
    </li>
  );
};

export const NeuralPipelineBar = () => {
  const drawerId = useId();
  const status = useNeuralBusStore((s) => s.status);
  const active = useNeuralBusStore((s) => s.active);
  const trace = useNeuralBusStore((s) => s.trace);
  const drawerOpen = useNeuralBusStore((s) => s.drawerOpen);
  const toggleDrawer = useNeuralBusStore((s) => s.toggleDrawer);
  const setDrawerOpen = useNeuralBusStore((s) => s.setDrawerOpen);
  const connect = useNeuralBusStore((s) => s.connect);
  const recent = useRecentChains();

  useEffect(() => (MOCK_AUTH ? undefined : connect()), [connect]);
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawerOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen, setDrawerOpen]);

  const running = active && !active.done ? Object.values(active.steps).find((s) => s.status === "RUNNING") : undefined;
  const last = !active ? recent.data?.chains[0] : undefined;
  const live = status === "open";
  const fixture = active?.trigger.fixture ? `${active.trigger.fixture.home} v ${active.trigger.fixture.away}` : null;
  const line = running
    ? `${running.module_name}: ${fixture ?? active?.trigger.headline ?? ""}`
    : active
      ? `${active.done ? active.outcome ?? "Done" : "Running"} · ${fixture ?? active.trigger.headline}`
      : last
        ? `Last chain ${ago(last.completed_at ?? last.started_at)} · ${last.trigger.fixture ? `${last.trigger.fixture.home} v ${last.trigger.fixture.away}` : last.trigger.headline} · ${last.outcome}`
        : "Wire → Score Predictor → Oracle → Consultant → Vault: waiting for a breaking story or a chain you run";

  return (
    <div className="relative">
      <div className="flex h-9 w-full items-center gap-2 overflow-hidden bg-stone-900 px-3 text-[11px] text-stone-200 sm:px-4 dark:bg-black/60">
        <span className="flex shrink-0 items-center gap-1.5 font-mono font-bold tracking-wide" title={live ? "Live: frames arrive as each step hands off" : `Socket ${status}`}>
          <span aria-hidden="true" className={`size-2 rounded-full ${running ? "animate-pulse bg-emerald-400" : live ? "bg-emerald-500" : "bg-amber-500"}`} />
          <span className="hidden text-emerald-300 sm:inline">NEURAL BUS</span>
        </span>
        <ol className="flex shrink-0 items-center gap-0.5" aria-label="Pipeline modules">
          {MODULES.map((m, i) => {
            const step = active?.steps[i + 1];
            return (
              <li key={m.module} className="flex items-center gap-0.5">
                {i > 0 && <span className="material-symbols-outlined text-[12px] text-stone-600" aria-hidden="true">chevron_right</span>}
                <span className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 ${CHIP[step?.status ?? "PENDING"]}`} title={`${m.name}${step ? ` · ${step.status.toLowerCase()}` : ""}`}>
                  <span className="material-symbols-outlined text-[13px]" aria-hidden="true">{m.icon}</span>
                  <span className="hidden font-medium xl:inline">{m.short}</span>
                </span>
              </li>
            );
          })}
        </ol>
        <span className="min-w-0 flex-1 truncate text-stone-300" aria-live="polite">{line}</span>
        <button type="button" onClick={toggleDrawer} aria-expanded={drawerOpen} aria-controls={drawerId}
                className="flex shrink-0 items-center gap-1 rounded-md px-2 py-0.5 font-medium text-stone-300 transition-colors hover:bg-white/10 hover:text-white">
          <span className="material-symbols-outlined text-[15px]" aria-hidden="true">{drawerOpen ? "expand_less" : "account_tree"}</span>
          <span className="hidden sm:inline">{drawerOpen ? "Close trace" : "Trace"}</span>
        </button>
      </div>

      {drawerOpen && (
        <section id={drawerId} aria-label="Neural bus trace"
                 className="absolute inset-x-0 top-full z-40 max-h-[70vh] overflow-y-auto border-b border-stone-200 bg-white/95 p-4 shadow-soft-lg backdrop-blur-xl sm:p-5 dark:border-white/10 dark:bg-stone-900/95">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-sm font-bold text-stone-900 dark:text-stone-100">
              <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">account_tree</span>
              {active ? (active.mine ? "Your chain" : "The Wire's chain") : "The neural chain"}
            </h2>
            <span className="text-[11px] text-stone-500 dark:text-stone-400">
              {live ? "live" : `socket ${status}`} · each step is a real engine call; SKIPPED and FAILED say why
            </span>
          </div>
          {active ? (
            <>
              <p className="mt-2 text-xs text-stone-600 dark:text-stone-300">
                <strong className="text-stone-900 dark:text-stone-100">{active.trigger.headline}</strong> · {active.trigger.source} · started {ago(active.started_at)}
                {active.trigger.fixture && (
                  <> · <Link to={active.trigger.fixture.oracle_link} onClick={() => setDrawerOpen(false)} className="font-semibold text-[var(--accent-text)] underline-offset-2 hover:underline">
                    Open {active.trigger.fixture.home} v {active.trigger.fixture.away} in the Oracle</Link></>
                )}
              </p>
              <ol className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5">
                {MODULES.map((m, i) => <StepCard key={m.module} number={i + 1} step={active.steps[i + 1]} />)}
              </ol>
            </>
          ) : (
            <p className="mt-2 text-xs text-stone-500 dark:text-stone-400">
              No chain has run in this session yet. One starts when the Wire's scan reads a breaking story or a catalyst on a tracked fixture, or when you run one from a card on The Wire's board.
            </p>
          )}
          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="min-w-0">
              <h3 className="text-[10px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Stream</h3>
              {trace.length === 0 ? (
                <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">Handoffs appear here as they happen.</p>
              ) : (
                <ul className="mt-1 flex max-h-56 flex-col gap-1 overflow-y-auto pr-1 font-mono text-[11px]">
                  {trace.map((l) => (
                    <li key={l.key} className="flex items-start justify-between gap-2">
                      <span className={`min-w-0 ${STATUS_TONE[l.step.status]}`}>
                        <span className="text-stone-400">{new Date(l.at).toLocaleTimeString()}</span> {l.step.module_name}
                        {l.fixture ? ` · ${l.fixture}` : ""}: <span className="text-stone-600 dark:text-stone-300">{l.step.summary}</span>
                      </span>
                      <span className="shrink-0 text-[10px] uppercase text-stone-400">{l.mine ? "yours" : "wire"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="min-w-0">
              <h3 className="text-[10px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Recent chains</h3>
              {(recent.data?.chains ?? []).length === 0 ? (
                <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">No chain is kept right now.</p>
              ) : (
                <ul className="mt-1 flex max-h-56 flex-col gap-1.5 overflow-y-auto pr-1">
                  {(recent.data?.chains ?? []).map((c) => (
                    <li key={c.chain_id}>
                      <details className="group rounded-lg bg-stone-50 px-2.5 py-1.5 text-xs dark:bg-white/[0.03]">
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
                          <span className="min-w-0 truncate">
                            <span className="font-semibold text-stone-800 dark:text-stone-100">{c.trigger.fixture ? `${c.trigger.fixture.home} v ${c.trigger.fixture.away}` : c.trigger.headline}</span>
                            <span className="text-stone-500 dark:text-stone-400"> · {c.outcome}</span>
                          </span>
                          <span className="shrink-0 text-[10px] text-stone-400">{c.mine ? "yours" : "wire"} · {ago(c.started_at)}</span>
                        </summary>
                        <ol className="mt-1.5 flex flex-col gap-1 border-t border-stone-200 pt-1.5 dark:border-white/10">
                          {c.steps.map((s) => (
                            <li key={s.number} className={STATUS_TONE[s.status]}>
                              <span className="font-semibold">{s.module_name}</span> <span className="text-stone-600 dark:text-stone-300">{s.summary}</span>
                            </li>
                          ))}
                        </ol>
                      </details>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </section>
      )}
    </div>
  );
};
