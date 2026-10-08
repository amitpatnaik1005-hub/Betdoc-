/** Event horizon: ingestion batches and quorum sweeps as the workers publish them on /omni/ws/stream. */
import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { formatInt, formatTime } from "../../lib/format";
import { subscribeChannel } from "../../services/realtime";
import { useMarketStore } from "../../store/useMarketStore";
import { EmptyState, LiveDot, Panel, SPRING } from "../../ui/kit";
import { cx } from "./types";

interface HorizonEntry {
  id: string;
  kind: "batch" | "quorum";
  title: string;
  detail: string;
  tone: "good" | "warning" | "neutral";
  at: number;
}

function toHorizon(data: unknown): HorizonEntry | null {
  if (!data || typeof data !== "object") return null;
  const m = data as { kind?: string; provider_name?: string; payload?: Record<string, unknown>; at?: string };
  const at = m.at ? new Date(m.at).getTime() : Date.now();
  const p = m.payload ?? {};
  if (m.kind === "omni.fleet.batch") {
    const scope = Array.isArray(p.scope) ? (p.scope as string[]).join(", ") : "";
    return {
      id: `${m.kind}-${m.provider_name}-${at}`,
      kind: "batch",
      title: `${m.provider_name ?? "Source"} · ${formatInt(Number(p.ticks ?? 0))} ticks`,
      detail: `${formatInt(Number(p.fixtures ?? 0))} fixtures${scope ? ` · ${scope}` : ""} · ${formatInt(Number(p.latency_ms ?? 0))} ms`,
      tone: "good",
      at,
    };
  }
  if (m.kind === "omni.quorum") {
    const quarantined = Number(p.quarantined ?? 0);
    const outliers = Number(p.outliers ?? 0);
    return {
      id: `${m.kind}-${at}`,
      kind: "quorum",
      title: `Quorum · ${formatInt(Number(p.resolved ?? 0))} agreed`,
      detail: [quarantined ? `${quarantined} quarantined` : "", outliers ? `${outliers} outlier${outliers === 1 ? "" : "s"} dropped (IQR)` : ""].filter(Boolean).join(" · ") || "every multi-source cell agrees",
      tone: quarantined || outliers ? "warning" : "neutral",
      at,
    };
  }
  return null;
}

const TRANSPORT_LABEL = { ws: "WebSocket", polling: "HTTP polling (3s)", connecting: "Connecting", offline: "Offline" } as const;

export const EventHorizon = ({ boardCells, className }: { boardCells: number | null; className?: string }) => {
  const [entries, setEntries] = useState<HorizonEntry[]>([]);
  const feedLive = useMarketStore((s) => s.isConnected);
  const transport = useMarketStore((s) => s.transport);
  const liveMatches = useMarketStore((s) => Object.keys(s.matches).length);

  useEffect(
    () =>
      subscribeChannel(
        "/omni/ws/stream",
        (data) => {
          const entry = toHorizon(data);
          if (entry) setEntries((prev) => [entry, ...prev.filter((e) => e.id !== entry.id)].slice(0, 10));
        },
        "kind",
      ),
    [],
  );

  return (
    <Panel title="Event horizon" icon="satellite_alt" className={className} subtitle="live from the workers">
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-2xl bg-stone-50 px-4 py-3 dark:bg-white/[0.03]">
            <p className="text-[11px] text-stone-500 dark:text-stone-400">Board cells (Redis)</p>
            <p className="font-mono text-lg tabular-nums text-stone-800 dark:text-stone-100">{boardCells === null ? "—" : formatInt(boardCells)}</p>
          </div>
          <div className="rounded-2xl bg-stone-50 px-4 py-3 dark:bg-white/[0.03]">
            <p className="flex items-center gap-1.5 text-[11px] text-stone-500 dark:text-stone-400">
              <LiveDot active={feedLive} tone={transport === "polling" ? "warning" : "good"} /> {TRANSPORT_LABEL[transport]}
            </p>
            <p className="font-mono text-lg tabular-nums text-stone-800 dark:text-stone-100">
              {formatInt(liveMatches)} <span className="text-xs text-stone-400">matches</span>
            </p>
          </div>
        </div>
        {entries.length === 0 ? (
          <EmptyState icon="satellite_alt" title="Listening for the next batch" detail="Each ingest run and quorum sweep appears here the moment a worker publishes it." />
        ) : (
          <ul className="flex flex-col gap-1.5">
            <AnimatePresence initial={false}>
              {entries.map((e) => (
                <motion.li
                  key={e.id}
                  layout
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={SPRING}
                  className="flex items-start gap-3 rounded-2xl px-3 py-2.5 transition-colors hover:bg-stone-50 dark:hover:bg-white/[0.03]"
                >
                  <span
                    className={cx(
                      "mt-0.5 grid size-7 shrink-0 place-items-center rounded-full",
                      e.tone === "good"
                        ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300"
                        : e.tone === "warning"
                          ? "bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-200"
                          : "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-300",
                    )}
                  >
                    <span className="material-symbols-outlined text-[15px]">{e.kind === "batch" ? "download" : "balance"}</span>
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-stone-800 dark:text-stone-100">{e.title}</span>
                    <span className="block truncate text-[11px] text-stone-500 dark:text-stone-400">{e.detail}</span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] tabular-nums text-stone-400">{formatTime(e.at)}</span>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        )}
      </div>
    </Panel>
  );
};
