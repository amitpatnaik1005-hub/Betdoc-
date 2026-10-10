/**
 * Order Routing & Slices (Group 71): every routed order's parent stake against its venue slices, live
 * status pills, the venues' circuit breakers, and the manual release of orphaned reservations.
 */
import { type FormEvent, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { formatAgo, formatINR, humanize } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import {
  type RoutedOrder,
  type RoutedSlice,
  type SliceStatus,
  releaseSlice,
  resetVenue,
  routeOrder,
  useRoutedOrders,
  useVenueBreakers,
} from "../../lib/router";
import { Async, Button, ConfirmButton, EmptyState, Field, LiveDot, NumberInput, Panel, Pill, SPRING, Segmented, TextInput, type Tone } from "../../ui/kit";

const INVALIDATE = ["router", "vault"];

const SLICE_TONE: Record<SliceStatus, Tone> = {
  RESERVED: "info", DISPATCHED: "accent", FILLED: "good", PARTIAL: "warning", REJECTED: "critical", UNKNOWN: "serious", RELEASED: "neutral",
};
const ORDER_TONE: Record<string, Tone> = {
  ROUTING: "info", RESERVED: "info", DISPATCHING: "accent", FILLED: "good", PARTIAL: "warning", UNCONFIRMED: "serious", REJECTED: "critical", ABORTED: "neutral",
};
const BAR: Record<SliceStatus, string> = {
  RESERVED: "bg-sky-400", DISPATCHED: "bg-[var(--accent)]", FILLED: "bg-emerald-500", PARTIAL: "bg-amber-500", REJECTED: "bg-rose-500", UNKNOWN: "bg-orange-500", RELEASED: "bg-stone-300 dark:bg-stone-600",
};

const money = (value: string | null | undefined, currency: string): string => {
  const n = Number(value ?? 0);
  return currency === "INR" ? formatINR(n) : `${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })} ${currency}`;
};
const odds = (value: string | null | undefined): string => (value ? Number(value).toFixed(2) : "—");

const useNow = (ms = 1000): number => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
};

