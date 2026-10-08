/**
 * Control Panel, Execution terminal: the Omni-Sniper at work.
 *
 * - Venues: each bookmaker's execution venue, its live session and outbound limit.
 * - Live feed: every step of every order, streamed over `/ws/sniper` as it happens
 *   (`[14:02:01] Authenticating… [14:02:03] Firing… [14:02:04] 200 OK - remote_id: 9982`).
 * - Executions: each outcome with the exact JSON sent and received (payload inspector).
 * - Dead letters: orders the resolver gave up on, for a person to rule on.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { apiClient } from "../../api/client";
import type { Position } from "../../lib/cfo";
import { formatAgo, formatINR, formatOdds, formatTime, humanize } from "../../lib/format";
import { runMutation, useResource } from "../../lib/resource";
import { type CatalogSync, type Execution, type FeedLine, type Venue, useDeadLetters, useExecutions, useVenues } from "../../lib/sniper";
import { OmniSocket, type OmniSocketStatus } from "../../services/OmniGateway";
import { subscribeChannel } from "../../services/realtime";
import { useAuthStore } from "../../store/useAuthStore";
import { Async, Button, CARD_VARIANTS, ConfirmButton, EmptyState, LiveDot, Panel, Pill, SPRING, Segmented } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const MAX_LINES = 300;
const FEED_PATH = "/ws/sniper";

type View = "feed" | "executions" | "dlq";
const VIEWS = [
  { value: "feed", label: "Live feed", icon: "terminal" },
  { value: "executions", label: "Executions", icon: "data_object" },
  { value: "dlq", label: "Dead letters", icon: "report" },
] as const;

const LEVEL_TONE: Record<string, string> = {
  success: "text-emerald-300",
  warning: "text-amber-300",
  error: "text-rose-300",
  info: "text-stone-300",
};

const lineKey = (l: FeedLine): string => `${l.ts}|${l.ref ?? ""}|${l.step}|${l.message}`;

function parseLine(raw: unknown): FeedLine | null {
  if (raw === null || typeof raw !== "object") return null;
  const l = raw as Partial<FeedLine>;
  return typeof l.ts === "string" && typeof l.message === "string" && typeof l.step === "string" ? { ...(l as FeedLine), level: l.level ?? "info" } : null;
}

const clock = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString("en-GB", { hour12: false });
};

// ---------------------------------------------------------------- venues
const SessionState = ({ venue }: { venue: Venue }) => {
  const s = venue.session;
  if (!s.authenticated) return <span className="text-xs text-stone-400">No session yet · opens on the first shot</span>;
  if (s.seconds_left === null) return <span className="text-xs text-emerald-700 dark:text-emerald-300">Static key</span>;
  const minutes = Math.floor(s.seconds_left / 60);
  return (
    <span className={cx("text-xs", s.seconds_left < 300 ? "text-amber-700 dark:text-amber-300" : "text-emerald-700 dark:text-emerald-300")}>
      Session live · {minutes}m {String(s.seconds_left % 60).padStart(2, "0")}s left
    </span>
  );
};

const VenueCard = ({ venue, isAdmin }: { venue: Venue; isAdmin: boolean }) => {
  const [busy, setBusy] = useState(false);
  const sync = async () => {
    setBusy(true);
    await runMutation(() => apiClient.post<CatalogSync>(`/omni/venues/${venue.id}/sync`), {
      invalidate: ["cfo"],
      success: (r) => `${venue.display_name}: ${r.mapped} of ${r.events} events mapped${r.unresolved.length ? `, ${r.unresolved.length} need a manual id` : ""}`,
      errorTitle: "Catalog sync failed",
    });
    setBusy(false);
  };
  return (
    <motion.div layout transition={SPRING} className="flex min-w-0 flex-col gap-3 rounded-2xl bg-white/70 p-5 shadow-soft ring-1 ring-inset ring-stone-900/[0.04] backdrop-blur-md dark:bg-stone-900/60 dark:shadow-none dark:ring-white/[0.06]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-stone-900 dark:text-stone-50">{venue.display_name}</p>
          <p className="mt-0.5 truncate font-mono text-[11px] text-stone-400">{venue.base_url}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {venue.is_sandbox && <Pill tone="info">Sandbox</Pill>}
          <LiveDot active={venue.is_enabled} tone={venue.is_enabled ? "good" : "warning"} />
        </div>
      </div>
      <SessionState venue={venue} />
      <dl className="grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <dt className="text-stone-400">Outbound</dt>
          <dd className="mt-0.5 font-mono text-stone-700 dark:text-stone-200">
            {venue.bets_per_second}/s · {venue.burst}
          </dd>
        </div>
        <div>
          <dt className="text-stone-400">Mapped</dt>
          <dd className="mt-0.5 font-mono text-stone-700 dark:text-stone-200">{venue.fixtures_mapped} fixtures</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-stone-400">Routes</dt>
          <dd className="mt-0.5 truncate font-mono text-stone-700 dark:text-stone-200">{venue.routes.includes("*") ? "every book" : [venue.id, ...venue.routes].join(", ")}</dd>
        </div>
      </dl>
      {isAdmin && (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" icon="sync_alt" busy={busy} onClick={() => void sync()}>
            Sync catalog
          </Button>
        </div>
      )}
    </motion.div>
  );
};

// ---------------------------------------------------------------- live feed
const LiveFeed = () => {
  const history = useResource("cfo:sniper-feed", () => apiClient.get<FeedLine[]>("/omni/sniper/feed"));
  const [live, setLive] = useState<FeedLine[]>([]);
  const [status, setStatus] = useState<OmniSocketStatus>("idle");
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const offStatus = OmniSocket.channel(FEED_PATH).onStatus(setStatus);
    const off = subscribeChannel(FEED_PATH, (data) => {
      const line = parseLine(data);
      if (line) setLive((prev) => [...prev, line].slice(-MAX_LINES));
    });
    return () => {
      off();
      offStatus();
    };
  }, []);

  const lines = useMemo(() => {
    const seen = new Set<string>();
    const merged: FeedLine[] = [];
    for (const l of [...(history.data ?? []).slice().reverse(), ...live]) {
      const k = lineKey(l);
      if (!seen.has(k)) {
        seen.add(k);
        merged.push(l);
      }
    }
    return merged.slice(-MAX_LINES);
  }, [history.data, live]);

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="overflow-hidden rounded-2xl bg-stone-950/90 shadow-soft-lg ring-1 ring-white/[0.06] backdrop-blur-md">
      <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-3">
        <div className="flex items-center gap-1.5" aria-hidden="true">
          <span className="size-2.5 rounded-full bg-rose-400/70" />
          <span className="size-2.5 rounded-full bg-amber-300/70" />
          <span className="size-2.5 rounded-full bg-emerald-400/70" />
        </div>
        <span className="inline-flex items-center gap-2 font-mono text-[11px] text-stone-400" role="status">
          <LiveDot active={status === "open"} tone={status === "open" ? "good" : "warning"} />
          {status === "open" ? "streaming" : status}
        </span>
      </div>
      <div
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
        className="h-[420px] overflow-y-auto px-5 py-4 font-mono text-[12.5px] leading-6"
        aria-live="polite"
        aria-label="Execution feed"
      >
        {lines.length === 0 ? (
          <p className="text-stone-500">$ waiting for the first shot…</p>
        ) : (
          <AnimatePresence initial={false}>
            {lines.map((l) => (
              <motion.p key={lineKey(l)} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.2 }} className="whitespace-pre-wrap break-words">
                <span className="text-stone-500">[{clock(l.ts)}]</span>{" "}
                {l.ref && <span className="text-stone-600">{l.ref.slice(0, 8)} </span>}
                <span className={LEVEL_TONE[l.level] ?? LEVEL_TONE.info}>{l.message}</span>
              </motion.p>
            ))}
          </AnimatePresence>
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------- executions + payload inspector
const EVENT: Record<string, [string, "good" | "critical" | "warning"]> = {
  EXECUTED: ["Placed", "good"],
  BOOKMAKER_REJECTED: ["Rejected", "critical"],
  EXECUTION_UNKNOWN: ["Unconfirmed", "warning"],
  COMMIT_FAILED: ["Ledger failure", "critical"],
};

const Json = ({ label, value }: { label: string; value: unknown }) => {
  const text = useMemo(() => (value === null || value === undefined ? "—" : typeof value === "string" ? value : JSON.stringify(value, null, 2)), [value]);
  const [copied, setCopied] = useState(false);
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center justify-between">
        <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-stone-400">{label}</p>
        <button
          type="button"
          onClick={() => void navigator.clipboard?.writeText(text).then(() => setCopied(true))}
          onBlur={() => setCopied(false)}
          className="text-[11px] text-stone-400 transition-colors hover:text-stone-200"
        >
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre className="max-h-72 overflow-auto rounded-xl bg-stone-950/90 p-4 font-mono text-[12px] leading-5 text-stone-200 ring-1 ring-white/[0.06]">{text}</pre>
    </div>
  );
};

const Executions = () => {
  const executions = useExecutions();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Async resource={executions} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="data_object" title="No executions yet" detail="Every order the sniper fires lands here with the exact payload sent and received." />}>
      {(rows) => (
        <ul className="flex flex-col gap-2">
          {rows.map((x: Execution) => {
            const [label, tone] = EVENT[x.event] ?? [humanize(x.event), "warning"];
            const expanded = open === x.id;
            return (
              <motion.li key={x.id} layout transition={SPRING} className="overflow-hidden rounded-2xl bg-white/70 ring-1 ring-inset ring-stone-900/[0.04] backdrop-blur-md dark:bg-stone-900/60 dark:ring-white/[0.06]">
                <button
                  type="button"
                  onClick={() => setOpen(expanded ? null : x.id)}
                  aria-expanded={expanded}
                  className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3.5 text-left transition-colors hover:bg-stone-900/[0.02] dark:hover:bg-white/[0.02]"
                >
                  <Pill tone={tone}>{label}</Pill>
                  <span className="min-w-0 flex-1 truncate text-sm text-stone-800 dark:text-stone-100">
                    {x.selection ?? "—"} @ {formatOdds(x.odds)} · {formatINR(x.stake_inr)}
                    <span className="ml-2 text-xs text-stone-400">{x.venue_id ?? ""}</span>
                  </span>
                  <span className="font-mono text-[11px] text-stone-400">
                    {x.http_status ?? "—"} · {x.latency_ms !== null ? `${x.latency_ms} ms` : "—"} · {x.remote_bet_id ?? humanize(x.reason)}
                  </span>
                  <span className="text-[11px] text-stone-400">{formatAgo(x.created_at)}</span>
                  <span className={cx("material-symbols-outlined text-[18px] text-stone-400 transition-transform", expanded && "rotate-180")}>expand_more</span>
                </button>
                <AnimatePresence initial={false}>
                  {expanded && (
                    <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={SPRING} className="overflow-hidden">
                      <div className="grid grid-cols-1 gap-4 border-t border-stone-900/[0.05] px-5 py-5 lg:grid-cols-2 dark:border-white/[0.06]">
                        <Json label="Sent" value={x.request_payload} />
                        <Json label={`Received${x.http_status ? ` · ${x.http_status}` : ""}`} value={x.response_payload} />
                        <p className="font-mono text-[11px] text-stone-400 lg:col-span-2">
                          order {x.idempotency_key ?? "—"} · {humanize(x.reason)}
                          {x.matched_odds ? ` · struck at ${x.matched_odds}` : ""} · {formatTime(x.created_at)}
                        </p>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.li>
            );
          })}
        </ul>
      )}
    </Async>
  );
};

// ---------------------------------------------------------------- dead letters
const OUTCOMES = [
  { outcome: "WON", label: "Won", tone: "primary" },
  { outcome: "LOST", label: "Lost", tone: "secondary" },
  { outcome: "VOID", label: "Void", tone: "secondary" },
  { outcome: "NOT_PLACED", label: "Not placed", tone: "ghost" },
  { outcome: "OPEN", label: "Re-open", tone: "ghost" },
] as const;

const DeadLetters = ({ isAdmin }: { isAdmin: boolean }) => {
  const [everyone, setEveryone] = useState(false);
  const dlq = useDeadLetters(isAdmin && everyone);
  const resolve = (p: Position, outcome: string) =>
    runMutation(() => apiClient.post(`/omni/positions/${p.id}/resolve`, { outcome }), {
      invalidate: ["cfo"],
      success: `Resolved ${humanize(outcome)}`,
      errorTitle: "Resolution refused",
    });
  return (
    <div className="flex flex-col gap-4">
      {isAdmin && (
        <label className="inline-flex items-center gap-2 self-end text-xs text-stone-500">
          <input type="checkbox" checked={everyone} onChange={(e) => setEveryone(e.target.checked)} className="accent-[var(--accent)]" />
          Every user
        </label>
      )}
      <Async resource={dlq} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="task_alt" title="Nothing needs a person" detail="Orders the resolver gives up on (venue unreachable 10 times, or unresolved a day past kick-off) wait here with their stake held." />}>
        {(rows) => (
          <ul className="flex flex-col gap-2">
            {rows.map((p) => (
              <li key={p.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl bg-white/70 px-5 py-4 ring-1 ring-inset ring-rose-500/15 backdrop-blur-md dark:bg-stone-900/60">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-stone-800 dark:text-stone-100">
                    {p.selection} @ {formatOdds(p.odds)} · {formatINR(p.stake_inr)} held
                  </p>
                  <p className="mt-0.5 truncate font-mono text-[11px] text-stone-400">
                    {p.bookmaker_id} · {p.remote_bet_id ?? "no remote id"} · {p.last_resolve_error ?? "—"} · {p.resolve_attempts ?? 0} tries
                  </p>
                </div>
                {isAdmin ? (
                  <div className="flex flex-wrap gap-1.5">
                    {OUTCOMES.map((o) => (
                      <ConfirmButton
                        key={o.outcome}
                        size="sm"
                        variant={o.tone}
                        disabled={o.outcome === "OPEN" && !p.remote_bet_id}
                        confirmLabel={`${o.label}?`}
                        onConfirm={() => void resolve(p, o.outcome)}
                      >
                        {o.label}
                      </ConfirmButton>
                    ))}
                  </div>
                ) : (
                  <span className="text-[11px] text-stone-400">An admin resolves this from the bookmaker's records</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Async>
    </div>
  );
};

// ---------------------------------------------------------------- the tab
export const SniperTerminal = () => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const venues = useVenues();
  const [view, setView] = useState<View>("feed");
  const [sweeping, setSweeping] = useState(false);

  const sweep = async () => {
    setSweeping(true);
    await runMutation(() => apiClient.post<Record<string, unknown>>("/omni/sniper/resolve-now"), {
      invalidate: ["cfo"],
      success: (r) => `Resolver: ${String(r.polled ?? 0)} polled, ${String(r.graded ?? 0)} settled, ${String(r.dead_lettered ?? 0)} dead-lettered`,
      errorTitle: "Resolver sweep failed",
    });
    setSweeping(false);
  };

  return (
    <>
      <Panel
        title="Execution venues"
        icon="hub"
        className="lg:col-span-12"
        updatedAt={venues.updatedAt}
        bodyClassName="p-0 bg-transparent shadow-none ring-0 dark:bg-transparent"
        actions={isAdmin ? <Button size="sm" icon="sync" busy={sweeping} onClick={() => void sweep()}>Resolve orders now</Button> : undefined}
      >
        <Async resource={venues} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="hub" title="No execution venues" detail="Live orders route to a bookmaker's execution venue. In development the sandbox venue appears here once MASTER_VAULT_KEY is set." />}>
          {(rows) => (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,19rem),1fr))] gap-4">
              {rows.map((v) => (
                <VenueCard key={v.id} venue={v} isAdmin={isAdmin} />
              ))}
            </div>
          )}
        </Async>
      </Panel>
      <motion.section variants={CARD_VARIANTS} className="flex min-w-0 flex-col gap-4 lg:col-span-12" aria-label="Execution terminal">
        <div className="flex flex-wrap items-center justify-between gap-3 px-1">
          <div className="flex items-center gap-2.5">
            <span className="material-symbols-outlined text-[18px] text-stone-400 dark:text-stone-500">terminal</span>
            <h2 className="text-[15px] font-semibold text-stone-900 dark:text-stone-100">Execution terminal</h2>
          </div>
          <Segmented<View> options={VIEWS} value={view} onChange={setView} label="Terminal views" size="sm" />
        </div>
        {view === "feed" ? <LiveFeed /> : view === "executions" ? <Executions /> : <DeadLetters isAdmin={isAdmin} />}
      </motion.section>
    </>
  );
};
