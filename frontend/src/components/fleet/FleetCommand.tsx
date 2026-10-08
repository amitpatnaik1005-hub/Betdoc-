/**
 * Fleet Command: the Universal Ingestion Matrix's control surface (Control Panel, KAUTILYA).
 *
 * - Health & Failover Matrix: every source (built-in or config-driven), with its switch, circuit
 *   breaker, quota bar, failover role, latency, success rate, last sync and a write-only key field.
 * - Market coverage: which source is fetching each market right now, and every active failover.
 * - Add provider: a JSON spec, dry-run against a pasted sample, then live with no backend code.
 * - Event horizon: batches and quorum sweeps as the workers publish them.
 *
 * Freshness: the backend publishes a `fleet` event on `/ws/events` after every run, toggle, trip and
 * sweep; services/realtime turns it into invalidate("fleet"). A 10s poll is the safety net.
 */
import { useEffect, useMemo, useState } from "react";
import { formatAgo } from "../../lib/format";
import { useAuthStore } from "../../store/useAuthStore";
import { useMarketStore } from "../../store/useMarketStore";
import { Async, Button, EmptyState, Panel, Pill } from "../../ui/kit";
import { EventHorizon } from "./EventHorizon";
import { FleetMatrix } from "./FleetMatrix";
import { ProviderModal } from "./ProviderModal";
import { type FleetGroup, type FleetOverview, type FleetSource, cx, groupLabel, useDeadLetters, useFleet } from "./types";

const MODE_LABEL: Record<FleetOverview["mode"], [string, "good" | "info" | "critical", string]> = {
  celery: ["Celery workers", "good", "dns"],
  inprocess: ["In-process fallback", "info", "memory"],
  offline: ["Ingestion offline", "critical", "cloud_off"],
};

const REASON: Record<string, string> = {
  quota_reserve: "quota reserve",
  circuit_open: "tripped",
  disabled: "off",
  paused: "dead-lettered",
  needs_key: "needs key",
};

function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

const CoverageRow = ({ group, names }: { group: FleetGroup; names: Record<string, string> }) => {
  const [icon, tone] = group.uncovered ? ["error", "text-rose-600 dark:text-rose-300"] : group.failover ? ["swap_horiz", "text-amber-600 dark:text-amber-300"] : ["check_circle", "text-emerald-600 dark:text-emerald-300"];
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-2xl px-3 py-2.5 transition-colors hover:bg-stone-50 dark:hover:bg-white/[0.03]">
      <span className={cx("material-symbols-outlined text-[18px]", tone)}>{icon}</span>
      <span className="w-32 shrink-0 truncate text-sm font-medium text-stone-800 dark:text-stone-100">{groupLabel(group.group)}</span>
      <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        {group.active.map((sid) => (
          <Pill key={sid} tone={group.failover ? "warning" : "accent"}>{names[sid] ?? sid}</Pill>
        ))}
        {group.free.map((sid) => (
          <Pill key={sid} tone="info">{names[sid] ?? sid}</Pill>
        ))}
        {Object.entries(group.down).map(([sid, reason]) => (
          <span key={sid} className="text-[11px] text-stone-400 line-through decoration-stone-300 dark:text-stone-500" title={reason}>
            {names[sid] ?? sid} · {REASON[reason] ?? reason}
          </span>
        ))}
        {group.uncovered && <span className="text-[11px] text-rose-600 dark:text-rose-300">no source available</span>}
      </span>
    </li>
  );
};