// --------------------------------------------------------------------------- breakers
const Breakers = () => {
  const breakers = useVenueBreakers();
  const now = useNow();
  return (
    <Panel title="Venue circuit breakers" icon="electrical_services" className="lg:col-span-12" updatedAt={breakers.updatedAt}
      subtitle={breakers.data ? `${breakers.data.policy.failures} failures in ${breakers.data.policy.window_seconds}s pause a venue ${Math.round(breakers.data.policy.pause_seconds / 60)} min` : undefined}>
      <Async resource={breakers} isEmpty={(b) => b.venues.length === 0} empty={<EmptyState icon="storefront" title="No venue yet" detail="Venues appear once the Vault holds an active account." />}>
        {(b) => (
          <ul className="flex flex-wrap gap-2">
            {b.venues.map((v) => {
              const paused = v.state === "PAUSED";
              const left = paused && v.paused_until ? Math.max(0, Math.round((Date.parse(v.paused_until) - now) / 1000)) : 0;
              return (
                <li key={v.venue_id} className={`flex items-center gap-3 rounded-2xl px-4 py-2.5 ring-1 ring-inset ${paused ? "bg-rose-50 ring-rose-200 dark:bg-rose-500/10 dark:ring-rose-500/30" : "bg-emerald-50/60 ring-emerald-200/70 dark:bg-emerald-500/[0.06] dark:ring-emerald-500/20"}`}>
                  <LiveDot active tone={paused ? "critical" : "good"} />
                  <div className="flex flex-col">
                    <span className="text-sm font-semibold text-stone-800 dark:text-stone-100">{v.venue_id}</span>
                    <span className="text-[11px] text-stone-500 dark:text-stone-400" title={v.last_failure_reason ?? ""}>
                      {paused ? `Paused · ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left` : `Live · ${v.consecutive_failures} recent failure(s)`}
                      {v.trips ? ` · ${v.trips} trip(s)` : ""}
                    </span>
                  </div>
                  {paused && (
                    <ConfirmButton size="sm" variant="ghost" icon="restart_alt" confirmLabel="Resume now?"
                      onConfirm={() => void runMutation(() => resetVenue(v.venue_id), { invalidate: INVALIDATE, success: `${v.venue_id} live again` })}>
                      Reset
                    </ConfirmButton>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Async>
    </Panel>
  );
};

// --------------------------------------------------------------------------- one order
const SliceBar = ({ order }: { order: RoutedOrder }) => {
  const total = Number(order.desired_total_stake) || 1;
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800" aria-label="slices across venues">
      {order.slices.map((s) => (
        <motion.div key={s.id} title={`${s.venue_id}: ${money(s.stake, s.currency)} ${s.status}`} className={`h-full ${BAR[s.status]} border-r border-white/70 last:border-r-0 dark:border-stone-900/70`}
          initial={{ width: 0 }} animate={{ width: `${(Number(s.stake) / total) * 100}%` }} transition={SPRING} />
      ))}
    </div>
  );
};

const SliceRow = ({ s }: { s: RoutedSlice }) => (
  <tr className="text-[13px] text-stone-700 dark:text-stone-300">
    <td className="py-2 pr-3 font-medium">{s.venue_id}</td>
    <td className="py-2 pr-3 text-right font-mono tabular-nums">{money(s.stake, s.currency)}</td>
    <td className="py-2 pr-3 text-right font-mono tabular-nums">{Number(s.filled_stake) > 0 ? money(s.filled_stake, s.currency) : "—"}</td>
    <td className="py-2 pr-3 text-right font-mono tabular-nums" title="quoted → guard → matched">{odds(s.quoted_odds)} → {odds(s.guard_odds)} → {odds(s.matched_odds)}</td>
    <td className="py-2 pr-3"><Pill tone={SLICE_TONE[s.status]}>{s.status}</Pill></td>
    <td className="max-w-[16rem] truncate py-2 pr-3 font-mono text-[11px] text-stone-500" title={`${s.idempotency_key}\n${s.client_ref}`}>{s.idempotency_key}</td>
    <td className="py-2 pr-3 font-mono text-[11px] text-stone-500">{s.remote_bet_id ?? (s.reason ? humanize(s.reason) : "")}</td>
    <td className="py-2 text-right">
      {s.orphaned ? (
        <ConfirmButton size="sm" variant="danger" icon="lock_open" confirmLabel="Release hold?"
          onConfirm={() => void runMutation(() => releaseSlice(s.id), { invalidate: INVALIDATE, success: `${s.venue_id} hold released`, errorTitle: "Release refused" })}>
          Release
        </ConfirmButton>
      ) : s.status === "RESERVED" ? (
        <span className="text-[11px] text-stone-400">waiting {formatAgo(s.reserved_at)}</span>
      ) : null}
    </td>
  </tr>
);

const OrderCard = ({ order }: { order: RoutedOrder }) => {
  const [open, setOpen] = useState(order.status !== "FILLED");
  const orphans = order.slices.filter((s) => s.orphaned).length;
  return (
    <motion.li layout className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-center gap-x-4 gap-y-2 text-left">
        <span className="material-symbols-outlined text-[18px] text-stone-400">{open ? "expand_less" : "expand_more"}</span>
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="truncate font-mono text-[12px] text-stone-500">{order.order_id}</span>
          <span className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{order.selection} @ {odds(order.odds)} · {order.match_id}</span>
        </div>
        <div className="flex flex-col items-end">
          <span className="font-mono text-sm tabular-nums text-stone-800 dark:text-stone-100">{money(order.filled_stake, order.currency)} / {money(order.desired_total_stake, order.currency)}</span>
          <span className="text-[11px] text-stone-400">{order.slices.length} slice(s){order.blended_odds ? ` · blended ${odds(order.blended_odds)}` : ""}</span>
        </div>
        <Pill tone={ORDER_TONE[order.status] ?? "neutral"}>{order.status}</Pill>
        {order.hedge_state === "ELIGIBLE" && <Pill tone="warning" icon="call_split">Legged · hedge</Pill>}
        {orphans > 0 && <Pill tone="critical" icon="hourglass_bottom">{orphans} orphaned</Pill>}
      </button>
      <div className="mt-3"><SliceBar order={order} /></div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            {order.detail?.message && <p className="mt-3 text-[12px] text-stone-500 dark:text-stone-400">{order.detail.message}</p>}
            {order.slices.length > 0 && (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[720px] text-left">
                  <thead>
                    <tr className="text-[11px] text-stone-400">
                      <th className="pb-1 pr-3 font-medium">Venue</th><th className="pb-1 pr-3 text-right font-medium">Stake</th><th className="pb-1 pr-3 text-right font-medium">Filled</th>
                      <th className="pb-1 pr-3 text-right font-medium">Odds</th><th className="pb-1 pr-3 font-medium">Status</th><th className="pb-1 pr-3 font-medium">Idempotency key</th>
                      <th className="pb-1 pr-3 font-medium">Confirmation</th><th className="pb-1" />
                    </tr>
                  </thead>
                  <tbody>{order.slices.map((s) => <SliceRow key={s.id} s={s} />)}</tbody>
                </table>
              </div>
            )}
            {order.receipt_sha256 && (
              <p className="mt-2 font-mono text-[11px] text-stone-400" title={order.receipt_sha256}>
                receipt sha256 {order.receipt_sha256.slice(0, 16)}… {order.nalanda_seq ? `· Nalanda #${order.nalanda_seq}` : "· mirroring"}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.li>
  );
};

const Orders = () => {
  const [scope, setScope] = useState<"active" | "all">("active");
  const orders = useRoutedOrders(scope === "active");
  return (
    <Panel title="Order routing & slices" icon="call_split" className="lg:col-span-12" updatedAt={orders.updatedAt}
      actions={<Segmented<"active" | "all"> size="sm" label="Orders shown" value={scope} onChange={setScope} options={[{ value: "active", label: "Active" }, { value: "all", label: "All" }]} />}>
      <Async resource={orders} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="call_split" title={scope === "active" ? "Nothing in flight" : "No routed order yet"} detail="Orders routed across the fleet appear here with their venue slices." />}>
        {(rows) => <ul className="flex flex-col gap-3">{rows.map((o) => <OrderCard key={o.id} order={o} />)}</ul>}
      </Async>
    </Panel>
  );
};

// --------------------------------------------------------------------------- manual route
const RouteForm = () => {
  const [d, setD] = useState({ match_id: "", selection: "HOME", odds: "", floor: "", stake: "", slip: "2", books: "parimatch, 1xbet" });
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await runMutation(() => routeOrder({
      order_id: `ui-${crypto.randomUUID()}`, match_id: d.match_id.trim(), selection: d.selection.trim(), odds: d.odds, desired_total_stake: d.stake,
      min_acceptable_odds: d.floor || d.odds, max_slippage_pct: d.slip, target_bookmakers: d.books.split(",").map((b) => b.trim()).filter(Boolean),
    }), { invalidate: INVALIDATE, errorTitle: "Order refused", success: (o) => `${o.status}: ${money(o.filled_stake, o.currency)} across ${o.slices.length} slice(s)` });
    setBusy(false);
  };
  const ready = d.match_id.trim() && d.selection.trim() && Number(d.odds) > 1 && Number(d.stake) > 0;
  return (
    <Panel title="Route an order" icon="alt_route" className="lg:col-span-12" subtitle="sliced across the Vault fleet, guarded before dispatch">
      <form onSubmit={submit} className="grid grid-cols-2 gap-x-4 sm:grid-cols-4 lg:grid-cols-8">
        <Field label="Match id" className="col-span-2"><TextInput value={d.match_id} onChange={(e) => setD({ ...d, match_id: e.target.value })} className="font-mono" /></Field>
        <Field label="Selection"><TextInput value={d.selection} onChange={(e) => setD({ ...d, selection: e.target.value })} /></Field>
        <Field label="Odds"><NumberInput step="0.01" min="1.01" value={d.odds} onChange={(e) => setD({ ...d, odds: e.target.value })} /></Field>
        <Field label="Floor"><NumberInput step="0.01" min="1.01" value={d.floor} placeholder={d.odds} onChange={(e) => setD({ ...d, floor: e.target.value })} /></Field>
        <Field label="Stake ₹"><NumberInput min="1" value={d.stake} onChange={(e) => setD({ ...d, stake: e.target.value })} /></Field>
        <Field label="Slippage %"><NumberInput step="0.5" min="0" max="50" value={d.slip} onChange={(e) => setD({ ...d, slip: e.target.value })} /></Field>
        <Field label="Bookmakers"><TextInput value={d.books} onChange={(e) => setD({ ...d, books: e.target.value })} /></Field>
        <div className="col-span-2 flex justify-end sm:col-span-4 lg:col-span-8">
          <Button type="submit" variant="primary" icon="send" busy={busy} disabled={!ready || busy}>Route order</Button>
        </div>
      </form>
    </Panel>
  );
};

export const OrderRouting = () => (
  <>
    <Breakers />
    <Orders />
    <RouteForm />
  </>
);