export const FleetCommand = () => {
  const fleet = useFleet();
  const deadLetters = useDeadLetters();
  const canEdit = useAuthStore((s) => s.user?.role === "ADMIN");
  const transport = useMarketStore((s) => s.transport);
  const now = useNow();
  const [modal, setModal] = useState<{ open: boolean; editing: FleetSource | null }>({ open: false, editing: null });
  const data = fleet.data;
  const names = useMemo(() => Object.fromEntries((data?.sources ?? []).map((s) => [s.source_id, s.display_name])), [data]);
  const [modeLabel, modeTone, modeIcon] = MODE_LABEL[data?.mode ?? "offline"];
  const failovers = (data?.groups ?? []).filter((g) => g.failover).length;

  return (
    <>
      <Panel
        title="Fleet command"
        icon="hub"
        className="lg:col-span-12"
        updatedAt={fleet.updatedAt}
        subtitle="health & failover matrix"
        actions={
          <div className="flex flex-wrap items-center justify-end gap-2">
            {data && (
              <span className="hidden flex-wrap items-center gap-2 md:flex">
                <Pill tone={modeTone} icon={modeIcon}>{modeLabel}</Pill>
                <Pill tone={transport === "ws" ? "good" : transport === "polling" ? "warning" : "neutral"} icon={transport === "polling" ? "sync_alt" : "bolt"}>
                  Feed {transport === "ws" ? "live" : transport === "polling" ? "polling" : transport}
                </Pill>
                {failovers > 0 && <Pill tone="warning" icon="swap_horiz">{failovers} failover{failovers === 1 ? "" : "s"}</Pill>}
              </span>
            )}
            <Button size="sm" variant="primary" icon="add" disabled={!canEdit} onClick={() => setModal({ open: true, editing: null })}>
              Add provider
            </Button>
          </div>
        }
      >
        <Async resource={fleet} skeletonRows={5}>
          {(overview) => (
            <div className="flex flex-col gap-4">
              {!canEdit && (
                <p className="flex items-center gap-2 text-xs text-stone-500 dark:text-stone-400">
                  <span className="material-symbols-outlined text-[15px]">shield_person</span>
                  Read-only: switching sources, storing keys and adding providers needs an admin account.
                </p>
              )}
              {!overview.redis_available && (
                <p className="flex items-center gap-2 rounded-2xl bg-amber-50 px-4 py-2.5 text-xs text-amber-800 dark:bg-amber-400/10 dark:text-amber-200">
                  <span className="material-symbols-outlined text-[16px]">cloud_off</span>
                  Redis is unreachable: workers are spooling results in memory and will flush them when it returns.
                </p>
              )}
              <FleetMatrix overview={overview} canEdit={canEdit} now={now} onEdit={(s) => setModal({ open: true, editing: s })} />
            </div>
          )}
        </Async>
      </Panel>

      <Panel title="Market coverage" icon="lan" className="lg:col-span-7" subtitle="who is fetching each market now">
        <Async resource={fleet} isEmpty={(o) => o.groups.length === 0} empty={<EmptyState icon="lan" title="No markets covered yet" />}>
          {(overview) => (
            <div className="flex flex-col gap-5">
              <ul className="flex flex-col gap-0.5">
                {overview.groups.map((g) => (
                  <CoverageRow key={g.group} group={g} names={names} />
                ))}
              </ul>
              {(deadLetters.data?.length ?? 0) > 0 && (
                <div className="flex flex-col gap-2">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-stone-500 dark:text-stone-400">
                    <span className="material-symbols-outlined text-[15px]">move_to_inbox</span> Dead letters
                  </p>
                  <ul className="flex flex-col gap-1">
                    {deadLetters.data!.slice(0, 4).map((d) => (
                      <li key={`${d.source_id}-${d.at}`} className="flex items-baseline justify-between gap-3 rounded-xl bg-rose-50/60 px-3 py-2 text-xs dark:bg-rose-500/[0.06]">
                        <span className="min-w-0 truncate">
                          <span className="font-semibold text-rose-800 dark:text-rose-200">{names[d.source_id] ?? d.source_id}</span>{" "}
                          <span className="text-stone-500 dark:text-stone-400">· {d.failures} failures · {d.error}</span>
                        </span>
                        <span className="shrink-0 font-mono text-[11px] text-stone-400">{formatAgo(d.at, now)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Async>
      </Panel>

      <EventHorizon boardCells={data?.board_cells ?? null} className="lg:col-span-5" />

      <ProviderModal open={modal.open} editing={modal.editing} onClose={() => setModal({ open: false, editing: null })} />
    </>
  );
};
